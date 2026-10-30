"""MCP server: real client sessions over streamable HTTP, scoped tokens,
token-budgeted responses."""

import asyncio
import json
import socket
import threading
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx2
import pytest
import uvicorn
from fastapi.testclient import TestClient
from sqlalchemy import Engine

# UTC, not local — see the note in tests/test_networth.py (issue #140).
TODAY = datetime.now(UTC).date()
# A fixed past day for rows whose date only orders them (#262: never let a
# seed depend on where today falls in the month).
SPLIT_DAY = date(2026, 7, 15)

# Every tool the MCP server exposes, pinned by equality in
# test_full_toolset_exposed. A tool group that stops being imported by
# purser/mcp/server.py un-registers silently otherwise (issue #90) — the same
# contract tests/test_scheduler.py keeps for scheduled jobs. Adding a tool
# means adding it here.
EXPECTED_TOOLS = {
    # resources (the purser:// series as a tool, #354)
    "read_series",
    # tools_read
    "describe",
    "list_accounts",
    "account_balance",
    "search_transactions",
    "get_transaction",
    "list_categories",
    "list_schedules",
    "list_unmatched",
    "search_docs",
    "present",
    # tools_analytics
    "spending_summary",
    "net_worth",
    "balance_history",
    "forecast_balance",
    "list_recurring",
    "whats_new",
    # tools_holdings (#390)
    "list_holdings",
    "record_position",
    "record_price",
    # tools_memories
    "list_memories",
    "propose_memory",
    "archive_memory",
    # tools_review
    "review_batch",
    "mark_reviewed",
    # tools_rules
    "list_rules",
    "preview_rule",
    "propose_rule",
    # tools_suggestions
    "list_suggestions",
    "decide_suggestion",
    "detect_schedules",
    # tools_write
    "create_transaction",
    "update_transaction",
    "create_schedule",
    "update_schedule",
    "end_schedule",
    "skip_instance",
    "override_instance",
    "reset_instance",
    "record_statement_balance",
    "match",
    "confirm_transfer",
    "reconcile_account",
    "record_valuation",
    "update_account",
    "trigger_sync",
}


class LiveServer:
    """create_app() served for real — the MCP client needs actual HTTP."""

    def __init__(self) -> None:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = int(s.getsockname()[1])
        self.url = f"http://127.0.0.1:{self.port}"
        from purser.api.app import create_app

        config = uvicorn.Config(create_app(), host="127.0.0.1", port=self.port, log_level="error")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def start(self) -> None:
        self._thread.start()
        deadline = time.time() + 15
        while not self._server.started:
            if time.time() > deadline:
                raise RuntimeError("server failed to start")
            time.sleep(0.05)

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)


@pytest.fixture(scope="module")
def live_server(migrated_engine: Engine) -> Iterator[LiveServer]:
    server = LiveServer()
    server.start()
    yield server
    server.stop()


@pytest.fixture
def seeded_tokens(admin_client: TestClient, migrated_engine: Engine) -> dict[str, str]:
    """Admin session seeds data; returns bearer tokens by scope profile."""
    admin_client.post(
        "/accounts", json={"name": "Checking", "type": "checking", "path": "assets.checking"}
    )
    admin_client.post(
        "/categories",
        json={"name": "Dining", "type": "category_expense", "path": "expenses.dining"},
    )
    admin_client.post(
        "/categories",
        json={
            "name": "Restaurants",
            "type": "category_expense",
            "path": "expenses.dining.restaurants",
        },
    )
    admin_client.post(
        "/categories",
        json={"name": "Groceries", "type": "category_expense", "path": "expenses.groceries"},
    )
    # Restaurant spending across three months + groceries noise.
    for months_back, amount in [(2, "-120.00"), (1, "-180.00"), (0, "-95.00")]:
        on = (TODAY.replace(day=15) - timedelta(days=30 * months_back)).isoformat()
        admin_client.post(
            "/transactions",
            json={
                "account": "assets.checking",
                "category": "expenses.dining.restaurants",
                "amount": amount,
                "date": on,
                "payee": "Various Restaurants",
                "status": "posted",
            },
        )
    admin_client.post(
        "/transactions",
        json={
            "account": "assets.checking",
            "category": "expenses.groceries",
            "amount": "-300.00",
            "date": TODAY.isoformat(),
            "status": "posted",
        },
    )
    tokens = {}
    for name, scopes in [
        ("reader", ["read"]),
        ("proposer", ["write"]),
        ("agent", ["read", "write"]),
        ("operator", ["read", "write"]),
    ]:
        resp = admin_client.post("/tokens", json={"name": name, "scopes": scopes})
        tokens[name] = resp.json()["token"]
    return tokens


def _mcp_call(url: str, token: str, calls: list[tuple[str, dict[str, Any]]]) -> list[Any]:
    """Run a real MCP client session and return structured results."""

    async def run() -> list[Any]:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        results = []
        async with (
            httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http,
            streamable_http_client(f"{url}/mcp", http_client=http) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            for tool, args in calls:
                result = await session.call_tool(tool, args)
                if result.is_error:
                    results.append({"error": result.content[0].text})  # type: ignore[union-attr]
                elif result.structured_content is not None:
                    results.append(result.structured_content)
                else:
                    results.append(json.loads(result.content[0].text))  # type: ignore[union-attr]
        return results

    return asyncio.run(run())


def _mcp_list_tools(url: str, token: str) -> list[str]:
    async def run() -> list[str]:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async with (
            httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http,
            streamable_http_client(f"{url}/mcp", http_client=http) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            tools = await session.list_tools()
            return [t.name for t in tools.tools]

    return asyncio.run(run())


class TestScopeDecorator:
    """Deny-by-default MCP tool auth (issue #188).

    Before this, every tool hand-rolled `_require(security.can_read, "read")`
    inside its session closure. Omitting that line registered a tool that
    served any authenticated principal, with no test and no type error to
    catch it. The scope is now declared on the decorator and enforced there.
    """

    def test_a_tool_cannot_register_without_declaring_a_scope(self) -> None:
        from purser.mcp._registry import audited_tool
        from purser.mcp.privacy import NOTHING

        with pytest.raises(TypeError, match="scope"):
            audited_tool(kind="read", entity="system", emits=NOTHING)  # type: ignore[call-arg]

    def test_an_unknown_scope_fails_at_import_not_at_call_time(self) -> None:
        from purser.mcp._registry import audited_tool
        from purser.mcp.privacy import NOTHING

        with pytest.raises(ValueError, match="unknown scope"):
            audited_tool(kind="read", entity="system", scope="reed", emits=NOTHING)

    def test_a_tool_cannot_register_without_declaring_what_it_emits(self) -> None:
        """#437: minimal disclosure is structural like scope — a tool that
        does not say what privatizable fields it emits does not register."""
        from purser.mcp._registry import audited_tool

        with pytest.raises(TypeError, match="emits"):
            audited_tool(kind="read", entity="system", scope="read")  # type: ignore[call-arg]


def _mcp_raw_call(url: str, token: str, tool: str, args: dict[str, Any]) -> Any:
    """One tool call returning the raw CallToolResult, content blocks included."""

    async def run() -> Any:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async with (
            httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http,
            streamable_http_client(f"{url}/mcp", http_client=http) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            return await session.call_tool(tool, args)

    return asyncio.run(run())


class TestEmptyListContent:
    """Issue #391: FastMCP sent an empty list as zero content blocks, and
    ContextForge — which rebuilds a result from its content blocks — then
    dropped structuredContent too, so a validating client failed exactly
    when the answer was "none". Every result now carries a text block."""

    def test_an_empty_list_gets_a_text_block_and_keeps_its_structured_shape(self) -> None:
        from purser.mcp._registry import _ensure_text_block

        result = _ensure_text_block([])
        assert [block.text for block in result.content] == ["[]"]
        assert result.structured_content == {"result": []}

    def test_a_non_empty_list_is_left_to_fastmcp(self) -> None:
        from purser.mcp._registry import _ensure_text_block

        rows = [{"symbol": "TEST"}]
        assert _ensure_text_block(rows) is rows

    def test_a_three_tier_result_with_a_headline_is_unchanged(self) -> None:
        from mcp.types import CallToolResult, TextContent

        from purser.mcp._registry import _ensure_text_block

        result = CallToolResult(
            content=[TextContent(type="text", text="Headline.")],
            structuredContent={"total": "1.00"},
        )
        assert _ensure_text_block(result) is result

    def test_structured_content_without_any_block_gets_its_json_as_text(self) -> None:
        from mcp.types import CallToolResult

        from purser.mcp._registry import _ensure_text_block

        result = _ensure_text_block(CallToolResult(content=[], structuredContent={"items": []}))
        assert [json.loads(block.text) for block in result.content] == [{"items": []}]
        assert result.structured_content == {"items": []}

    @pytest.mark.parametrize(
        ("tool", "args"),
        [
            ("list_unmatched", {"direction": "expected"}),  # no expected rows
            ("list_schedules", {"status": "ended"}),  # no schedules at all
        ],
    )
    def test_empty_bare_list_tools_answer_with_a_text_block(
        self,
        seeded_tokens: dict[str, str],
        live_server: LiveServer,
        tool: str,
        args: dict[str, Any],
    ) -> None:
        result = _mcp_raw_call(live_server.url, seeded_tokens["reader"], tool, args)
        assert not result.is_error
        assert [block.text for block in result.content] == ["[]"]  # type: ignore[union-attr]
        assert result.structured_content == {"result": []}

    def test_an_empty_three_tier_list_holdings_keeps_its_headline(
        self, seeded_tokens: dict[str, str], live_server: LiveServer
    ) -> None:
        """list_holdings answers in three tiers (#390), so with no brokerage
        account it is not a bare list: its text block is the headline, beside
        the structured accounts/totals, and _ensure_text_block leaves it be."""
        result = _mcp_raw_call(live_server.url, seeded_tokens["reader"], "list_holdings", {})
        assert not result.is_error
        assert [block.text for block in result.content] == [  # type: ignore[union-attr]
            "No open brokerage accounts."
        ]
        assert result.structured_content == {
            "accounts": [],
            "totals": {"market_value": "0.00"},
            "truncated": False,
        }

    def test_a_non_empty_bare_list_tool_answers_as_before(
        self, seeded_tokens: dict[str, str], live_server: LiveServer
    ) -> None:
        result = _mcp_raw_call(live_server.url, seeded_tokens["reader"], "list_accounts", {})
        assert len(result.content) == 1
        rows = result.structured_content["result"]
        assert rows, "the fixture seeds a checking account"
        assert json.loads(result.content[0].text) == rows  # type: ignore[union-attr]


class TestMcpServer:
    def test_full_toolset_exposed(
        self, seeded_tokens: dict[str, str], live_server: LiveServer
    ) -> None:
        """The complete toolset, pinned by EQUALITY (issue #90).

        This was a subset assertion over a hand-maintained list that had
        drifted to 18 of the 22 registered tools — so it could not have caught
        the failure it exists for: a tool module that stops being imported by
        purser/mcp/server.py silently un-registers its whole group.
        """
        tools = _mcp_list_tools(live_server.url, seeded_tokens["reader"])
        assert set(tools) == EXPECTED_TOOLS

    def test_bad_token_rejected(
        self, seeded_tokens: dict[str, str], live_server: LiveServer
    ) -> None:
        with pytest.raises((RuntimeError, ExceptionGroup)):
            _mcp_list_tools(live_server.url, "purser_not_a_real_token")

    def test_reconcile_account_round_trip_and_live_check(
        self, seeded_tokens: dict[str, str], live_server: LiveServer, client: TestClient
    ) -> None:
        """reconcile_account over MCP (#268): dry run == commit, an idempotent
        retry, list_accounts' live check, and a backdated write that turns the
        retry into an error carrying the difference. It uses its own account
        and past dates, so the fixture's day-of-month seeding cannot reach it
        (#262)."""
        opened, statement = TODAY - timedelta(days=20), TODAY - timedelta(days=10)
        assert (
            client.post(
                "/accounts", json={"name": "Wallet", "type": "cash", "path": "assets.wallet"}
            ).status_code
            == 201
        )

        def post(amount: str, on: str) -> None:
            resp = client.post(
                "/transactions",
                json={
                    "account": "assets.wallet",
                    "category": "expenses.groceries",
                    "amount": amount,
                    "date": on,
                    "status": "posted",
                },
            )
            assert resp.status_code == 201, resp.text

        post("40.00", opened.isoformat())
        args = {
            "account": "assets.wallet",
            "as_of": statement.isoformat(),
            "statement_balance": "45.00",
        }
        dry, done, retry, listing = _mcp_call(
            live_server.url,
            seeded_tokens["operator"],
            [
                ("reconcile_account", {**args, "dry_run": True}),
                ("reconcile_account", args),
                ("reconcile_account", args),
                ("list_accounts", {"include_balances": False}),
            ],
        )
        assert "error" not in done, done
        computed = ("as_of", "basis", "target_balance", "ledger_balance", "adjustment", "category")
        assert {k: dry[k] for k in computed} == {k: done[k] for k in computed}
        assert dry["dry_run"] is True and dry["reconciliation_id"] is None
        assert done["created"] is True and done["adjustment"] == "5.0000"
        assert done["ledger_balance"] == "40.0000"
        assert retry == {**done, "created": False}
        wallet = next(a for a in listing["result"] if a["path"] == "assets.wallet")
        assert wallet["reconciliation"] == {
            "as_of": statement.isoformat(),
            "basis": "posted",
            "in_balance": True,
            "difference": "0.0000",
        }
        checking = next(a for a in listing["result"] if a["path"] == "assets.checking")
        assert "reconciliation" not in checking  # absent, not null (token budget)

        post("2.00", (statement - timedelta(days=1)).isoformat())
        moved, relisted = _mcp_call(
            live_server.url,
            seeded_tokens["operator"],
            [("reconcile_account", args), ("list_accounts", {"include_balances": False})],
        )
        assert "error" in moved and "2.0000" in moved["error"]
        wallet = next(a for a in relisted["result"] if a["path"] == "assets.wallet")
        assert wallet["reconciliation"]["in_balance"] is False
        assert wallet["reconciliation"]["difference"] == "2.0000"

    def test_reconcile_account_requires_write_scope(
        self, seeded_tokens: dict[str, str], live_server: LiveServer
    ) -> None:
        args = {
            "account": "assets.checking",
            "as_of": (TODAY - timedelta(days=1)).isoformat(),
            "statement_balance": "1.00",
            "dry_run": True,
        }
        for profile in ("reader",):
            (denied,) = _mcp_call(
                live_server.url, seeded_tokens[profile], [("reconcile_account", args)]
            )
            assert "error" in denied and "requires scope: write" in denied["error"], denied

    def test_record_valuation_stores_a_statement_figure_in_ledger_sign(
        self, seeded_tokens: dict[str, str], live_server: LiveServer, client: TestClient
    ) -> None:
        """#323: the reconcile how-to sends an agent to record a loan's
        valuation, and MCP can now do it. A loan's amount owed entered as the
        statement prints it, positive with convention='statement', is stored
        negative: the ledger sign every reader of valuations assumes
        (decision 71)."""
        resp = client.post(
            "/accounts", json={"name": "Mortgage", "type": "loan", "path": "liabilities.mortgage"}
        )
        assert resp.status_code == 201, resp.text
        loan_id = resp.json()["id"]
        as_of = (TODAY - timedelta(days=3)).isoformat()
        loan = {"account": "liabilities.mortgage", "as_of": as_of}

        refused, recorded = _mcp_call(
            live_server.url,
            seeded_tokens["operator"],
            [
                ("reconcile_account", {**loan, "statement_balance": "250000.00"}),
                ("record_valuation", {**loan, "value": "250000.00", "convention": "statement"}),
            ],
        )
        # The refusal names the tool that does what the agent was trying to.
        assert "record_valuation" in refused["error"], refused
        assert "error" not in recorded, recorded
        assert recorded == {
            "id": recorded["id"],
            "account": "liabilities.mortgage",
            "as_of": as_of,
            "value": "-250000.0000",
            "source": "manual",
        }
        (stored,) = client.get(f"/accounts/{loan_id}/valuations").json()
        assert (stored["id"], Decimal(stored["value"])) == (recorded["id"], Decimal("-250000"))
        account = client.get(f"/accounts/{loan_id}").json()
        assert account["balance_method"] == "valuation"
        assert Decimal(account["balance"]) == Decimal("-250000")
        (event,) = client.get("/events", params={"type": "valuation.recorded"}).json()
        assert event["payload"] == {"as_of": as_of, "value": "-250000.0000", "source": "manual"}
        # Audited twice, like every MCP write: the change row and the call row.
        (change,) = client.get("/audit", params={"action": "valuation.create"}).json()["items"]
        assert change["before_after"]["after"]["value"] == "-250000.0000"
        (call,) = client.get("/audit", params={"action": "mcp.record_valuation"}).json()["items"]
        assert call["request_id"] == change["request_id"]

    def test_record_valuation_refuses_a_positive_loan_figure_as_rest_does(
        self, seeded_tokens: dict[str, str], live_server: LiveServer, client: TestClient
    ) -> None:
        """The ledger convention's refusal (decision 71) reaches the agent with
        REST's 422 message word for word, and nothing is stored."""
        resp = client.post(
            "/accounts", json={"name": "Mortgage", "type": "loan", "path": "liabilities.mortgage"}
        )
        assert resp.status_code == 201, resp.text
        loan_id = resp.json()["id"]
        as_of = (TODAY - timedelta(days=3)).isoformat()
        rest = client.post(
            f"/accounts/{loan_id}/valuations", json={"as_of": as_of, "value": "250000.00"}
        )
        assert rest.status_code == 422
        args = {"account": "liabilities.mortgage", "as_of": as_of, "value": "250000.00"}
        refusals = _mcp_call(
            live_server.url,
            seeded_tokens["operator"],
            [
                ("record_valuation", args),
                ("record_valuation", {**args, "value": "-1.00001"}),
                ("record_valuation", {**args, "value": "a lot"}),
                ("record_valuation", {**args, "convention": "bank"}),
            ],
        )
        prefix = "Error calling tool 'record_valuation': "
        assert [refused["error"].removeprefix(prefix) for refused in refusals] == [
            rest.json()["detail"],
            "value has more than 4 decimal places",
            "value must be a decimal string",
            "convention must be one of: ledger, statement",
        ]
        assert client.get(f"/accounts/{loan_id}/valuations").json() == []

    def test_record_valuation_refuses_a_non_valuation_account_as_rest_does(
        self, seeded_tokens: dict[str, str], live_server: LiveServer, client: TestClient
    ) -> None:
        """#343 (decision 81): only loan and property accounts are read by
        their valuations, so a checking or brokerage valuation is refused with
        REST's 422 message word for word, and nothing is stored."""
        resp = client.post(
            "/accounts",
            json={"name": "Brokerage", "type": "brokerage", "path": "assets.brokerage"},
        )
        assert resp.status_code == 201, resp.text
        as_of = (TODAY - timedelta(days=3)).isoformat()
        listed = {a["path"]: a["id"] for a in client.get("/accounts").json()}
        paths = ("assets.checking", "assets.brokerage")
        rest = [
            client.post(
                f"/accounts/{listed[path]}/valuations", json={"as_of": as_of, "value": "100.00"}
            )
            for path in paths
        ]
        assert [r.status_code for r in rest] == [422, 422]
        refusals = _mcp_call(
            live_server.url,
            seeded_tokens["operator"],
            [
                ("record_valuation", {"account": p, "as_of": as_of, "value": "100.00"})
                for p in paths
            ],
        )
        prefix = "Error calling tool 'record_valuation': "
        assert [refused["error"].removeprefix(prefix) for refused in refusals] == [
            r.json()["detail"] for r in rest
        ]
        assert "valuations apply only to loan and property accounts" in rest[0].json()["detail"]
        for path in paths:
            assert client.get(f"/accounts/{listed[path]}/valuations").json() == []
        assert client.get("/events", params={"type": "valuation.recorded"}).json() == []

    def test_record_valuation_requires_write_scope(
        self, seeded_tokens: dict[str, str], live_server: LiveServer
    ) -> None:
        args = {
            "account": "assets.checking",
            "as_of": (TODAY - timedelta(days=1)).isoformat(),
            "value": "1.00",
        }
        for profile in ("reader",):
            (denied,) = _mcp_call(
                live_server.url, seeded_tokens[profile], [("record_valuation", args)]
            )
            assert "error" in denied and "requires scope: write" in denied["error"], denied

    def test_update_account_renames_and_keeps_the_path(
        self, seeded_tokens: dict[str, str], live_server: LiveServer, client: TestClient
    ) -> None:
        """#353: an agent can rename a real account. Only the display name,
        description and hidden flag change; the path, which outside
        integrations map accounts by, stays exactly where it was."""
        (account,) = [a for a in client.get("/accounts").json() if a["path"] == "assets.checking"]
        renamed, listing, shown = _mcp_call(
            live_server.url,
            seeded_tokens["operator"],
            [
                (
                    "update_account",
                    {
                        "account": "assets.checking",
                        "name": "  Everyday Checking  ",
                        "description": "Joint account",
                        "hidden": True,
                    },
                ),
                ("list_accounts", {"include_balances": False}),
                ("update_account", {"account": account["id"], "hidden": False}),
            ],
        )
        assert renamed == {
            "id": account["id"],
            "path": "assets.checking",
            "name": "Everyday Checking",
            "hidden": True,
            "min_balance": None,
        }
        (listed,) = [a for a in listing["result"] if a["id"] == account["id"]]
        assert (listed["path"], listed["name"], listed["hidden"]) == (
            "assets.checking",
            "Everyday Checking",
            True,
        )
        # A UUID reference works too, and only the field sent changes.
        assert shown == {**renamed, "hidden": False}
        stored = client.get(f"/accounts/{account['id']}").json()
        assert (stored["path"], stored["name"], stored["description"]) == (
            "assets.checking",
            "Everyday Checking",
            "Joint account",
        )
        # Audited twice, like every MCP write: the change row and the call row.
        changes = client.get("/audit", params={"action": "account.update"}).json()["items"]
        first = next(c for c in changes if c["before_after"]["after"]["hidden_at"] is not None)
        assert first["before_after"]["before"]["name"] == "Checking"
        assert first["before_after"]["after"]["name"] == "Everyday Checking"
        calls = client.get("/audit", params={"action": "mcp.update_account"}).json()["items"]
        assert first["request_id"] in {call["request_id"] for call in calls}

    def test_update_account_refusals(
        self, seeded_tokens: dict[str, str], live_server: LiveServer, client: TestClient
    ) -> None:
        """No path argument (moves stay REST/UI-only), no categories, no empty
        or no-op edit — and nothing is written by any of them."""
        refusals = _mcp_call(
            live_server.url,
            seeded_tokens["operator"],
            [
                ("update_account", {"account": "assets.checking", "path": "assets.moved"}),
                ("update_account", {"account": "expenses.dining", "name": "Eating out"}),
                ("update_account", {"account": "assets.checking", "name": "   "}),
                ("update_account", {"account": "assets.checking"}),
            ],
        )
        moved, category, blank, noop = (r.get("error", "") for r in refusals)
        assert "path" in moved and "Unexpected keyword argument" in moved, refusals[0]
        assert "renamed in the app" in category
        assert "name must not be empty" in blank
        assert "nothing to change" in noop
        paths = {a["path"]: a["name"] for a in client.get("/accounts").json()}
        assert paths["assets.checking"] == "Checking" and "assets.moved" not in paths
        assert client.get("/audit", params={"action": "account.update"}).json()["items"] == []

    def test_update_account_requires_write_scope(
        self, seeded_tokens: dict[str, str], live_server: LiveServer, client: TestClient
    ) -> None:
        for args in (
            {"account": "assets.checking", "name": "Renamed"},
            {"account": "assets.checking", "min_balance": "500.00"},
        ):
            for profile in ("reader",):
                (denied,) = _mcp_call(
                    live_server.url, seeded_tokens[profile], [("update_account", args)]
                )
                assert "error" in denied and "requires scope: write" in denied["error"], denied
        (checking,) = [a for a in client.get("/accounts").json() if a["path"] == "assets.checking"]
        assert (checking["name"], checking["min_balance"]) == ("Checking", None)

    def test_update_account_sets_and_clears_the_floor(
        self, seeded_tokens: dict[str, str], live_server: LiveServer, client: TestClient
    ) -> None:
        """#466: "warn me if checking goes under $500" is min_balance="500.00"
        with write scope; "" clears it. Both are audited account.update rows
        with min_balance before and after; the floor shows in forecast_balance."""
        set_floor, forecast, cleared = _mcp_call(
            live_server.url,
            seeded_tokens["operator"],
            [
                ("update_account", {"account": "assets.checking", "min_balance": "500.00"}),
                ("forecast_balance", {"account": "assets.checking", "days": 30}),
                ("update_account", {"account": "assets.checking", "min_balance": ""}),
            ],
        )
        assert set_floor["min_balance"] == "500.00", set_floor
        assert set_floor["name"] == "Checking"  # only the field sent changes
        assert Decimal(forecast["min_balance"]) == Decimal("500.00"), forecast
        assert cleared["min_balance"] is None, cleared
        (checking,) = [a for a in client.get("/accounts").json() if a["path"] == "assets.checking"]
        assert checking["min_balance"] is None
        changes = client.get("/audit", params={"action": "account.update"}).json()["items"]
        floors = {
            (c["before_after"]["before"]["min_balance"], c["before_after"]["after"]["min_balance"])
            for c in changes
        }
        assert floors == {(None, "500.0000"), ("500.0000", None)}

    def test_update_account_floor_refusals(
        self, seeded_tokens: dict[str, str], live_server: LiveServer, client: TestClient
    ) -> None:
        """A floor that is not a decimal, has too many places, or sits on an
        account type it does not apply to is refused, and nothing is written."""
        client.post(
            "/accounts",
            json={"name": "Mortgage", "type": "loan", "path": "liabilities.mortgage"},
        )
        refusals = _mcp_call(
            live_server.url,
            seeded_tokens["operator"],
            [
                ("update_account", {"account": "assets.checking", "min_balance": "lots"}),
                ("update_account", {"account": "assets.checking", "min_balance": "1.23456"}),
                ("update_account", {"account": "liabilities.mortgage", "min_balance": "0"}),
            ],
        )
        word, places, loan = (r.get("error", "") for r in refusals)
        assert "decimal string" in word, refusals[0]
        assert "4 decimal places" in places, refusals[1]
        assert "applies only to" in loan, refusals[2]
        assert all(a["min_balance"] is None for a in client.get("/accounts").json())
        assert client.get("/audit", params={"action": "account.update"}).json()["items"] == []

    def test_bracket_category_converts_to_transfer(
        self, seeded_tokens: dict[str, str], live_server: LiveServer, client: TestClient
    ) -> None:
        """update_transaction category='[account]' is the transfer sugar."""
        assert (
            client.post(
                "/accounts", json={"name": "Savings", "type": "savings", "path": "assets.savings"}
            ).status_code
            == 201
        )
        (page,) = _mcp_call(
            live_server.url, seeded_tokens["operator"], [("search_transactions", {"limit": 1})]
        )
        tx_id = page["items"][0]["id"]
        (updated,) = _mcp_call(
            live_server.url,
            seeded_tokens["operator"],
            [("update_transaction", {"transaction_id": tx_id, "category": "[assets.savings]"})],
        )
        assert "error" not in updated, updated
        (detail,) = _mcp_call(
            live_server.url,
            seeded_tokens["operator"],
            [("get_transaction", {"transaction_id": updated["id"]})],
        )
        paths = {p["account"] for p in detail["postings"]}
        assert paths == {"assets.checking", "assets.savings"}  # two real legs = transfer

        # The flat search no longer blanks a transfer (#232): with no
        # perspective it shows the payer's leg and the receiving account;
        # under a perspective, that account's leg and the other account.
        (flat, from_checking, from_savings) = _mcp_call(
            live_server.url,
            seeded_tokens["operator"],
            [
                ("search_transactions", {"status": ["posted"], "limit": 50}),
                ("search_transactions", {"account": "assets.checking", "limit": 50}),
                ("search_transactions", {"account": "assets.savings", "limit": 50}),
            ],
        )
        row = next(r for r in flat["items"] if r["id"] == updated["id"])
        assert row["amount"] is not None and Decimal(row["amount"]) < 0
        assert row["counterparty"] == "assets.savings"
        assert row["category"] is None
        assert "counterparty" not in next(r for r in flat["items"] if r["id"] != updated["id"])
        checking_row = next(r for r in from_checking["items"] if r["id"] == updated["id"])
        savings_row = next(r for r in from_savings["items"] if r["id"] == updated["id"])
        assert Decimal(checking_row["amount"]) == -Decimal(savings_row["amount"])
        assert checking_row["counterparty"] == "assets.savings"
        assert savings_row["counterparty"] == "assets.checking"

    def test_confirm_transfer_books_a_split_loan_payment(
        self,
        seeded_tokens: dict[str, str],
        live_server: LiveServer,
        client: TestClient,
        migrated_engine: Engine,
    ) -> None:
        """Decision 62 over MCP: remainder_category turns the confirm into a
        split loan payment — commit scope, exact bank legs, one interest leg —
        and search_transactions shows the counterparty AND the remainder."""
        from tests.test_matching import _imported

        for endpoint, body in (
            ("/accounts", {"name": "Car Loan", "type": "loan", "path": "liabilities.carloan"}),
            (
                "/categories",
                {
                    "name": "Interest charges",
                    "type": "category_expense",
                    "path": "expenses.fees.interest",
                },
            ),
        ):
            assert client.post(endpoint, json=body).status_code == 201
        out_id = _imported(
            client,
            migrated_engine,
            "assets.checking",
            "-612.40",
            SPLIT_DAY,
            payee="AUTO LOAN PAYMENT",
        )
        in_id = _imported(
            client,
            migrated_engine,
            "liabilities.carloan",
            "540.15",
            SPLIT_DAY,
            payee="PAYMENT RECEIVED",
        )
        args = {
            "outgoing_id": out_id,
            "incoming_id": in_id,
            "remainder_category": "expenses.fees.interest",
        }
        (denied,) = _mcp_call(
            live_server.url, seeded_tokens["reader"], [("confirm_transfer", args)]
        )
        assert "requires scope: write" in denied["error"]

        (confirmed,) = _mcp_call(
            live_server.url, seeded_tokens["operator"], [("confirm_transfer", args)]
        )
        assert "error" not in confirmed, confirmed
        assert set(confirmed) == {"merged_transaction_id", "remainder", "remainder_category"}
        assert Decimal(confirmed["remainder"]) == Decimal("72.25")
        assert confirmed["remainder_category"] == "expenses.fees.interest"
        merged_id = confirmed["merged_transaction_id"]

        (detail, from_checking, from_loan) = _mcp_call(
            live_server.url,
            seeded_tokens["reader"],
            [
                ("get_transaction", {"transaction_id": merged_id}),
                ("search_transactions", {"account": "assets.checking", "limit": 50}),
                ("search_transactions", {"account": "liabilities.carloan", "limit": 50}),
            ],
        )
        legs = {p["account"]: Decimal(p["amount"]) for p in detail["postings"]}
        assert legs == {
            "assets.checking": Decimal("-612.40"),
            "liabilities.carloan": Decimal("540.15"),
            "expenses.fees.interest": Decimal("72.25"),
        }
        checking_row = next(r for r in from_checking["items"] if r["id"] == merged_id)
        assert Decimal(checking_row["amount"]) == Decimal("-612.40")
        assert checking_row["counterparty"] == "liabilities.carloan"
        assert checking_row["category"] == "expenses.fees.interest"
        assert Decimal(checking_row["remainder"]) == Decimal("72.25")
        loan_row = next(r for r in from_loan["items"] if r["id"] == merged_id)
        assert Decimal(loan_row["amount"]) == Decimal("540.15")
        assert loan_row["counterparty"] == "assets.checking"
        assert Decimal(loan_row["remainder"]) == Decimal("72.25")
        # Ordinary rows keep the pinned compact shape: no remainder key.
        assert all(
            "remainder" not in r and "counterparty" not in r
            for r in from_checking["items"]
            if r["id"] != merged_id
        )

    def test_multi_posting_row_is_not_reported_as_a_transfer(
        self,
        seeded_tokens: dict[str, str],
        live_server: LiveServer,
        client: TestClient,
        migrated_engine: Engine,
    ) -> None:
        """Two real accounts plus two categories (#245's template shape) is
        not a transfer: no counterparty, no remainder."""
        from tests.test_split_loan_payment import _insert

        assert (
            client.post(
                "/accounts", json={"name": "Savings", "type": "savings", "path": "assets.savings"}
            ).status_code
            == 201
        )
        tx_id = _insert(
            migrated_engine,
            [
                ("assets.checking", "-100.00"),
                ("assets.savings", "60.00"),
                ("expenses.dining", "30.00"),
                ("expenses.groceries", "10.00"),
            ],
        )
        (flat, from_checking) = _mcp_call(
            live_server.url,
            seeded_tokens["reader"],
            [
                ("search_transactions", {"limit": 50}),
                ("search_transactions", {"account": "assets.checking", "limit": 50}),
            ],
        )
        for page in (flat, from_checking):
            row = next(r for r in page["items"] if r["id"] == tx_id)
            assert "counterparty" not in row
            assert "remainder" not in row
            assert row["category"] is None
        assert Decimal(next(r for r in from_checking["items"] if r["id"] == tx_id)["amount"]) == (
            Decimal("-100.00")
        )

    def test_split_row_carries_its_line_count(
        self,
        seeded_tokens: dict[str, str],
        live_server: LiveServer,
        client: TestClient,
        migrated_engine: Engine,
    ) -> None:
        """Decision 87: a split transaction (one real leg, several category
        legs) carries `split: n` and no category; other rows never carry the
        key."""
        from tests.test_split_loan_payment import _insert

        tx_id = _insert(
            migrated_engine,
            [
                ("assets.checking", "-100.00"),
                ("expenses.dining", "70.00"),
                ("expenses.groceries", "30.00"),
            ],
        )
        (page,) = _mcp_call(
            live_server.url,
            seeded_tokens["reader"],
            [("search_transactions", {"account": "assets.checking", "limit": 50})],
        )
        row = next(r for r in page["items"] if r["id"] == tx_id)
        assert row["split"] == 2
        assert row["category"] is None
        assert Decimal(row["amount"]) == Decimal("-100.00")
        assert all("split" not in r for r in page["items"] if r["id"] != tx_id)
        rest = client.get(f"/transactions/{tx_id}").json()
        assert rest["split_lines"] == 2
        assert rest["category_path"] is None

    def test_search_transactions_item_shape_is_pinned(
        self, seeded_tokens: dict[str, str], live_server: LiveServer
    ) -> None:
        """The MCP serializer is independent of the REST TransactionOut:
        register-perspective fields must not leak into this token-budgeted
        shape. Without an account there is no perspective, so no `balance`
        either — the same transaction has a different running balance in each
        account it touches."""
        (page,) = _mcp_call(live_server.url, seeded_tokens["reader"], [("search_transactions", {})])
        assert page["items"], "fixture seeds transactions"
        assert set(page["items"][0]) == {"id", "date", "amount", "payee", "category", "status"}

    def test_balance_present_under_a_real_account_perspective(
        self, seeded_tokens: dict[str, str], live_server: LiveServer
    ) -> None:
        """#197: the register's Balance column, served to agents. Without it
        an agent asked to chart an account's balance has to walk rows and
        subtract — the cross-row arithmetic guardrail 6 forbids."""
        (page,) = _mcp_call(
            live_server.url,
            seeded_tokens["reader"],
            [("search_transactions", {"account": "assets.checking"})],
        )
        assert page["items"], "fixture seeds transactions"
        assert set(page["items"][0]) == {
            "id",
            "date",
            "amount",
            "payee",
            "category",
            "status",
            "balance",
        }
        posted = [i for i in page["items"] if i["status"] in ("pending", "posted")]
        assert posted, "fixture seeds pending/posted rows"
        assert all(i["balance"] is not None for i in posted)
        # Recorded, not computed: the running balance is the ledger's own
        # number, so it must equal account_balance at that row's date.
        newest = max(posted, key=lambda i: i["date"])
        (point,) = _mcp_call(
            live_server.url,
            seeded_tokens["reader"],
            [("account_balance", {"account": "assets.checking", "as_of": newest["date"]})],
        )
        assert Decimal(newest["balance"]) == Decimal(point["balance"])

    def test_balance_key_absent_for_a_category_perspective(
        self, seeded_tokens: dict[str, str], live_server: LiveServer
    ) -> None:
        """Categories are virtual and have no running-balance rows, so they
        get no column at all rather than a column of nulls."""
        (page,) = _mcp_call(
            live_server.url,
            seeded_tokens["reader"],
            [("search_transactions", {"category": "expenses.dining"})],
        )
        assert page["items"], "fixture seeds dining transactions"
        assert all("balance" not in i for i in page["items"])

        # `account` resolves categories too (resolve_account is not
        # real-account-only), so the guard has to be on is_virtual rather than
        # on the parameter name. Use the LEAF category — account_ref matches an
        # exact account, and postings attach to the leaf, so the parent would
        # match nothing and make this assertion vacuous.
        (via_account,) = _mcp_call(
            live_server.url,
            seeded_tokens["reader"],
            [("search_transactions", {"account": "expenses.dining.restaurants"})],
        )
        assert via_account["items"], "a category resolves as an account_ref too"
        assert all("balance" not in i for i in via_account["items"])

    def test_balance_matches_the_rest_twin_byte_for_byte(
        self, seeded_tokens: dict[str, str], live_server: LiveServer, client: TestClient
    ) -> None:
        """One stored source, one query (balances.balances_for_transactions),
        so the register and the agent can never disagree."""
        (page,) = _mcp_call(
            live_server.url,
            seeded_tokens["reader"],
            [("search_transactions", {"account": "assets.checking", "limit": 50})],
        )
        rest = client.get(
            "/transactions", params={"account": "assets.checking", "limit": 50}
        ).json()
        rest_by_id = {r["id"]: r["balance"] for r in rest["items"]}
        assert page["items"], "fixture seeds transactions"
        for item in page["items"]:
            assert item["balance"] == rest_by_id[item["id"]]

    def test_restaurant_trend_without_paging_rows(
        self, seeded_tokens: dict[str, str], live_server: LiveServer
    ) -> None:
        """The dogfood acceptance: a fresh session with ONLY the MCP
        connection answers 'how did restaurant spending trend this year?'"""
        (describe_out, summary) = _mcp_call(
            live_server.url,
            seeded_tokens["reader"],
            [
                ("describe", {}),
                (
                    "spending_summary",
                    {"group_by": "month", "category": "expenses.dining"},
                ),
            ],
        )
        # describe orients: categories visible without any other call
        assert "expenses.dining.restaurants" in describe_out["categories"]
        # the trend is served aggregated: one row per month, totals computed
        rows = summary["rows"]
        assert len(rows) == 3
        assert [Decimal(r["total"]) for r in rows] == [
            Decimal("120.00"),
            Decimal("180.00"),
            Decimal("95.00"),
        ]
        # the grand total answers "how much did I spend" without summing rows
        assert Decimal(summary["grand_total"]) == Decimal("395.00")
        assert summary["truncated"] is False
        # no transaction rows were paged to answer this
        assert all("items" not in r for r in rows)

    def test_describe_requires_read_scope(
        self, seeded_tokens: dict[str, str], live_server: LiveServer
    ) -> None:
        """describe returns accounts, balances, and the category tree — it
        must enforce read scope like every other read tool (issue #34)."""
        (denied,) = _mcp_call(live_server.url, seeded_tokens["proposer"], [("describe", {})])
        assert "error" in denied
        assert "requires scope: read" in denied["error"]
        (allowed,) = _mcp_call(live_server.url, seeded_tokens["reader"], [("describe", {})])
        assert "accounts" in allowed and "categories" in allowed

    def test_spending_summary_grand_total_survives_truncation(
        self, seeded_tokens: dict[str, str], live_server: LiveServer, client: TestClient
    ) -> None:
        """With more groups than the limit, rows truncate (flagged) but
        grand_total still sums ALL matching spending (issue #43)."""
        for i in range(1, 25):  # 24 categories > the group limit of 20
            path = f"expenses.trunc.c{i:02d}"
            client.post(
                "/categories",
                json={"name": f"Trunc {i}", "type": "category_expense", "path": path},
            )
            client.post(
                "/transactions",
                json={
                    "account": "assets.checking",
                    "category": path,
                    "amount": "-10.00",
                    "date": TODAY.isoformat(),
                    "status": "posted",
                },
            )
        (summary,) = _mcp_call(
            live_server.url,
            seeded_tokens["reader"],
            [("spending_summary", {"group_by": "category", "category": "expenses.trunc"})],
        )
        assert summary["truncated"] is True
        assert len(summary["rows"]) == 20
        returned_sum = sum(Decimal(r["total"]) for r in summary["rows"])
        assert Decimal(summary["grand_total"]) == Decimal("240.00")
        assert Decimal(summary["grand_total"]) > returned_sum

    def test_responses_fit_token_budget(
        self, seeded_tokens: dict[str, str], live_server: LiveServer
    ) -> None:
        """Typical answers target < 500 tokens (~4 chars/token heuristic)."""
        results = _mcp_call(
            live_server.url,
            seeded_tokens["reader"],
            [
                ("list_accounts", {}),
                ("spending_summary", {"group_by": "category"}),
                ("net_worth", {}),
                ("forecast_balance", {"account": "assets.checking", "days": 30}),
                ("list_unmatched", {"direction": "posted"}),
            ],
        )
        for result in results:
            approx_tokens = len(json.dumps(result)) / 4
            assert approx_tokens < 500, f"response too large: {approx_tokens:.0f} tokens"

    def test_write_scope_gets_the_requested_status_via_mcp(
        self, seeded_tokens: dict[str, str], live_server: LiveServer, client: TestClient
    ) -> None:
        (created,) = _mcp_call(
            live_server.url,
            seeded_tokens["agent"],
            [
                (
                    "create_transaction",
                    {
                        "account": "assets.checking",
                        "category": "expenses.dining",
                        "amount": "-25.00",
                        "date_": TODAY.isoformat(),
                        "payee": "Chipotle",
                        "status": "posted",  # honoured: the agent acts for the user (#436)
                    },
                )
            ],
        )
        assert created["status"] == "posted"
        detail = client.get(f"/transactions/{created['id']}").json()
        assert detail["origin"] == "agent"

    def test_read_only_token_cannot_write(
        self, seeded_tokens: dict[str, str], live_server: LiveServer
    ) -> None:
        (result,) = _mcp_call(
            live_server.url,
            seeded_tokens["reader"],
            [
                (
                    "create_transaction",
                    {
                        "account": "assets.checking",
                        "category": "expenses.dining",
                        "amount": "-25.00",
                        "date_": TODAY.isoformat(),
                    },
                )
            ],
        )
        assert "error" in result
        assert "requires scope: write" in result["error"]

    def test_an_agent_proposal_carries_its_note_and_no_confidence(
        self, seeded_tokens: dict[str, str], live_server: LiveServer, client: TestClient
    ) -> None:
        """#250 over MCP: create_schedule(propose=true) keeps the agent's
        note as evidence and claims no confidence; a note past 200
        characters, or a payee the account already has a schedule for, is
        refused and lands nothing."""
        internet = {
            "name": "Internet",
            "account": "assets.checking",
            "rrule": "FREQ=MONTHLY;BYMONTHDAY=3",
            "amount": "-80.00",
            "payee": "Example Fiber",
        }
        note = "Statement 2026-09: 80.00 due on the 3rd"
        proposed, too_long, listed = _mcp_call(
            live_server.url,
            seeded_tokens["agent"],
            [
                ("create_schedule", {**internet, "note": note, "propose": True}),
                (
                    "create_schedule",
                    {**internet, "payee": "Example Water", "note": "x" * 201, "propose": True},
                ),
                ("list_suggestions", {"kind": "schedule"}),
            ],
        )
        assert proposed["status"] == "proposed", proposed
        assert "note must be 200 characters or fewer" in too_long["error"]
        (item,) = listed["items"]
        assert (item["id"], item["source"], item["confidence"]) == (
            proposed["suggestion_id"],
            "agent",
            None,
        )
        assert item["evidence_summary"]["note"] == note

        # Once a schedule covers the payee, proposing it again is refused.
        decided = client.post(
            f"/suggestions/{proposed['suggestion_id']}/decide", json={"decision": "accept"}
        )
        assert decided.status_code == 200, decided.text
        schedule_id = decided.json()["schedule"]["id"]
        (refused,) = _mcp_call(
            live_server.url,
            seeded_tokens["agent"],
            [("create_schedule", {**internet, "name": "Fiber again", "propose": True})],
        )
        assert f"'Internet' (active, id {schedule_id})" in refused["error"]
        assert client.get("/suggestions").json() == []

    def test_schedule_instance_tools_round_trip(
        self, seeded_tokens: dict[str, str], live_server: LiveServer, client: TestClient
    ) -> None:
        """create_schedule (dtstart persisted) -> override/skip instances ->
        update/end the rule, all over MCP (#231)."""
        anchor = (TODAY - timedelta(days=3)).isoformat()
        (created,) = _mcp_call(
            live_server.url,
            seeded_tokens["operator"],
            [
                (
                    "create_schedule",
                    {
                        "name": "Gym",
                        "account": "assets.checking",
                        "rrule": "FREQ=WEEKLY",
                        "dtstart": anchor,
                        "amount": "-60.00",
                        "category": "expenses.dining",
                    },
                )
            ],
        )
        assert created["status"] == "active"
        detail = client.get(f"/schedules/{created['id']}").json()
        assert detail["dtstart"] == anchor
        first, second = detail["instances"][0], detail["instances"][1]

        overridden, skipped, updated = _mcp_call(
            live_server.url,
            seeded_tokens["operator"],
            [
                (
                    "override_instance",
                    {"transaction_id": first["transaction_id"], "amount": "-75.00"},
                ),
                ("skip_instance", {"transaction_id": second["transaction_id"]}),
                ("update_schedule", {"schedule_id": created["id"], "amount": "-65.00"}),
            ],
        )
        assert overridden["pinned"] is True
        assert skipped["status"] == "void"
        assert updated["status"] == "active"
        live = {
            i["transaction_id"]: i
            for i in client.get(f"/schedules/{created['id']}").json()["instances"]
        }
        assert live[first["transaction_id"]]["amount"] == "-75.0000"  # pinned: untouched
        assert second["transaction_id"] not in live  # skipped: gone and its slot stays filled
        assert all(
            i["amount"] == "-65.0000" for tid, i in live.items() if tid != first["transaction_id"]
        )

        # end_schedule also pauses and resumes: since confirm_schedule's
        # retirement (#347) action=resume is the MCP way back to active.
        paused, resumed, unknown = _mcp_call(
            live_server.url,
            seeded_tokens["operator"],
            [
                ("end_schedule", {"schedule_id": created["id"], "action": "pause"}),
                ("end_schedule", {"schedule_id": created["id"], "action": "resume"}),
                ("end_schedule", {"schedule_id": created["id"], "action": "activate"}),
            ],
        )
        assert (paused["status"], paused["next_due"]) == ("paused", None)
        assert resumed["status"] == "active" and resumed["next_due"] is not None
        assert "action must be pause | resume | end" in unknown["error"]
        resumed_live = client.get(f"/schedules/{created['id']}").json()["instances"]
        assert {i["transaction_id"] for i in resumed_live} >= {first["transaction_id"]}
        assert len(resumed_live) == len(live)  # the dropped instances came back

        (ended,) = _mcp_call(
            live_server.url,
            seeded_tokens["operator"],
            [("end_schedule", {"schedule_id": created["id"]})],
        )
        assert ended["status"] == "ended"
        # The rule is gone; the user's pinned override is the one row that
        # survives (it is theirs, not the rule's — skip it explicitly).
        remaining = client.get(f"/schedules/{created['id']}").json()["instances"]
        assert [i["transaction_id"] for i in remaining] == [first["transaction_id"]]

        # A read-only token cannot skip (a void); writes need write scope.
        (denied,) = _mcp_call(
            live_server.url,
            seeded_tokens["reader"],
            [("skip_instance", {"transaction_id": first["transaction_id"]})],
        )
        assert "requires scope: write" in denied["error"]

    def test_override_instance_on_a_card_payment(
        self, seeded_tokens: dict[str, str], live_server: LiveServer, client: TestClient
    ) -> None:
        """#350: override_instance on a counterparty_balance card payment —
        amount, date, and both — moves both legs and pins the instance."""
        client.post(
            "/accounts",
            json={"name": "Amex", "type": "credit_card", "path": "liabilities.amex"},
        )
        client.post(
            "/transactions",
            json={
                "account": "liabilities.amex",
                "category": "expenses.dining",
                "amount": "-412.00",
                "date": TODAY.isoformat(),
                "status": "posted",
            },
        )
        created = client.post(
            "/schedules",
            json={
                "name": "Amex payment",
                "account": "assets.checking",
                "counterparty_account": "liabilities.amex",
                "rrule": "FREQ=WEEKLY",
                "dtstart": (TODAY - timedelta(days=3)).isoformat(),
                "amount_type": "estimated",
                "estimate_basis": "counterparty_balance",
            },
        ).json()
        first, second = client.get(f"/schedules/{created['id']}").json()["instances"][:2]
        assert first["amount"] == "-412.0000"
        moved_to = (date.fromisoformat(second["date"]) + timedelta(days=1)).isoformat()

        amount_only, date_only, both, flipped = _mcp_call(
            live_server.url,
            seeded_tokens["agent"],
            [
                (
                    "override_instance",
                    {"transaction_id": first["transaction_id"], "amount": "-8347.00"},
                ),
                (
                    "override_instance",
                    {"transaction_id": second["transaction_id"], "date": moved_to},
                ),
                (
                    "override_instance",
                    {
                        "transaction_id": first["transaction_id"],
                        "amount": "-8350.25",
                        "date": first["date"],
                    },
                ),
                (
                    "override_instance",
                    {"transaction_id": second["transaction_id"], "amount": "412.00"},
                ),
            ],
        )
        assert (amount_only["amount"], amount_only["pinned"]) == ("-8347.0000", True)
        assert (date_only["date"], date_only["amount"], date_only["pinned"]) == (
            moved_to,
            "-412.0000",
            True,
        )
        assert (both["amount"], both["date"]) == ("-8350.2500", first["date"])
        assert "would reverse" in flipped["error"]
        legs = {
            p["account_path"]: p["amount"]
            for p in client.get(f"/transactions/{first['transaction_id']}").json()["postings"]
        }
        assert legs == {"assets.checking": "-8350.2500", "liabilities.amex": "8350.2500"}

    def test_rename_relabels_and_reset_instance_round_trip(
        self, seeded_tokens: dict[str, str], live_server: LiveServer, client: TestClient
    ) -> None:
        """#353: update_schedule reports `relabeled`, a description edit does
        not pin, and reset_instance (commit scope) undoes an override."""
        created = client.post(
            "/schedules",
            json={
                "name": "Gym",
                "account": "assets.checking",
                "rrule": "FREQ=WEEKLY",
                "dtstart": (TODAY - timedelta(days=3)).isoformat(),
                "amount": "-60.00",
                "category": "expenses.dining",
            },
        ).json()
        instances = client.get(f"/schedules/{created['id']}").json()["instances"]
        first, second = instances[0], instances[1]

        labelled, overridden, renamed = _mcp_call(
            live_server.url,
            seeded_tokens["operator"],
            [
                (
                    "update_transaction",
                    {"transaction_id": second["transaction_id"], "description": "My gym"},
                ),
                (
                    "override_instance",
                    {"transaction_id": first["transaction_id"], "amount": "-99.00"},
                ),
                ("update_schedule", {"schedule_id": created["id"], "name": "Fitness club"}),
            ],
        )
        assert "error" not in labelled
        assert overridden["pinned"] is True
        # Every live instance, the pinned one included; the hand-labelled one
        # keeps its description and only its payee (still the old name) moves.
        assert renamed["relabeled"] == len(instances)
        live = {
            i["transaction_id"]: i
            for i in client.get(f"/schedules/{created['id']}").json()["instances"]
        }
        assert live[second["transaction_id"]]["pinned"] is False
        kept = client.get(f"/transactions/{second['transaction_id']}").json()
        assert (kept["description"], kept["payee"]) == ("My gym", "Fitness club")

        (denied,) = _mcp_call(
            live_server.url,
            seeded_tokens["reader"],
            [("reset_instance", {"transaction_id": first["transaction_id"]})],
        )
        assert "requires scope: write" in denied["error"]
        (reset,) = _mcp_call(
            live_server.url,
            seeded_tokens["operator"],
            [("reset_instance", {"transaction_id": first["transaction_id"]})],
        )
        assert reset == {
            "transaction_id": first["transaction_id"],
            "status": "expected",
            "date": first["date"],
            "amount": "-60.0000",
            "pinned": False,
        }
        detail = client.get(f"/transactions/{first['transaction_id']}").json()
        assert detail["description"] == "Fitness club"

    def test_suggestions_inbox_round_trip(
        self, seeded_tokens: dict[str, str], live_server: LiveServer, client: TestClient
    ) -> None:
        """propose-scope create_schedule -> inbox row -> decide over MCP (#233)."""
        (proposed,) = _mcp_call(
            live_server.url,
            seeded_tokens["agent"],
            [
                (
                    "create_schedule",
                    {
                        "name": "Internet",
                        "account": "assets.checking",
                        "rrule": "FREQ=MONTHLY;BYMONTHDAY=3",
                        "amount": "-80.00",
                        "payee": "Comcast",
                        "category": "expenses.dining",
                        "propose": True,
                    },
                )
            ],
        )
        assert proposed["status"] == "proposed"
        sid = proposed["suggestion_id"]
        (inbox,) = _mcp_call(live_server.url, seeded_tokens["agent"], [("list_suggestions", {})])
        (denied,) = _mcp_call(
            live_server.url,
            seeded_tokens["reader"],
            [("decide_suggestion", {"suggestion_id": sid, "decision": "accept"})],
        )
        assert inbox["open"] == 1
        assert inbox["items"][0]["id"] == sid
        assert inbox["items"][0]["source"] == "agent"
        assert set(inbox["items"][0]) >= {"headline", "draft", "evidence_summary", "confidence"}
        assert "error" in denied and "requires scope: write" in denied["error"]

        (snoozed, listed) = _mcp_call(
            live_server.url,
            seeded_tokens["operator"],
            [
                (
                    "decide_suggestion",
                    {
                        "suggestion_id": sid,
                        "decision": "snooze",
                        "snooze_until": (TODAY + timedelta(days=7)).isoformat(),
                    },
                ),
                ("list_suggestions", {}),
            ],
        )
        assert snoozed["status"] == "open" and snoozed["snooze_until"] is not None
        assert listed["open"] == 0  # hidden until the date

        (edited,) = _mcp_call(
            live_server.url,
            seeded_tokens["operator"],
            [
                (
                    "decide_suggestion",
                    {
                        "suggestion_id": sid,
                        "decision": "edit_then_accept",
                        "edits": {"amount": "-85.00"},
                    },
                )
            ],
        )
        assert edited["status"] == "accepted"
        assert edited["schedule"]["status"] == "active"
        schedule = client.get(f"/schedules/{edited['schedule']['id']}").json()
        assert Decimal(schedule["amount"]) == Decimal("-85.00")
        assert schedule["instances"]

    def test_detect_schedules_tool_lands_suggestions(
        self, seeded_tokens: dict[str, str], live_server: LiveServer, client: TestClient
    ) -> None:
        """The fixture's three monthly restaurant rows are a pattern (#234);
        detection over MCP lands it in the inbox with propose scope."""
        (denied,) = _mcp_call(live_server.url, seeded_tokens["reader"], [("detect_schedules", {})])
        assert "error" in denied
        (result,) = _mcp_call(live_server.url, seeded_tokens["agent"], [("detect_schedules", {})])
        assert result["created"] >= 1 and result["open"] >= 1
        item = next(i for i in result["items"] if i["draft"]["payee"] == "Various Restaurants")
        assert item["draft"]["rrule"].startswith("FREQ=MONTHLY")
        assert item["evidence_summary"]["count"] == 3
        (again,) = _mcp_call(live_server.url, seeded_tokens["agent"], [("detect_schedules", {})])
        assert again["created"] == 0 and again["refreshed"] >= 1

    def test_forecast_coverage_and_honest_headline(
        self, seeded_tokens: dict[str, str], live_server: LiveServer, client: TestClient
    ) -> None:
        """Zero schedules is said out loud (#235); with one, the horizon is
        honoured and projected events are flagged."""
        (flat,) = _mcp_call(
            live_server.url,
            seeded_tokens["reader"],
            [("forecast_balance", {"account": "assets.checking", "days": 30})],
        )
        assert flat["coverage"]["schedules_active"] == 0 and flat["events"] == []
        client.post(
            "/schedules",
            json={
                "name": "Rent",
                "account": "assets.checking",
                "rrule": "FREQ=MONTHLY;BYMONTHDAY=28",
                "amount": "-1800.00",
                "category": "expenses.dining",
            },
        )
        (long,) = _mcp_call(
            live_server.url,
            seeded_tokens["reader"],
            [("forecast_balance", {"account": "assets.checking", "days": 365})],
        )
        assert long["horizon_days"] == 365
        assert long["coverage"]["schedules_active"] == 1
        assert long["coverage"]["projected"] >= 9
        assert any(e.get("projected") for e in long["events"])
        # Flags ride only when true (guardrail 6): an instance event has neither.
        instance_event = next(e for e in long["events"] if e["transaction_id"])
        assert "projected" not in instance_event and "overdue" not in instance_event

    def test_net_worth_and_forecast_composed(
        self, seeded_tokens: dict[str, str], live_server: LiveServer
    ) -> None:
        (nw, fc) = _mcp_call(
            live_server.url,
            seeded_tokens["reader"],
            [("net_worth", {}), ("forecast_balance", {"account": "assets.checking"})],
        )
        # The day-15 restaurant charge this month may be future-dated relative
        # to today; net worth (postings <= today) excludes it in that case.
        assert Decimal(nw["net_worth"]) in (Decimal("-600.0000"), Decimal("-695.0000"))
        assert fc["account"] == "assets.checking"
        # Forecast starts from the full current balance (all pending+posted).
        assert Decimal(fc["start_balance"]) == Decimal("-695.00")


def test_llms_txt_generated_from_tool_source(clean_db: None, client: TestClient) -> None:
    resp = client.get("/llms.txt")
    assert resp.status_code == 200
    text = resp.text
    # Generated from the live tool registry — every tool documented.
    for tool in ("describe", "spending_summary", "forecast_balance", "trigger_sync"):
        assert f"**{tool}**" in text
    assert "read + write" in text


def test_tool_schemas_unchanged_by_audit_wrapper() -> None:
    """The audited_tool wrapper must be schema-transparent: tools/list output
    is pinned to the pre-wrapper snapshot (issue #122)."""
    from pathlib import Path

    from purser.mcp.server import mcp

    async def dump() -> list[dict[str, Any]]:
        tools = await mcp.list_tools()
        return sorted(
            (
                {"name": t.name, "description": t.description, "inputSchema": t.parameters}
                for t in tools
            ),
            key=lambda t: t["name"],
        )

    live = asyncio.run(dump())
    snapshot = json.loads((Path(__file__).parent / "fixtures/mcp_tools_snapshot.json").read_text())
    assert live == snapshot


def test_search_docs_tool(
    live_server: LiveServer, seeded_tokens: dict[str, str], migrated_engine: Engine
) -> None:
    """search_docs answers from the bundled index (issue #156, decision 41):
    reader gets ranked sections, proposer is scope-denied, and the call lands
    a kind='read' audit row with entity_type='docs'."""
    from sqlalchemy import select
    from sqlalchemy.orm import Session as OrmSession

    from purser.models import AuditLog

    (result,) = _mcp_call(
        live_server.url,
        seeded_tokens["reader"],
        [("search_docs", {"query": "encryption key"})],
    )
    assert result["results"], result
    first = result["results"][0]
    assert set(first) == {"title", "section", "url", "snippet"}
    # No PURSER_DOCS_URL in the test env -> site-relative links.
    assert first["url"].startswith("/")

    (denied,) = _mcp_call(
        live_server.url, seeded_tokens["proposer"], [("search_docs", {"query": "anything"})]
    )
    assert "error" in denied

    with OrmSession(migrated_engine) as session:
        rows = list(
            session.execute(
                select(AuditLog).where(AuditLog.action == "mcp.search_docs").order_by(AuditLog.at)
            ).scalars()
        )
    ok_rows = [r for r in rows if r.detail and r.detail.get("ok") is True]
    assert ok_rows
    row = ok_rows[-1]
    assert row.kind == "read"
    assert row.entity_type == "docs"
    assert row.detail["args"]["query"] == "encryption key"
    denied_rows = [r for r in rows if r.detail and r.detail.get("ok") is False]
    assert denied_rows and "read" in (denied_rows[-1].detail.get("error") or "")


def test_tool_calls_write_audit_call_rows(
    live_server: LiveServer, seeded_tokens: dict[str, str], migrated_engine: Engine
) -> None:
    """A read tool writes one kind='read' call row with actor + clipped args;
    a scope-denied call records ok=false and the error (issue #122)."""
    from sqlalchemy import select
    from sqlalchemy.orm import Session as OrmSession

    from purser.models import AuditLog

    _mcp_call(
        live_server.url,
        seeded_tokens["agent"],
        [("list_accounts", {"include_balances": False})],
    )
    # proposer lacks read: the tool raises, MCP surfaces the error result
    (denied,) = _mcp_call(live_server.url, seeded_tokens["proposer"], [("list_accounts", {})])
    assert "error" in denied

    with OrmSession(migrated_engine) as session:
        rows = list(
            session.execute(
                select(AuditLog).where(AuditLog.action == "mcp.list_accounts").order_by(AuditLog.at)
            ).scalars()
        )
    assert len(rows) >= 2
    ok_rows = [r for r in rows if r.detail and r.detail.get("ok") is True]
    denied_rows = [r for r in rows if r.detail and r.detail.get("ok") is False]
    assert ok_rows and denied_rows
    good = ok_rows[-1]
    assert good.kind == "read"
    assert good.entity_type == "account"
    assert good.actor_token_id is not None
    assert good.request_id is not None
    assert good.detail["tool"] == "list_accounts"
    assert good.detail["args"] == {"include_balances": "False"}
    bad = denied_rows[-1]
    assert "read" in (bad.detail.get("error") or "")


# --------------------------------------------------------------------------
# Holdings tools (#390, decisions 88/89)
# --------------------------------------------------------------------------


def _mcp_raw(url: str, token: str, tool: str, args: dict[str, Any]) -> Any:
    """One call, returned whole: the content blocks matter here (C6)."""

    async def run() -> Any:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async with (
            httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http,
            streamable_http_client(f"{url}/mcp", http_client=http) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            return await session.call_tool(tool, args)

    return asyncio.run(run())


class TestHoldingsTools:
    @pytest.fixture
    def broker(self, seeded_tokens: dict[str, str], admin_client: TestClient) -> str:
        resp = admin_client.post(
            "/accounts", json={"name": "Broker", "type": "brokerage", "path": "assets.broker"}
        )
        account_id = resp.json()["id"]
        resp = admin_client.patch(f"/accounts/{account_id}", json={"holdings_source": "manual"})
        assert resp.status_code == 200, resp.text
        return account_id

    def test_an_empty_account_still_answers_with_a_text_block(
        self, broker: str, seeded_tokens: dict[str, str], live_server: LiveServer
    ) -> None:
        """C6: an empty answer must carry a content block, or a gateway drops
        structuredContent and the call fails."""
        result = _mcp_raw(
            live_server.url, seeded_tokens["reader"], "list_holdings", {"account": "assets.broker"}
        )
        assert not result.is_error
        assert result.content and result.content[0].type == "text"
        assert "holds no positions" in result.content[0].text
        assert result.structured_content == {
            "accounts": [
                {
                    "account": "assets.broker",
                    "source": "manual",
                    "as_of": None,
                    "market_value": "0.00",
                    "positions": 0,
                    "stale_price": 0,
                    "no_price": 0,
                    "value_out_of_range": 0,
                    "unpriceable": 0,
                    "top": [],
                }
            ],
            "totals": {"market_value": "0.00"},
            "truncated": False,
            "next_cursor": None,
        }

    def test_the_source_is_the_rows_not_the_account_setting(
        self, seeded_tokens: dict[str, str], live_server: LiveServer, admin_client: TestClient
    ) -> None:
        """Just switched to manual, the account's newest set is still the
        feed's: list_holdings says where the figures come from."""
        from purser import db
        from purser.models import HoldingsSnapshot

        account_id = admin_client.post(
            "/accounts", json={"name": "Switched", "type": "brokerage", "path": "assets.switched"}
        ).json()["id"]
        with db.get_sessionmaker()() as session:
            session.add(
                HoldingsSnapshot(
                    account_id=uuid.UUID(account_id),
                    as_of=TODAY - timedelta(days=1),
                    symbol="ACME",
                    quantity=Decimal("3"),
                    market_value=Decimal("30.00"),
                    source="feed",
                )
            )
            session.commit()
        resp = admin_client.patch(f"/accounts/{account_id}", json={"holdings_source": "manual"})
        assert resp.status_code == 200, resp.text
        (listed,) = _mcp_call(
            live_server.url,
            seeded_tokens["reader"],
            [("list_holdings", {"account": "assets.switched"})],
        )
        (entry,) = listed["accounts"]
        assert entry["source"] == "feed" and entry["market_value"] == "30.00"

    def test_a_value_too_large_says_check_the_share_count(
        self, broker: str, seeded_tokens: dict[str, str], live_server: LiveServer
    ) -> None:
        """A close exists but the value does not fit an amount (a share-count
        typo): record_position and list_holdings say to check the share
        count, never that the price is missing or to record one."""
        from purser import db
        from purser.models import SecurityPrice

        symbol = f"V{uuid.uuid4().hex[:6].upper()}"
        with db.get_sessionmaker()() as session:
            session.add(
                SecurityPrice(
                    symbol=symbol, price_date=TODAY, source="manual", close=Decimal("25000")
                )
            )
            session.commit()
        result = _mcp_raw(
            live_server.url,
            seeded_tokens["operator"],
            "record_position",
            {
                "account": "assets.broker",
                "symbol": symbol,
                "quantity": "1000000000000",
                "as_of": TODAY.isoformat(),
            },
        )
        assert not result.is_error
        payload = result.structured_content
        assert payload["value_out_of_range"] is True and payload["market_value"] is None
        assert payload["price"] == "25000"
        assert "check the share count" in payload["warning"]
        for text in (payload["warning"], result.content[0].text):
            assert "record_price" not in text and "no price" not in text
        listed = _mcp_raw(
            live_server.url, seeded_tokens["reader"], "list_holdings", {"account": "assets.broker"}
        )
        (entry,) = listed.structured_content["accounts"]
        assert entry["value_out_of_range"] == 1 and entry["no_price"] == 0
        (line,) = [p for p in entry["top"] if p["symbol"] == symbol]
        assert line["value_out_of_range"] is True
        assert "check the share count" in listed.content[0].text
        assert "no price" not in listed.content[0].text

    def test_record_position_needs_write_and_answers_in_three_tiers(
        self, broker: str, seeded_tokens: dict[str, str], live_server: LiveServer
    ) -> None:
        symbol = f"M{uuid.uuid4().hex[:6].upper()}"
        args = {
            "account": "assets.broker",
            "symbol": symbol,
            "quantity": "412.5",
            "as_of": TODAY.isoformat(),
            "source_note": "broker holdings page",
        }
        (denied,) = _mcp_call(live_server.url, seeded_tokens["reader"], [("record_position", args)])
        assert "requires scope: write" in denied["error"]  # a tool error, never a draft
        result = _mcp_raw(live_server.url, seeded_tokens["operator"], "record_position", args)
        assert not result.is_error
        payload = result.structured_content
        assert payload["created"] is True and payload["quantity"] == "412.5"
        assert payload["market_value"] is None and "no price yet" in payload["warning"]
        assert "no price yet" in result.content[0].text

        (priced,) = _mcp_call(
            live_server.url,
            seeded_tokens["operator"],
            [
                (
                    "record_price",
                    {"symbol": symbol.lower(), "date": TODAY.isoformat(), "close": "10"},
                )
            ],
        )
        assert priced["accounts_revalued"] == 1 and priced["symbol"] == symbol
        (listed,) = _mcp_call(live_server.url, seeded_tokens["reader"], [("list_holdings", {})])
        (entry,) = [a for a in listed["accounts"] if a["account"] == "assets.broker"]
        assert entry["market_value"] == "4125.00" and entry["no_price"] == 0
        assert entry["top"] == [
            {
                "symbol": symbol,
                "quantity": "412.5",
                "market_value": "4125.00",
                "price": "10",
                "price_as_of": TODAY.isoformat(),
            }
        ]

    def test_a_sold_out_position_is_never_on_a_stale_close(
        self, broker: str, seeded_tokens: dict[str, str], live_server: LiveServer
    ) -> None:
        """Zero shares are valued on nothing: their old close is not stale,
        and the answer does not say it is."""
        symbol = f"Z{uuid.uuid4().hex[:6].upper()}"
        old = (TODAY - timedelta(days=30)).isoformat()
        _mcp_call(
            live_server.url,
            seeded_tokens["operator"],
            [
                (
                    "record_position",
                    {"account": "assets.broker", "symbol": symbol, "quantity": "4", "as_of": old},
                ),
                ("record_price", {"symbol": symbol, "date": old, "close": "10"}),
            ],
        )
        result = _mcp_raw(
            live_server.url,
            seeded_tokens["operator"],
            "record_position",
            {
                "account": "assets.broker",
                "symbol": symbol,
                "quantity": "0",
                "as_of": TODAY.isoformat(),
            },
        )
        assert not result.is_error
        assert result.structured_content["stale_price"] is False
        assert "stale" not in result.content[0].text

    def test_forty_symbols_stay_inside_the_budget(
        self, broker: str, seeded_tokens: dict[str, str], live_server: LiveServer
    ) -> None:
        from purser import db
        from purser.holdings import prices as price_service
        from purser.holdings import service as holdings_service

        with db.get_sessionmaker()() as session:
            for n in range(40):
                symbol = f"B{n:02d}{uuid.uuid4().hex[:5].upper()}"
                holdings_service.record_position(
                    session,
                    account_ref="assets.broker",
                    symbol=symbol,
                    quantity=Decimal(f"{n + 1}.123456"),
                    as_of=TODAY,
                    cost_basis=Decimal("100.00"),
                    token=None,
                )
                price_service.record_price(
                    session,
                    symbol=symbol,
                    price_date=TODAY - timedelta(days=n % 12),
                    close=Decimal(f"{n + 10}.654321"),
                    token=None,
                )
            session.commit()
        result = _mcp_raw(live_server.url, seeded_tokens["reader"], "list_holdings", {})
        payload = result.structured_content
        assert len(json.dumps(payload)) / 4 < 500, len(json.dumps(payload))
        (entry,) = payload["accounts"]
        assert entry["positions"] == 40
        assert payload["truncated"] is True and 0 < len(entry["top"]) < 40
        # The largest positions are the ones listed.
        values = [Decimal(p["market_value"]) for p in entry["top"]]
        assert values == sorted(values, reverse=True)
        assert entry["stale_price"] > 0
        assert result.content[0].type == "text"

        # One named account is complete through paging, and every page stays
        # inside the budget too (guardrail 6). Each row carries its basis.
        seen: list[dict] = []
        cursor: str | None = None
        pages = 0
        while pages == 0 or cursor is not None:
            args = {"account": "assets.broker"} | ({"cursor": cursor} if cursor else {})
            result = _mcp_raw(live_server.url, seeded_tokens["reader"], "list_holdings", args)
            payload = result.structured_content
            assert len(json.dumps(payload)) / 4 < 500, len(json.dumps(payload))
            (entry,) = payload["accounts"]
            assert entry["top"], "a page always lists something while positions remain"
            assert payload["truncated"] is (payload["next_cursor"] is not None)
            assert payload["totals"] == {"market_value": entry["market_value"]}
            seen.extend(entry["top"])
            cursor = payload["next_cursor"]
            pages += 1
            if pages == 1:
                # A write between two pages (the agent corrects the first
                # page's close): keyset paging neither repeats nor skips.
                with db.get_sessionmaker()() as session:
                    price_service.record_price(
                        session,
                        symbol=entry["top"][0]["symbol"],
                        price_date=TODAY,
                        close=Decimal("99999.5"),
                        token=None,
                    )
                    session.commit()
        assert pages > 1 and len(seen) == 40
        assert len({p["symbol"] for p in seen}) == 40  # each position exactly once
        assert {p["cost_basis"] for p in seen} == {"100.00"}
        symbols = [p["symbol"] for p in seen]
        assert symbols == sorted(symbols)  # symbol order, across pages
        assert (
            "call again with cursor"
            in _mcp_raw(
                live_server.url,
                seeded_tokens["reader"],
                "list_holdings",
                {"account": "assets.broker"},
            )
            .content[0]
            .text
        )

    def test_paging_cursors_are_checked(
        self, broker: str, seeded_tokens: dict[str, str], live_server: LiveServer
    ) -> None:
        """A cursor without an account is a tool error; a cursor past the
        last symbol is an empty last page."""
        result = _mcp_raw(
            live_server.url, seeded_tokens["reader"], "list_holdings", {"cursor": "ACME"}
        )
        assert result.is_error
        result = _mcp_raw(
            live_server.url,
            seeded_tokens["reader"],
            "list_holdings",
            {"account": "assets.broker", "cursor": "ZZZZ"},
        )
        assert not result.is_error
        payload = result.structured_content
        assert payload["accounts"][0]["top"] == [] and payload["next_cursor"] is None

    def test_a_named_closed_account_is_marked_and_left_out_of_totals(
        self, seeded_tokens: dict[str, str], live_server: LiveServer, admin_client: TestClient
    ) -> None:
        """A closed account's last set no longer counts in net worth, so the
        single-account answer says it is closed and leaves it out of totals."""
        from purser import db
        from purser.models import HoldingsSnapshot

        account_id = admin_client.post(
            "/accounts", json={"name": "Old Plan", "type": "brokerage", "path": "assets.oldplan"}
        ).json()["id"]
        with db.get_sessionmaker()() as session:
            session.add(
                HoldingsSnapshot(
                    account_id=uuid.UUID(account_id),
                    as_of=TODAY - timedelta(days=3),
                    symbol="ACME",
                    quantity=Decimal("10"),
                    market_value=Decimal("1234.5600"),
                    source="feed",
                )
            )
            session.commit()
        resp = admin_client.post(f"/accounts/{account_id}/close", json={})
        assert resp.status_code == 200, resp.text
        result = _mcp_raw(
            live_server.url,
            seeded_tokens["reader"],
            "list_holdings",
            {"account": "assets.oldplan"},
        )
        assert not result.is_error
        payload = result.structured_content
        (entry,) = payload["accounts"]
        assert entry["closed"] is True and entry["market_value"] == "1234.56"
        assert payload["totals"] == {"market_value": "0.00"}
        assert "closed" in result.content[0].text
