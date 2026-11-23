"""Agents map bank accounts from Purser's proposal (issue #583, DESIGN §7.1,
decision 126): what the feed states becomes a proposed name, type and path;
a batch is checked whole and lands whole; ids and balances stay out of what a
non-admin token sees. All data is synthetic.
"""

import json
import uuid
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from purser import crypto, db
from purser.feed import proposals
from purser.models import Account, IgnoredFeedAccount, Transaction
from tests import test_feed, test_mcp
from tests.test_feed import FeedHarness, _old_tx, account_of
from tests.test_feed_inventory import _only
from tests.test_feed_v2 import CONN, as_v2

feed_env = test_feed.feed_env
harness = test_feed.harness
live_server = test_mcp.live_server

SECOND_LOGIN = dict(CONN, conn_id="CON-DEMO-2", name="Demo Bank - Sam")


def _sf(name: str, account_id: str, balance: str, **extra: Any) -> dict[str, Any]:
    account = account_of(extra.pop("transactions", []), account_id=account_id, balance=balance)
    account["name"] = name
    account.update(extra)
    return account


class TestProposeType:
    @pytest.mark.parametrize(
        ("account", "expected"),
        [
            # The name says nothing and the feed lists holdings: brokerage.
            (
                {"name": "Account 7", "balance": "10.00", "holdings": [{"symbol": "SYNTH"}]},
                ("brokerage", "holdings", True),
            ),
            # The name names a cash product and the feed lists holdings too:
            # the name's type, marked unsure, for the person to settle.
            (
                {"name": "Demo Savings", "balance": "10.00", "holdings": [{"symbol": "SYNTH"}]},
                ("savings", "name", False),
            ),
            (
                {"name": "Rollover IRA", "balance": "10.00", "holdings": [{"symbol": "SYNTH"}]},
                ("brokerage", "name", True),
            ),
            # The bank's own words, the product before the brand.
            ({"name": "Everyday Checking", "balance": "5.00"}, ("checking", "name", True)),
            (
                {"name": "Example Card Company Online Savings", "balance": "5.00"},
                ("savings", "name", True),
            ),
            ({"name": "Rewards Visa Signature", "balance": "-5.00"}, ("credit_card", "name", True)),
            ({"name": "Home Mortgage", "balance": "-250000.00"}, ("loan", "name", True)),
            ({"name": "Rollover IRA", "balance": "0.00"}, ("brokerage", "name", True)),
            # A name that says nothing: the balance decides, and says how sure.
            (
                {"name": "Platinum", "balance": "-400.00", "available-balance": "9600.00"},
                ("credit_card", "balance", True),
            ),
            ({"name": "Account 7", "balance": "-18000.00"}, ("loan", "balance", False)),
            (
                {"name": "Account 7", "balance": "-80.00", "transactions": [{"id": "t"}]},
                ("credit_card", "balance", False),
            ),
            # Nothing says: offered as checking, and marked unsure.
            ({"name": "Joint Account", "balance": "100.00"}, ("checking", "default", False)),
            ({"name": None, "balance": None}, ("checking", "default", False)),
        ],
    )
    def test_the_type_rests_on_what_the_feed_states(
        self, account: dict[str, Any], expected: tuple[str, str, bool]
    ) -> None:
        assert proposals.propose_type(account) == expected


def _rows() -> list[dict[str, Any]]:
    """Two posted rows dated back from the test's day (built per test, #533)."""
    return [_old_tx(20, "-40.00", "Hardware Store"), _old_tx(3, "900.00", "Acme Payroll")]


def _payload() -> dict[str, Any]:
    """Five delivered accounts at one bank, the last one a joint account that
    a second login delivers again."""
    checking = _sf("Everyday Checking (1111)", "ACT-CHK", "5000.00", transactions=_rows())
    savings = _sf("Rainy Day Savings *2222", "ACT-SAV", "1200.00")
    card = _sf(
        "Platinum (3333)",
        "ACT-CRD",
        "-400.00",
        **{"available-balance": "9600.00"},
        transactions=[_old_tx(5, "-60.00", "Bookshop")],
    )
    broker = _sf("Account (4444)", "ACT-BRK", "300.00")
    broker["holdings"] = [{"symbol": "SYNTH", "shares": "1.0", "market_value": "250.00"}]
    joint = _sf("Joint Account", "ACT-JNT-A", "77.00")
    twin = _sf("Joint Account", "ACT-JNT-B", "77.00")
    payload = as_v2({"accounts": [checking, savings, card, broker, joint, twin]})
    payload["connections"].append(SECOND_LOGIN)
    payload["accounts"][5]["conn_id"] = SECOND_LOGIN["conn_id"]
    return payload


def _ready(harness: FeedHarness) -> str:
    harness.use_payload(_payload())
    connection_id = harness.connect()
    _only(connection_id)
    harness.sync(connection_id)
    return connection_id


def _token(harness: FeedHarness, scopes: list[str]) -> dict[str, str]:
    resp = harness.client.post("/tokens", json={"name": "-".join(scopes), "scopes": scopes})
    assert resp.status_code in (200, 201), resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def _agent(harness: FeedHarness) -> TestClient:
    """The same app as an agent would reach it: its bearer token, and no
    admin session cookie."""
    headers = _token(harness, ["read", "write"])
    harness.client.cookies.clear()
    harness.client.headers.update(headers)
    return harness.client


def _plan(client: TestClient) -> list[dict[str, Any]]:
    resp = client.post("/connections/map-accounts", json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["results"] is None
    return list(body["plan"])


def _by_bank_name(plan: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for line in plan:
        out.setdefault(str(line["bank_name"]), line)
    return out


def _counts() -> tuple[int, int, int]:
    with db.get_sessionmaker()() as session:
        return (
            session.scalar(
                select(func.count()).select_from(Account).where(Account.is_virtual.is_(False))
            )
            or 0,
            session.scalar(select(func.count()).select_from(Transaction)) or 0,
            session.scalar(select(func.count()).select_from(IgnoredFeedAccount)) or 0,
        )


class TestPlan:
    def test_every_waiting_account_gets_a_proposal_from_the_feed(
        self, harness: FeedHarness
    ) -> None:
        _ready(harness)
        plan = _plan(_agent(harness))

        assert len(plan) == 6
        lines = _by_bank_name(plan)
        assert lines["Everyday Checking"] == {
            "ref": lines["Everyday Checking"]["ref"],
            "institution": CONN["name"],
            "bank_name": "Everyday Checking",
            "action": "map",
            "name": "Everyday Checking",
            "type": "checking",
            "type_basis": "name",
            "confident": True,
            "path": "assets.everyday_checking",
            "starting_balance": True,
        }
        assert (lines["Rainy Day Savings"]["type"], lines["Rainy Day Savings"]["path"]) == (
            "savings",
            "assets.rainy_day_savings",
        )
        # No word for the product: a debt with credit left is a card.
        assert (lines["Platinum"]["type"], lines["Platinum"]["type_basis"]) == (
            "credit_card",
            "balance",
        )
        assert lines["Platinum"]["path"] == "liabilities.platinum"
        # Holdings in the feed: brokerage, valued from the feed, so no
        # starting balance.
        assert (lines["Account"]["type"], lines["Account"]["type_basis"]) == (
            "brokerage",
            "holdings",
        )
        assert lines["Account"]["starting_balance"] is False

    def test_a_cash_product_that_lists_holdings_is_left_to_the_person(
        self, harness: FeedHarness
    ) -> None:
        mixed = _sf("Demo Savings", "ACT-MIX", "900.00")
        mixed["holdings"] = [{"symbol": "SYNTH", "shares": "2.0", "market_value": "500.00"}]
        harness.use_payload(as_v2({"accounts": [mixed]}))
        connection_id = harness.connect()
        _only(connection_id)
        harness.sync(connection_id)

        [line] = _plan(_agent(harness))
        assert (line["type"], line["type_basis"], line["confident"]) == ("savings", "name", False)
        assert line["note"] == "the feed lists holdings for this account"
        assert line["starting_balance"] is True

    def test_a_second_login_delivering_the_same_account_is_proposed_as_ignore(
        self, harness: FeedHarness
    ) -> None:
        _ready(harness)
        joints = [line for line in _plan(_agent(harness)) if line["bank_name"] == "Joint Account"]

        first, second = joints
        assert first["action"] == "map" and "duplicate_of" not in first
        # Nothing in the feed says what a "Joint Account" is.
        assert (first["type"], first["type_basis"], first["confident"]) == (
            "checking",
            "default",
            False,
        )
        assert second["action"] == "ignore" and second["duplicate_of"] == first["ref"]
        assert second["institution"] == SECOND_LOGIN["name"]

    def test_the_bank_ids_digits_and_balances_never_appear(self, harness: FeedHarness) -> None:
        _ready(harness)
        dumped = json.dumps(_plan(_agent(harness)))

        for secret in ("ACT-", "1111", "2222", "3333", "5000.00", "1200.00", "-400.00", "77.00"):
            assert secret not in dumped, secret
        # The ref is stable and says nothing about the id it stands for.
        with db.get_sessionmaker()() as session:
            del session
        cid = uuid.uuid4()
        ref = crypto.feed_account_ref(cid, "ACT-CHK")
        assert ref == crypto.feed_account_ref(cid, "ACT-CHK")
        assert ref.startswith("fa_") and "ACT" not in ref
        assert ref != crypto.feed_account_ref(uuid.uuid4(), "ACT-CHK")

    def test_names_and_paths_stay_clear_of_what_the_ledger_already_holds(
        self, harness: FeedHarness
    ) -> None:
        _ready(harness)
        resp = harness.client.post(
            "/accounts",
            json={
                "name": "Everyday Checking",
                "type": "checking",
                "path": "assets.everyday_checking",
            },
        )
        assert resp.status_code == 201, resp.text

        line = _by_bank_name(_plan(_agent(harness)))["Everyday Checking"]
        assert line["name"] == f"Everyday Checking ({CONN['name']})"
        assert line["path"] == "assets.everyday_checking_demo_bank_pat"

    def test_discovery_carries_the_proposal_and_feed_status_the_ref(
        self, harness: FeedHarness
    ) -> None:
        connection_id = _ready(harness)
        discovered = harness.client.get(f"/connections/{connection_id}/discovered").json()
        by_id = {row["external_id"]: row for row in discovered}
        assert by_id["ACT-CHK"]["proposal"]["type"] == "checking"
        assert by_id["ACT-JNT-B"]["proposal"]["action"] == "ignore"

        harness.map_new(connection_id, "ACT-CHK", "assets.checking")
        discovered = harness.client.get(f"/connections/{connection_id}/discovered").json()
        assert {r["external_id"]: r for r in discovered}["ACT-CHK"]["proposal"] is None

        from purser.feed import inventory

        with db.get_sessionmaker()() as session:
            status = inventory.feed_status(session)
            plan = {p.bank_name: p.ref for p in proposals.plan(session)}
        waiting = {a["name"]: a for a in status["attention"] if "not_connected" in a["problems"]}
        assert waiting["Rainy Day Savings"]["ref"] == plan["Rainy Day Savings"]
        assert "external_id" not in waiting["Rainy Day Savings"]


class TestApply:
    def test_the_plan_applied_as_proposed_maps_imports_and_starts_from_the_bank(
        self, harness: FeedHarness
    ) -> None:
        _ready(harness)
        agent = _agent(harness)
        plan = _plan(agent)
        lines = [{"ref": line["ref"]} for line in plan]

        dry = agent.post("/connections/map-accounts", json={"accounts": lines, "dry_run": True})
        assert dry.status_code == 200, dry.text
        assert dry.json()["dry_run"] is True
        assert _counts() == (0, 0, 0)

        done = agent.post("/connections/map-accounts", json={"accounts": lines})
        assert done.status_code == 200, done.text
        results = {r["bank_name"]: r for r in done.json()["results"] if r["action"] == "map"}
        assert [r["action"] for r in done.json()["results"]].count("ignore") == 1
        # What the dry run said is what happened.
        assert [(r["ref"], r["action"], r.get("account")) for r in dry.json()["results"]] == [
            (r["ref"], r["action"], r.get("account")) for r in done.json()["results"]
        ]

        accounts = {
            a["path"]: a for a in agent.get("/accounts", params={"include_balances": True}).json()
        }
        assert {p: a["type"] for p, a in accounts.items() if not a["is_virtual"]} == {
            "assets.everyday_checking": "checking",
            "assets.rainy_day_savings": "savings",
            "liabilities.platinum": "credit_card",
            "assets.account": "brokerage",
            "assets.joint_account": "checking",
        }
        # The rows already fetched are in, and each cash and card account
        # starts from the bank's balance (decision 123).
        assert Decimal(accounts["assets.everyday_checking"]["balance"]) == Decimal("5000.00")
        assert Decimal(accounts["liabilities.platinum"]["balance"]) == Decimal("-400.00")
        assert Decimal(results["Everyday Checking"]["starting_balance"]) == Decimal("4140.00")
        assert results["Account"]["starting_balance"] is False
        assert _counts()[2] == 1
        # Nothing is left to map, and a second apply of the same refs is refused.
        assert _plan(agent) == []
        again = agent.post("/connections/map-accounts", json={"accounts": lines[:1]})
        assert again.status_code == 422 and "not a bank account waiting" in again.text

    def test_a_line_overrides_what_it_names_and_keeps_the_rest(self, harness: FeedHarness) -> None:
        _ready(harness)
        agent = _agent(harness)
        lines = _by_bank_name(_plan(agent))

        resp = agent.post(
            "/connections/map-accounts",
            json={
                "accounts": [
                    # The person says the joint account is a savings account.
                    {
                        "ref": lines["Joint Account"]["ref"],
                        "type": "savings",
                        "name": "Holiday Fund",
                    },
                    {
                        "ref": lines["Platinum"]["ref"],
                        "path": "liabilities.cards.platinum",
                        "starting_balance": False,
                    },
                ]
            },
        )
        assert resp.status_code == 200, resp.text
        first, second = resp.json()["results"]
        assert (first["account"], first["name"], first["type"]) == (
            "assets.holiday_fund",
            "Holiday Fund",
            "savings",
        )
        assert Decimal(first["starting_balance"]) == Decimal("77.00")
        assert (second["account"], second["starting_balance"]) == (
            "liabilities.cards.platinum",
            False,
        )

    def test_mapping_onto_an_existing_account_and_ignoring_on_request(
        self, harness: FeedHarness
    ) -> None:
        _ready(harness)
        created = harness.client.post(
            "/accounts", json={"name": "House money", "type": "savings", "path": "assets.house"}
        )
        assert created.status_code == 201, created.text
        agent = _agent(harness)
        lines = _by_bank_name(_plan(agent))

        resp = agent.post(
            "/connections/map-accounts",
            json={
                "accounts": [
                    {"ref": lines["Rainy Day Savings"]["ref"], "account": "assets.house"},
                    {"ref": lines["Account"]["ref"], "action": "ignore"},
                ]
            },
        )
        assert resp.status_code == 200, resp.text
        onto, ignored = resp.json()["results"]
        assert (onto["account"], onto["new_account"], onto["starting_balance"]) == (
            "assets.house",
            False,
            False,
        )
        assert ignored == {
            "ref": lines["Account"]["ref"],
            "institution": CONN["name"],
            "bank_name": "Account",
            "action": "ignore",
        }
        waiting = {line["bank_name"] for line in _plan(agent)}
        assert "Rainy Day Savings" not in waiting and "Account" not in waiting

    def test_one_bad_line_stops_the_whole_batch(self, harness: FeedHarness) -> None:
        _ready(harness)
        agent = _agent(harness)
        lines = _by_bank_name(_plan(agent))

        resp = agent.post(
            "/connections/map-accounts",
            json={
                "accounts": [
                    {"ref": lines["Everyday Checking"]["ref"]},
                    {"ref": lines["Rainy Day Savings"]["ref"], "path": "assets.everyday_checking"},
                    {"ref": lines["Platinum"]["ref"], "account": "assets.nowhere"},
                    {"ref": "fa_notinanyplan"},
                    {"ref": lines["Account"]["ref"], "account": "assets.x", "type": "loan"},
                ]
            },
        )
        assert resp.status_code == 422
        # Every problem is named at once, and nothing was written.
        for said in ("accounts[1]", "accounts[2]", "accounts[3]", "accounts[4]"):
            assert said in resp.text, said
        assert "accounts[0]" not in resp.text
        assert _counts() == (0, 0, 0)

    def test_a_type_outside_the_ledgers_and_a_category_target_are_refused(
        self, harness: FeedHarness
    ) -> None:
        _ready(harness)
        agent = _agent(harness)
        ref = _by_bank_name(_plan(agent))["Everyday Checking"]["ref"]

        bad_type = agent.post(
            "/connections/map-accounts", json={"accounts": [{"ref": ref, "type": "equity"}]}
        )
        assert bad_type.status_code == 422 and "type must be one of" in bad_type.text
        system = agent.post(
            "/connections/map-accounts", json={"accounts": [{"ref": ref, "path": "equity.mine"}]}
        )
        assert system.status_code == 422
        assert _counts() == (0, 0, 0)

    def test_it_needs_write_scope(self, harness: FeedHarness) -> None:
        _ready(harness)
        headers = _token(harness, ["read"])
        harness.client.cookies.clear()
        resp = harness.client.post("/connections/map-accounts", json={}, headers=headers)
        assert resp.status_code == 403


class TestMcp:
    def test_plan_then_apply_over_mcp(
        self, harness: FeedHarness, live_server: test_mcp.LiveServer
    ) -> None:
        _ready(harness)
        raw = harness.client.post(
            "/tokens", json={"name": "agent", "scopes": ["read", "write"]}
        ).json()["token"]

        [planned, status] = test_mcp._mcp_call(
            live_server.url, raw, [("map_feed_accounts", {}), ("feed_status", {})]
        )
        plan = planned["plan"]
        assert len(plan) == 6
        refs = {a["ref"] for a in status["attention"] if "not_connected" in a["problems"]}
        assert {line["ref"] for line in plan} == refs

        lines = [{"ref": line["ref"]} for line in plan]
        dry, done, after = test_mcp._mcp_call(
            live_server.url,
            raw,
            [
                ("map_feed_accounts", {"accounts": lines, "dry_run": True}),
                ("map_feed_accounts", {"accounts": lines}),
                ("map_feed_accounts", {}),
            ],
        )
        assert dry["dry_run"] is True and done["dry_run"] is False
        assert len(done["results"]) == 6 and after == {"plan": []}
        assert _counts()[0] == 5

    def test_a_reader_is_refused(
        self, harness: FeedHarness, live_server: test_mcp.LiveServer
    ) -> None:
        _ready(harness)
        raw = harness.client.post("/tokens", json={"name": "reader", "scopes": ["read"]}).json()[
            "token"
        ]
        [denied] = test_mcp._mcp_call(live_server.url, raw, [("map_feed_accounts", {})])
        assert "write" in str(denied).lower()


class TestReplacingAFeed:
    """A line onto an account another bank account already feeds (#648 M1,
    decision 134): refused for write scope, naming the feed; an admin's
    explicit replace_feed replaces it, and the audit keeps the old mapping."""

    def _mapped_checking(self, harness: FeedHarness) -> tuple[str, dict[str, str]]:
        """Everyday Checking mapped as proposed; the plan's other refs."""
        connection_id = _ready(harness)
        refs = {k: v["ref"] for k, v in _by_bank_name(_plan(harness.client)).items()}
        done = harness.client.post(
            "/connections/map-accounts", json={"accounts": [{"ref": refs["Everyday Checking"]}]}
        )
        assert done.status_code == 200, done.text
        return connection_id, refs

    def _feed_of(self, path: str) -> tuple[str | None, uuid.UUID | None]:
        with db.get_sessionmaker()() as session:
            account = session.execute(select(Account).where(Account.path == path)).scalar_one()
            return account.external_id, account.connection_id

    def _onto_checking(self, ref: str, **extra: Any) -> dict[str, Any]:
        return {"accounts": [{"ref": ref, "account": "assets.everyday_checking", **extra}]}

    def test_a_write_token_is_refused_naming_the_feed_dry_run_included(
        self, harness: FeedHarness
    ) -> None:
        connection_id, refs = self._mapped_checking(harness)
        before = (self._feed_of("assets.everyday_checking"), _counts())
        agent = _agent(harness)
        body = self._onto_checking(refs["Rainy Day Savings"])

        for dry_run in (True, False):
            resp = agent.post("/connections/map-accounts", json={**body, "dry_run": dry_run})
            assert resp.status_code == 422, resp.text
            detail = resp.json()["detail"]
            assert (
                "assets.everyday_checking already takes the feed of Everyday Checking at "
                f"{CONN['name']}" in detail
            )
            assert "replace_feed" in detail and "ACT-CHK" not in detail
        asked = agent.post(
            "/connections/map-accounts",
            json=self._onto_checking(refs["Rainy Day Savings"], replace_feed=True),
        )
        assert asked.status_code == 422 and "needs admin scope" in asked.text
        assert (self._feed_of("assets.everyday_checking"), _counts()) == before
        assert self._feed_of("assets.everyday_checking") == ("ACT-CHK", uuid.UUID(connection_id))

    def test_an_admin_replaces_it_explicitly_and_the_audit_keeps_the_old_feed(
        self, harness: FeedHarness
    ) -> None:
        connection_id, refs = self._mapped_checking(harness)
        admin = _token(harness, ["read", "write", "admin"])
        harness.client.cookies.clear()
        body = self._onto_checking(refs["Rainy Day Savings"], replace_feed=True)

        dry = harness.client.post(
            "/connections/map-accounts", json={**body, "dry_run": True}, headers=admin
        )
        assert dry.status_code == 200, dry.text
        replaced = {
            "institution": CONN["name"],
            "bank_name": "Everyday Checking",
            "delivered": True,
        }
        (line,) = dry.json()["results"]
        assert (line["account"], line["new_account"], line["replaces"]) == (
            "assets.everyday_checking",
            False,
            replaced,
        )
        assert self._feed_of("assets.everyday_checking")[0] == "ACT-CHK"

        done = harness.client.post("/connections/map-accounts", json=body, headers=admin)
        assert done.status_code == 200, done.text
        assert done.json()["results"][0]["replaces"] == replaced
        assert self._feed_of("assets.everyday_checking") == ("ACT-SAV", uuid.UUID(connection_id))
        # The replaced bank account waits to be mapped again.
        harness.client.headers.update(admin)
        assert "Everyday Checking" in _by_bank_name(_plan(harness.client))
        rows = harness.client.get("/audit", params={"action": "account.map"}, headers=admin).json()[
            "items"
        ]
        newest = rows[0]["before_after"]
        assert newest["before"] == {"external_id": "ACT-CHK", "connection_id": connection_id}
        assert newest["after"] == {"external_id": "ACT-SAV", "connection_id": connection_id}

    def test_the_admin_form_records_the_feed_it_replaces(self, harness: FeedHarness) -> None:
        connection_id, _ = self._mapped_checking(harness)
        with db.get_sessionmaker()() as session:
            checking = session.execute(
                select(Account.id).where(Account.path == "assets.everyday_checking")
            ).scalar_one()
        resp = harness.client.post(
            f"/connections/{connection_id}/map",
            json={"external_id": "ACT-SAV", "account_id": str(checking)},
        )
        assert resp.status_code == 200, resp.text
        rows = harness.client.get("/audit", params={"action": "account.map"}).json()["items"]
        assert rows[0]["before_after"]["before"] == {
            "external_id": "ACT-CHK",
            "connection_id": connection_id,
        }
        # A first mapping records that there was none.
        assert rows[-1]["before_after"]["before"] == {"external_id": None, "connection_id": None}

    def test_replace_feed_needs_an_existing_account(self, harness: FeedHarness) -> None:
        _ready(harness)
        ref = _by_bank_name(_plan(harness.client))["Rainy Day Savings"]["ref"]
        resp = harness.client.post(
            "/connections/map-accounts", json={"accounts": [{"ref": ref, "replace_feed": True}]}
        )
        assert resp.status_code == 422 and "replace_feed applies only with account" in resp.text

    def test_over_mcp_a_write_token_is_refused_too(
        self, harness: FeedHarness, live_server: test_mcp.LiveServer
    ) -> None:
        _, refs = self._mapped_checking(harness)
        raw = harness.client.post(
            "/tokens", json={"name": "agent", "scopes": ["read", "write"]}
        ).json()["token"]
        [refused] = test_mcp._mcp_call(
            live_server.url,
            raw,
            [("map_feed_accounts", self._onto_checking(refs["Rainy Day Savings"]))],
        )
        assert "already takes the feed of Everyday Checking" in json.dumps(refused)
        assert self._feed_of("assets.everyday_checking")[0] == "ACT-CHK"
