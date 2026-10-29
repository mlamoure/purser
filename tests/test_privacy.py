# ruff: noqa: F811  (pytest fixtures re-exported from sibling modules)
"""Minimal disclosure to agents (issue #437): the canary test.

Seeds distinctive synthetic payees, descriptions, schedule names, rule text
and amounts; turns both privacy settings on; calls every tool that emits
something privatizable, with no `include`; and asserts none of the seeded
values appears in any tier — the text blocks, `structuredContent`, or an
error. Then each `include` field brings back exactly its own field.

Every amount is chosen so no aggregate an agent may legitimately see (a
category or payee total, a balance, a weekly bill total) equals a single
row's amount: each payee and category has at least two rows, and both
schedules are weekly.
"""

import asyncio
import json
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient

from tests.test_mcp import (  # noqa: F401 (fixture re-export)
    TODAY,
    LiveServer,
    live_server,
)

PAYEES = ["Zephyrine Bakery", "Quillon Hardware", "Marrowfield Grocer", "Orrery Utilities"]
SCHEDULE_PAYEES = ["Xylophone Streaming", "Vellichor Fitness"]
SCHEDULE_NAMES = ["Canary Subscription Xylo", "Canary Gym Plan"]
DESCRIPTIONS = [
    "CANARY-DESC-ALPHA",
    "CANARY-DESC-BRAVO",
    "CANARY-DESC-CHARLIE",
    "CANARY-DESC-DELTA",
    "CANARY-DESC-ECHO",
    "CANARY-DESC-FOXTROT",
    "CANARY-DESC-GOLF",
    "CANARY-DESC-HOTEL",
    "CANARY-DESC-ORRERY",
]
RULE_TEXT = "CANARYMATCHTEXT"
# (payee, category, amount, description, status)
ROWS = [
    ("Zephyrine Bakery", "expenses.dining", "-317.43", "CANARY-DESC-ALPHA", "posted"),
    ("Zephyrine Bakery", "expenses.dining", "-45.67", "CANARY-DESC-BRAVO", "posted"),
    ("Quillon Hardware", "expenses.groceries", "-288.19", "CANARY-DESC-CHARLIE", "posted"),
    ("Quillon Hardware", "expenses.groceries", "-61.29", "CANARY-DESC-DELTA", "pending"),
    ("Marrowfield Grocer", "expenses.groceries", "-133.71", "CANARY-DESC-ECHO", "posted"),
    ("Marrowfield Grocer", "expenses.dining", "-7.77", "CANARY-DESC-FOXTROT", "posted"),
    # No payee: the digest labels it by its description (review finding 2).
    (None, "expenses.dining", "-93.17", "CANARY-DESC-GOLF", "posted"),
    (None, "expenses.groceries", "-3.31", "CANARY-DESC-HOTEL", "posted"),
]
# On the card, so its payment schedule has a balance to estimate from.
CARD_ROWS = [("Quillon Hardware", "-84.16"), ("Marrowfield Grocer", "-12.04")]
LATE_BILL = ("Orrery Utilities", "expenses.groceries", "-52.83", "CANARY-DESC-ORRERY")
SCHEDULE_AMOUNTS = ["-19.37", "-23.41"]
RULE_AMOUNT = "-71.93"
SINGLE_AMOUNTS = (
    [r[2] for r in ROWS]
    + [a for _, a in CARD_ROWS]
    + [LATE_BILL[2], *SCHEDULE_AMOUNTS, RULE_AMOUNT]
)


def _next_20th() -> date:
    """The card payment schedule's next instance (BYMONTHDAY=20)."""
    this_month = TODAY.replace(day=20)
    if this_month >= TODAY:
        return this_month
    return (TODAY.replace(day=1) + timedelta(days=32)).replace(day=20)


def _forms(amount: str) -> list[str]:
    return [amount, amount.lstrip("-")]


def _call_all(
    url: str, token: str, calls: list[tuple[str, dict[str, Any]]]
) -> list[dict[str, Any]]:
    """Every call's full answer: all text blocks, structuredContent, error."""

    async def run() -> list[dict[str, Any]]:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        out = []
        async with (
            httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http,
            streamable_http_client(f"{url}/mcp", http_client=http) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            for tool, args in calls:
                result = await session.call_tool(tool, args)
                out.append(
                    {
                        "tool": tool,
                        "error": result.is_error,
                        "text": [getattr(b, "text", "") for b in result.content],
                        "structured": result.structured_content,
                    }
                )
        return out

    return asyncio.run(run())


def _dump(answer: dict[str, Any]) -> str:
    return json.dumps(answer["structured"]) + "\n".join(answer["text"])


@pytest.fixture
def seeded(admin_client: TestClient) -> dict[str, Any]:
    c = admin_client
    ok = {200, 201}
    for path, name, kind in (
        ("assets.checking", "Checking", "checking"),
        ("liabilities.card", "Card", "credit_card"),
    ):
        r = c.post("/accounts", json={"name": name, "type": kind, "path": path})
        assert r.status_code in ok, r.text
    cats = {}
    for path, name in (
        ("expenses.dining", "Dining"),
        ("expenses.groceries", "Groceries"),
    ):
        r = c.post("/categories", json={"name": name, "type": "category_expense", "path": path})
        assert r.status_code in ok, r.text
        cats[path] = r.json()["id"]
    # Opening balances, older than every canary row and never checked: the
    # balance after an account's first row would otherwise equal its amount,
    # and a balance is an aggregate the agent may see.
    for account, opening in (("assets.checking", "5000.00"), ("liabilities.card", "-100.00")):
        r = c.post(
            "/transactions",
            json={
                "account": account,
                "category": "expenses.dining",
                "amount": opening,
                "date": (TODAY - timedelta(days=60)).isoformat(),
                "status": "posted",
            },
        )
        assert r.status_code in ok, r.text
    tx_ids = []
    for i, (payee, category, amount, description, status) in enumerate(ROWS):
        r = c.post(
            "/transactions",
            json={
                "account": "assets.checking",
                "category": category,
                "amount": amount,
                "date": (TODAY - timedelta(days=3 + i)).isoformat(),
                **({"payee": payee} if payee else {}),
                "description": description,
                "status": status,
            },
        )
        assert r.status_code in ok, r.text
        tx_ids.append(r.json()["id"])
    for i, (payee, amount) in enumerate(CARD_ROWS):
        r = c.post(
            "/transactions",
            json={
                "account": "liabilities.card",
                "category": "expenses.groceries",
                "amount": amount,
                "date": (TODAY - timedelta(days=4 + i)).isoformat(),
                "payee": payee,
                "status": "posted",
            },
        )
        assert r.status_code in ok, r.text
    payee, category, amount, description = LATE_BILL
    r = c.post(
        "/transactions",
        json={
            "account": "assets.checking",
            "category": category,
            "amount": amount,
            "date": (TODAY - timedelta(days=12)).isoformat(),
            "payee": payee,
            "description": description,
            "status": "expected",
        },
    )
    assert r.status_code in ok, r.text
    late_bill = r.json()["id"]
    schedules = []
    for name, schedule_payee, schedule_amount in zip(
        SCHEDULE_NAMES, SCHEDULE_PAYEES, SCHEDULE_AMOUNTS, strict=True
    ):
        r = c.post(
            "/schedules",
            json={
                "name": name,
                "account": "assets.checking",
                "rrule": "FREQ=WEEKLY",
                "dtstart": (TODAY - timedelta(days=2)).isoformat(),
                "amount": schedule_amount,
                "payee": schedule_payee,
                "category": "expenses.dining",
            },
        )
        assert r.status_code in ok, r.text
        schedules.append(r.json()["id"])
    r = c.post(
        "/schedules",
        json={
            "name": "Card payment",
            "account": "assets.checking",
            "rrule": "FREQ=MONTHLY;BYMONTHDAY=20",
            "dtstart": TODAY.isoformat(),
            "amount_type": "estimated",
            "estimate_basis": "counterparty_balance",
            "counterparty_account": "liabilities.card",
        },
    )
    assert r.status_code in ok, r.text
    # A proposal: an inbox card whose headline names a payee.
    r = c.post(
        "/schedules",
        json={
            "name": "Proposed Orrery bill",
            "account": "assets.checking",
            "rrule": "FREQ=MONTHLY;BYMONTHDAY=9",
            "amount": "-52.83",
            "payee": "Orrery Utilities",
            "category": "expenses.groceries",
            "propose": True,
        },
    )
    assert r.status_code in ok, r.text
    suggestion = r.json()["suggestion_id"]
    r = c.post(
        "/rules",
        json={
            "type": "categorize",
            "category_id": cats["expenses.dining"],
            "match_field": "description",
            "match_mode": "contains",
            "match_text": RULE_TEXT,
            "amount_exact": RULE_AMOUNT,
            "activate": True,
        },
    )
    assert r.status_code in ok, r.text
    r = c.post(
        "/memories",
        json={
            "text": "Zephyrine Bakery is the office breakfast run.",
            "payees": ["Zephyrine Bakery"],
            "activate": True,
        },
    )
    assert r.status_code in ok, r.text
    instance = c.get(f"/schedules/{schedules[0]}").json()["instances"][0]["transaction_id"]
    r = c.put("/config", json={"privacy_purchase": True, "privacy_amount": True})
    assert r.status_code == 200, r.text
    agent = c.post("/tokens", json={"name": "agent", "scopes": ["read", "write"]}).json()
    return {
        "token": agent["token"],
        "tx_ids": tx_ids,
        "late_bill": late_bill,
        "schedules": schedules,
        "instance": instance,
        "suggestion": suggestion,
    }


def _read_calls(seeded: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    period = TODAY.strftime("%Y-%m")
    return [
        ("describe", {}),
        ("list_accounts", {}),
        ("search_transactions", {}),
        ("search_transactions", {"account": "assets.checking"}),
        ("get_transaction", {"transaction_id": seeded["tx_ids"][0]}),
        ("list_schedules", {}),
        ("list_unmatched", {}),
        ("list_unmatched", {"direction": "posted"}),
        ("spending_summary", {}),
        ("spending_summary", {"group_by": "payee"}),
        ("spending_summary", {"group_by": "payee", "period": period, "baseline": "avg_3mo"}),
        ("net_worth", {}),
        ("net_worth", {"period": "90d"}),
        ("balance_history", {"account": "assets.checking", "period": "30d"}),
        ("forecast_balance", {"account": "assets.checking", "days": 30}),
        ("list_recurring", {}),
        ("whats_new", {}),
        ("list_memories", {}),
        ("review_batch", {}),
        ("list_rules", {}),
        (
            "preview_rule",
            {"match_field": "description", "match_mode": "contains", "match_text": "CANARY-DESC"},
        ),
        ("list_suggestions", {}),
        ("detect_schedules", {}),
    ]


def _write_calls(seeded: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    return [
        ("override_instance", {"transaction_id": seeded["instance"], "amount": "-19.37"}),
        ("reset_instance", {"transaction_id": seeded["instance"]}),
        (
            "record_statement_balance",
            {
                "statement_balance": "412.18",
                "account": "liabilities.card",
                "due_date": _next_20th().isoformat(),
            },
        ),
        ("match", {"expected_id": seeded["late_bill"], "posted_id": seeded["tx_ids"][5]}),
        (
            "decide_suggestion",
            {
                "suggestion_id": seeded["suggestion"],
                "decision": "snooze",
                "snooze_until": (TODAY + timedelta(days=7)).isoformat(),
            },
        ),
        (
            "propose_rule",
            {
                "type": "categorize",
                "category": "expenses.dining",
                "match_field": "payee",
                "match_mode": "equals",
                "match_text": "Quillon Hardware",
            },
        ),
    ]


def _assert_clean(answers: list[dict[str, Any]]) -> None:
    leaks = []
    secrets = PAYEES + SCHEDULE_PAYEES + SCHEDULE_NAMES + DESCRIPTIONS + [RULE_TEXT]
    for answer in answers:
        dumped = _dump(answer)
        for secret in secrets:
            if secret in dumped:
                leaks.append((answer["tool"], secret))
        for amount in SINGLE_AMOUNTS:
            for form in _forms(amount):
                if form in dumped:
                    i = dumped.index(form)
                    leaks.append((answer["tool"], form, dumped[max(0, i - 120) : i + 20]))
    assert not leaks, leaks


class TestCanary:
    def test_no_tool_discloses_a_withheld_field(
        self, seeded: dict[str, Any], live_server: LiveServer
    ) -> None:
        from purser.mcp._registry import EMITS

        calls = _read_calls(seeded) + _write_calls(seeded)
        emitting = {name for name, emits in EMITS.items() if emits.fields()}
        # read_series needs URIs another call hands out: TestSeries covers it.
        covered = {tool for tool, _ in calls} | {"read_series"}
        assert emitting <= covered, emitting - covered
        answers = _call_all(live_server.url, seeded["token"], calls)
        failed = [(a["tool"], a["text"]) for a in answers if a["error"]]
        assert not failed, "\n".join(f"{tool}: {text}" for tool, text in failed)
        _assert_clean(answers)

        by_tool = {a["tool"]: a for a in answers}
        search = by_tool["search_transactions"]["structured"]
        assert search["withheld"] == ["amount", "payee"]
        assert all(i["payee"].startswith("py_") for i in search["items"] if i["payee"])
        assert all("amount" not in i for i in search["items"])

    def test_include_brings_back_exactly_its_field(
        self, seeded: dict[str, Any], live_server: LiveServer
    ) -> None:
        tx = seeded["tx_ids"][0]
        payee, description, amount = _call_all(
            live_server.url,
            seeded["token"],
            [
                ("get_transaction", {"transaction_id": tx, "include": ["payee"]}),
                ("get_transaction", {"transaction_id": tx, "include": ["description"]}),
                ("get_transaction", {"transaction_id": tx, "include": ["amount"]}),
            ],
        )
        p, d, a = payee["structured"], description["structured"], amount["structured"]
        assert p["payee"] == "Zephyrine Bakery" and "description" not in p
        assert all("amount" not in leg for leg in p["postings"])
        assert p["withheld"] == ["amount", "description"]
        assert d["description"] == "CANARY-DESC-ALPHA" and d["payee"].startswith("py_")
        assert d["withheld"] == ["amount", "payee"]
        assert {leg["amount"] for leg in a["postings"]} == {"-317.4300", "317.4300"}
        assert a["payee"].startswith("py_") and "description" not in a
        assert a["withheld"] == ["description", "payee"]

    def test_external_ids_are_withheld_by_default(
        self, seeded: dict[str, Any], live_server: LiveServer
    ) -> None:
        default, included = _call_all(
            live_server.url,
            seeded["token"],
            [("list_accounts", {}), ("list_accounts", {"include": ["external_ids"]})],
        )
        assert all("external_id" not in a for a in default["structured"]["result"])
        assert all("external_id" in a for a in included["structured"]["result"])


class TestSeries:
    def test_resources_and_read_series_withhold_like_the_tools(
        self, seeded: dict[str, Any], live_server: LiveServer
    ) -> None:
        """Review finding 1: the purser:// series carry every event label and
        amount, and the balance history every delta."""
        forecast, history = _call_all(
            live_server.url,
            seeded["token"],
            [
                ("forecast_balance", {"account": "assets.checking", "days": 30}),
                ("balance_history", {"account": "assets.checking", "period": "30d"}),
            ],
        )
        uris = [forecast["structured"]["daily_uri"], history["structured"]["series_uri"]]
        answers = _call_all(
            live_server.url, seeded["token"], [("read_series", {"uri": u}) for u in uris]
        )
        assert not [a for a in answers if a["error"]], answers
        _assert_clean(answers)
        body = json.loads(answers[1]["text"][0])
        assert body["withheld"] == ["amount"]
        assert all(set(p) == {"date", "balance"} for p in body["points"])

        (included,) = _call_all(
            live_server.url,
            seeded["token"],
            [("read_series", {"uri": uris[1], "include": ["amount"]})],
        )
        assert "delta" in json.loads(included["text"][0])["points"][0]


class TestOneSettingAndNone:
    def test_amount_only_withholds_rule_and_bill_amounts(
        self, seeded: dict[str, Any], live_server: LiveServer, admin_client: TestClient
    ) -> None:
        """Review finding 3: with only the amount setting on, amounts quoted
        in rule text and suggestion headlines are withheld too."""
        r = admin_client.put("/config", json={"privacy_purchase": False})
        assert r.status_code == 200, r.text
        rules, suggestions = _call_all(
            live_server.url,
            seeded["token"],
            [("list_rules", {}), ("list_suggestions", {})],
        )
        for answer in (rules, suggestions):
            dumped = _dump(answer)
            for amount in (RULE_AMOUNT, "-52.83"):
                for form in _forms(amount):
                    assert form not in dumped, (answer["tool"], form)
        assert "Orrery Utilities" in _dump(suggestions)  # payees are not withheld

    def test_aggregates_are_never_overwritten(
        self, seeded: dict[str, Any], live_server: LiveServer
    ) -> None:
        """Review finding 4: an aggregate equal to one withheld item stays a
        number."""
        forecast, recurring, news = _call_all(
            live_server.url,
            seeded["token"],
            [
                ("forecast_balance", {"account": "assets.checking", "days": 30}),
                ("list_recurring", {}),
                ("whats_new", {}),
            ],
        )
        f = forecast["structured"]
        for value in (f["end_balance"], f["start_balance"], f["low"]["balance"]):
            Decimal(value)
        Decimal(recurring["structured"]["monthly_total"])
        Decimal(news["structured"]["transactions"]["total"])
        Decimal(news["structured"]["upcoming"]["total"])

    def test_privacy_off_returns_everything_and_names_nothing(
        self, seeded: dict[str, Any], live_server: LiveServer, admin_client: TestClient
    ) -> None:
        r = admin_client.put("/config", json={"privacy_purchase": False, "privacy_amount": False})
        assert r.status_code == 200, r.text
        search, forecast, transaction = _call_all(
            live_server.url,
            seeded["token"],
            [
                ("search_transactions", {}),
                ("forecast_balance", {"account": "assets.checking", "days": 30}),
                ("get_transaction", {"transaction_id": seeded["tx_ids"][0]}),
            ],
        )
        assert "withheld" not in search["structured"]
        assert any(i["payee"] == "Zephyrine Bakery" for i in search["structured"]["items"])
        assert all("amount" in i for i in search["structured"]["items"])
        assert "withheld" not in forecast["structured"]
        assert all("amount" in e for e in forecast["structured"]["events"])
        assert transaction["structured"]["description"] == "CANARY-DESC-ALPHA"


class TestReferences:
    def test_a_reference_is_stable_and_works_as_a_filter(
        self, seeded: dict[str, Any], live_server: LiveServer
    ) -> None:
        (first,) = _call_all(
            live_server.url,
            seeded["token"],
            [("get_transaction", {"transaction_id": seeded["tx_ids"][0]})],
        )
        ref = first["structured"]["payee"]
        (search,) = _call_all(
            live_server.url,
            seeded["token"],
            [("search_transactions", {"payee": ref})],
        )
        items = search["structured"]["items"]
        assert {i["id"] for i in items} == set(seeded["tx_ids"][:2])
        assert {i["payee"] for i in items} == {ref}

    def test_a_reference_in_free_text_is_refused(
        self, seeded: dict[str, Any], live_server: LiveServer
    ) -> None:
        (first,) = _call_all(
            live_server.url,
            seeded["token"],
            [("get_transaction", {"transaction_id": seeded["tx_ids"][0]})],
        )
        ref = first["structured"]["payee"]
        refused, unknown = _call_all(
            live_server.url,
            seeded["token"],
            [
                (
                    "create_transaction",
                    {
                        "account": "assets.checking",
                        "category": "expenses.dining",
                        "amount": "-1.00",
                        "date_": TODAY.isoformat(),
                        "description": f"lunch at {ref}",
                    },
                ),
                (
                    "create_transaction",
                    {
                        "account": "assets.checking",
                        "category": "expenses.dining",
                        "amount": "-1.00",
                        "date_": TODAY.isoformat(),
                        "payee": "py_aaaaaaaaaa",
                    },
                ),
            ],
        )
        assert refused["error"] and "cannot contain a payee reference" in refused["text"][0]
        assert unknown["error"] and "unknown payee reference" in unknown["text"][0]

    def test_a_reference_names_the_payee_on_a_write(
        self, seeded: dict[str, Any], live_server: LiveServer, admin_client: TestClient
    ) -> None:
        (first,) = _call_all(
            live_server.url,
            seeded["token"],
            [("get_transaction", {"transaction_id": seeded["tx_ids"][0]})],
        )
        ref = first["structured"]["payee"]
        (created,) = _call_all(
            live_server.url,
            seeded["token"],
            [
                (
                    "create_transaction",
                    {
                        "account": "assets.checking",
                        "category": "expenses.dining",
                        "amount": "-2.00",
                        "date_": TODAY.isoformat(),
                        "payee": ref,
                    },
                )
            ],
        )
        detail = admin_client.get(f"/transactions/{created['structured']['id']}").json()
        assert detail["payee"] == "Zephyrine Bakery"

    def test_references_survive_key_rotation(self, seeded: dict[str, Any]) -> None:
        """A reference is HMAC over the payee id under a wrapped DEK, so
        rotating the primary key re-wraps the DEK and leaves it unchanged."""
        import uuid

        from purser import crypto

        payee_id = uuid.uuid4()
        before = crypto.payee_ref(payee_id)
        crypto.reset_dek_cache()
        assert crypto.payee_ref(payee_id) == before
        assert before.startswith("py_") and len(before) == 13


class TestErrors:
    def test_a_refusal_is_scrubbed_of_payee_and_schedule_names(
        self, seeded: dict[str, Any], live_server: LiveServer
    ) -> None:
        (refused,) = _call_all(
            live_server.url,
            seeded["token"],
            [
                (
                    "create_schedule",
                    {
                        "name": "Again",
                        "account": "assets.checking",
                        "rrule": "FREQ=WEEKLY",
                        "amount": "-1.00",
                        "payee": "Xylophone Streaming",
                        "category": "expenses.dining",
                        "propose": True,
                    },
                )
            ],
        )
        assert refused["error"]
        message = refused["text"][0]
        assert "already has a schedule" in message
        assert "Xylophone Streaming" not in message
        assert "Canary Subscription Xylo" not in message


class TestPresent:
    def test_present_links_the_rows_for_a_signed_in_user(
        self, seeded: dict[str, Any], live_server: LiveServer, admin_client: TestClient
    ) -> None:
        ids = seeded["tx_ids"][:2]
        (presented,) = _call_all(
            live_server.url,
            seeded["token"],
            [("present", {"transaction_ids": ids, "note": "two largest"})],
        )
        url = presented["structured"]["url"]
        assert presented["structured"]["count"] == 2
        assert "/selection?" in url and "note=two+largest" in url
        query = url.split("?", 1)[1]
        rows = admin_client.get(f"/transactions/selection?{query}")
        assert rows.status_code == 200, rows.text
        assert [r["id"] for r in rows.json()] == ids
        assert rows.json()[0]["payee"] == "Zephyrine Bakery"
        agent = admin_client.get(
            f"/transactions/selection?{query}",
            headers={"Authorization": f"Bearer {seeded['token']}"},
        )
        assert agent.status_code == 403

    def test_present_refuses_an_unknown_id(
        self, seeded: dict[str, Any], live_server: LiveServer
    ) -> None:
        (refused,) = _call_all(
            live_server.url,
            seeded["token"],
            [("present", {"transaction_ids": ["00000000-0000-0000-0000-000000000000"]})],
        )
        assert refused["error"] and "unknown transaction id" in refused["text"][0]


class TestAudit:
    def test_a_call_that_includes_beyond_the_defaults_is_marked_disclosed(
        self, seeded: dict[str, Any], live_server: LiveServer, admin_client: TestClient
    ) -> None:
        _call_all(
            live_server.url,
            seeded["token"],
            [
                ("search_transactions", {"include": ["payee"]}),
                ("search_transactions", {}),
            ],
        )
        rows = admin_client.get("/audit", params={"disclosed": "true", "limit": 50}).json()
        items = rows["items"] if isinstance(rows, dict) else rows
        disclosed = [
            r for r in items if (r.get("detail") or {}).get("tool") == "search_transactions"
        ]
        assert len(disclosed) == 1
        assert disclosed[0]["disclosed"] == ["payee"]


class TestScrubberUnits:
    """The value scrub on its own (review findings 7 and 8)."""

    def _scrubber(self, withheld: set[str]) -> Any:
        from purser.mcp import privacy

        dictionary = privacy.Dictionary(
            payees={"BP": "py_aaaaaaaaaa", "Zephyrine Bakery": "py_bbbbbbbbbb"},
            refs={"py_aaaaaaaaaa": "BP", "py_bbbbbbbbbb": "Zephyrine Bakery"},
            narratives={"Canary Gym Plan"},
            item_amounts={"-23.4100"},
        )
        emits = privacy.Emits(payee=True, narrative=True)
        return privacy.Scrubber(withheld=withheld, emits=emits, dictionary=dictionary)

    def test_short_and_differently_cased_names_are_replaced(self) -> None:
        s = self._scrubber({"payee", "description"})
        assert s.text("biggest move BP -45.00") == "biggest move py_aaaaaaaaaa -45.00"
        assert s.text("mostly zephyrine bakery (3 vs 1)") == "mostly py_bbbbbbbbbb (3 vs 1)"
        assert s.text("canary gym plan: weekly") == "[withheld]: weekly"
        assert s.text("BPX and 1BP stay") == "BPX and 1BP stay"

    def test_numbers_are_never_value_scrubbed(self) -> None:
        s = self._scrubber({"amount"})
        assert s.text("-23.41") == "-23.41"  # an aggregate that equals a bill
        assert s.text("Gym: weekly ~-23.41 on assets.checking") == (
            "Gym: weekly ~[withheld] on assets.checking"
        )

    def test_nested_references_resolve_and_free_text_refuses(self) -> None:
        import pytest

        from purser.mcp import privacy

        dictionary = privacy.Dictionary(refs={"py_aaaaaaaaaa": "BP"})
        out = privacy.resolve_inputs(
            {"edits": {"payee": "py_aaaaaaaaaa"}, "match_text": "py_aaaaaaaaaa"}, dictionary
        )
        assert out == {"edits": {"payee": "BP"}, "match_text": "BP"}
        with pytest.raises(privacy.DisclosureError, match="cannot contain"):
            privacy.resolve_inputs({"edits": {"name": "fuel at py_aaaaaaaaaa"}}, dictionary)
        with pytest.raises(privacy.DisclosureError, match="on its own"):
            privacy.resolve_inputs({"payee": "near py_aaaaaaaaaa"}, dictionary)

    def test_errors_lose_names_and_amounts(self) -> None:
        s = self._scrubber({"payee", "description", "amount"})
        out = s.error("leg BP is -45.00 and Canary Gym Plan is due")
        assert "BP" not in out and "-45.00" not in out and "Canary Gym Plan" not in out


class TestIdentifiersSurvive:
    """Second review, finding 1: a payee or bill named like an account path
    segment must not rewrite the path, or the agent's next call breaks."""

    def test_paths_uris_and_ids_are_never_value_scrubbed(self) -> None:
        from purser.mcp import privacy

        dictionary = privacy.Dictionary(
            payees={"Mortgage": "py_cccccccccc", "76": "py_dddddddddd"},
            refs={"py_cccccccccc": "Mortgage", "py_dddddddddd": "76"},
            narratives={"Rent", "Interest"},
        )
        s = privacy.Scrubber(
            withheld={"payee", "description"},
            emits=privacy.Emits(payee=True, narrative=True),
            dictionary=dictionary,
        )
        for kept in (
            "assets.mortgage",
            "expenses.housing.rent",
            "income.interest",
            "purser://forecast/assets.mortgage/14d",
            "[liabilities.mortgage]",
            "0b5c4c86-5b1e-4f7a-9a43-2f1d8f1c2a10",
        ):
            assert s.text(kept) == kept
        assert s.text("paid Mortgage.") == "paid py_cccccccccc."
        assert (
            s.text("Rent: monthly on assets.checking") == "[withheld]: monthly on assets.checking"
        )
        assert s.text("76") == "py_dddddddddd"  # a number-like payee is still a payee

    def test_an_account_named_like_a_payee_keeps_its_path_end_to_end(
        self, seeded: dict[str, Any], live_server: LiveServer, admin_client: TestClient
    ) -> None:
        r = admin_client.post(
            "/transactions",
            json={
                "account": "assets.checking",
                "category": "expenses.dining",
                "amount": "-4.44",
                "date": TODAY.isoformat(),
                "payee": "Checking",
                "status": "posted",
            },
        )
        assert r.status_code == 201, r.text
        (forecast,) = _call_all(
            live_server.url,
            seeded["token"],
            [("forecast_balance", {"account": "assets.checking", "days": 30})],
        )
        f = forecast["structured"]
        assert f["account"] == "assets.checking"
        (series,) = _call_all(
            live_server.url, seeded["token"], [("read_series", {"uri": f["daily_uri"]})]
        )
        assert not series["error"], series["text"]
        assert json.loads(series["text"][0])["account"] == "assets.checking"


class TestSeriesDisclosure:
    def test_read_series_with_include_is_recorded(
        self, seeded: dict[str, Any], live_server: LiveServer, admin_client: TestClient
    ) -> None:
        """Second review, finding 3."""
        (forecast,) = _call_all(
            live_server.url,
            seeded["token"],
            [("forecast_balance", {"account": "assets.checking", "days": 30})],
        )
        _call_all(
            live_server.url,
            seeded["token"],
            [
                (
                    "read_series",
                    {"uri": forecast["structured"]["daily_uri"], "include": ["amount"]},
                )
            ],
        )
        rows = admin_client.get("/audit", params={"disclosed": "true", "limit": 50}).json()
        tools = [(r.get("detail") or {}).get("tool") for r in rows["items"]]
        assert "read_series" in tools
