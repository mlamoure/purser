"""Malformed ltree paths are a 422 naming the segment, never a 500 (issue #214).

One validator — ``purser.core.ledger.validate_path`` — runs at every boundary
that accepts an account/category path (creates, "path or id" references and
their bracket forms, subtree filters), through ``ledger.check_path``. A narrow
handler backstops whatever the validator admits but the database's locale
still refuses: REST answers 422, MCP a clean tool error.
"""

from collections.abc import Iterator
from typing import Annotated, Any

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st
from pydantic import BaseModel
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from purser.core import ledger as core
from tests.test_mcp import (  # noqa: F401 (fixture re-export)
    LiveServer,
    _mcp_call,
    seeded_tokens,
)

# ---------------------------------------------------------------------------
# The validator itself (pure)
# ---------------------------------------------------------------------------

VALID_PATHS = [
    pytest.param("expenses", id="single-label"),
    pytest.param("expenses.dining.coffee", id="nested"),
    pytest.param("Assets.Checking_2", id="mixed-case-digit-underscore"),
    pytest.param("expenses.e-commerce", id="hyphen-inside"),
    pytest.param("-pending.cleared-", id="hyphen-at-label-ends"),
    pytest.param("_", id="underscore-only"),
    pytest.param("x" * core.LTREE_LABEL_MAX_CHARS, id="label-at-max-length"),
    pytest.param(".".join(["a"] * core.LTREE_MAX_LABELS), id="max-label-count"),
    pytest.param("income.café", id="non-ascii-letter"),
]

INVALID_PATHS = [
    pytest.param("", "path is empty", id="empty-path"),
    pytest.param("expenses..dining", "segment 2 is empty", id="empty-segment"),
    pytest.param(".expenses", "segment 1 is empty", id="leading-dot"),
    pytest.param("expenses.", "segment 2 is empty", id="trailing-dot"),
    pytest.param(".", "segment 1 is empty", id="lone-dot"),
    pytest.param(
        "expenses.eating out", "segment 2 ('eating out') contains ' '", id="space-in-segment"
    ),
    pytest.param(" expenses", "segment 1 (' expenses') contains ' '", id="leading-space"),
    pytest.param("assets/checking", "contains '/'", id="slash"),
    pytest.param("expenses.dad's", 'contains "\'"', id="apostrophe"),
    pytest.param("expenses.dad\u2019s", "contains '\u2019'", id="curly-apostrophe"),
    pytest.param("expenses.a\u00a0b", "contains '\\xa0'", id="nbsp"),
    pytest.param("expenses.dining\n", "contains '\\n'", id="newline"),
    pytest.param("expenses.€", "contains '€'", id="currency-symbol"),
    pytest.param("[assets.savings]", "contains '['", id="bracket-form-is-not-a-path"),
    pytest.param(
        "expenses." + "x" * (core.LTREE_LABEL_MAX_CHARS + 1),
        "segment 2 ('xxxx",
        id="label-too-long",
    ),
    pytest.param(
        ".".join(["a"] * (core.LTREE_MAX_LABELS + 1)),
        "65536 segments; at most 65535",
        id="too-many-labels",
    ),
]


@pytest.mark.parametrize("path", VALID_PATHS)
def test_valid_paths_pass_unchanged(path: str) -> None:
    assert core.validate_path(path) == path


@pytest.mark.parametrize(("path", "fragment"), INVALID_PATHS)
def test_invalid_paths_name_the_offending_segment(path: str, fragment: str) -> None:
    with pytest.raises(core.InvalidPathError) as raised:
        core.validate_path(path)
    assert fragment in str(raised.value)


def test_too_long_label_reports_length_and_clips_the_echo() -> None:
    with pytest.raises(core.InvalidPathError, match="5000 characters long; at most 1000") as raised:
        core.validate_path("expenses." + "x" * 5000)
    assert len(str(raised.value)) < 300  # the 5 KB input is clipped, not echoed back


def test_every_shipped_default_category_validates() -> None:
    from purser.core.categories import DEFAULT_CATEGORIES

    for spec in DEFAULT_CATEGORIES:
        assert core.validate_path(spec.path) == spec.path


@given(name=st.text(min_size=1, max_size=40))
def test_derived_labels_always_validate(name: str) -> None:
    """path_label (the slugifier) only ever produces valid labels."""
    try:
        label = core.path_label(name)
    except ValueError:
        return
    assert core.validate_path(label) == label
    assert core.validate_path(f"expenses.{label}") == f"expenses.{label}"


# ---------------------------------------------------------------------------
# The validator agrees with Postgres 16
# ---------------------------------------------------------------------------


def _postgres_accepts(engine: Engine, path: str) -> bool:
    with engine.connect() as conn:
        try:
            conn.execute(text("SELECT CAST(:p AS ltree)"), {"p": path})
        except DBAPIError:
            return False
    return True


def _validator_accepts(path: str) -> bool:
    try:
        core.validate_path(path)
    except core.InvalidPathError:
        return False
    return True


@settings(
    max_examples=150, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(path=st.text(alphabet="aZ09_-...... /'![:\t", min_size=1, max_size=16))
@example(path="-")
@example(path="a-")
@example(path="a..b")
@example(path=".")
@example(path="a.b ")
def test_validator_matches_postgres_on_ascii(migrated_engine: Engine, path: str) -> None:
    """ASCII is locale-independent, so validator and ltree must agree exactly
    (the empty string — ltree's zero-label path — is excluded by min_size;
    the validator refuses it on purpose)."""
    assert _validator_accepts(path) == _postgres_accepts(migrated_engine, path), path


@pytest.fixture
def utf8_database(migrated_engine: Engine) -> Engine:
    with migrated_engine.connect() as conn:
        ctype = conn.execute(
            text("SELECT datctype FROM pg_database WHERE datname = current_database()")
        ).scalar_one()
    if "utf8" not in ctype.lower().replace("-", ""):
        pytest.skip(f"non-ASCII ltree labels depend on LC_CTYPE; this database is {ctype}")
    return migrated_engine


@pytest.mark.parametrize(
    ("path", "accepted"),
    [
        ("expenses.café", True),
        ("expenses.日本", True),
        ("expenses.खाना", True),  # Devanagari vowel signs are combining marks
        ("expenses.straße", True),
        ("expenses.€", False),
        ("expenses.©", False),
        ("expenses.x²", False),
        ("expenses.a\u2014b", False),  # em dash
        ("expenses.a\u00a0b", False),  # NBSP
    ],
)
def test_validator_matches_postgres_on_non_ascii(
    utf8_database: Engine, path: str, accepted: bool
) -> None:
    assert _validator_accepts(path) is accepted
    assert _postgres_accepts(utf8_database, path) is accepted


# ---------------------------------------------------------------------------
# REST boundaries: every one answers 422 (the routes' {"detail": str} shape)
# ---------------------------------------------------------------------------

QIF_ONE_ROW = """!Account
NChecking
TBank
^
!Type:Bank
D1/10/2024
T-100.00
PStore A
LGroceries
^
"""


@pytest.fixture
def world(admin_client: TestClient, migrated_engine: Engine) -> Iterator[dict[str, str]]:
    """Admin session plus one of every entity a path-taking route needs."""

    def created(resp: Any) -> dict[str, Any]:
        assert resp.status_code == 201, resp.text
        return dict(resp.json())

    for name, type_, path in [
        ("Checking", "checking", "assets.checking"),
        ("Savings", "savings", "assets.savings"),
        ("Wallet", "cash", "assets.wallet"),
    ]:
        created(admin_client.post("/accounts", json={"name": name, "type": type_, "path": path}))
    created(
        admin_client.post(
            "/categories",
            json={"name": "Dining", "type": "category_expense", "path": "expenses.dining"},
        )
    )
    wallet = admin_client.get("/accounts").json()
    wallet_id = next(a["id"] for a in wallet if a["path"] == "assets.wallet")

    def posted(account: str) -> dict[str, Any]:
        return created(
            admin_client.post(
                "/transactions",
                json={
                    "account": account,
                    "category": "expenses.dining",
                    "amount": "-12.50",
                    "date": "2026-01-15",
                    "payee": "Store A",
                    "status": "posted",
                },
            )
        )

    tx = posted("assets.checking")
    posted("assets.wallet")  # a nonzero balance, so close needs its adjustment category
    schedule = created(
        admin_client.post(
            "/schedules",
            json={
                "name": "Gym",
                "account": "assets.savings",
                "rrule": "FREQ=MONTHLY;BYMONTHDAY=1",
                "amount": "-40.00",
                "category": "expenses.dining",
            },
        )
    )
    memory = created(admin_client.post("/memories", json={"text": "synthetic note"}))
    agent = created(
        admin_client.post("/tokens", json={"name": "agent", "scopes": ["read", "write"]})
    )
    suggestion = created(
        admin_client.post(
            "/schedules",
            json={
                "name": "Internet",
                "account": "assets.checking",
                "rrule": "FREQ=MONTHLY;BYMONTHDAY=1",
                "amount": "-80.00",
                "propose": True,
            },
            headers={"Authorization": f"Bearer {agent['token']}"},
        )
    )
    with migrated_engine.begin() as conn:
        connection_id = conn.execute(
            text("INSERT INTO connections DEFAULT VALUES RETURNING id")
        ).scalar_one()
    yield {
        "wallet": wallet_id,
        "tx": tx["id"],
        "schedule": schedule["id"],
        "memory": memory["id"],
        "suggestion": suggestion["suggestion_id"],
        "connection": str(connection_id),
    }
    # clean_db never truncates connections; leave no URL-less row behind for
    # later files' sync-all/health checks (the map call failed, so nothing
    # references it).
    with migrated_engine.begin() as conn:
        conn.execute(text("DELETE FROM connections WHERE id = :id"), {"id": connection_id})


_FLAT_SCHEDULE = {"name": "Rent", "account": "assets.checking", "rrule": "FREQ=MONTHLY"}

BOUNDARIES = [
    # accounts & categories
    pytest.param(
        "post",
        "/accounts",
        {"json": {"name": "Spare", "type": "checking", "path": "assets..spare"}},
        "segment 2 is empty",
        id="create-account-path",
    ),
    pytest.param(
        "post",
        "/accounts",
        {"json": {"name": "!!!", "type": "checking"}},
        "cannot derive an ltree label",
        id="create-account-unsluggable-name",
    ),
    pytest.param(
        "post",
        "/categories",
        {"json": {"name": "Eating out", "type": "category_expense", "path": "expenses.eating out"}},
        "segment 2 ('eating out') contains ' '",
        id="create-category-path",
    ),
    pytest.param(
        "patch",
        "/accounts/{wallet}",
        {"json": {"path": "assets..wallet"}},
        "segment 2 is empty",
        id="move-account-path",
    ),
    pytest.param(
        "post",
        "/accounts/{wallet}/close",
        {"json": {"adjustment_category": "expenses..adjustments"}},
        "segment 2 is empty",
        id="close-adjustment-category",
    ),
    pytest.param(
        "get", "/categories/expenses.eating%20out/rollup", {}, "contains ' '", id="category-rollup"
    ),
    # transactions: writes and register filters
    pytest.param(
        "post",
        "/transactions",
        {
            "json": {
                "account": "assets.checking",
                "category": "expenses.dining.",
                "amount": "-5.00",
                "date": "2026-01-15",
            }
        },
        "segment 3 is empty",
        id="create-transaction-category",
    ),
    pytest.param(
        "post",
        "/transactions",
        {
            "json": {
                "account": "assets checking",
                "category": "expenses.dining",
                "amount": "-5.00",
                "date": "2026-01-15",
            }
        },
        "contains ' '",
        id="create-transaction-account",
    ),
    pytest.param(
        "post",
        "/transactions/transfer",
        {
            "json": {
                "from_account": "assets.checking",
                "to_account": ".assets.savings",
                "amount": "5.00",
                "date": "2026-01-15",
            }
        },
        "segment 1 is empty",
        id="create-transfer",
    ),
    pytest.param(
        "patch",
        "/transactions/{tx}",
        {"json": {"category": "[assets..savings]"}},
        "segment 2 is empty",
        id="update-transaction-bracket-target",
    ),
    pytest.param(
        "patch",
        "/transactions/{tx}",
        {"json": {"category": "expenses.dining/coffee"}},
        "contains '/'",
        id="update-transaction-category",
    ),
    pytest.param(
        "get",
        "/transactions",
        {"params": {"category": "expenses..dining"}},
        "segment 2 is empty",
        id="search-category-subtree",
    ),
    pytest.param(
        "get",
        "/transactions",
        {"params": {"account": "assets/checking"}},
        "contains '/'",
        id="search-account",
    ),
    pytest.param(
        "get",
        "/transactions/review-batch",
        {"params": {"account": "assets.checking!"}},
        "contains '!'",
        id="review-batch-account",
    ),
    # schedules, forecast, holdings, suggestions
    pytest.param(
        "post",
        "/schedules",
        {"json": {**_FLAT_SCHEDULE, "amount": "-900.00", "category": "expenses..rent"}},
        "segment 2 is empty",
        id="create-schedule-category",
    ),
    pytest.param(
        "post",
        "/schedules",
        {"json": {**_FLAT_SCHEDULE, "account": "assets.checking ", "amount": "-900.00"}},
        "contains ' '",
        id="create-schedule-account",
    ),
    pytest.param(
        "post",
        "/schedules",
        {"json": {**_FLAT_SCHEDULE, "amount": "-50.00", "counterparty_account": "assets:savings"}},
        "contains ':'",
        id="create-schedule-counterparty",
    ),
    pytest.param(
        "patch",
        "/schedules/{schedule}",
        {"json": {"category": "expenses.gym?"}},
        "contains '?'",
        id="update-schedule-category",
    ),
    pytest.param(
        "get",
        "/forecast",
        {"params": {"account": "assets..checking"}},
        "segment 2 is empty",
        id="forecast-account",
    ),
    pytest.param(
        "get",
        "/holdings",
        {"params": {"account": "assets..checking"}},
        "segment 2 is empty",
        id="holdings-account",
    ),
    pytest.param(
        "get",
        "/suggestions",
        {"params": {"account": "assets..checking"}},
        "segment 2 is empty",
        id="suggestions-account",
    ),
    pytest.param(
        "post",
        "/suggestions/{suggestion}/decide",
        {"json": {"decision": "edit_then_accept", "edits": {"category": "expenses..internet"}}},
        "segment 2 is empty",
        id="decide-suggestion-category",
    ),
    # memories
    pytest.param(
        "post",
        "/memories",
        {"json": {"text": "synthetic note", "accounts": ["[assets..checking]"]}},
        "segment 2 is empty",
        id="create-memory-account-link",
    ),
    pytest.param(
        "patch",
        "/memories/{memory}",
        {"json": {"accounts": ["assets checking"]}},
        "contains ' '",
        id="update-memory-account-link",
    ),
    # imports and feed mapping
    pytest.param(
        "post",
        "/import/qif/preview",
        {"json": {"content": QIF_ONE_ROW, "account_map": {"Checking": "assets..checking"}}},
        "segment 2 is empty",
        id="qif-preview-account-map",
    ),
    pytest.param(
        "post",
        "/import/qif",
        {
            "json": {
                "content": QIF_ONE_ROW,
                "account_map": {"Checking": "assets.checking"},
                "category_map": {"Groceries": "expenses..groceries"},
            }
        },
        "segment 2 is empty",
        id="qif-import-category-map",
    ),
    pytest.param(
        "post",
        "/import/qif",
        {
            "json": {
                "content": QIF_ONE_ROW.replace("LGroceries", "L-"),
                "account_map": {"Checking": "assets.checking"},
            }
        },
        "file category '-' yields no path",
        id="qif-import-unsluggable-category",
    ),
    pytest.param(
        "post",
        "/connections/{connection}/map",
        {
            "json": {
                "external_id": "ext-1",
                "create": {"name": "Spare", "type": "checking", "path": "assets..spare"},
            }
        },
        "segment 2 is empty",
        id="feed-map-create-path",
    ),
]


@pytest.mark.parametrize(("method", "url", "kwargs", "fragment"), BOUNDARIES)
def test_rest_boundary_answers_422_naming_the_segment(
    world: dict[str, str],
    client: TestClient,
    method: str,
    url: str,
    kwargs: dict[str, Any],
    fragment: str,
) -> None:
    resp = client.request(method.upper(), url.format(**world), **kwargs)
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert isinstance(detail, str)  # the routes' own shape, not pydantic's list
    assert fragment in detail


def test_unknown_but_wellformed_references_keep_their_404(
    world: dict[str, str], client: TestClient
) -> None:
    """Validation only changes the malformed case: a well-formed path that
    names nothing is still a 404 (review-batch and memories used to 500)."""
    missing = "assets.no_such_account"
    assert client.get("/transactions", params={"account": missing}).status_code == 404
    review = client.get("/transactions/review-batch", params={"account": missing})
    assert review.status_code == 404
    memory = client.post("/memories", json={"text": "synthetic", "accounts": [missing]})
    assert memory.status_code == 404


def test_uuid_references_bypass_path_validation(world: dict[str, str], client: TestClient) -> None:
    """'path or id' parameters validate only the path form."""
    resp = client.get("/transactions", params={"account": world["wallet"]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["total"] == 1


def test_rejected_paths_create_nothing(
    world: dict[str, str], client: TestClient, migrated_engine: Engine
) -> None:
    """The close flow used to auto-create a category at the EMPTY path ''
    (ltree's zero-label path) for adjustment_category=''; the validator stops
    it before any write."""
    resp = client.post(f"/accounts/{world['wallet']}/close", json={"adjustment_category": ""})
    assert resp.status_code == 422
    assert "path is empty" in resp.json()["detail"]
    with migrated_engine.connect() as conn:
        empty = conn.execute(text("SELECT count(*) FROM accounts WHERE nlevel(path) = 0"))
        assert empty.scalar_one() == 0


@pytest.mark.parametrize("command", ["delete-account", "purge-feed-rows", "purge-retired-rows"])
def test_cli_malformed_ref_exits_1_with_the_message(migrated_engine: Engine, command: str) -> None:
    from purser.cli import app

    result = CliRunner().invoke(app, ["admin", command, "assets..duplicate"])
    assert result.exit_code == 1
    assert "segment 2 is empty" in result.output


# ---------------------------------------------------------------------------
# MCP tools: the same validator, surfaced as a tool error
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def live_server(migrated_engine: Engine) -> Iterator[LiveServer]:
    server = LiveServer()
    server.start()
    yield server
    server.stop()


def test_mcp_tools_reject_malformed_paths(
    seeded_tokens: dict[str, str],  # noqa: F811
    live_server: LiveServer,
) -> None:
    (page,) = _mcp_call(
        live_server.url, seeded_tokens["operator"], [("search_transactions", {"limit": 1})]
    )
    tx_id = page["items"][0]["id"]
    calls: list[tuple[str, dict[str, Any], str]] = [
        ("search_transactions", {"category": "expenses..dining"}, "segment 2 is empty"),
        ("search_transactions", {"account": "assets/checking"}, "contains '/'"),
        ("spending_summary", {"category": ".expenses"}, "segment 1 is empty"),
        (
            "spending_summary",
            {"category": "expenses dining", "period": "2026-01", "baseline": "avg_3mo"},
            "contains ' '",
        ),
        ("account_balance", {"account": "assets checking"}, "contains ' '"),
        (
            "create_transaction",
            {
                "account": "assets.checking",
                "category": "expenses.dining out",
                "amount": "-5.00",
                "date_": "2026-01-15",
            },
            "segment 2 ('dining out') contains ' '",
        ),
        (
            "update_transaction",
            {"transaction_id": tx_id, "category": "[assets..savings]"},
            "segment 2 is empty",
        ),
        (
            "create_schedule",
            {
                "name": "Rent",
                "account": "assets.checking",
                "rrule": "FREQ=MONTHLY",
                "amount": "-900.00",
                "category": "expenses..rent",
            },
            "segment 2 is empty",
        ),
        ("propose_rule", {"type": "categorize", "category": "expenses.."}, "segment 2 is empty"),
        ("propose_memory", {"text": "synthetic", "accounts": ["assets checking"]}, "contains ' '"),
    ]
    results = _mcp_call(
        live_server.url,
        seeded_tokens["operator"],
        [(tool, args) for tool, args, _ in calls],
    )
    for (tool, _, fragment), result in zip(calls, results, strict=True):
        assert "error" in result, (tool, result)
        assert fragment in result["error"], (tool, result["error"])
        assert "[SQL" not in result["error"], (tool, result["error"])


# ---------------------------------------------------------------------------
# The backstop: ltree refusals the validator let through
# ---------------------------------------------------------------------------

# Admitted by the validator (U+0301 is a combining mark, a letter-class
# character) yet refused by Postgres under any locale: an NFD-decomposed
# accent is not alphabetic on its own. The one input class that reaches SQL.
NFD_PATH = "expenses.cafe\u0301"


class _CastBody(BaseModel):
    text: str


@pytest.fixture
def backstop_client(migrated_engine: Engine) -> Iterator[TestClient]:
    """The handler on a throwaway app whose routes hand raw text to SQL."""
    from purser.api.app import ltree_refusal_handler
    from purser.db import get_session

    db = Annotated[Session, Depends(get_session)]
    app = FastAPI()
    app.add_exception_handler(DBAPIError, ltree_refusal_handler)

    @app.post("/cast")
    def cast(body: _CastBody, session: db) -> dict[str, str]:
        session.execute(text("SELECT CAST(:p AS ltree)"), {"p": body.text})
        return {"ok": body.text}

    @app.get("/own-syntax-error")
    def own_syntax_error(session: db) -> None:
        session.execute(text("SELECT FROM WHERE"))  # 42601, but not ltree's

    @app.get("/division-by-zero")
    def division_by_zero(session: db) -> None:
        session.execute(text("SELECT 1 / 0"))

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.mark.parametrize(
    ("raw", "fragment"),
    [
        pytest.param("a..b", "ltree syntax error at character 3", id="42601-syntax"),
        pytest.param("a" * 1001, "label string is too long", id="42622-label-length"),
        pytest.param(".".join(["a"] * 65536), "number of ltree labels", id="54000-label-count"),
    ],
)
def test_backstop_maps_ltree_refusals_to_422(
    backstop_client: TestClient, raw: str, fragment: str
) -> None:
    resp = backstop_client.post("/cast", json={"text": raw})
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"].startswith("invalid path: the database refused it (")
    assert fragment in resp.json()["detail"]


@pytest.mark.parametrize("route", ["/own-syntax-error", "/division-by-zero"])
def test_backstop_leaves_other_database_errors_as_500(
    backstop_client: TestClient, route: str
) -> None:
    """Narrow by construction: SQLSTATE AND ltree's own message must match,
    so a syntax error in our SQL (also 42601) still surfaces as a 500."""
    assert backstop_client.get(route).status_code == 500


def test_the_boundary_relying_on_the_backstop_answers_422(
    world: dict[str, str], client: TestClient
) -> None:
    """The real app, a real boundary: the NFD path passes the validator, the
    database refuses it, and the handler \u2014 not a 500 \u2014 answers."""
    assert core.validate_path(NFD_PATH) == NFD_PATH
    resp = client.get("/transactions", params={"category": NFD_PATH})
    assert resp.status_code == 422, resp.text
    assert "the database refused it (ltree syntax error" in resp.json()["detail"]


def test_mcp_backstop_returns_a_clean_tool_error(
    seeded_tokens: dict[str, str],  # noqa: F811
    live_server: LiveServer,
) -> None:
    (result,) = _mcp_call(
        live_server.url,
        seeded_tokens["reader"],
        [("search_transactions", {"category": NFD_PATH})],
    )
    assert "error" in result, result
    assert "the database refused it (ltree syntax error" in result["error"]
    assert "[SQL" not in result["error"]
