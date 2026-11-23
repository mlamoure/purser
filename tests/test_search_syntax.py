# ruff: noqa: F811  (pytest fixtures re-exported from sibling modules)
"""The search grammar end to end (#141, decision 135): REST ``q`` and MCP
``q`` over a small synthetic ledger, from the in-memory cache and from the
row-by-row (cold) path, which must agree."""

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select

from purser import crypto
from purser.db import get_sessionmaker
from purser.models import Payee
from purser.search import textcache
from tests.test_mcp import live_server  # noqa: F401 (fixture re-export)

ROWS = [
    # key, account, category, amount, payee, description, status, date
    (
        "cup1",
        "assets.checking",
        "expenses.dining",
        "-42.10",
        "Blue Cup",
        "POS PURCHASE BLUE CUP #12",
        "posted",
        "2026-01-05",
    ),
    (
        "cup2",
        "assets.checking",
        "expenses.dining.coffee",
        "-4.50",
        "Blue Cup",
        "CHECKCARD BLUE CUP",
        "posted",
        "2026-01-20",
    ),
    (
        "groc",
        "assets.checking",
        "expenses.groceries",
        "-120.00",
        "Whole Foods",
        "WHOLE FOODS MARKET 7",
        "pending",
        "2026-02-03",
    ),
    (
        "pay",
        "assets.savings",
        "income.salary",
        "2500.00",
        "Example Payroll",
        "ACH CREDIT PAYROLL",
        "posted",
        "2026-02-10",
    ),
    (
        "store",
        "assets.checking",
        "expenses.groceries",
        "-15.25",
        "7-Eleven",
        "7-ELEVEN 1234 Café",
        "posted",
        "2026-03-01",
    ),
]


@pytest.fixture
def ledger(admin_client: TestClient) -> dict[str, str]:
    c = admin_client
    for body in (
        {"name": "Checking", "type": "checking", "path": "assets.checking"},
        {"name": "Savings", "type": "savings", "path": "assets.savings"},
    ):
        assert c.post("/accounts", json=body).status_code == 201
    for path, name, kind in (
        ("expenses.dining", "Eating out", "category_expense"),
        ("expenses.dining.coffee", "Coffee", "category_expense"),
        ("expenses.groceries", "Groceries", "category_expense"),
        ("income.salary", "Salary", "category_income"),
    ):
        body = {"name": name, "type": kind, "path": path}
        assert c.post("/categories", json=body).status_code == 201, path
    ids: dict[str, str] = {}
    for key, account, category, amount, payee, description, status, day in ROWS:
        resp = c.post(
            "/transactions",
            json={
                "account": account,
                "category": category,
                "amount": amount,
                "payee": payee,
                "description": description,
                "status": status,
                "date": day,
            },
        )
        assert resp.status_code == 201, resp.text
        ids[key] = resp.json()["id"]
    resp = c.post(
        "/transactions/transfer",
        json={
            "from_account": "assets.checking",
            "to_account": "assets.savings",
            "amount": "300.00",
            "date": "2026-01-25",
            "description": "Monthly savings",
            "status": "posted",
        },
    )
    assert resp.status_code == 201, resp.text
    ids["xfer"] = resp.json()["id"]
    # A memo on the pending grocery row, through a split.
    resp = c.put(
        f"/transactions/{ids['groc']}/split",
        json={
            "lines": [
                {"category": "expenses.groceries", "amount": "-100.00", "memo": "Team lunch"},
                {"category": "expenses.dining", "amount": "-20.00", "memo": "Snacks"},
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    return ids


@pytest.fixture(params=["cache", "cold"])
def engine(request: pytest.FixtureRequest, ledger: dict[str, str]) -> Iterator[str]:
    textcache.reset()
    if request.param == "cache":
        textcache.warm()
    yield request.param
    textcache.reset()


def _keys(client: TestClient, ids: dict[str, str], engine: str, **params: Any) -> list[str]:
    assert (textcache.ready() is not None) == (engine == "cache")
    resp = client.get("/transactions", params={"limit": 200, **params})
    assert resp.status_code == 200, resp.text
    page = resp.json()
    by_id = {v: k for k, v in ids.items()}
    keys = [by_id.get(i["id"], "?") for i in page["items"]]
    assert page["total"] == len(keys)
    return sorted(keys)


@pytest.mark.parametrize(
    ("q", "params", "expected"),
    [
        ("blue cup", {}, ["cup1", "cup2"]),
        ("BLUE   cUp", {}, ["cup1", "cup2"]),
        ('"cup #12"', {}, ["cup1"]),
        ("cup -checkcard", {}, ["cup1"]),
        ("payee:blue", {}, ["cup1", "cup2"]),
        ("payee:purchase", {}, []),
        ("desc:checkcard", {}, ["cup2"]),
        ("memo:lunch", {}, ["groc"]),
        ("lunch", {}, ["groc"]),  # a plain word reaches memos
        ("payroll", {}, ["pay"]),  # and payees and descriptions
        ("7-eleven", {}, ["store"]),
        ("7 eleven", {}, ["store"]),
        ("cafe", {}, ["store"]),  # accents folded
        ("-blue", {"account": "assets.checking"}, ["groc", "store", "xfer"]),
        ("account:savings", {}, ["pay", "xfer"]),
        ("account:assets.check", {}, ["cup1", "cup2", "groc", "store", "xfer"]),
        ("-account:savings", {}, ["cup1", "cup2", "groc", "store"]),
        ("category:dining", {}, ["cup1", "cup2", "groc"]),  # path, with its subtree
        ("category:eating", {}, ["cup1", "cup2", "groc"]),  # name, with its subtree
        ("category:coffee", {}, ["cup2"]),
        ("amount:42.10", {}, ["cup1"]),
        ("amount:42.1", {}, ["cup1"]),
        ("amount:>100", {}, ["groc", "pay", "xfer"]),
        ("amount:>=120", {}, ["groc", "pay", "xfer"]),
        ("amount:10..50", {}, ["cup1", "store"]),
        ("amount:..5", {}, ["cup2"]),
        ("amount:$2,500.00", {}, ["pay"]),
        ("is:credit", {}, ["pay", "xfer"]),  # a transfer seen from nowhere goes both ways
        ("is:debit", {}, ["cup1", "cup2", "groc", "store", "xfer"]),
        ("is:credit", {"account": "assets.checking"}, []),
        ("is:credit", {"account": "assets.savings"}, ["pay", "xfer"]),
        ("amount:300 is:debit", {"account": "assets.checking"}, ["xfer"]),
        ("date:2026-02", {}, ["groc", "pay"]),
        ("date:<2026-02", {}, ["cup1", "cup2", "xfer"]),
        ("date:2026-01-20..2026-02-03", {}, ["cup2", "groc", "xfer"]),
        ("date:2026", {"start": "2026-03-01"}, ["store"]),
        ("status:pending", {}, ["groc"]),
        ("status:posted", {"status": ["pending"]}, []),
        ("status:pending,posted blue", {}, ["cup1", "cup2"]),
        ("is:debit is:credit", {}, []),
        ("blue category:coffee amount:<10 date:2026-01", {}, ["cup2"]),
        ("REF:12", {}, []),  # not a field: text
        ("", {}, ["cup1", "cup2", "groc", "pay", "store", "xfer"]),
        ("   ", {}, ["cup1", "cup2", "groc", "pay", "store", "xfer"]),
    ],
)
def test_queries(
    admin_client: TestClient,
    ledger: dict[str, str],
    engine: str,
    q: str,
    params: dict[str, Any],
    expected: list[str],
) -> None:
    assert _keys(admin_client, ledger, engine, q=q, **params) == expected


def test_is_unreviewed_is_the_reviewed_parameter(
    admin_client: TestClient, ledger: dict[str, str], engine: str
) -> None:
    for flag, value in (("unreviewed", "false"), ("reviewed", "true")):
        by_query = _keys(admin_client, ledger, engine, q=f"is:{flag}")
        assert by_query == _keys(admin_client, ledger, engine, reviewed=value)
    assert _keys(admin_client, ledger, engine, q="is:reviewed", reviewed="false") == []


def _payee_ref(name: str) -> str:
    with get_sessionmaker()() as session:
        for payee in session.execute(select(Payee)).scalars():
            if payee.name == name:
                return crypto.payee_ref(payee.id)
    raise AssertionError(name)


def test_payee_references(admin_client: TestClient, ledger: dict[str, str], engine: str) -> None:
    ref = _payee_ref("Blue Cup")
    assert _keys(admin_client, ledger, engine, q=f"payee:{ref}") == ["cup1", "cup2"]
    assert _keys(admin_client, ledger, engine, q=ref) == ["cup1", "cup2"]
    assert _keys(admin_client, ledger, engine, q=f"-{ref} account:checking") == [
        "groc",
        "store",
        "xfer",
    ]
    assert _keys(admin_client, ledger, engine, q=f"{ref} -checkcard") == ["cup1"]
    unknown = "py_" + "a" * 10
    resp = admin_client.get("/transactions", params={"q": f"payee:{unknown}"})
    assert resp.status_code == 422
    assert "unknown payee reference" in resp.json()["detail"]


@pytest.mark.parametrize(
    ("q", "message"),
    [
        ("paye:blue", "did you mean payee:"),
        ("amount:-5", "is:debit"),
        ("amount:ten", "not a number"),
        ("date:yesterday", "not a date"),
        ("status:cleared", "not a status"),
        ("-amount:5", "not supported"),
    ],
)
def test_malformed_queries_are_a_422_naming_the_fix(
    admin_client: TestClient, ledger: dict[str, str], q: str, message: str
) -> None:
    resp = admin_client.get("/transactions", params={"q": q})
    assert resp.status_code == 422
    assert message in resp.json()["detail"]


def test_the_cold_and_cached_answers_are_the_same_pages(
    admin_client: TestClient, ledger: dict[str, str], migrated_engine: Engine
) -> None:
    """Order and paging, not just membership."""
    params: dict[str, Any] = {"q": "-blue", "limit": 2, "offset": 1}
    textcache.reset()
    cold = admin_client.get("/transactions", params=params).json()
    textcache.warm()
    try:
        cached = admin_client.get("/transactions", params=params).json()
    finally:
        textcache.reset()
    assert [i["id"] for i in cached["items"]] == [i["id"] for i in cold["items"]]
    assert cached["total"] == cold["total"] == 4


class TestMcp:
    def test_q_and_a_malformed_q(
        self, admin_client: TestClient, ledger: dict[str, str], live_server: Any
    ) -> None:
        from tests.test_privacy import _call_all

        resp = admin_client.post("/tokens", json={"name": "agent", "scopes": ["read"]})
        token = resp.json()["token"]
        textcache.warm()
        try:
            found, refused = _call_all(
                live_server.url,
                token,
                [
                    ("search_transactions", {"q": "payee:blue amount:<10"}),
                    ("search_transactions", {"q": "paye:blue"}),
                ],
            )
        finally:
            textcache.reset()
        assert not found["error"]
        assert [i["id"] for i in found["structured"]["items"]] == [ledger["cup2"]]
        assert refused["error"]
        assert "did you mean payee:" in " ".join(refused["text"])

    def test_the_q_description_fits_its_budget(self) -> None:
        """Guardrail 6: about 90 tokens (the suite's len / 4)."""
        from purser.mcp import tools_read

        doc = tools_read.search_transactions.__doc__ or ""
        paragraph = doc[doc.index("q: words") :].split("\n\n")[0]
        assert len(paragraph) / 4 <= 90, len(paragraph) / 4
