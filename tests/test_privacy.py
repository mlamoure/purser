# ruff: noqa: F811  (pytest fixtures re-exported from sibling modules)
"""Agent-requested privacy on MCP (issues #437, #613): the canary test.

Seeds distinctive synthetic payees, descriptions, schedule names, rule text
and amounts; calls every tool that emits something withholdable with
``withhold=[payee, description, amount]``; and asserts none of the seeded
values appears in any tier — the text blocks, `structuredContent`, or an
error. Then each ``withhold`` field leaves out exactly its own field, and a
call that withholds nothing gets everything. The REST side is
tests/test_privacy_rest.py.

Every amount is chosen so no aggregate an agent may legitimately see (a
category or payee total, a balance, a weekly bill total) equals a single
row's amount: each payee and category has at least two rows, and both
schedules are weekly.
"""

import asyncio
import json
import re
import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.conftest import today
from tests.test_mcp import (  # noqa: F401 (fixture re-export)
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
    "CANARY-DESC-INDIA",
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
# The broker's own copy of a sale's cash on a hand-tracked account: a
# record_sale dry run finds it (already_recorded) and would post the same
# amount, a single transaction's amount either way.
SALE_ROW = ("418.27", "CANARY-DESC-INDIA")
# The cash in lieu a record_split pays for a fractional share (#624): one
# transaction's amount.
SPLIT_CASH = "13.57"
SINGLE_AMOUNTS = (
    [r[2] for r in ROWS]
    + [a for _, a in CARD_ROWS]
    + [LATE_BILL[2], *SCHEDULE_AMOUNTS, RULE_AMOUNT, SALE_ROW[0], SPLIT_CASH]
)


ALL = ["payee", "description", "amount"]


def _private(
    calls: list[tuple[str, dict[str, Any]]], fields: list[str] = ALL
) -> list[tuple[str, dict[str, Any]]]:
    """The calls with ``withhold`` added wherever the tool takes it."""
    import purser.mcp.server  # noqa: F401  (registers every tool)
    from purser.mcp._registry import EMITS

    return [
        (tool, {**args, "withhold": list(fields)}) if EMITS[tool].withholdable() else (tool, args)
        for tool, args in calls
    ]


def _next_20th() -> date:
    """The card payment schedule's next instance (BYMONTHDAY=20)."""
    this_month = today().replace(day=20)
    if this_month >= today():
        return this_month
    return (today().replace(day=1) + timedelta(days=32)).replace(day=20)


def _forms(amount: str) -> list[str]:
    # An estimate in a headline spells a negative amount with a real minus
    # sign, U+2212 (#556).
    return [amount, "\u2212" + amount.lstrip("-"), amount.lstrip("-")]


def _amount_at(text: str, amount: str) -> int:
    """Where ``text`` states ``amount`` as a number of its own (signed or
    not, any trailing zeros: ``-3.3100``), or -1. Never inside a longer
    number: a timestamp's ``13.317000`` seconds is not ``3.31``, which a
    plain substring test reported as a leak whenever the clock lined up."""
    for form in _forms(amount):
        m = re.search(r"(?<![\d.])" + re.escape(form) + r"0*(?!\d)", text)
        if m:
            return m.start()
    return -1


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
    # Unescaped: "\\u2212" would hide "52.83" behind a digit from _amount_at.
    return json.dumps(answer["structured"], ensure_ascii=False) + "\n".join(answer["text"])


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
                "date": (today() - timedelta(days=60)).isoformat(),
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
                # Never before the 1st: aggregates are exact by design, and a
                # group the month boundary splits to one row would report
                # that row's amount as its total (the canary failed on the
                # 10th, when the last row fell into the previous month).
                "date": max(today() - timedelta(days=3 + i), today().replace(day=1)).isoformat(),
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
                # In this month too: they share payee groups with ROWS.
                "date": max(today() - timedelta(days=4 + i), today().replace(day=1)).isoformat(),
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
            "date": (today() - timedelta(days=12)).isoformat(),
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
                "dtstart": (today() - timedelta(days=2)).isoformat(),
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
            "dtstart": today().isoformat(),
            "amount_type": "estimated",
            "estimate_basis": "counterparty_balance",
            "counterparty_account": "liabilities.card",
        },
    )
    assert r.status_code in ok, r.text
    # A proposal: an inbox card whose headline names a payee and an
    # estimated amount (#556).
    r = c.post(
        "/schedules",
        json={
            "name": "Proposed Orrery bill",
            "account": "assets.checking",
            "rrule": "FREQ=MONTHLY;BYMONTHDAY=9",
            "amount_type": "estimated",
            "estimate_basis": "last",
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
    # A hand-tracked brokerage account holding 10 CNRY, whose feed already
    # shows a sale's cash (decision 103): record_sale's answer names it.
    r = c.post("/accounts", json={"name": "Broker", "type": "brokerage", "path": "assets.broker"})
    assert r.status_code in ok, r.text
    broker = r.json()["id"]
    r = c.patch(f"/accounts/{broker}", json={"holdings_source": "manual"})
    assert r.status_code == 200, r.text
    r = c.post(
        f"/accounts/{broker}/positions",
        json={
            "symbol": "CNRY",
            "quantity": "10",
            "as_of": (today() - timedelta(days=30)).isoformat(),
        },
    )
    assert r.status_code == 201, r.text
    # And 7 CNRS, which the canary's record_split halves (cash in lieu).
    r = c.post(
        f"/accounts/{broker}/positions",
        json={
            "symbol": "CNRS",
            "quantity": "7",
            "as_of": (today() - timedelta(days=30)).isoformat(),
        },
    )
    assert r.status_code == 201, r.text
    # Income, so no spending total is the row's own amount.
    r = c.post(
        "/categories",
        json={"name": "Proceeds", "type": "category_income", "path": "income.proceeds"},
    )
    assert r.status_code in ok, r.text
    for amount, on, description in (
        ("2500.00", today() - timedelta(days=60), None),  # never a row's own balance
        (SALE_ROW[0], today() - timedelta(days=1), SALE_ROW[1]),
    ):
        r = c.post(
            "/transactions",
            json={
                "account": "assets.broker",
                "category": "income.proceeds",
                "amount": amount,
                "date": on.isoformat(),
                **({"description": description} if description else {}),
                "status": "posted",
            },
        )
        assert r.status_code in ok, r.text
    # A hand-valued asset (#566): its purchase and one later change, which
    # the canary's reverse_value_change undoes.
    r = c.post("/accounts", json={"name": "House", "type": "property", "path": "assets.house"})
    assert r.status_code in ok, r.text
    house = r.json()["id"]
    value_changes = []
    for amount, days in (("350000.00", 90), ("7000.00", 10)):
        r = c.post(
            f"/accounts/{house}/value-changes",
            json={"as_of": (today() - timedelta(days=days)).isoformat(), "amount": amount},
        )
        assert r.status_code in ok, r.text
        value_changes.append(r.json()["transaction_id"])
    agent = c.post("/tokens", json={"name": "agent", "scopes": ["read", "write"]}).json()
    return {
        "token": agent["token"],
        "value_change": value_changes[-1],
        "tx_ids": tx_ids,
        "late_bill": late_bill,
        "schedules": schedules,
        "instance": instance,
        "suggestion": suggestion,
    }


def _read_calls(seeded: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    period = today().strftime("%Y-%m")
    return [
        ("describe", {}),
        ("list_accounts", {}),
        ("search_transactions", {}),
        ("search_transactions", {"account": "assets.checking"}),
        ("get_transaction", {"transaction_id": seeded["tx_ids"][0]}),
        ("list_schedules", {}),
        ("get_schedule_occurrence", {"transaction_id": seeded["instance"]}),
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
        # Lots and awards (#393): refs are external ids, plan labels narrative.
        ("list_lots", {"account": "assets.broker", "symbol": "CNRY", "status": "all"}),
        ("list_awards", {"account": "assets.broker"}),
    ]


def _write_calls(seeded: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    return [
        (
            "record_value_change",
            {
                "account": "assets.house",
                "as_of": today().isoformat(),
                "amount": "2500.00",
                "note": f"{DESCRIPTIONS[0]} {PAYEES[0]}",
            },
        ),
        ("reverse_value_change", {"transaction_id": seeded["value_change"]}),
        (
            "dispose_asset",
            {"account": "assets.house", "as_of": today().isoformat(), "dry_run": True},
        ),
        (
            "update_occurrence_certainty",
            {
                "transaction_id": seeded["instance"],
                "amount": {
                    "state": "confirmed",
                    "source": "invoice",
                    "as_of": today().isoformat(),
                    "note": f"{DESCRIPTIONS[0]} {PAYEES[0]} {SCHEDULE_AMOUNTS[0]}",
                },
            },
        ),
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
        # A manual pending row splits in place (its id stays), then un-splits
        # (#590): the result's amounts, line amounts and memos are withheld.
        (
            "split_transaction",
            {
                "transaction_id": seeded["tx_ids"][3],
                "lines": [
                    {"category": "expenses.dining", "amount": "-60.00", "memo": DESCRIPTIONS[0]},
                    {"category": "expenses.groceries", "amount": "-1.29"},
                ],
            },
        ),
        (
            "unsplit_transaction",
            {"transaction_id": seeded["tx_ids"][3], "category": "expenses.groceries"},
        ),
        (
            "decide_suggestion",
            {
                "suggestion_id": seeded["suggestion"],
                "decision": "snooze",
                "snooze_until": (today() + timedelta(days=7)).isoformat(),
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
        (
            "record_sale",
            {
                "account": "assets.broker",
                "symbol": "CNRY",
                "quantity": "1",
                "price": SALE_ROW[0],
                "trade_date": (today() - timedelta(days=2)).isoformat(),
                "dry_run": True,
            },
        ),
        (
            "record_split",
            {
                "account": "assets.broker",
                "symbol": "CNRS",
                "ratio": "1:2",
                "effective_date": (today() - timedelta(days=10)).isoformat(),
                "cash_in_lieu": SPLIT_CASH,
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
            i = _amount_at(dumped, amount)
            if i >= 0:
                leaks.append((answer["tool"], amount, dumped[max(0, i - 120) : i + 20]))
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
        answers = _call_all(live_server.url, seeded["token"], _private(calls))
        failed = [(a["tool"], a["text"]) for a in answers if a["error"]]
        assert not failed, "\n".join(f"{tool}: {text}" for tool, text in failed)
        _assert_clean(answers)

        by_tool = {a["tool"]: a for a in answers}
        search = by_tool["search_transactions"]["structured"]
        assert search["withheld"] == ["amount", "payee"]
        assert all(i["payee"].startswith("py_") for i in search["items"] if i["payee"])
        assert all("amount" not in i for i in search["items"])

    def test_withhold_leaves_out_exactly_its_field(
        self, seeded: dict[str, Any], live_server: LiveServer
    ) -> None:
        tx = seeded["tx_ids"][0]
        payee, description, amount = _call_all(
            live_server.url,
            seeded["token"],
            [
                ("get_transaction", {"transaction_id": tx, "withhold": ["payee"]}),
                ("get_transaction", {"transaction_id": tx, "withhold": ["description"]}),
                ("get_transaction", {"transaction_id": tx, "withhold": ["amount"]}),
            ],
        )
        p, d, a = payee["structured"], description["structured"], amount["structured"]
        assert p["payee"].startswith("py_") and p["description"] == "CANARY-DESC-ALPHA"
        assert {leg["amount"] for leg in p["postings"]} == {"-317.4300", "317.4300"}
        assert p["withheld"] == ["payee"]
        assert d["payee"] == "Zephyrine Bakery" and "description" not in d
        assert d["withheld"] == ["description"]
        assert all("amount" not in leg for leg in a["postings"])
        assert a["payee"] == "Zephyrine Bakery" and a["description"] == "CANARY-DESC-ALPHA"
        assert a["withheld"] == ["amount"]

    def test_withhold_and_include_refuse_what_they_do_not_take(
        self, seeded: dict[str, Any], live_server: LiveServer
    ) -> None:
        """include adds back external identifiers only: payees, descriptions
        and amounts are returned unless withheld, and the error says so."""
        tx = seeded["tx_ids"][0]
        old, unknown = _call_all(
            live_server.url,
            seeded["token"],
            [
                ("list_accounts", {"include": ["payee"]}),
                ("get_transaction", {"transaction_id": tx, "withhold": ["memo"]}),
            ],
        )
        assert old["error"] and "returned unless the call passes withhold" in old["text"][0]
        assert unknown["error"] and "unknown withhold field" in unknown["text"][0]

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
            live_server.url,
            seeded["token"],
            [("read_series", {"uri": u, "withhold": ALL}) for u in uris],
        )
        assert not [a for a in answers if a["error"]], answers
        _assert_clean(answers)
        body = json.loads(answers[1]["text"][0])
        assert body["withheld"] == ["amount"]
        assert all(set(p) == {"date", "balance"} for p in body["points"])

        (full,) = _call_all(
            live_server.url,
            seeded["token"],
            [("read_series", {"uri": uris[1]})],
        )
        assert "delta" in json.loads(full["text"][0])["points"][0]


class TestPartialAndNone:
    def test_amount_only_withholds_rule_and_bill_amounts(
        self, seeded: dict[str, Any], live_server: LiveServer
    ) -> None:
        """Review finding 3: withholding only amounts withholds the amounts
        quoted in rule text and suggestion headlines too."""
        rules, suggestions = _call_all(
            live_server.url,
            seeded["token"],
            [
                ("list_rules", {"withhold": ["amount"]}),
                ("list_suggestions", {"withhold": ["amount"]}),
            ],
        )
        for answer in (rules, suggestions):
            dumped = _dump(answer)
            for amount in (RULE_AMOUNT, "-52.83"):
                assert _amount_at(dumped, amount) < 0, (answer["tool"], amount)
        assert "Orrery Utilities" in _dump(suggestions)  # payees are not withheld

    def test_headlines_name_accounts_and_still_lose_the_withheld_values(
        self, seeded: dict[str, Any], live_server: LiveServer
    ) -> None:
        """#552: a suggestion headline names its account ("on Checking"), and
        a name is not an identifier the scrubber leaves whole, so the
        headline is scrubbed like any text: the payee becomes its reference
        and the bill's name and amount go, in the inbox and the digest."""
        (plain,) = _call_all(live_server.url, seeded["token"], [("list_suggestions", {})])
        (shown,) = plain["structured"]["items"]
        # The estimate's own spelling (#556): U+2248, a space, U+2212.
        assert "monthly \u2248 \u221252.83 on Checking" in shown["headline"], shown["headline"]
        inbox, news = _call_all(
            live_server.url,
            seeded["token"],
            _private([("list_suggestions", {}), ("whats_new", {})]),
        )
        (card,) = inbox["structured"]["items"]
        (top,) = news["structured"]["suggestions"]["top"]
        for headline in (card["headline"], top["headline"]):
            assert " on Checking with py_" in headline, headline
            assert "assets.checking" not in headline
            assert "monthly \u2248 [withheld] on" in headline, headline
            for secret in ("Orrery", "Proposed", "52.83", "\u2212"):
                assert secret not in headline, (secret, headline)
        assert card["draft"]["account"] == "assets.checking"  # the path stays for calls

    def test_aggregates_are_never_overwritten(
        self, seeded: dict[str, Any], live_server: LiveServer
    ) -> None:
        """Review finding 4: an aggregate equal to one withheld item stays a
        number."""
        forecast, recurring, news = _call_all(
            live_server.url,
            seeded["token"],
            _private(
                [
                    ("forecast_balance", {"account": "assets.checking", "days": 30}),
                    ("list_recurring", {}),
                    ("whats_new", {}),
                ]
            ),
        )
        f = forecast["structured"]
        for value in (f["end_balance"], f["start_balance"], f["low"]["balance"]):
            Decimal(value)
        Decimal(recurring["structured"]["monthly_total"])
        Decimal(news["structured"]["transactions"]["total"])
        Decimal(news["structured"]["upcoming"]["total"])

    def test_no_withhold_returns_everything_and_names_nothing(
        self, seeded: dict[str, Any], live_server: LiveServer
    ) -> None:
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
            [("get_transaction", {"transaction_id": seeded["tx_ids"][0], "withhold": ["payee"]})],
        )
        ref = first["structured"]["payee"]
        (search,) = _call_all(
            live_server.url,
            seeded["token"],
            [("search_transactions", {"payee": ref, "withhold": ["payee"]})],
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
            [("get_transaction", {"transaction_id": seeded["tx_ids"][0], "withhold": ["payee"]})],
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
                        "date_": today().isoformat(),
                        "description": f"lunch at {ref}",
                    },
                ),
                (
                    "create_transaction",
                    {
                        "account": "assets.checking",
                        "category": "expenses.dining",
                        "amount": "-1.00",
                        "date_": today().isoformat(),
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
            [("get_transaction", {"transaction_id": seeded["tx_ids"][0], "withhold": ["payee"]})],
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
                        "date_": today().isoformat(),
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
    def test_a_refusal_is_scrubbed_when_the_call_withholds(
        self, seeded: dict[str, Any], live_server: LiveServer
    ) -> None:
        """An error from a call that withholds amounts loses the single amount
        it would quote; one that withholds nothing keeps it."""
        args = {
            "transaction_id": seeded["tx_ids"][0],
            "lines": [
                {"category": "expenses.dining", "amount": "-100.00"},
                {"category": "expenses.groceries", "amount": "-100.00"},
            ],
            "dry_run": True,
        }
        private, plain = _call_all(
            live_server.url,
            seeded["token"],
            [
                ("split_transaction", {**args, "withhold": ["amount"]}),
                ("split_transaction", args),
            ],
        )
        assert private["error"] and plain["error"], (private, plain)
        assert "317.43" in plain["text"][0]
        assert "317.43" not in private["text"][0]


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
    def test_a_call_that_includes_external_ids_records_the_request(
        self, seeded: dict[str, Any], live_server: LiveServer, admin_client: TestClient
    ) -> None:
        """include is recorded as requested; ``disclosed`` (and its filter)
        needs the identifiers to have been returned, and these accounts have
        none, so a request alone proves nothing."""
        _call_all(
            live_server.url,
            seeded["token"],
            [("list_accounts", {"include": ["external_ids"]}), ("list_accounts", {})],
        )
        rows = admin_client.get("/audit", params={"limit": 100}).json()["items"]
        calls = [r for r in rows if (r.get("detail") or {}).get("tool") == "list_accounts"]
        requested = [r for r in calls if r["detail"]["requested_extra"]]
        assert len(requested) == 1
        assert requested[0]["detail"]["requested_extra"] == ["external_ids"]
        assert requested[0]["disclosed"] is None

    def test_the_row_says_whether_privacy_was_used_or_the_answer_was_identifiable(
        self, seeded: dict[str, Any], live_server: LiveServer, admin_client: TestClient
    ) -> None:
        """#613: every call row records what it asked to withhold, what the
        answer withheld, and which identifiable fields it returned."""
        _call_all(
            live_server.url,
            seeded["token"],
            [("search_transactions", {"withhold": ALL}), ("search_transactions", {})],
        )

        def search_rows(**params: str) -> list[dict[str, Any]]:
            rows = admin_client.get("/audit", params={"limit": 100, **params}).json()
            return [
                r
                for r in rows["items"]
                if (r.get("detail") or {}).get("tool") == "search_transactions"
            ]

        (private,) = search_rows(privacy_requested="true")
        assert private["privacy_requested"] == sorted(ALL)
        assert private["identifiable"] == []
        assert set(private["detail"]["privacy"]["withheld"]) >= {"amount", "payee"}
        assert "payee" in str(private["detail"]["args"]["withhold"])

        (plain,) = search_rows(identifiable="true")
        assert plain["privacy_requested"] == []
        assert {"amount", "payee"} <= set(plain["identifiable"])

        activity = admin_client.get(
            "/audit/activity", params={"view": "sharing", "privacy": "requested"}
        ).json()["items"]
        assert any(
            e["action"] == "mcp.search_transactions" and set(e["privacy"]["requested"]) == set(ALL)
            for e in activity
        )
        identifiable = admin_client.get(
            "/audit/activity", params={"view": "sharing", "privacy": "identifiable"}
        ).json()["items"]
        assert all(e["privacy"]["identifiable"] for e in identifiable)
        assert any(e["action"] == "mcp.search_transactions" for e in identifiable)


class TestScrubberUnits:
    """The value scrub on its own (review findings 7 and 8)."""

    def _scrubber(self, withheld: set[str]) -> Any:
        from purser import privacy

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
        assert s.text("Gym: weekly -23.41 on assets.checking") == (
            "Gym: weekly [withheld] on assets.checking"
        )

    def test_an_estimate_loses_its_amount_whole(self) -> None:
        """#556: an estimate reads "≈" and the amount with a real minus sign
        (U+2212); the whole amount goes, the sign with it, so the direction
        does not leak."""
        from decimal import Decimal

        from purser import privacy, suggestions

        s = self._scrubber({"amount"})
        estimate = suggestions.estimate_text(Decimal("-23.4100"))
        assert estimate == "\u2248 \u221223.41"
        assert s.text(f"Gym: weekly {estimate} on assets.checking") == (
            "Gym: weekly \u2248 [withheld] on assets.checking"
        )
        assert s.text("Gym: weekly \u2248 \u221223.4100 on assets.checking") == (
            "Gym: weekly \u2248 [withheld] on assets.checking"
        )
        assert s.text("\u221223.41") == "[withheld]"  # not a plain number: scrubbed
        assert s.error(f"Gym is {estimate} weekly") == "Gym is \u2248 [withheld] weekly"
        assert s.error("due \u22121,234.56 today") == "due [withheld] today"
        forms = privacy.amount_forms("-1234.5")
        assert {"\u22121234.50", "\u22121,234.50", "-1,234.50", "1,234.50"} <= forms
        assert not any(f.startswith("\u2212") for f in privacy.amount_forms("1234.5"))
        assert privacy.amount_forms("-0.00") == set()

    def test_nested_references_resolve_and_free_text_refuses(self) -> None:
        import pytest

        from purser import privacy

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
        from purser import privacy

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
                "date": today().isoformat(),
                "payee": "Checking",
                "status": "posted",
            },
        )
        assert r.status_code == 201, r.text
        (forecast,) = _call_all(
            live_server.url,
            seeded["token"],
            [("forecast_balance", {"account": "assets.checking", "days": 30, "withhold": ALL})],
        )
        f = forecast["structured"]
        assert f["account"] == "assets.checking"
        (series,) = _call_all(
            live_server.url,
            seeded["token"],
            [("read_series", {"uri": f["daily_uri"], "withhold": ALL})],
        )
        assert not series["error"], series["text"]
        assert json.loads(series["text"][0])["account"] == "assets.checking"


class TestSeriesDisclosure:
    def test_read_series_with_withhold_is_recorded(
        self, seeded: dict[str, Any], live_server: LiveServer, admin_client: TestClient
    ) -> None:
        """Second review, finding 3: the series read records the call's choice."""
        (forecast,) = _call_all(
            live_server.url,
            seeded["token"],
            [("forecast_balance", {"account": "assets.checking", "days": 30})],
        )
        _call_all(
            live_server.url,
            seeded["token"],
            [("read_series", {"uri": forecast["structured"]["daily_uri"], "withhold": ALL})],
        )
        rows = admin_client.get("/audit", params={"privacy_requested": "true", "limit": 50}).json()
        tools = [(r.get("detail") or {}).get("tool") for r in rows["items"]]
        assert "read_series" in tools


def test_attention_latest_amount_respects_mcp_disclosure(
    seeded: dict[str, Any],
    live_server: LiveServer,
    db_session: Session,
) -> None:
    from purser.models import Suggestion

    row = db_session.get(Suggestion, uuid.UUID(seeded["suggestion"]))
    assert row is not None
    row.evidence = {**(row.evidence or {}), "amount": {"latest": "-73.19", "price_changed": True}}
    db_session.commit()
    hidden, included = _call_all(
        live_server.url,
        seeded["token"],
        [("list_suggestions", {"withhold": ["amount"]}), ("list_suggestions", {})],
    )
    assert "73.19" not in _dump(hidden)
    assert "73.19" in _dump(included)
