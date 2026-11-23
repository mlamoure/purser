# ruff: noqa: F811  (pytest fixtures re-exported from sibling modules)
"""Agent-requested privacy on the REST API (issue #613).

The same seeded ledger as tests/test_privacy.py (distinctive payees,
descriptions, schedule names, rule text and single amounts). Every REST read
route must declare what it emits; every declared route that can withhold is
called as an API token with ``withhold=payee,description,amount`` and must
not return a seeded value. A request without ``withhold`` gets full detail.
Payee references work as inputs, ``POST /transactions/present`` links rows
for the signed-in person, and the audit row records the request's privacy.
"""

import json
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi import routing
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from purser import crypto
from purser.api.disclosure import EMITS_ATTR
from purser.core.ledger import normalize_payee
from tests.conftest import now, today
from tests.test_privacy import (  # noqa: F401 (fixture re-export)
    DESCRIPTIONS,
    PAYEES,
    RULE_TEXT,
    SCHEDULE_NAMES,
    SCHEDULE_PAYEES,
    SINGLE_AMOUNTS,
    _amount_at,
    seeded,
)
from tests.test_suggestions import _month_day

ALL = "payee,description,amount"
# Admin, sign-in and instance routes: not ledger data an integration reads.
EXEMPT_PREFIXES = ("/auth", "/users", "/tokens", "/audit", "/config", "/clock")
EXEMPT_PATHS = {"/llms.txt", "/source", "/healthz", "/mcp"}
READ_POSTS = {"/import/qif/preview", "/import/csv/preview", "/transactions/present"}


# The real route set is about 55 read routes; a walk that finds far fewer has
# stopped seeing the included routers (as the APIRoute-only walk did under
# FastAPI 0.162, #648 M3) and would pass every check below vacuously.
MIN_READ_ROUTES = 40


def _declared(route: Any) -> Any:
    for dep in route.dependant.dependencies:
        emits = getattr(dep.call, EMITS_ATTR, None)
        if emits is not None:
            return emits
    return None


def _all_routes(client: TestClient) -> list[Any]:
    """Every HTTP route the app serves, with its effective path and
    dependencies. FastAPI 0.162 keeps an included router as one
    ``_IncludedRouter`` entry in ``app.routes``; ``iter_route_contexts``
    (what FastAPI's own OpenAPI generation walks) unwraps it at any depth."""
    return [
        ctx
        for ctx in routing.iter_route_contexts(client.app.routes)  # type: ignore[attr-defined]
        if ctx.methods and getattr(ctx, "dependant", None) is not None
    ]


def _read_routes(client: TestClient) -> list[Any]:
    out = []
    for route in _all_routes(client):
        if not route.include_in_schema:
            continue
        if route.path in EXEMPT_PATHS or route.path.startswith(EXEMPT_PREFIXES):
            continue
        if "GET" in route.methods or (route.path in READ_POSTS and "POST" in route.methods):
            out.append(route)
    return out


def _response_schema(spec: dict[str, Any], path: str, method: str) -> dict[str, Any]:
    """The JSON schema of a route's success response in the OpenAPI document."""
    responses = spec["paths"][path][method]["responses"]
    ok = next(body for code, body in sorted(responses.items()) if code.startswith("2"))
    return dict(ok.get("content", {}).get("application/json", {}).get("schema", {}))


def _steps(path: str) -> list[str]:
    """A declared path as schema steps: ``items[].amount`` -> items, [], amount."""
    out: list[str] = []
    for part in path.split("."):
        out += [part[:-2], "[]"] if part.endswith("[]") and part != "[]" else [part]
    return out


def _alternatives(spec: dict[str, Any], schema: dict[str, Any]) -> list[dict[str, Any]]:
    while "$ref" in schema:
        schema = spec["components"]["schemas"][schema["$ref"].rsplit("/", 1)[-1]]
    for key in ("anyOf", "oneOf", "allOf"):
        if key in schema:
            return [alt for part in schema[key] for alt in _alternatives(spec, part)]
    return [schema]


def _schema_has(spec: dict[str, Any], schema: dict[str, Any], steps: list[str]) -> bool:
    """Whether ``steps`` lead to a field of the response schema. A free-form
    object (a ``dict`` with no declared properties) may hold any key, so a
    path into one is taken on trust: the canary's seeded values check it."""
    if not steps:
        return True
    head, rest = steps[0], steps[1:]
    for alt in _alternatives(spec, schema):
        if head == "[]":
            if alt.get("type") == "array" and _schema_has(spec, alt.get("items", {}), rest):
                return True
            continue
        properties = alt.get("properties") or {}
        if head in properties:
            if _schema_has(spec, properties[head], rest):
                return True
            continue
        extra = alt.get("additionalProperties", not properties)
        free_form = alt.get("type", "object") == "object" and not properties and extra
        if free_form and (not isinstance(extra, dict) or _schema_has(spec, extra, rest)):
            return True
    return False


def _bearer(seeded: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {seeded['token']}"}


def _leaks(
    path: str,
    body: str,
    extra_texts: tuple[str, ...] = (),
    extra_amounts: tuple[str, ...] = (),
) -> list[tuple[str, str]]:
    found = []
    for secret in PAYEES + SCHEDULE_PAYEES + SCHEDULE_NAMES + DESCRIPTIONS + [RULE_TEXT]:
        if secret in body:
            found.append((path, secret))
    for secret in extra_texts:
        if secret in body:
            found.append((path, secret))
    for amount in (*SINGLE_AMOUNTS, *extra_amounts):
        i = _amount_at(body, amount)
        if i >= 0:
            found.append((path, body[max(0, i - 100) : i + 20]))
    return found


# Evidence-bearing suggestions (#648 M2): only GET /suggestions/{id} returns
# a card's full evidence, and the seed's one card (an agent proposal) has
# none. A detected schedule's evidence carries each occurrence's amount and
# statistics of them; a match card's carries the bank row's own text.
EVIDENCE_PAYEE = "Tessellate Insurance"
EVIDENCE_AMOUNTS = ("-74.13", "-76.58", "-79.02", "-81.47")
# Two bills the matcher proposes against bank rows (expected, posted, the
# variance between them): two, so the forecast's net of pending matches (an
# aggregate) is no one bill's amount.
MATCH_PAIRS = (
    ("Halcyon Waterworks", "Halcyon Waterworks Pmt", "-188.41", "-97.36", "91.05"),
    ("Brindlewood Gas", "Brindlewood Gas Pmt", "-143.27", "-66.52", "76.75"),
)
MATCH_PAYEES = tuple(name for pair in MATCH_PAIRS for name in pair[:2])
MATCH_AMOUNTS = tuple(amount for pair in MATCH_PAIRS for amount in pair[2:])
MATCH_BANK_TEXT = "CANARY-EVID-BANKTEXT"
EVIDENCE_TEXTS = (EVIDENCE_PAYEE, *MATCH_PAYEES, MATCH_BANK_TEXT)


def _bank_row(engine: Engine, *, payee: str, amount: str, on: Any, description: str) -> None:
    """An import-origin row on checking, as the importer writes one."""
    with engine.begin() as conn:
        account = conn.execute(
            text("SELECT id FROM accounts WHERE path = 'assets.checking'")
        ).scalar_one()
        category = conn.execute(
            text("SELECT id FROM accounts WHERE path = 'expenses.groceries'")
        ).scalar_one()
        payee_id = (
            conn.execute(
                text("SELECT id FROM payees WHERE name_bidx = :b"),
                {"b": crypto.blind_index(normalize_payee(payee))},
            ).scalar_one_or_none()
            or conn.execute(
                text("INSERT INTO payees (name_enc, name_bidx) VALUES (:n, :b) RETURNING id"),
                {"n": crypto.encrypt(payee), "b": crypto.blind_index(normalize_payee(payee))},
            ).scalar_one()
        )
        tx = conn.execute(
            text(
                "INSERT INTO transactions (date, payee_id, description_enc, status, origin)"
                " VALUES (:d, :p, :desc, 'posted', 'import') RETURNING id"
            ),
            {"d": on, "p": payee_id, "desc": crypto.encrypt(description)},
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO postings (transaction_id, account_id, amount) VALUES"
                " (:tx, :account, :amount), (:tx, :category, :negated)"
            ),
            {
                "tx": tx,
                "account": account,
                "category": category,
                "amount": amount,
                "negated": str(-Decimal(amount)),
            },
        )


@pytest.fixture
def evidence_cards(
    seeded: dict[str, Any], admin_client: TestClient, migrated_engine: Engine
) -> dict[str, str]:
    """A detected schedule card and a match card, by kind."""
    for months_ago, amount in zip((3, 2, 1, 0), EVIDENCE_AMOUNTS, strict=True):
        _bank_row(
            migrated_engine,
            payee=EVIDENCE_PAYEE,
            amount=amount,
            on=_month_day(months_ago, 15),
            description=f"{EVIDENCE_PAYEE.upper()} PREMIUM",
        )
    r = admin_client.post("/schedules/detect")
    assert r.status_code == 200, r.text
    # Bills and bank rows the matcher proposes but does not auto-match:
    # close payee and date, far-off amount.
    for i, (bill_payee, bank_payee, expected, posted, _) in enumerate(MATCH_PAIRS):
        r = admin_client.post(
            "/transactions",
            json={
                "account": "assets.checking",
                "category": "expenses.groceries",
                "amount": expected,
                "date": (today() - timedelta(days=i)).isoformat(),
                "payee": bill_payee,
                "status": "expected",
            },
        )
        assert r.status_code == 201, r.text
        _bank_row(
            migrated_engine,
            payee=bank_payee,
            amount=posted,
            on=today() - timedelta(days=i) + timedelta(days=3),
            description=f"{MATCH_BANK_TEXT}-{i}",
        )
    r = admin_client.post("/matching/run")
    assert r.status_code == 200, r.text
    cards: dict[str, str] = {}
    for card in admin_client.get("/suggestions", params={"status": "all"}).json():
        detail = admin_client.get(f"/suggestions/{card['id']}").json()
        if (detail.get("evidence") or {}).get("occurrences"):
            cards.setdefault(card["kind"], card["id"])
    assert {"schedule", "match"} <= set(cards), cards
    return cards


# A hand-tracked position with every event shape that carries a price, fees
# or a basis (#667): a buy with fees that opens a lot, an award vest at a fair
# market value, a sale with fees drawn from that lot, and a reverse split
# whose fractional share leaves as cash in lieu. A trade's cash row and the
# lots' basis are single amounts as well; so are price and fees, since the
# shares times the price, plus or minus the fees, give the cash away.
HOLDING_SYMBOL = "CNRL"
BUY = {"quantity": "7", "price": "41.37", "fees": "4.83"}  # cash and lot basis 294.42
SELL = {"quantity": "2", "price": "47.61", "fees": "3.17"}  # proceeds 92.05
VEST = {"gross_quantity": "5", "withheld_quantity": "2", "fmv": "52.19"}  # basis 156.57
CASH_IN_LIEU = "17.23"
HOLDING_TEXTS = ("CANARY-PLAN-LOT", "CANARY-PLAN-AWARD", "CANARY-GRANT-REF")
HOLDING_AMOUNTS = (
    *(BUY["price"], BUY["fees"], "294.42"),
    *(SELL["price"], SELL["fees"], "92.05"),
    *("84.12", "7.93"),  # the sale's disposition: basis and gain
    *(VEST["fmv"], "156.57"),
    CASH_IN_LIEU,
)


@pytest.fixture
def holdings(admin_client: TestClient) -> dict[str, Any]:
    """Ids of the position, a lot, the award and the account, plus the price
    the cash-in-lieu sale was recorded at (cash in lieu / fraction)."""
    c = admin_client
    r = c.post("/accounts", json={"name": "Plan", "type": "brokerage", "path": "assets.plan"})
    assert r.status_code == 201, r.text
    account = r.json()["id"]
    assert c.patch(f"/accounts/{account}", json={"holdings_source": "manual"}).status_code == 200
    r = c.post(
        f"/accounts/{account}/positions",
        json={"symbol": HOLDING_SYMBOL, "as_of": (today() - timedelta(days=35)).isoformat()},
    )
    assert r.status_code == 201, r.text
    position = r.json()["id"]
    events = f"/positions/{position}/events"
    r = c.post(
        events,
        json={
            "kind": "buy",
            "occurred_on": (today() - timedelta(days=30)).isoformat(),
            **BUY,
            "funded_from": "cash",
            "lot": {"plan": HOLDING_TEXTS[0]},
        },
    )
    assert r.status_code == 201, r.text
    capture = {
        "as_of": (today() - timedelta(days=28)).isoformat(),
        "captured_at": now().isoformat(),
        "completeness": "complete",
        "awards": [
            {
                "grant_ref": HOLDING_TEXTS[2],
                "symbol": HOLDING_SYMBOL,
                "granted_on": (today() - timedelta(days=400)).isoformat(),
                "granted_quantity": "20",
                "plan": HOLDING_TEXTS[1],
                "schedule": [
                    {"vest_date": (today() - timedelta(days=25)).isoformat(), "quantity": "5"},
                    {"vest_date": (today() + timedelta(days=200)).isoformat(), "quantity": "5"},
                ],
            }
        ],
    }
    preview = c.post(f"/accounts/{account}/award-captures", json=capture)
    assert preview.status_code == 200, preview.text
    done = c.post(
        f"/accounts/{account}/award-captures",
        json={**capture, "dry_run": False, "expect_hash": preview.json()["plan_hash"]},
    )
    assert done.status_code == 200 and done.json()["applied"], done.text
    [award] = c.get(f"/accounts/{account}/awards").json()["awards"]
    r = c.post(
        f"/awards/{award['id']}/vests",
        json={"vest_date": (today() - timedelta(days=25)).isoformat(), **VEST},
    )
    assert r.status_code == 200, r.text
    assert r.json()["position_id"] == position, r.text
    r = c.post(
        events,
        json={
            "kind": "sell",
            "occurred_on": (today() - timedelta(days=20)).isoformat(),
            **SELL,
            "lot_method": "fifo",
        },
    )
    assert r.status_code == 201, r.text
    # 8 shares, 1:3: 2.666667 shares, and the 0.666667 left over is sold.
    r = c.post(
        events,
        json={
            "kind": "split",
            "occurred_on": (today() - timedelta(days=10)).isoformat(),
            "split_numerator": 1,
            "split_denominator": 3,
            "cash_in_lieu": CASH_IN_LIEU,
            "lot_method": "fifo",
        },
    )
    assert r.status_code == 201, r.text
    in_lieu = r.json()["cash_in_lieu"]
    assert in_lieu is not None, r.text
    lots = c.get(f"/positions/{position}/lots", params={"status": "all"}).json()["lots"]
    bought = next(lot for lot in lots if lot["plan"] == HOLDING_TEXTS[0])
    return {
        "account": account,
        "position": position,
        "lot": bought["id"],
        "award": award["id"],
        "in_lieu_price": in_lieu["price"],
    }


class TestDeclarations:
    def test_the_walk_sees_every_read_route(self, admin_client: TestClient) -> None:
        """The checks below are only as good as the walk: it must find every
        GET the OpenAPI schema documents (a floor alone would not notice a
        router dropping out)."""
        found = {(r.path, m) for r in _read_routes(admin_client) for m in r.methods}
        assert len({p for p, _ in found}) >= MIN_READ_ROUTES, sorted(found)
        spec = admin_client.get("/openapi.json").json()
        documented = {
            (path, method.upper())
            for path, ops in spec["paths"].items()
            for method in ops
            if (method == "get" or (method == "post" and path in READ_POSTS))
            and path not in EXEMPT_PATHS
            and not path.startswith(EXEMPT_PREFIXES)
        }
        assert documented <= found, sorted(documented - found)

    def test_every_read_route_declares_what_it_emits(self, admin_client: TestClient) -> None:
        """No default-open route: a read route without a disclosure
        declaration fails here, like an MCP tool without ``emits``."""
        missing = [r.path for r in _read_routes(admin_client) if _declared(r) is None]
        assert not missing, missing

    def test_declared_paths_name_fields_the_response_has(self, admin_client: TestClient) -> None:
        """A declared path the response lacks drops nothing, while the field
        it meant goes out beside a ``withheld`` that says it did not (#667:
        the position events route named ``[].proceeds`` and ``[].amount``,
        which its model has not, and missed ``[].price`` and ``[].fees``)."""
        spec = admin_client.get("/openapi.json").json()
        phantom = []
        for route in _read_routes(admin_client):
            emits = _declared(route)
            method = "get" if "GET" in route.methods else "post"
            schema = _response_schema(spec, route.path, method)
            for path in (*emits.description, *emits.amount, *emits.balance, *emits.external_ids):
                if not _schema_has(spec, schema, _steps(path)):
                    phantom.append((route.path, path))
        assert not phantom, phantom

    def test_the_path_check_finds_a_phantom(self, admin_client: TestClient) -> None:
        """The check itself: a real field passes, a missing one fails, at any depth."""
        spec = admin_client.get("/openapi.json").json()
        schema = _response_schema(spec, "/lots/{lot_id}", "get")
        assert _schema_has(spec, schema, _steps("disposition_rows[].proceeds"))
        assert not _schema_has(spec, schema, _steps("disposition_rows[].amount"))
        assert not _schema_has(spec, schema, _steps("proceeds"))
        events = _response_schema(spec, "/positions/{position_id}/events", "get")
        assert _schema_has(spec, events, _steps("[].price"))
        assert not _schema_has(spec, events, _steps("[].amount"))

    def test_withholdable_routes_take_the_parameter(self, admin_client: TestClient) -> None:
        spec = admin_client.get("/openapi.json").json()
        for route in _read_routes(admin_client):
            emits = _declared(route)
            method = "get" if "GET" in route.methods else "post"
            params = {p["name"] for p in spec["paths"][route.path][method].get("parameters", [])}
            assert ("withhold" in params) == emits.withholdable(), route.path


def _urls(
    admin_client: TestClient, seeded: dict[str, Any], holdings: dict[str, Any]
) -> dict[str, str]:
    """A callable URL for each declared read route that can withhold."""
    accounts = {a["path"]: a["id"] for a in admin_client.get("/accounts").json()}
    checking = accounts["assets.checking"]
    position = holdings["position"]
    tx = seeded["tx_ids"][0]
    schedule = seeded["schedules"][0]
    instance = admin_client.get(f"/transactions/{seeded['instance']}").json()
    return {
        "/transactions": "/transactions",
        "/transactions/review-batch": "/transactions/review-batch",
        "/transactions/{tx_id}": f"/transactions/{tx}",
        "/transactions/{tx_id}/split/candidates": (
            f"/transactions/{tx}/split/candidates?account=liabilities.card"
        ),
        "/accounts/{account_id}/balance/series": f"/accounts/{checking}/balance/series",
        "/matching/proposed": "/matching/proposed",
        "/matching/unmatched": "/matching/unmatched",
        "/matching/transfer-candidates": "/matching/transfer-candidates",
        "/schedules": "/schedules",
        "/schedules/{schedule_id}": f"/schedules/{schedule}",
        "/schedules/instances/{transaction_id}/certainty": (
            f"/schedules/instances/{seeded['instance']}/certainty"
        ),
        "/schedules/{schedule_id}/occurrences/{occurrence_date}/certainty": (
            f"/schedules/{schedule}/occurrences/{instance['date']}/certainty"
        ),
        "/forecast": "/forecast?account=assets.checking&days=30",
        "/suggestions": "/suggestions",
        "/suggestions/{suggestion_id}": f"/suggestions/{seeded['suggestion']}",
        "/rules": "/rules",
        "/memories": "/memories",
        "/events": "/events",
        # The seeded position, lot and award (#667): each route answers 200
        # with the shapes that carry a price, fees, a basis or a vest's value.
        "/accounts/{account_id}/awards": (
            f"/accounts/{holdings['account']}/awards?include_vests=true"
        ),
        "/positions/{position_id}/lots": f"/positions/{position}/lots?status=all",
        "/positions/{position_id}/events": f"/positions/{position}/events",
        "/lots/{lot_id}": f"/lots/{holdings['lot']}",
        "/awards/{award_id}": f"/awards/{holdings['award']}",
    }


# Withholdable routes the seed leaves empty, so the canary proves nothing for
# them yet: no transfer candidate pair is seeded. Every other route must
# answer with seeded data.
UNSEEDED = {"/matching/transfer-candidates"}


class TestCanary:
    def test_no_route_discloses_a_withheld_field(
        self,
        seeded: dict[str, Any],
        evidence_cards: dict[str, str],
        holdings: dict[str, Any],
        admin_client: TestClient,
    ) -> None:
        withholdable = {r.path for r in _read_routes(admin_client) if _declared(r).withholdable()}
        assert withholdable, "the walk found no withholdable route"
        urls = _urls(admin_client, seeded, holdings)
        assert withholdable <= set(urls), withholdable - set(urls)
        extra = {
            "extra_texts": EVIDENCE_TEXTS + HOLDING_TEXTS,
            "extra_amounts": (
                EVIDENCE_AMOUNTS + MATCH_AMOUNTS + HOLDING_AMOUNTS + (holdings["in_lieu_price"],)
            ),
        }
        leaks = []
        empty = []
        for path in sorted(withholdable):
            url = urls[path]
            sep = "&" if "?" in url else "?"
            r = admin_client.get(f"{url}{sep}withhold={ALL}", headers=_bearer(seeded))
            assert r.status_code == 200, (path, r.text)
            if r.json() in ([], {}):
                empty.append(path)
            leaks += _leaks(path, r.text, **extra)
        # Each evidence-bearing card through the detail route, too.
        for kind, card in sorted(evidence_cards.items()):
            r = admin_client.get(f"/suggestions/{card}?withhold={ALL}", headers=_bearer(seeded))
            assert r.status_code == 200, r.text
            assert r.json()["evidence"]["occurrences"], kind
            leaks += _leaks(f"/suggestions/{{id}} ({kind})", r.text, **extra)
        assert not leaks, leaks
        # A 404 or an empty answer passes the leak check without proving
        # anything, so every route must reach seeded data (#667).
        assert set(empty) <= UNSEEDED, sorted(set(empty) - UNSEEDED)

    def test_withholding_amounts_drops_them_from_suggestion_evidence(
        self,
        seeded: dict[str, Any],
        evidence_cards: dict[str, str],
        admin_client: TestClient,
    ) -> None:
        """``withheld: ["amount"]`` is only true when the evidence lost its
        amounts; the dates, cadence and descriptions stay (#648 M2)."""
        card = evidence_cards["schedule"]
        full = admin_client.get(f"/suggestions/{card}", headers=_bearer(seeded)).json()
        assert all("amount" in o for o in full["evidence"]["occurrences"])
        private = admin_client.get(
            f"/suggestions/{card}?withhold=amount", headers=_bearer(seeded)
        ).json()
        assert private["withheld"] == ["amount"]
        occurrences = private["evidence"]["occurrences"]
        assert occurrences and all(set(o) >= {"date"} and "amount" not in o for o in occurrences)
        statistics = private["evidence"]["amount"]
        assert not {"typical", "latest", "low", "high"} & set(statistics)
        assert "price_changed" in statistics
        assert private["evidence"]["cadence"] == full["evidence"]["cadence"]

        match = evidence_cards["match"]
        private = admin_client.get(
            f"/suggestions/{match}?withhold=description", headers=_bearer(seeded)
        ).json()
        assert private["withheld"] == ["description"]
        (occurrence,) = private["evidence"]["occurrences"]
        assert "description" not in occurrence and "amount" in occurrence
        assert MATCH_BANK_TEXT not in json.dumps(private)
        full = admin_client.get(f"/suggestions/{match}", headers=_bearer(seeded)).json()
        assert full["evidence"]["occurrences"][0]["description"].startswith(MATCH_BANK_TEXT)

    def test_position_events_keep_the_shares_and_drop_price_fees_and_basis(
        self, seeded: dict[str, Any], holdings: dict[str, Any], admin_client: TestClient
    ) -> None:
        """Shares are not money, but shares times a price, plus or minus the
        fees, is a trade's cash, and a split's cash in lieu is its fraction
        times a price chosen to give that cash exactly (#667)."""
        url = f"/positions/{holdings['position']}/events"
        full = admin_client.get(url, headers=_bearer(seeded)).json()
        assert {"buy", "sell", "vest", "split"} <= {e["kind"] for e in full}
        lieu = next(e for e in full if e["in_lieu_of"])
        cash = (Decimal(lieu["price"]) * Decimal(lieu["quantity"])).quantize(Decimal("0.01"))
        assert cash == Decimal(CASH_IN_LIEU)  # what the seed proves the route must hide
        private = admin_client.get(f"{url}?withhold=amount", headers=_bearer(seeded)).json()
        assert [e["id"] for e in private] == [e["id"] for e in full]
        for before, after in zip(full, private, strict=True):
            assert after["withheld"] == ["amount"], after
            assert not {"price", "fees", "cost_basis"} & set(after), after
            assert after["quantity"] == before["quantity"]
        vest = next(e for e in private if e["kind"] == "vest")
        assert (vest["gross_quantity"], vest["withheld_quantity"]) == ("5.000000", "2.000000")

    def test_award_vests_drop_their_fair_market_value(
        self, seeded: dict[str, Any], holdings: dict[str, Any], admin_client: TestClient
    ) -> None:
        """A vest's net shares times its fair market value is the basis of the
        lot it opened, which the lot routes withhold (#667)."""
        detail = f"/awards/{holdings['award']}"
        listing = f"/accounts/{holdings['account']}/awards?include_vests=true"
        for url, vests in (
            (f"{detail}?", lambda body: body["vests"]),
            (f"{listing}&", lambda body: body["awards"][0]["vests"]),
        ):
            full = vests(admin_client.get(url, headers=_bearer(seeded)).json())
            assert any(v["vested_fmv"] is not None for v in full), url
            body = admin_client.get(f"{url}withhold=amount", headers=_bearer(seeded)).json()
            private = vests(body)
            assert body["withheld"] == ["amount"], url
            assert private and all("vested_fmv" not in v for v in private), url
            assert [v["vested_net"] for v in private] == [v["vested_net"] for v in full]

    def test_without_withhold_the_response_is_full_detail(
        self, seeded: dict[str, Any], admin_client: TestClient
    ) -> None:
        r = admin_client.get("/transactions", headers=_bearer(seeded))
        body = r.json()
        assert "withheld" not in body
        assert any(i["payee"] == "Zephyrine Bakery" for i in body["items"])
        assert all("amount" in i for i in body["items"])

    def test_withhold_names_exactly_what_it_left_out(
        self, seeded: dict[str, Any], admin_client: TestClient
    ) -> None:
        tx = seeded["tx_ids"][0]
        r = admin_client.get(f"/transactions/{tx}?withhold=amount", headers=_bearer(seeded))
        body = r.json()
        assert body["withheld"] == ["amount"]
        assert body["payee"] == "Zephyrine Bakery" and body["description"] == "CANARY-DESC-ALPHA"
        assert "amount" not in body and all("amount" not in leg for leg in body["postings"])

        r = admin_client.get(
            "/transactions?withhold=payee&withhold=description", headers=_bearer(seeded)
        )
        body = r.json()
        assert body["withheld"] == ["description", "payee"]
        assert all(i["payee"].startswith("py_") for i in body["items"] if i["payee"])
        assert all("description" not in i for i in body["items"])
        assert all("amount" in i for i in body["items"])

    def test_an_unknown_field_is_refused(
        self, seeded: dict[str, Any], admin_client: TestClient
    ) -> None:
        r = admin_client.get("/transactions?withhold=memo", headers=_bearer(seeded))
        assert r.status_code == 422 and "unknown withhold field" in r.text

    def test_the_web_app_is_unaffected(
        self, seeded: dict[str, Any], admin_client: TestClient
    ) -> None:
        """A signed-in session never passes withhold and sees full detail."""
        body = admin_client.get("/transactions").json()
        assert any(i["payee"] == "Zephyrine Bakery" for i in body["items"])

    def test_balance_series_collapses_to_end_of_day_without_amounts(
        self, seeded: dict[str, Any], admin_client: TestClient
    ) -> None:
        accounts = {a["path"]: a["id"] for a in admin_client.get("/accounts").json()}
        url = f"/accounts/{accounts['assets.checking']}/balance/series"
        full = admin_client.get(url, headers=_bearer(seeded)).json()
        private = admin_client.get(f"{url}?withhold=amount", headers=_bearer(seeded)).json()
        assert all(p.get("delta") is not None for p in full["points"])
        assert private["withheld"] == ["amount"]
        assert all(set(p) <= {"date", "balance"} for p in private["points"])
        assert len({p["date"] for p in private["points"]}) == len(private["points"])


class TestReferences:
    def _ref(self, seeded: dict[str, Any], client: TestClient) -> str:
        tx = seeded["tx_ids"][0]
        ref = client.get(f"/transactions/{tx}?withhold=payee", headers=_bearer(seeded)).json()
        assert ref["payee"].startswith("py_")
        return str(ref["payee"])

    def test_a_reference_filters_like_the_payee_name(
        self, seeded: dict[str, Any], admin_client: TestClient
    ) -> None:
        ref = self._ref(seeded, admin_client)
        r = admin_client.get(f"/transactions?payee={ref}&withhold=payee", headers=_bearer(seeded))
        assert r.status_code == 200, r.text
        items = r.json()["items"]
        assert {i["id"] for i in items} == set(seeded["tx_ids"][:2])
        assert {i["payee"] for i in items} == {ref}
        # The audit records the reference the caller sent, not the name.
        rows = admin_client.get("/audit", params={"limit": 20}).json()["items"]
        call = next(
            r
            for r in rows
            if (r.get("detail") or {}).get("path") == "/transactions"
            and "payee" in ((r.get("detail") or {}).get("query") or {})
        )
        assert ref in str(call["detail"]["query"]["payee"])
        assert "Zephyrine" not in json.dumps(call["detail"])

    def test_a_reference_names_the_payee_on_a_write(
        self, seeded: dict[str, Any], admin_client: TestClient
    ) -> None:
        ref = self._ref(seeded, admin_client)
        r = admin_client.post(
            "/transactions",
            json={
                "account": "assets.checking",
                "category": "expenses.dining",
                "amount": "-2.00",
                "date": "2026-01-02",
                "payee": ref,
                "status": "posted",
            },
            headers=_bearer(seeded),
        )
        assert r.status_code == 201, r.text
        assert admin_client.get(f"/transactions/{r.json()['id']}").json()["payee"] == (
            "Zephyrine Bakery"
        )

    def test_a_reference_in_free_text_or_unknown_is_refused(
        self, seeded: dict[str, Any], admin_client: TestClient
    ) -> None:
        ref = self._ref(seeded, admin_client)
        base = {
            "account": "assets.checking",
            "category": "expenses.dining",
            "amount": "-1.00",
            "date": "2026-01-02",
            "status": "posted",
        }
        refused = admin_client.post(
            "/transactions",
            json={**base, "description": f"lunch at {ref}"},
            headers=_bearer(seeded),
        )
        assert refused.status_code == 422 and "cannot contain a payee reference" in refused.text
        unknown = admin_client.post(
            "/transactions", json={**base, "payee": "py_aaaaaaaaaa"}, headers=_bearer(seeded)
        )
        assert unknown.status_code == 422 and "unknown payee reference" in unknown.text


class TestPresent:
    def test_present_links_rows_for_the_signed_in_person(
        self, seeded: dict[str, Any], admin_client: TestClient
    ) -> None:
        ids = seeded["tx_ids"][:2]
        r = admin_client.post(
            "/transactions/present",
            json={"transaction_ids": ids, "note": "two largest"},
            headers=_bearer(seeded),
        )
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["count"] == 2 and "/selection?" in out["url"]
        assert "note=two+largest" in out["url"]
        query = out["url"].split("?", 1)[1]
        rows = admin_client.get(f"/transactions/selection?{query}")
        assert [row["id"] for row in rows.json()] == ids
        assert (
            admin_client.get(
                f"/transactions/selection?{query}", headers=_bearer(seeded)
            ).status_code
            == 403
        )

    def test_present_refuses_an_unknown_id(
        self, seeded: dict[str, Any], admin_client: TestClient
    ) -> None:
        r = admin_client.post(
            "/transactions/present",
            json={"transaction_ids": ["00000000-0000-0000-0000-000000000000"]},
            headers=_bearer(seeded),
        )
        assert r.status_code == 422 and "unknown transaction id" in r.text


class TestAudit:
    @pytest.mark.parametrize("withhold", [ALL, None])
    def test_the_row_says_whether_privacy_was_used_or_the_answer_was_identifiable(
        self, seeded: dict[str, Any], admin_client: TestClient, withhold: str | None
    ) -> None:
        url = "/transactions" + (f"?withhold={withhold}" if withhold else "")
        assert admin_client.get(url, headers=_bearer(seeded)).status_code == 200
        rows = admin_client.get("/audit", params={"limit": 50}).json()["items"]
        row = next(
            r
            for r in rows
            if (r.get("detail") or {}).get("path") == "/transactions"
            and (r.get("detail") or {}).get("method") == "GET"
        )
        privacy = row["detail"]["privacy"]
        if withhold:
            assert row["privacy_requested"] == ["amount", "description", "payee"]
            assert row["identifiable"] == []
            assert {"amount", "description", "payee"} <= set(privacy["withheld"])
        else:
            assert row["privacy_requested"] == []
            assert {"amount", "payee"} <= set(row["identifiable"])
        filtered = admin_client.get(
            "/audit/activity",
            params={"view": "sharing", "privacy": "requested" if withhold else "identifiable"},
        ).json()["items"]
        assert any(e["action"] == "GET /transactions" for e in filtered), json.dumps(filtered)[:500]
