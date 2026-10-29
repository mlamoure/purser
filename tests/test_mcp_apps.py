"""MCP Apps presentation layer (issue #72): ui:// widget resources and
purser:// full-series resources over real client sessions."""

import asyncio
import json
import string
import uuid
from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx2
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from mcp.shared.exceptions import MCPError as McpError
from sqlalchemy import Engine

from tests.test_mcp import (  # noqa: F401 (fixture re-export)
    TODAY,
    LiveServer,
    _mcp_call,
    seeded_tokens,
)

WIDGET_MIME = "text/html;profile=mcp-app"


@pytest.fixture(scope="module")
def live_server(migrated_engine: Engine) -> Iterator[LiveServer]:
    server = LiveServer()
    server.start()
    yield server
    server.stop()


@pytest.fixture
def widget_bundles(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """Stub self-contained bundles in a temp PURSER_WIDGETS_DIR."""
    from purser.config import get_settings
    from purser.mcp.presentation import WIDGETS

    for name in WIDGETS:
        (tmp_path / f"{name}.html").write_text(
            f'<!doctype html><html><body data-widget="{name}">stub</body></html>',
            encoding="utf-8",
        )
    monkeypatch.setenv("PURSER_WIDGETS_DIR", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def _leaf_exceptions(exc: BaseException) -> list[BaseException]:
    if isinstance(exc, BaseExceptionGroup):
        return [leaf for sub in exc.exceptions for leaf in _leaf_exceptions(sub)]
    return [exc]


def _read_resource(url: str, token: str, uri: str) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async with (
            httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http,
            streamable_http_client(f"{url}/mcp", http_client=http) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            result = await session.read_resource(uri)
            content = result.contents[0]
            return {"mime": content.mime_type, "text": content.text}  # type: ignore[union-attr]

    try:
        return asyncio.run(run())
    except BaseExceptionGroup as eg:
        # The client's task group wraps request errors; surface the McpError
        # itself so tests can assert on the server-side message.
        mcp_errors = [e for e in _leaf_exceptions(eg) if isinstance(e, McpError)]
        if mcp_errors:
            raise mcp_errors[0] from eg
        raise


def _list_resources(url: str, token: str) -> list[str]:
    async def run() -> list[str]:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async with (
            httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http,
            streamable_http_client(f"{url}/mcp", http_client=http) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            listed = await session.list_resources()
            return [str(r.uri) for r in listed.resources]

    return asyncio.run(run())


class TestWidgetResources:
    def test_all_widgets_listed_and_served(
        self,
        seeded_tokens: dict[str, str],  # noqa: F811
        live_server: LiveServer,
        widget_bundles: Path,
    ) -> None:
        from purser.mcp.presentation import WIDGETS

        uris = _list_resources(live_server.url, seeded_tokens["reader"])
        for name in WIDGETS:
            assert f"ui://purser/{name}" in uris
        got = _read_resource(live_server.url, seeded_tokens["reader"], "ui://purser/forecast")
        assert got["mime"] == WIDGET_MIME
        assert 'data-widget="forecast"' in got["text"]

    def test_widget_read_requires_read_scope(
        self,
        seeded_tokens: dict[str, str],  # noqa: F811
        live_server: LiveServer,
        widget_bundles: Path,
    ) -> None:
        with pytest.raises(McpError, match="read"):
            _read_resource(live_server.url, seeded_tokens["proposer"], "ui://purser/forecast")

    def test_unauthenticated_rejected(
        self,
        seeded_tokens: dict[str, str],  # noqa: F811
        live_server: LiveServer,
        widget_bundles: Path,
    ) -> None:
        with pytest.raises((RuntimeError, ExceptionGroup, McpError)):
            _read_resource(live_server.url, "purser_not_a_real_token", "ui://purser/forecast")

    def test_missing_bundle_is_a_clear_error(
        self,
        seeded_tokens: dict[str, str],  # noqa: F811
        live_server: LiveServer,
        widget_bundles: Path,
    ) -> None:
        (widget_bundles / "forecast.html").unlink()
        with pytest.raises(McpError, match=r"forecast\.html"):
            _read_resource(live_server.url, seeded_tokens["reader"], "ui://purser/forecast")


class TestSeriesResources:
    def test_forecast_daily_full_series(
        self,
        seeded_tokens: dict[str, str],  # noqa: F811
        live_server: LiveServer,
    ) -> None:
        got = _read_resource(
            live_server.url,
            seeded_tokens["reader"],
            "purser://forecast/assets.checking/30d",
        )
        assert got["mime"] == "application/json"
        payload = json.loads(got["text"])
        assert payload["account"] == "assets.checking"
        assert payload["horizon_days"] == 30
        assert len(payload["days"]) == 31  # day 0 through day 30, one entry per day
        Decimal(payload["start_balance"])  # decimal strings end-to-end
        assert payload["days"][0]["balance"] == payload["start_balance"]

    def test_forecast_daily_bad_horizon_rejected(
        self,
        seeded_tokens: dict[str, str],  # noqa: F811
        live_server: LiveServer,
    ) -> None:
        with pytest.raises(McpError):
            _read_resource(
                live_server.url,
                seeded_tokens["reader"],
                "purser://forecast/assets.checking/9999d",
            )

    def test_net_worth_daily_series(
        self,
        seeded_tokens: dict[str, str],  # noqa: F811
        live_server: LiveServer,
    ) -> None:
        got = _read_resource(live_server.url, seeded_tokens["reader"], "purser://net-worth/daily")
        payload = json.loads(got["text"])
        assert payload["days"], "seeded ledger must produce a series"
        last = payload["days"][-1]
        assert last["date"] == payload["end"]
        # decimal strings, and the identity assets - liabilities = net holds
        assert Decimal(last["assets"]) - Decimal(last["liabilities"]) == Decimal(last["net"])

    def test_series_resources_require_read_scope(
        self,
        seeded_tokens: dict[str, str],  # noqa: F811
        live_server: LiveServer,
    ) -> None:
        with pytest.raises(McpError, match="read"):
            _read_resource(live_server.url, seeded_tokens["proposer"], "purser://net-worth/daily")


class TestReadSeries:
    """#354 (decision 77): read_series serves the purser:// bodies as a tool,
    for clients and gateways that forward tools but not resources."""

    @pytest.fixture
    def uris(
        self,
        seeded_tokens: dict[str, str],  # noqa: F811
        client: Any,
    ) -> list[str]:
        resp = client.post(
            "/schedules",
            json={
                "name": "Netflix",
                "account": "assets.checking",
                "rrule": "FREQ=WEEKLY",
                "amount": "-17.99",
                "amount_type": "fixed",
                "payee": "Netflix",
            },
        )
        assert resp.status_code == 201, resp.text
        start, end = TODAY - timedelta(days=60), TODAY
        return [
            "purser://forecast/assets.checking/90d",
            "purser://net-worth/daily",
            "purser://balance-history/assets.checking/90d",
            f"purser://balance-history/assets.checking/{start.isoformat()}_{end.isoformat()}",
        ]

    def test_byte_identical_to_resources_read(
        self,
        uris: list[str],
        seeded_tokens: dict[str, str],  # noqa: F811
        live_server: LiveServer,
    ) -> None:
        for uri in uris:
            via_tool = _mcp_call_raw(
                live_server.url, seeded_tokens["reader"], "read_series", {"uri": uri}
            )
            via_resource = _read_resource(live_server.url, seeded_tokens["reader"], uri)
            assert via_tool["text"] == via_resource["text"], uri
            assert via_tool["structured"] is None  # never sent twice
            json.loads(via_tool["text"])

    def test_refuses_anything_but_purser_series(
        self,
        seeded_tokens: dict[str, str],  # noqa: F811
        live_server: LiveServer,
    ) -> None:
        refused = _mcp_call(
            live_server.url,
            seeded_tokens["reader"],
            [
                ("read_series", {"uri": "ui://purser/forecast"}),
                ("read_series", {"uri": "https://example.com/series.json"}),
            ],
        )
        assert all("purser:// series only" in r["error"] for r in refused)

    def test_needs_read_scope_and_writes_an_audit_row(
        self,
        seeded_tokens: dict[str, str],  # noqa: F811
        live_server: LiveServer,
        client: Any,
    ) -> None:
        (denied,) = _mcp_call(
            live_server.url,
            seeded_tokens["proposer"],
            [("read_series", {"uri": "purser://net-worth/daily"})],
        )
        assert "read" in denied["error"]
        _mcp_call_raw(
            live_server.url,
            seeded_tokens["reader"],
            "read_series",
            {"uri": "purser://net-worth/daily"},
        )
        rows = client.get("/audit", params={"action": "mcp.read_series"}).json()["items"]
        assert {row["detail"]["ok"] for row in rows} == {True, False}

    def test_refuses_a_body_over_the_ceiling(
        self,
        seeded_tokens: dict[str, str],  # noqa: F811
        live_server: LiveServer,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from purser.mcp import resources

        monkeypatch.setattr(resources, "_READ_SERIES_MAX_CHARS", 200)
        (refused,) = _mcp_call(
            live_server.url,
            seeded_tokens["reader"],
            [("read_series", {"uri": "purser://forecast/assets.checking/90d"})],
        )
        assert "ceiling" in refused["error"] and "narrow the window" in refused["error"]

    def test_a_full_year_forecast_fits_under_the_ceiling(
        self,
        uris: list[str],
        seeded_tokens: dict[str, str],  # noqa: F811
        live_server: LiveServer,
    ) -> None:
        from purser.mcp.resources import _READ_SERIES_MAX_CHARS

        got = _mcp_call_raw(
            live_server.url,
            seeded_tokens["reader"],
            "read_series",
            {"uri": "purser://forecast/assets.checking/365d"},
        )
        assert len(json.loads(got["text"])["days"]) == 366
        assert len(got["text"]) < _READ_SERIES_MAX_CHARS


def _mcp_call_raw(url: str, token: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """One tool call, returning all three tiers (text, structured, _meta)."""

    async def run() -> dict[str, Any]:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async with (
            httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http,
            streamable_http_client(f"{url}/mcp", http_client=http) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            result = await session.call_tool(tool, args)
            assert not result.is_error, result.content
            return {
                "text": result.content[0].text,  # type: ignore[union-attr]
                "structured": result.structured_content,
                "meta": result.meta,
            }

    return asyncio.run(run())


def _tool_meta(url: str, token: str, tool_name: str) -> dict[str, Any] | None:
    async def run() -> dict[str, Any] | None:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async with (
            httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http,
            streamable_http_client(f"{url}/mcp", http_client=http) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            tools = await session.list_tools()
            tool = next(t for t in tools.tools if t.name == tool_name)
            return tool.meta

    return asyncio.run(run())


class TestForecastPresentation:
    """Issue #73: forecast_balance three-tier reshape + daily series parity."""

    @pytest.fixture
    def scheduled(self, seeded_tokens: dict[str, str], client: Any) -> dict[str, str]:  # noqa: F811
        """Add expected activity: a weekly bill and a monthly payroll."""
        for body in [
            {
                "name": "Netflix",
                "account": "assets.checking",
                "rrule": "FREQ=WEEKLY",
                "amount": "-17.99",
                "amount_type": "fixed",
                "payee": "Netflix",
            },
            {
                "name": "Payroll",
                "account": "assets.checking",
                "rrule": "FREQ=MONTHLY",
                "amount": "3100.00",
                "amount_type": "fixed",
                "payee": "Employer Inc",
            },
        ]:
            resp = client.post("/schedules", json=body)
            assert resp.status_code == 201, resp.text
        return seeded_tokens

    def test_declaration_carries_widget_meta(
        self,
        seeded_tokens: dict[str, str],  # noqa: F811
        live_server: LiveServer,
    ) -> None:
        meta = _tool_meta(live_server.url, seeded_tokens["reader"], "forecast_balance")
        assert meta is not None
        assert meta["ui"]["resourceUri"] == "ui://purser/forecast"

    def test_three_tiers_and_contract_shape(
        self, scheduled: dict[str, str], live_server: LiveServer
    ) -> None:
        got = _mcp_call_raw(
            live_server.url,
            scheduled["reader"],
            "forecast_balance",
            {"account": "assets.checking", "days": 30},
        )
        # tier 1: a prose headline, not a JSON dump
        assert got["text"] and not got["text"].lstrip().startswith("{")
        # tier 3: per-result widget pointer
        assert got["meta"]["ui"]["resourceUri"] == "ui://purser/forecast"
        # tier 2: the contract payload
        data = got["structured"]
        assert data["account"] == "assets.checking"
        assert data["horizon_days"] == 30
        assert data["truncated"] is False
        assert data["daily_uri"] == "purser://forecast/assets.checking/30d"
        assert {"date", "name", "amount", "balance_after", "transaction_id"} <= set(
            data["events"][0]
        )

    def test_events_chain_and_daily_series_agree(
        self, scheduled: dict[str, str], live_server: LiveServer
    ) -> None:
        """Every dip in the rendered line corresponds to an event: the
        balance_after chain reconstructs from start_balance, the projected
        low equals the daily-series minimum, and every daily balance change
        happens on an event date."""
        got = _mcp_call_raw(
            live_server.url,
            scheduled["reader"],
            "forecast_balance",
            {"account": "assets.checking", "days": 30},
        )
        data = got["structured"]
        assert data["events"], "expected instances must produce events"

        running = Decimal(data["start_balance"])
        for event in data["events"]:
            running += Decimal(event["amount"])
            assert Decimal(event["balance_after"]) == running

        daily = json.loads(
            _read_resource(live_server.url, scheduled["reader"], data["daily_uri"])["text"]
        )
        assert daily["days"][0]["date"] == data["as_of"]
        series_low = min(Decimal(d["balance"]) for d in daily["days"])
        assert Decimal(data["low"]["balance"]) == series_low

        event_dates = {e["date"] for e in data["events"]}
        previous = Decimal(daily["days"][0]["balance"])
        for day in daily["days"][1:]:
            balance = Decimal(day["balance"])
            if balance != previous:
                assert day["date"] in event_dates, f"unexplained move on {day['date']}"
            previous = balance

    def test_budget_fit_keeps_low_and_accounts_for_the_rest(
        self, scheduled: dict[str, str], live_server: LiveServer, client: Any
    ) -> None:
        """#354 (decision 77): no count cap — the events that fit the budget,
        the low's always among them, the rest in omitted, and end_balance
        closing the sum."""
        resp = client.post(
            "/schedules",
            json={
                "name": "Coffee",
                "account": "assets.checking",
                "rrule": "FREQ=DAILY",
                "amount": "-4.50",
                "amount_type": "fixed",
                "payee": "Corner Cafe",
            },
        )
        assert resp.status_code == 201, resp.text
        got = _mcp_call_raw(
            live_server.url,
            scheduled["reader"],
            "forecast_balance",
            {"account": "assets.checking", "days": 90},
        )
        data = got["structured"]
        assert data["truncated"] is True
        assert len(json.dumps(data)) <= 1900  # the fitted payload's budget
        assert len(json.dumps(data)) / 4 < 500  # amendment 1 (#73): token budget
        daily = json.loads(
            _read_resource(live_server.url, scheduled["reader"], data["daily_uri"])["text"]
        )
        every = [e for d in daily["days"] for e in d["events"]]
        assert data["omitted"]["count"] == len(every) - len(data["events"])
        # the biggest movements survive the fit, in date order
        assert any(e["name"] == "Payroll" for e in data["events"])
        assert [e["date"] for e in data["events"]] == sorted(e["date"] for e in data["events"])
        # low is computed over ALL days, and its event is listed
        assert Decimal(data["low"]["balance"]) == min(Decimal(d["balance"]) for d in daily["days"])
        assert any(
            e["date"] == data["low"]["date"] and e["balance_after"] == data["low"]["balance"]
            for e in data["events"]
        )
        # nothing is lost: start + listed + omitted.net == end, which is the
        # daily series' last day and the resource's end_balance
        listed = sum((Decimal(e["amount"]) for e in data["events"]), Decimal(0))
        end = Decimal(data["start_balance"]) + listed + Decimal(data["omitted"]["net"])
        assert end == Decimal(data["end_balance"]) == Decimal(daily["end_balance"])
        assert daily["low"]["date"] == data["low"]["date"]
        assert (
            f"{data['omitted']['count']} more movement(s) (net {data['omitted']['net']}) are "
            "not itemized; read_series(daily_uri) has every day." in got["text"]
        )

    def test_materiality_lists_only_large_movements(
        self, scheduled: dict[str, str], live_server: LiveServer
    ) -> None:
        got = _mcp_call_raw(
            live_server.url,
            scheduled["reader"],
            "forecast_balance",
            {"account": "assets.checking", "days": 60, "materiality": "100.00"},
        )
        data = got["structured"]
        low = data["low"]
        kept = [
            e
            for e in data["events"]
            if not (e["date"] == low["date"] and e["balance_after"] == low["balance"])
        ]
        assert kept and all(abs(Decimal(e["amount"])) >= 100 for e in kept)
        # Netflix is below the bar: omitted, but materiality is not truncation
        assert data["omitted"]["count"] >= 1
        assert data["truncated"] is False
        assert "not itemized" not in got["text"]
        (refused,) = _mcp_call(
            live_server.url,
            scheduled["reader"],
            [("forecast_balance", {"account": "assets.checking", "materiality": "lots"})],
        )
        assert "materiality" in refused["error"]

    def test_budget_holds_with_long_names_and_every_optional_key(
        self,
        seeded_tokens: dict[str, str],  # noqa: F811
        live_server: LiveServer,
        client: Any,
        migrated_engine: Engine,
    ) -> None:
        """The realistic worst case the old cap only passed by luck: 60-character
        names, projected and overdue flags, a pending match net in coverage, a
        floor, and a Grafana link — still inside the budget."""
        from tests.test_schedules import _import_row, _propose

        assert client.put(
            "/config", json={"grafana_url": "https://grafana.example.com/"}
        ).status_code in (200, 204)
        names = [
            "Northeastern Regional Electric Utility Monthly Service Charge",
            "Metropolitan Water and Sewer Authority Quarterly Billing Cycle",
            "Platinum Rewards Travel Credit Card Statement Balance Payment",
            "Employer Payroll Direct Deposit Semi-Monthly Net Compensation",
        ]
        for i, name in enumerate(names):
            assert len(name) >= 60
            resp = client.post(
                "/schedules",
                json={
                    "name": name,
                    "account": "assets.checking",
                    "rrule": "FREQ=WEEKLY",
                    "amount": "2400.00" if i == 3 else f"-{100 + i * 37}.25",
                    "amount_type": "fixed",
                    "payee": name,
                },
            )
            assert resp.status_code == 201, resp.text
        overdue = client.post(
            "/transactions",
            json={
                "account": "assets.checking",
                "category": "expenses.groceries",
                "amount": "-75.00",
                "date": (TODAY - timedelta(days=3)).isoformat(),
                "payee": "Overdue Synthetic Bill With A Rather Long Descriptive Name",
                "status": "expected",
            },
        )
        assert overdue.status_code == 201, overdue.text
        settled = client.post(
            "/transactions",
            json={
                "account": "assets.checking",
                "category": "expenses.groceries",
                "amount": "-918.56",
                "date": (TODAY - timedelta(days=2)).isoformat(),
                "payee": "Proposed Synthetic Bill",
                "status": "expected",
            },
        ).json()["id"]
        bank = _import_row(
            migrated_engine,
            account_path="assets.checking",
            amount="-918.56",
            on=TODAY - timedelta(days=4),
            payee="Synthetic Bank Line",
            description="SYNTHETIC BANK LINE",
        )
        _propose(migrated_engine, settled, bank)
        try:
            got = _mcp_call_raw(
                live_server.url,
                seeded_tokens["reader"],
                "forecast_balance",
                {"account": "assets.checking", "days": 365},
            )
        finally:
            client.put("/config", json={"grafana_url": None})
        data = got["structured"]
        assert data["links"]["grafana"].startswith("https://grafana.example.com/")
        assert data["coverage"]["matches_pending_net"] == "-918.56"
        assert data["truncated"] is True and data["omitted"]["count"] > 0
        assert len(json.dumps(data)) <= 1900
        assert len(json.dumps(data)) / 4 < 500

    @pytest.mark.parametrize("days", [1, 30, 365])
    def test_every_accepted_horizon_hands_out_a_resolving_uri(
        self,
        days: int,
        seeded_tokens: dict[str, str],  # noqa: F811
        live_server: LiveServer,
    ) -> None:
        got = _mcp_call_raw(
            live_server.url,
            seeded_tokens["reader"],
            "forecast_balance",
            {"account": "assets.checking", "days": days},
        )
        uri = got["structured"]["daily_uri"]
        assert uri == f"purser://forecast/assets.checking/{days}d"
        daily = json.loads(_read_resource(live_server.url, seeded_tokens["reader"], uri)["text"])
        assert len(daily["days"]) == days + 1

    @pytest.mark.parametrize("days", [0, -1, 366])
    def test_out_of_range_horizons_are_refused_everywhere(
        self,
        days: int,
        seeded_tokens: dict[str, str],  # noqa: F811
        live_server: LiveServer,
        client: Any,
    ) -> None:
        """One horizon check (schedules.validate_horizon) for the tool, the
        resource and REST: nothing the tool accepts is refused by the
        resource, and nothing either refuses crashes."""
        (tool,) = _mcp_call(
            live_server.url,
            seeded_tokens["reader"],
            [("forecast_balance", {"account": "assets.checking", "days": days})],
        )
        assert "days must be 1..365" in tool["error"]
        with pytest.raises(McpError, match=r"1\.\.365"):
            _read_resource(
                live_server.url,
                seeded_tokens["reader"],
                f"purser://forecast/assets.checking/{days}d",
            )
        rest = client.get("/forecast", params={"account": "assets.checking", "days": days})
        assert rest.status_code == 422


class TestForecastMatchesPending:
    """#352 (decision 76) on the MCP surfaces: the left-out instance is
    counted in coverage and named in the headline, the net crosses every
    surface as a decimal string, and match() on the proposed pair settles it."""

    @pytest.fixture
    def proposed(
        self,
        seeded_tokens: dict[str, str],  # noqa: F811
        client: Any,
        migrated_engine: Engine,
    ) -> dict[str, str]:
        from tests.test_schedules import _import_row, _propose

        resp = client.post(
            "/transactions",
            json={
                "account": "assets.checking",
                "category": "expenses.groceries",
                "amount": "-200.00",
                "date": (TODAY - timedelta(days=4)).isoformat(),
                "payee": "JP Cleaning",
                "status": "expected",
            },
        )
        assert resp.status_code == 201, resp.text
        expected_id = resp.json()["id"]
        posted_id = _import_row(
            migrated_engine,
            account_path="assets.checking",
            amount="-200.00",
            on=TODAY - timedelta(days=7),
            payee="Cleaning Service Bill O",
            description="CLEANING SERVICE BILL O",
            origin="system",  # in the review scope, so the confirm's mark is visible
        )
        _propose(migrated_engine, expected_id, posted_id)
        # A late bill with no proposal: its list_unmatched row omits the key.
        other = client.post(
            "/transactions",
            json={
                "account": "assets.checking",
                "category": "expenses.groceries",
                "amount": "-40.00",
                "date": (TODAY - timedelta(days=2)).isoformat(),
                "payee": "Window Washer",
                "status": "expected",
            },
        )
        assert other.status_code == 201, other.text
        return {
            **seeded_tokens,
            "expected_id": expected_id,
            "posted_id": posted_id,
            "other_id": other.json()["id"],
        }

    def test_coverage_headline_and_resource(
        self, proposed: dict[str, str], live_server: LiveServer
    ) -> None:
        got = _mcp_call_raw(
            live_server.url,
            proposed["reader"],
            "forecast_balance",
            {"account": "assets.checking", "days": 30},
        )
        data = got["structured"]
        assert data["coverage"]["matches_pending"] == 1
        assert data["coverage"]["matches_pending_net"] == "-200.00"
        assert all(e["transaction_id"] != proposed["expected_id"] for e in data["events"])
        assert (
            "1 instance(s) (net -200.00) are left out as paid, pending confirmation of a "
            "proposed match (list_unmatched gives each one's proposed_posted_id; match "
            "confirms it)." in got["text"]
        )
        daily = json.loads(
            _read_resource(live_server.url, proposed["reader"], data["daily_uri"])["text"]
        )
        assert daily["coverage"]["matches_pending"] == 1
        assert Decimal(daily["coverage"]["matches_pending_net"]) == Decimal("-200.00")

    def test_list_unmatched_names_the_pair_and_match_confirms_it(
        self, proposed: dict[str, str], live_server: LiveServer, migrated_engine: Engine
    ) -> None:
        """The agent's whole path, over MCP only: list_unmatched names the
        proposal's bank row, match on that pair confirms the proposal (the
        bank row counts as reviewed, via='match'), and the instance leaves
        coverage.matches_pending."""
        from sqlalchemy import text

        (listed,) = _mcp_call(
            live_server.url, proposed["reader"], [("list_unmatched", {"direction": "expected"})]
        )
        rows = {r["transaction_id"]: r for r in listed["result"]}
        late = rows[proposed["expected_id"]]
        assert late["has_proposed_match"] is True
        assert late["proposed_posted_id"] == proposed["posted_id"]
        quiet = rows[proposed["other_id"]]
        assert quiet["has_proposed_match"] is False
        assert "proposed_posted_id" not in quiet  # present only when set (guardrail 6)
        assert "proposal_id" not in quiet

        with migrated_engine.connect() as conn:
            proposal_id = str(
                conn.execute(
                    text("SELECT id FROM matches WHERE expected_id = :e AND state = 'proposed'"),
                    {"e": proposed["expected_id"]},
                ).scalar_one()
            )
        assert late["proposal_id"] == proposal_id  # decision 86
        (matched,) = _mcp_call(
            live_server.url,
            proposed["operator"],
            [
                (
                    "match",
                    {
                        "expected_id": late["transaction_id"],
                        "posted_id": late["proposed_posted_id"],
                    },
                )
            ],
        )
        assert matched["state"] == "confirmed", matched
        assert matched["match_id"] == proposal_id  # the proposal itself, confirmed in place
        with migrated_engine.connect() as conn:
            via = conn.execute(
                text("SELECT via FROM transaction_reviews WHERE transaction_id = :t"),
                {"t": proposed["posted_id"]},
            ).scalar_one()
        assert via == "match"
        got = _mcp_call_raw(
            live_server.url,
            proposed["reader"],
            "forecast_balance",
            {"account": "assets.checking", "days": 30},
        )
        assert got["structured"]["coverage"]["matches_pending"] == 0
        (relisted,) = _mcp_call(
            live_server.url, proposed["reader"], [("list_unmatched", {"direction": "expected"})]
        )
        assert proposed["expected_id"] not in {r["transaction_id"] for r in relisted["result"]}

    def test_match_on_the_proposed_pair_settles_it(
        self, proposed: dict[str, str], live_server: LiveServer
    ) -> None:
        (matched,) = _mcp_call(
            live_server.url,
            proposed["operator"],
            [
                (
                    "match",
                    {"expected_id": proposed["expected_id"], "posted_id": proposed["posted_id"]},
                )
            ],
        )
        assert matched["state"] == "confirmed", matched
        got = _mcp_call_raw(
            live_server.url,
            proposed["reader"],
            "forecast_balance",
            {"account": "assets.checking", "days": 30},
        )
        coverage = got["structured"]["coverage"]
        assert coverage["matches_pending"] == 0
        assert "matches_pending_net" not in coverage  # only when nonzero (guardrail 6)
        assert "pending confirmation" not in got["text"]


class TestRecurringRadar:
    """Issue #74: list_recurring + price-change detection + the inbox count."""

    def test_annual_normalization_and_income_exclusion(
        self,
        seeded_tokens: dict[str, str],  # noqa: F811
        live_server: LiveServer,
        client: Any,
    ) -> None:
        for body in [
            {
                "name": "GEICO auto",
                "account": "assets.checking",
                "rrule": "FREQ=MONTHLY;INTERVAL=6",
                "amount": "-920.00",
                "amount_type": "fixed",
                "payee": "GEICO",
            },
            {
                "name": "Netflix",
                "account": "assets.checking",
                "rrule": "FREQ=MONTHLY",
                "amount": "-17.99",
                "amount_type": "fixed",
                "payee": "Netflix",
            },
            {
                "name": "Payroll",
                "account": "assets.checking",
                "rrule": "FREQ=MONTHLY",
                "amount": "3100.00",
                "amount_type": "fixed",
                "payee": "Employer Inc",
            },
        ]:
            assert client.post("/schedules", json=body).status_code == 201

        got = _mcp_call_raw(live_server.url, seeded_tokens["reader"], "list_recurring", {})
        data = got["structured"]
        assert got["meta"]["ui"]["resourceUri"] == "ui://purser/recurring"
        by_payee = {i["payee"]: i for i in data["items"]}
        # semi-annual bill normalized to an honest monthly figure
        assert by_payee["GEICO"]["monthly_equiv"] == "153.33"
        assert by_payee["GEICO"]["cadence"] == "every 6 months"
        assert by_payee["Netflix"]["monthly_equiv"] == "17.99"
        # income schedules are not "what am I paying"
        assert "Employer Inc" not in by_payee
        assert data["count"] == 2
        assert Decimal(data["monthly_total"]) == Decimal("153.33") + Decimal("17.99")
        assert Decimal(data["annualized_total"]) == (Decimal(data["monthly_total"]) * 12)
        # guardrail 6: the whole answer fits the budget
        assert len(json.dumps(data)) / 4 < 500

    def test_price_change_history_and_event(
        self,
        seeded_tokens: dict[str, str],  # noqa: F811
        live_server: LiveServer,
        client: Any,
    ) -> None:
        """15.49 -> 17.99 across two confirmed matches: last_change reports the
        correct date+delta and recurring.price_changed is emitted exactly once
        (the first match confirms the existing estimate; the second shifts it)."""
        resp = client.post(
            "/schedules",
            json={
                "name": "Netflix",
                "account": "assets.checking",
                "rrule": "FREQ=MONTHLY",
                "amount": "-15.49",
                "amount_type": "estimated",
                "estimate_basis": "last",
                "payee": "Netflix",
            },
        )
        assert resp.status_code == 201, resp.text
        expected_rows = client.get(
            "/transactions", params={"status": "expected", "payee": "Netflix"}
        ).json()["items"]
        assert len(expected_rows) >= 2, "materialization should produce upcoming instances"
        expected_rows.sort(key=lambda r: r["date"])

        last_month = (TODAY - timedelta(days=30)).isoformat()
        history = [("-15.49", last_month), ("-17.99", TODAY.isoformat())]
        for (amount, on), expected_row in zip(history, expected_rows, strict=False):
            posted = client.post(
                "/transactions",
                json={
                    "account": "assets.checking",
                    "category": "expenses.dining",
                    "amount": amount,
                    "date": on,
                    "payee": "Netflix",
                    "status": "posted",
                },
            ).json()
            matched = client.post(
                "/matching/match",
                json={"expected_id": expected_row["id"], "posted_id": posted["id"]},
            )
            assert matched.status_code == 200, matched.text

        got = _mcp_call_raw(live_server.url, seeded_tokens["reader"], "list_recurring", {})
        item = next(i for i in got["structured"]["items"] if i["payee"] == "Netflix")
        assert item["last_change"] == {"date": TODAY.isoformat(), "delta": "2.50"}
        assert item["amount"] == "17.99"  # estimate follows the confirmed actual
        assert got["structured"]["changes_12mo"]["count"] == 1
        assert Decimal(got["structured"]["changes_12mo"]["delta"]) == Decimal("2.50")

        events = client.get("/events", params={"type": "recurring.price_changed"}).json()
        assert len(events) == 1
        payload = events[0]["payload"]
        assert payload["payee"] == "Netflix"
        assert Decimal(payload["delta"]) == Decimal("2.50")
        assert Decimal(payload["new_estimate"]) == Decimal("-17.99")

    def test_detected_never_auto_activates_and_accept_is_write_gated(
        self,
        seeded_tokens: dict[str, str],  # noqa: F811
        live_server: LiveServer,
        db_session: Any,
    ) -> None:
        """A detected pattern is a SUGGESTION (#233): list_recurring counts it
        as open, never lists it as a bill, and accepting it is a write
        decision. Once accepted it is an active schedule whose source still
        says "detected" — provenance, not a pending state (#347)."""
        from datetime import date

        from purser import ledger as ledger_service
        from purser import suggestions as suggestions_service

        account = ledger_service.resolve_account(db_session, "assets.checking")
        row, created = suggestions_service.upsert_schedule_suggestion(
            db_session,
            draft=suggestions_service.ScheduleDraft(
                name="Gym",
                account="assets.checking",
                rrule="FREQ=MONTHLY;BYMONTHDAY=12",
                dtstart=date.today(),
                amount_type="fixed",
                amount=Decimal("-45.00"),
                payee="Gym",
            ),
            source="detected",
            fingerprint=suggestions_service.schedule_fingerprint(account.id, "payee:gym"),
            subject_account_id=account.id,
            subject_payee_id=None,
            counterparty_account_id=None,
            evidence=None,
            confidence=Decimal("0.7"),
            detector="test",
            detector_version=1,
            actor_token_id=None,
        )
        db_session.commit()
        assert created and row.status == "open"

        got = _mcp_call_raw(live_server.url, seeded_tokens["reader"], "list_recurring", {})
        assert got["structured"]["suggestions_open"] == 1
        assert all(i["payee"] != "Gym" for i in got["structured"]["items"])

        accept = {"suggestion_id": str(row.id), "decision": "accept"}
        denied = _mcp_call(
            live_server.url, seeded_tokens["reader"], [("decide_suggestion", accept)]
        )[0]
        assert "error" in denied and "requires scope: write" in denied["error"]

        accepted = _mcp_call(
            live_server.url, seeded_tokens["operator"], [("decide_suggestion", accept)]
        )[0]
        assert accepted["status"] == "accepted"
        assert accepted["schedule"]["status"] == "active"
        after = _mcp_call_raw(live_server.url, seeded_tokens["reader"], "list_recurring", {})
        assert after["structured"]["suggestions_open"] == 0
        (gym,) = [i for i in after["structured"]["items"] if i["payee"] == "Gym"]
        assert gym["source"] == "detected"


class TestNetWorthStory:
    """Issue #75: series mode with adaptive density + attribution."""

    def test_point_mode_keeps_legacy_shape_with_three_tiers(
        self,
        seeded_tokens: dict[str, str],  # noqa: F811
        live_server: LiveServer,
    ) -> None:
        got = _mcp_call_raw(live_server.url, seeded_tokens["reader"], "net_worth", {})
        assert got["meta"]["ui"]["resourceUri"] == "ui://purser/net-worth"
        assert got["text"] and not got["text"].lstrip().startswith("{")
        data = got["structured"]
        assert {"as_of", "net_worth", "assets", "liabilities", "breakdown"} <= set(data)

    def test_series_mode_contract_and_attribution_sums(
        self,
        seeded_tokens: dict[str, str],  # noqa: F811
        live_server: LiveServer,
    ) -> None:
        got = _mcp_call_raw(
            live_server.url, seeded_tokens["reader"], "net_worth", {"period": "90d"}
        )
        data = got["structured"]
        assert data["change"]["period"] == "90d"
        assert data["series_uri"] == "purser://net-worth/daily"
        assert 2 <= len(data["series"]) <= 20  # adaptive density, never raw daily
        # buckets sum exactly to the period delta
        total = sum(Decimal(b["delta"]) for b in data["attribution"])
        assert total == Decimal(data["change"]["delta"])
        # the current point is today's series tail
        assert data["series"][-1]["net"] == data["current"]
        assert len(json.dumps(data)) / 4 < 500

    def test_year_series_stays_inside_budget(
        self,
        seeded_tokens: dict[str, str],  # noqa: F811
        live_server: LiveServer,
    ) -> None:
        got = _mcp_call_raw(live_server.url, seeded_tokens["reader"], "net_worth", {"period": "1y"})
        data = got["structured"]
        assert len(data["series"]) <= 20
        assert len(json.dumps(data)) / 4 < 500

    def test_bad_period_rejected(
        self,
        seeded_tokens: dict[str, str],  # noqa: F811
        live_server: LiveServer,
    ) -> None:
        result = _mcp_call(
            live_server.url, seeded_tokens["reader"], [("net_worth", {"period": "5y"})]
        )[0]
        assert "error" in result

    @staticmethod
    def _all_window(url: str, token: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """The net_worth tool's `all` answer and the series its URI serves."""
        data = _mcp_call_raw(url, token, "net_worth", {"period": "all"})["structured"]
        daily = json.loads(_read_resource(url, token, data["series_uri"])["text"])
        return data, daily

    def test_the_all_window_and_its_series_agree_past_ten_years(
        self,
        seeded_tokens: dict[str, str],  # noqa: F811
        live_server: LiveServer,
        migrated_engine: Engine,
    ) -> None:
        """Issue #343: past ten years of history the tool's `all` started at
        the earliest activity while purser://net-worth/daily clamped to the
        last ten years, so the series_uri served a different series than the
        one summarized. One function resolves the window for both now, with
        the ten-year bound on both sides: a net-worth day is a full valuation,
        computed live when it is not stored."""
        from sqlalchemy import text

        from purser import networth

        # One spare day below the window: the server reads its own today,
        # which is past the module's TODAY when a run crosses midnight UTC.
        start = TODAY - timedelta(days=networth.SERIES_CAP_DAYS + 1)
        with migrated_engine.begin() as conn:
            first_tx = conn.execute(text("SELECT min(date) FROM transactions")).scalar_one()
            # Twelve years of history, stored the way the nightly job keeps it:
            # one row twelve years back makes it the earliest activity, and the
            # window's days before the first transaction are stored (all zero,
            # which is what a live read of them computes) so the resource's
            # read stays fast.
            conn.execute(
                text(
                    "INSERT INTO net_worth_daily (as_of, assets, liabilities, net_worth) "
                    "SELECT CAST(d AS date), 0, 0, 0 FROM generate_series("
                    "CAST(:first AS date), CAST(:last AS date), interval '1 day') AS d "
                    "UNION ALL SELECT CAST(:old AS date), 0, 0, 0"
                ),
                {
                    "first": start,
                    "last": first_tx - timedelta(days=1),
                    "old": TODAY - timedelta(days=12 * 365),
                },
            )

        data, daily = self._all_window(live_server.url, seeded_tokens["reader"])
        today = date.fromisoformat(daily["end"])
        window_start = (today - timedelta(days=networth.SERIES_CAP_DAYS)).isoformat()
        assert data["series"][0]["date"] == daily["start"] == window_start
        assert data["series"][-1]["date"] == daily["end"]
        assert len(daily["days"]) == networth.SERIES_CAP_DAYS + 1
        assert Decimal(daily["days"][0]["net"]) == 0
        assert Decimal(data["series"][0]["net"]) == Decimal(daily["days"][0]["net"])
        assert Decimal(data["current"]) == Decimal(daily["days"][-1]["net"])
        assert len(json.dumps(data)) / 4 < 500

    def test_the_all_window_under_ten_years_starts_at_the_first_activity(
        self,
        seeded_tokens: dict[str, str],  # noqa: F811
        live_server: LiveServer,
        migrated_engine: Engine,
    ) -> None:
        from sqlalchemy import text

        with migrated_engine.connect() as conn:
            first_tx = conn.execute(text("SELECT min(date) FROM transactions")).scalar_one()
        data, daily = self._all_window(live_server.url, seeded_tokens["reader"])
        assert data["series"][0]["date"] == daily["start"] == first_tx.isoformat()
        assert daily["days"][0]["date"] == first_tx.isoformat()
        assert Decimal(data["current"]) == Decimal(daily["days"][-1]["net"])

    def test_a_ledger_with_only_future_dated_rows_answers_for_today(
        self,
        admin_client: Any,
        live_server: LiveServer,
    ) -> None:
        """Issue #343: when every row is future-dated (a scheduled instance,
        say), the earliest activity lies after today, and both the tool and
        the resource refused with "end must be >= start". `all` never starts
        after today now, so both answer for today alone."""
        client = admin_client
        assert (
            client.post(
                "/accounts",
                json={"name": "Checking", "type": "checking", "path": "assets.checking"},
            ).status_code
            == 201
        )
        assert (
            client.post(
                "/categories",
                json={"name": "Rent", "type": "category_expense", "path": "expenses.rent"},
            ).status_code
            == 201
        )
        resp = client.post(
            "/transactions",
            json={
                "account": "assets.checking",
                "category": "expenses.rent",
                "amount": "-1200.00",
                "date": (TODAY + timedelta(days=20)).isoformat(),
                "payee": "Synthetic landlord",
                "status": "pending",
            },
        )
        assert resp.status_code == 201, resp.text
        token = client.post("/tokens", json={"name": "reader", "scopes": ["read"]}).json()["token"]

        data, daily = self._all_window(live_server.url, token)
        # The server's today (the resource's end), not the module's TODAY,
        # which a run crossing midnight UTC leaves a day behind.
        today = daily["end"]
        assert today >= TODAY.isoformat()
        assert [p["date"] for p in data["series"]] == [today]
        assert daily["start"] == today
        assert [d["date"] for d in daily["days"]] == [today]
        assert data["change"]["delta"] == "0.00"


class TestSpendingStory:
    """Issue #76: baseline mode through the MCP surface."""

    def test_baseline_mode_contract_and_budget(
        self,
        seeded_tokens: dict[str, str],  # noqa: F811
        live_server: LiveServer,
    ) -> None:
        period = TODAY.strftime("%Y-%m")
        got = _mcp_call_raw(
            live_server.url,
            seeded_tokens["reader"],
            "spending_summary",
            {"period": period, "baseline": "avg_3mo"},
        )
        assert got["meta"]["ui"]["resourceUri"] == "ui://purser/spending"
        data = got["structured"]
        assert data["period"] == period
        assert data["baseline"] == "avg_3mo"
        delta_sum = sum(Decimal(r["delta"]) for r in data["rows"])
        if not data["truncated"]:
            assert delta_sum == Decimal(data["grand_total"]) - Decimal(data["baseline_total"])
        assert len(json.dumps(data)) / 4 < 500

    def test_drill_returns_payee_rows_same_signature(
        self,
        seeded_tokens: dict[str, str],  # noqa: F811
        live_server: LiveServer,
    ) -> None:
        period = TODAY.strftime("%Y-%m")
        got = _mcp_call_raw(
            live_server.url,
            seeded_tokens["reader"],
            "spending_summary",
            {
                "period": period,
                "baseline": "avg_3mo",
                "category": "expenses.dining",
                "group_by": "payee",
            },
        )
        data = got["structured"]
        assert data["group_by"] == "payee"
        # payee keys, not category paths
        assert all("." not in r["key"] or not r["key"].startswith("expenses") for r in data["rows"])

    def test_classic_mode_keeps_shape(
        self,
        seeded_tokens: dict[str, str],  # noqa: F811
        live_server: LiveServer,
    ) -> None:
        got = _mcp_call_raw(
            live_server.url,
            seeded_tokens["reader"],
            "spending_summary",
            {"group_by": "month", "category": "expenses.dining"},
        )
        data = got["structured"]
        assert {"group_by", "filter", "grand_total", "truncated", "rows"} <= set(data)
        assert got["text"] and not got["text"].lstrip().startswith("{")

    def test_baseline_requires_period(
        self,
        seeded_tokens: dict[str, str],  # noqa: F811
        live_server: LiveServer,
    ) -> None:
        result = _mcp_call(
            live_server.url,
            seeded_tokens["reader"],
            [("spending_summary", {"baseline": "avg_3mo"})],
        )[0]
        assert "error" in result


class TestWhatsNewDigest:
    """Issue #77: the digest through the MCP surface."""

    def test_digest_shape_cursor_roundtrip_and_budget(
        self,
        seeded_tokens: dict[str, str],  # noqa: F811
        live_server: LiveServer,
        client: Any,
    ) -> None:
        got = _mcp_call_raw(live_server.url, seeded_tokens["reader"], "whats_new", {})
        assert got["meta"]["ui"]["resourceUri"] == "ui://purser/digest"
        data = got["structured"]
        assert {
            "cursor",
            "transactions",
            "matches",
            "price_changes",
            "forecast_flags",
            "upcoming",
            "sync",
            "events_truncated",
        } <= set(data)
        assert len(json.dumps(data)) / 4 < 500
        first_cursor = data["cursor"]

        # new activity lands...
        client.post(
            "/transactions",
            json={
                "account": "assets.checking",
                "category": "expenses.dining",
                "amount": "-61.15",
                "date": TODAY.isoformat(),
                "payee": "Whole Foods",
                "status": "posted",
            },
        )
        # ...and the cursor-chained call sees exactly the new material
        got2 = _mcp_call_raw(
            live_server.url,
            seeded_tokens["reader"],
            "whats_new",
            {"since_cursor": first_cursor} if first_cursor else {},
        )
        data2 = got2["structured"]
        assert data2["transactions"]["count"] >= 1
        assert any(n["payee"] == "Whole Foods" for n in data2["transactions"]["notable"])
        assert data2["cursor"] != first_cursor

    def test_balance_drift_line_names_its_basis(
        self,
        seeded_tokens: dict[str, str],  # noqa: F811
        live_server: LiveServer,
    ) -> None:
        """Issue #283: a drift line carries the basis its delta is measured on
        (present only when recorded) and the headline says it — the declared
        outputSchema accepts the optional key."""
        from purser import db, events

        with db.get_sessionmaker()() as session:
            events.emit(
                session,
                type_="balance.drift",
                entity_type="account",
                entity_id=uuid.uuid4(),
                payload={
                    "account": "liabilities.card",
                    "delta": "-20.0000",
                    "mode": "posted_only",
                    "balance_date": None,
                },
                dedup_key="card:-20.0000",
            )
            session.commit()
        got = _mcp_call_raw(live_server.url, seeded_tokens["reader"], "whats_new", {})
        (line,) = got["structured"]["balance_drifts"]
        assert line["basis"] == "posted_only"
        assert "liabilities.card balance drift -20.0000 vs bank (posted-only basis)" in got["text"]
        assert len(json.dumps(got["structured"])) / 4 < 500

    def test_balance_drift_line_says_pinned(
        self,
        seeded_tokens: dict[str, str],  # noqa: F811
        live_server: LiveServer,
    ) -> None:
        """Issue #307: a drift line on an admin-pinned basis carries
        ``pinned: true``, the declared outputSchema accepts it, and the
        headline says so."""
        from purser import db, events

        with db.get_sessionmaker()() as session:
            events.emit(
                session,
                type_="balance.drift",
                entity_type="account",
                entity_id=uuid.uuid4(),
                payload={
                    "account": "liabilities.pinned_card",
                    "delta": "-12.0000",
                    "mode": "pending_inclusive",
                    "basis_pinned": True,
                    "balance_date": None,
                },
                dedup_key="pinned-card:-12.0000",
            )
            session.commit()
        got = _mcp_call_raw(live_server.url, seeded_tokens["reader"], "whats_new", {})
        lines = {line["account"]: line for line in got["structured"]["balance_drifts"]}
        assert lines["liabilities.pinned_card"]["pinned"] is True
        assert lines["liabilities.pinned_card"]["basis"] == "pending_inclusive"
        assert (
            "liabilities.pinned_card balance drift -12.0000 vs bank "
            "(pending-inclusive basis, pinned)" in got["text"]
        )

    def test_quiet_day_compresses_to_a_sentence(
        self,
        seeded_tokens: dict[str, str],  # noqa: F811
        live_server: LiveServer,
    ) -> None:
        got = _mcp_call_raw(live_server.url, seeded_tokens["reader"], "whats_new", {})
        cursor = got["structured"]["cursor"]
        again = _mcp_call_raw(
            live_server.url,
            seeded_tokens["reader"],
            "whats_new",
            {"since_cursor": cursor} if cursor else {},
        )
        # nothing new: the headline is one short sentence the agent can speak
        assert again["text"].count(".") <= 2
        assert len(again["text"]) < 200


class TestBalanceHistoryPresentation:
    """Issues #199/#200: balance_history three tiers + resource parity."""

    def test_declaration_carries_widget_meta(
        self,
        seeded_tokens: dict[str, str],  # noqa: F811
        live_server: LiveServer,
    ) -> None:
        meta = _tool_meta(live_server.url, seeded_tokens["reader"], "balance_history")
        assert meta is not None
        assert meta["ui"]["resourceUri"] == "ui://purser/balance-history"

    def test_three_tiers_and_contract_shape(
        self,
        seeded_tokens: dict[str, str],  # noqa: F811
        live_server: LiveServer,
    ) -> None:
        got = _mcp_call_raw(
            live_server.url,
            seeded_tokens["reader"],
            "balance_history",
            {"account": "assets.checking", "period": "90d"},
        )
        # tier 1: prose, not a JSON dump
        assert got["text"] and not got["text"].lstrip().startswith("{")
        assert "assets.checking" in got["text"]
        # tier 3: per-result widget pointer
        assert got["meta"]["ui"]["resourceUri"] == "ui://purser/balance-history"
        # tier 2: the contract payload
        data = got["structured"]
        assert data["account"] == "assets.checking"
        assert data["method"] == "postings"
        assert data["change"]["period"] == "90d"
        assert data["series_uri"] == "purser://balance-history/assets.checking/90d"
        assert set(data["series"]) == {"dates", "balances"}
        assert len(data["series"]["dates"]) == len(data["series"]["balances"])
        # Density is server-chosen and REPORTED, never a caller knob.
        assert data["density"]["points"] == len(data["series"]["dates"])
        assert data["density"]["interval_days"] >= 1
        # tier 2 obeys the token budget (guardrail 6)
        assert len(json.dumps(data)) / 4 < 500

    def test_thirty_days_is_a_true_daily_series(
        self,
        seeded_tokens: dict[str, str],  # noqa: F811
        live_server: LiveServer,
    ) -> None:
        """'graph my balance over the last 30 days' must mean 30 days, not a
        downsample of them — the ask that motivated this whole round."""
        data = _mcp_call_raw(
            live_server.url,
            seeded_tokens["reader"],
            "balance_history",
            {"account": "assets.checking", "period": "30d"},
        )["structured"]
        assert data["density"]["interval_days"] == 1
        assert len(data["series"]["dates"]) == 31
        dates = data["series"]["dates"]
        assert dates == sorted(dates)
        assert data["change"]["delta"] is not None

    def test_change_is_first_to_last_and_current_is_the_last_point(
        self,
        seeded_tokens: dict[str, str],  # noqa: F811
        live_server: LiveServer,
    ) -> None:
        data = _mcp_call_raw(
            live_server.url,
            seeded_tokens["reader"],
            "balance_history",
            {"account": "assets.checking", "period": "90d"},
        )["structured"]
        balances = data["series"]["balances"]
        assert Decimal(data["current"]) == Decimal(balances[-1])
        assert Decimal(data["change"]["delta"]) == Decimal(balances[-1]) - Decimal(balances[0])

    def test_extremes_bound_the_full_resource_not_the_sample(
        self,
        seeded_tokens: dict[str, str],  # noqa: F811
        live_server: LiveServer,
    ) -> None:
        """The point of computing low/high over EVERY day rather than the
        sample: they stay true in a downsampled series that never contains the
        extreme's date. They rank end-of-day balances (decision 63) — the last
        point of each date in the full resource."""
        data = _mcp_call_raw(
            live_server.url,
            seeded_tokens["reader"],
            "balance_history",
            {"account": "assets.checking", "period": "1y"},
        )["structured"]
        full = json.loads(
            _read_resource(live_server.url, seeded_tokens["reader"], data["series_uri"])["text"]
        )
        points = full["points"]
        assert points, "fixture seeds transactions inside the year"
        # Points are oldest-first, so the last one written per date wins:
        # that is the date's end-of-day balance.
        by_date = {p["date"]: Decimal(p["balance"]) for p in points}
        assert Decimal(data["low"]["balance"]) == min(by_date.values())
        assert Decimal(data["high"]["balance"]) == max(by_date.values())
        assert data["transactions"] == len(points)
        # Every sampled point equals the value the full series implies at that
        # date: sampling SELECTS, it never invents.
        opening = Decimal(full["opening"])
        for iso, balance in zip(data["series"]["dates"], data["series"]["balances"], strict=True):
            implied = opening
            for row_iso, row_balance in sorted(by_date.items()):
                if row_iso <= iso:
                    implied = row_balance
            assert Decimal(balance) == implied

    def test_extremes_ignore_a_same_day_gross_then_deductions_spike(
        self,
        seeded_tokens: dict[str, str],  # noqa: F811
        live_server: LiveServer,
        client: Any,
    ) -> None:
        """Decision 63 (#272) on the agent surface: a gross pay leg posted
        before its same-day deductions passes 4000.00 in the per-transaction
        resource, but the day ENDS at 3150.00, and that is the reported high."""
        assert (
            client.post(
                "/accounts",
                json={"name": "Payroll", "type": "checking", "path": "assets.payroll"},
            ).status_code
            == 201
        )
        client.post(
            "/categories",
            json={"name": "Salary", "type": "category_income", "path": "income.salary"},
        )
        opened, payday = TODAY - timedelta(days=20), TODAY - timedelta(days=10)
        # Each POST commits on its own, so same-day rows keep this order.
        for category, amount, on in (
            ("income.salary", "1000.00", opened),
            ("income.salary", "3000.00", payday),
            ("expenses.groceries", "-600.00", payday),
            ("expenses.groceries", "-250.00", payday),
        ):
            resp = client.post(
                "/transactions",
                json={
                    "account": "assets.payroll",
                    "category": category,
                    "amount": amount,
                    "date": on.isoformat(),
                    "payee": "Synthetic payroll",
                    "status": "posted",
                },
            )
            assert resp.status_code == 201, resp.text

        data = _mcp_call_raw(
            live_server.url,
            seeded_tokens["reader"],
            "balance_history",
            {"account": "assets.payroll", "period": "30d"},
        )["structured"]
        full = json.loads(
            _read_resource(live_server.url, seeded_tokens["reader"], data["series_uri"])["text"]
        )
        assert max(Decimal(p["balance"]) for p in full["points"]) == Decimal("4000.00")
        assert data["high"] == {"date": payday.isoformat(), "balance": "3150.00"}
        assert data["low"] == {"date": opened.isoformat(), "balance": "1000.00"}

    def test_resource_requires_read_and_rejects_a_bad_period(
        self,
        seeded_tokens: dict[str, str],  # noqa: F811
        live_server: LiveServer,
    ) -> None:
        with pytest.raises(McpError):
            _read_resource(
                live_server.url,
                seeded_tokens["reader"],
                "purser://balance-history/assets.checking/7y",
            )

    def test_an_explicit_window_series_uri_serves_that_window(
        self,
        seeded_tokens: dict[str, str],  # noqa: F811
        live_server: LiveServer,
        client: Any,
    ) -> None:
        """Issue #289: called with start and end, the tool handed out a
        series_uri the resource refused — it served only 30d | 90d | 1y | all.
        The URI now names that exact window, and reading it returns the full
        series the tool summarized."""
        assert (
            client.post(
                "/accounts",
                json={"name": "Window", "type": "checking", "path": "assets.window"},
            ).status_code
            == 201
        )
        client.post(
            "/categories",
            json={"name": "Salary", "type": "category_income", "path": "income.salary"},
        )
        for category, amount, days_ago in (
            ("income.salary", "1000.00", 40),  # before the window: carried in
            ("expenses.groceries", "-200.00", 25),
            ("expenses.groceries", "-300.00", 10),
            ("income.salary", "50.00", 3),  # after the window
        ):
            resp = client.post(
                "/transactions",
                json={
                    "account": "assets.window",
                    "category": category,
                    "amount": amount,
                    "date": (TODAY - timedelta(days=days_ago)).isoformat(),
                    "payee": "Synthetic payee",
                    "status": "posted",
                },
            )
            assert resp.status_code == 201, resp.text
        start, end = (
            (TODAY - timedelta(days=30)).isoformat(),
            (TODAY - timedelta(days=5)).isoformat(),
        )

        data = _mcp_call_raw(
            live_server.url,
            seeded_tokens["reader"],
            "balance_history",
            {"account": "assets.window", "start": start, "end": end},
        )["structured"]
        assert data["change"]["period"] == f"{start}..{end}"
        assert data["series_uri"] == f"purser://balance-history/assets.window/{start}_{end}"

        full = json.loads(
            _read_resource(live_server.url, seeded_tokens["reader"], data["series_uri"])["text"]
        )
        assert (full["account"], full["start"], full["end"], full["method"]) == (
            "assets.window",
            start,
            end,
            data["method"],
        )
        assert Decimal(full["opening"]) == Decimal("1000.00")
        assert [
            (p["date"], Decimal(p["delta"]), Decimal(p["balance"])) for p in full["points"]
        ] == [
            ((TODAY - timedelta(days=25)).isoformat(), Decimal("-200.00"), Decimal("800.00")),
            ((TODAY - timedelta(days=10)).isoformat(), Decimal("-300.00"), Decimal("500.00")),
        ]
        assert full["truncated"] is False
        # The tool's summary agrees with the series it points at, edge to edge.
        assert data["transactions"] == len(full["points"])
        assert Decimal(data["series"]["balances"][0]) == Decimal(full["opening"])
        assert Decimal(data["current"]) == Decimal(full["points"][-1]["balance"])

    def test_an_explicit_window_is_validated_alike_by_tool_and_resource(
        self,
        seeded_tokens: dict[str, str],  # noqa: F811
        live_server: LiveServer,
    ) -> None:
        """The resource runs the tool's own window check, so it accepts exactly
        the windows the tool does: an end before the start is refused by both."""
        later, earlier = TODAY.isoformat(), (TODAY - timedelta(days=7)).isoformat()
        with pytest.raises(BaseException) as excinfo:
            _mcp_call_raw(
                live_server.url,
                seeded_tokens["reader"],
                "balance_history",
                {"account": "assets.checking", "start": later, "end": earlier},
            )
        assert any("end must be >= start" in str(e) for e in _leaf_exceptions(excinfo.value))
        for segment, reason in (
            (f"{later}_{earlier}", "end must be >= start"),
            ("2026-13-01_2026-14-01", "not START_END ISO dates"),
            (f"{earlier}_", "not START_END ISO dates"),
        ):
            with pytest.raises(McpError) as refused:
                _read_resource(
                    live_server.url,
                    seeded_tokens["reader"],
                    f"purser://balance-history/assets.checking/{segment}",
                )
            assert reason in str(refused.value)

    def test_the_all_window_and_its_series_agree_past_ten_years(
        self,
        seeded_tokens: dict[str, str],  # noqa: F811
        live_server: LiveServer,
        client: Any,
    ) -> None:
        """Issue #322: for `all`, the resource clamped to the last ten years
        while the tool summarized the whole history, so on a longer history
        the series_uri served a different series than the one reported. Both
        resolve a named period with one function now: `all` is the whole
        history on both sides, bounded only by the 5,000-point cap."""
        assert (
            client.post(
                "/accounts",
                json={"name": "Decade", "type": "checking", "path": "assets.decade"},
            ).status_code
            == 201
        )
        client.post(
            "/categories",
            json={"name": "Salary", "type": "category_income", "path": "income.salary"},
        )
        # Twelve years of synthetic history, half of it past the old clamp.
        rows = (
            ("income.salary", "1000.00", 4400),  # the ledger's earliest activity
            ("expenses.groceries", "-400.00", 4000),
            ("income.salary", "250.00", 3800),
            ("expenses.groceries", "-100.00", 1500),
            ("income.salary", "75.00", 20),
        )
        for category, amount, days_ago in rows:
            resp = client.post(
                "/transactions",
                json={
                    "account": "assets.decade",
                    "category": category,
                    "amount": amount,
                    "date": (TODAY - timedelta(days=days_ago)).isoformat(),
                    "payee": "Synthetic payee",
                    "status": "posted",
                },
            )
            assert resp.status_code == 201, resp.text
        first_day = TODAY - timedelta(days=4400)

        got = _mcp_call_raw(
            live_server.url,
            seeded_tokens["reader"],
            "balance_history",
            {"account": "assets.decade", "period": "all"},
        )
        data = got["structured"]
        assert data["start"] == first_day.isoformat()
        # The window ends on the server's own today: a run that crosses 00:00 UTC
        # after this module captured TODAY lands one day later (#397).
        assert data["end"] in {TODAY.isoformat(), (TODAY + timedelta(days=1)).isoformat()}
        assert data["series_uri"] == "purser://balance-history/assets.decade/all"
        assert "over all" in got["text"]

        full = json.loads(
            _read_resource(live_server.url, seeded_tokens["reader"], data["series_uri"])["text"]
        )
        # The same window, edge to edge, and every change inside it.
        assert (full["start"], full["end"], full["method"]) == (
            data["start"],
            data["end"],
            data["method"],
        )
        assert Decimal(full["opening"]) == Decimal("0")
        assert [(p["date"], Decimal(p["balance"])) for p in full["points"]] == [
            ((TODAY - timedelta(days=days_ago)).isoformat(), Decimal(balance))
            for days_ago, balance in (
                (4400, "1000.00"),
                (4000, "600.00"),
                (3800, "850.00"),
                (1500, "750.00"),
                (20, "825.00"),
            )
        ]
        assert full["truncated"] is False
        assert data["transactions"] == len(full["points"])
        assert Decimal(data["current"]) == Decimal(full["points"][-1]["balance"])
        by_date = {p["date"]: Decimal(p["balance"]) for p in full["points"]}
        assert data["low"] == {
            "date": (TODAY - timedelta(days=4000)).isoformat(),
            "balance": "600.00",
        }
        assert Decimal(data["low"]["balance"]) == min(by_date.values())
        assert Decimal(data["high"]["balance"]) == max(by_date.values())

    def test_a_category_is_refused(
        self,
        seeded_tokens: dict[str, str],  # noqa: F811
        live_server: LiveServer,
    ) -> None:
        """Same refusal as account_balance — a category has no balance, and an
        error is safer than a plausible-looking zero series."""
        # _mcp_call_raw asserts not isError, and the MCP client wraps that
        # in its task group — so the refusal arrives as an ExceptionGroup.
        with pytest.raises(BaseException) as excinfo:
            _mcp_call_raw(
                live_server.url,
                seeded_tokens["reader"],
                "balance_history",
                {"account": "expenses.dining"},
            )
        assert "is a category" in str(excinfo.value) or any(
            "is a category" in str(e) for e in _leaf_exceptions(excinfo.value)
        )


def test_widget_registry_matches_sources() -> None:
    """Every WIDGETS key must have a real source directory.

    Nothing else ties the two together: the ``widget_bundles`` fixture STUBS
    an HTML file per WIDGETS key, and ``build-widgets.mjs`` auto-discovers
    ``widgets/*/index.html`` without ever reading WIDGETS. So a key with no
    source passed all of CI and then 404'd at runtime in the built image
    (found while planning #200).
    """
    from purser.mcp.presentation import WIDGETS

    widgets_dir = Path(__file__).resolve().parents[1] / "frontend" / "widgets"
    missing = [name for name in WIDGETS if not (widgets_dir / name / "index.html").is_file()]
    assert not missing, f"WIDGETS entries with no frontend/widgets/<name>/index.html: {missing}"
    on_disk = {
        p.parent.name for p in widgets_dir.glob("*/index.html") if p.parent.name not in {"harness"}
    }
    unregistered = sorted(on_disk - set(WIDGETS))
    assert not unregistered, f"widget sources not registered in WIDGETS: {unregistered}"


# ---------------------------------------------------------------------------
# #354 (decision 77): events fitted to the character budget
# ---------------------------------------------------------------------------

_FIT_NAMES = st.text(
    alphabet=string.ascii_letters + string.digits + " -&'.", min_size=1, max_size=60
)
_FIT_AMOUNTS = st.decimals(
    min_value=Decimal("-9000"),
    max_value=Decimal("9000"),
    places=2,
    allow_nan=False,
    allow_infinity=False,
).filter(lambda d: d != 0)


def _fit_base(start: Decimal, end: Decimal) -> dict[str, Any]:
    """Everything forecast_balance puts around the events, at its widest:
    a long path, every coverage key, a min balance and a Grafana link."""
    path = "assets.household_checking_joint_account"
    return {
        "account": path,
        "as_of": "2026-10-12",
        "horizon_days": 365,
        "start_balance": str(start),
        "min_balance": "-12500.00",
        "low": {"date": "2026-11-30", "balance": "-11999.99"},
        "end_balance": str(end),
        "coverage": {
            "schedules_active": 128,
            "instances": 1024,
            "projected": 4096,
            "suggestions_open": 12,
            "matches_pending": 18,
            "matches_pending_net": "-123456.78",
        },
        "events": [],
        "truncated": False,
        "daily_uri": f"purser://forecast/{path}/365d",
        "links": {"grafana": "https://grafana.example.com/d/purser-upcoming-bills"},
    }


@st.composite
def _fit_cases(
    draw: st.DrawFn,
) -> tuple[Decimal, list[dict[str, Any]], int | None, Decimal | None]:
    start = draw(_FIT_AMOUNTS)
    offsets = sorted(draw(st.lists(st.integers(min_value=0, max_value=364), max_size=40)))
    events: list[dict[str, Any]] = []
    running = start
    day_end: dict[int, tuple[Decimal, int]] = {}
    for index, offset in enumerate(offsets):
        amount = draw(_FIT_AMOUNTS)
        running += amount
        event: dict[str, Any] = {
            "date": (date(2026, 10, 12) + timedelta(days=offset)).isoformat(),
            "name": draw(_FIT_NAMES),
            "amount": str(amount),
            "balance_after": str(running),
            "transaction_id": draw(st.one_of(st.none(), st.uuids().map(str))),
        }
        if draw(st.booleans()):
            event["projected"] = True
        if draw(st.booleans()):
            event["overdue"] = True
        events.append(event)
        day_end[offset] = (running, index)
    # The low's event: the last event of the day with the lowest end-of-day
    # balance (none when the opening balance is the low).
    low_index = None
    if day_end:
        lowest = min(day_end.values(), key=lambda pair: pair[0])
        if lowest[0] < start:
            low_index = lowest[1]
    materiality = draw(
        st.one_of(
            st.none(),
            st.decimals(
                min_value=Decimal("0"),
                max_value=Decimal("5000"),
                places=2,
                allow_nan=False,
                allow_infinity=False,
            ),
        )
    )
    return start, events, low_index, materiality


class TestForecastEventFitter:
    @settings(max_examples=300, deadline=None)
    @given(case=_fit_cases())
    def test_fit_properties(
        self, case: tuple[Decimal, list[dict[str, Any]], int | None, Decimal | None]
    ) -> None:
        from purser.mcp.tools_analytics import _FORECAST_BUDGET_CHARS, _fit_events

        start, events, low_index, materiality = case
        end = start + sum((Decimal(e["amount"]) for e in events), Decimal(0))
        out = _fit_events(
            events, low_index=low_index, materiality=materiality, payload=_fit_base(start, end)
        )
        kept = out["events"]
        kept_ids = {id(e) for e in kept}

        # within budget, whatever the names and flags
        assert len(json.dumps(out)) <= _FORECAST_BUDGET_CHARS
        # the low's event is always listed
        if low_index is not None:
            assert id(events[low_index]) in kept_ids
        # date order, and the original order within a day
        positions = [next(i for i, e in enumerate(events) if e is k) for k in kept]
        assert positions == sorted(positions)

        def qualifies(e: dict[str, Any]) -> bool:
            return materiality is None or abs(Decimal(e["amount"])) >= materiality

        dropped = [e for e in events if id(e) not in kept_ids]
        listed_by_size = [e for e in kept if low_index is None or e is not events[low_index]]
        # every listed event (bar the low's) qualifies, and none is smaller
        # than a qualifying event left out
        assert all(qualifies(e) for e in listed_by_size)
        smallest = min((abs(Decimal(e["amount"])) for e in listed_by_size), default=None)
        if smallest is not None:
            assert all(abs(Decimal(e["amount"])) <= smallest for e in dropped if qualifies(e))
        # truncated means the budget dropped a qualifying event
        assert out["truncated"] is any(qualifies(e) for e in dropped)
        if materiality is None:
            assert out["truncated"] is bool(dropped)
        # omitted accounts for everything not listed: start + Σ + net == end
        if dropped:
            assert out["omitted"]["count"] == len(dropped)
            net = Decimal(out["omitted"]["net"])
        else:
            assert "omitted" not in out
            net = Decimal(0)
        assert start + sum((Decimal(e["amount"]) for e in kept), Decimal(0)) + net == end
