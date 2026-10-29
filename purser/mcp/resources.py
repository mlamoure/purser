"""MCP resources: ui:// widget bundles + purser:// full-resolution series.

``ui://purser/<name>`` — self-contained HTML widgets (SEP-1865 MCP Apps
hosts render them in a sandboxed iframe). Built at image build time from
frontend/widgets/ and served from PURSER_WIDGETS_DIR. Widgets carry no
account data — hosts feed them the tool's structuredContent — so these
files are static templates.

``purser://`` — full-resolution JSON series kept out of structuredContent
so tool responses stay inside the token budget (guardrail 10). The budget
does not apply to resources, nor to the ``read_series`` tool below, which
serves the same bodies to clients and gateways that forward tools only.

Everything under /mcp — including resources/read — passes through the same
Bearer wrapper as tools, so reads are token-authenticated; scope checks run
here via the usual ``_require``.
"""

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from mcp.types import CallToolResult, TextContent
from sqlalchemy.orm import Session

from purser.admin import security
from purser.config import get_settings
from purser.mcp._registry import _require, _run, audited_tool, disclose, mcp, withheld_now
from purser.mcp.presentation import WIDGETS
from purser.mcp.privacy import AMOUNT, Emits

WIDGET_MIME = "text/html;profile=mcp-app"

SERIES_SCHEME = "purser://"
# read_series' hard ceiling (decision 77). A typical 365-day forecast is
# ~25k characters; a balance history at its 5,000-point cap is several
# hundred thousand — the size at which an agent should narrow the window
# rather than pull the whole body into its context.
_READ_SERIES_MAX_CHARS = 200_000


def _widgets_dir() -> Path:
    """Configured bundle dir, with the same dev fallback shape as the SPA
    mount (api.app._mount_spa): the in-repo build output when the configured
    directory has nothing to serve."""
    configured = Path(get_settings().widgets_dir)
    if configured.is_dir() and any(configured.glob("*.html")):
        return configured
    dev = Path(__file__).resolve().parents[2] / "frontend" / "dist-widgets"
    if dev.is_dir() and any(dev.glob("*.html")):
        return dev
    return configured


def _widget_html(name: str) -> str:
    path = _widgets_dir() / f"{name}.html"
    if not path.is_file():
        raise FileNotFoundError(
            f"widget bundle {name}.html not found in {_widgets_dir()} — "
            "build with `npm run build:widgets` or point PURSER_WIDGETS_DIR at the bundles"
        )
    return path.read_text(encoding="utf-8")


def _register_widget(name: str, description: str) -> None:
    async def read_widget() -> str:
        def op(session: Session) -> str:
            _require(security.can_read, "read")
            return _widget_html(name)

        return await _run(op)

    read_widget.__name__ = f"widget_{name.replace('-', '_')}"
    mcp.resource(
        f"ui://purser/{name}",
        name=f"widget-{name}",
        description=description,
        mime_type=WIDGET_MIME,
    )(read_widget)


for _name, _description in WIDGETS.items():
    _register_widget(_name, _description)


def _parse_horizon(raw: str) -> int:
    """'30d' or '30' -> 30, bounded by the same check forecast_balance runs
    (schedules.validate_horizon), so the resource serves exactly the
    horizons the tool hands out a daily_uri for (#354)."""
    from purser import schedules

    try:
        days = int(raw.removesuffix("d"))
    except ValueError as exc:
        raise ValueError(f"horizon must look like '30d', got {raw!r}") from exc
    return schedules.validate_horizon(days)


def _coverage_json(coverage: dict[str, Any]) -> dict[str, Any]:
    """Forecast coverage as JSON: the one money field as a decimal string
    (guardrail 1); json.dumps would refuse the Decimal."""
    out = dict(coverage)
    if "matches_pending_net" in out:
        out["matches_pending_net"] = str(out["matches_pending_net"])
    return out


FORECAST_SERIES_EMITS = Emits(
    payee=True,
    narrative=True,
    description=("days[].events[].name",),
    amount=("days[].events[].amount", "days[].events[].range"),
)


@mcp.resource(
    "purser://forecast/{account}/{horizon}",
    name="forecast-daily",
    description=(
        "Full daily forecast series for one account (JSON): every day in the "
        "horizon with its projected balance and expected events, plus the end "
        "balance and the low. The compact forecast_balance tool response "
        "references this via daily_uri; horizon is like '30d' or '90d' (1..365 "
        "days, the horizons the tool accepts). Each event carries its basis and "
        "range (null unless estimated with a band); when any has a band, each "
        "day carries low and high, the pessimistic and optimistic balances."
    ),
    mime_type="application/json",
)
async def forecast_daily(account: str, horizon: str) -> str:
    days = _parse_horizon(horizon)

    def op(session: Session) -> dict[str, Any]:
        from purser import schedules as service

        _require(security.can_read, "read")
        result = service.forecast_account(session, account_ref=account, horizon_days=days)
        fc = result["forecast"]
        low = min(fc.days, key=lambda d: d.balance)
        # The estimate range band (#247): per-day low/high only when the
        # forecast has one; per-event range always (null when unranged).
        banded = fc.pessimistic_low is not None
        out: dict[str, Any] = {
            "account": result["account_path"],
            "horizon_days": days,
            "start_balance": str(fc.start_balance),
            "end_balance": str(fc.days[-1].balance),
            "low": {"date": low.date.isoformat(), "balance": str(low.balance)},
            "min_balance": str(fc.min_balance) if fc.min_balance is not None else None,
            "first_shortfall": fc.first_shortfall.isoformat() if fc.first_shortfall else None,
            "days": [
                {
                    "date": d.date.isoformat(),
                    "balance": str(d.balance),
                    **({"low": str(d.low), "high": str(d.high)} if banded else {}),
                    "events": [
                        {
                            "name": e.label,
                            "amount": str(e.amount),
                            "transaction_id": str(e.transaction_id) if e.transaction_id else None,
                            "projected": e.projected,
                            "overdue": e.overdue,
                            "basis": e.basis,
                            "range": (
                                {"low": str(e.range[0]), "high": str(e.range[1])}
                                if e.range is not None
                                else None
                            ),
                        }
                        for e in d.events
                    ],
                }
                for d in fc.days
            ],
            "coverage": _coverage_json(result["coverage"]),
        }
        return out

    # Minimal disclosure (#437): the daily series carries every event's label
    # and amount; each day's projected balance stays (an aggregate).
    return json.dumps(await disclose(await _run(op), FORECAST_SERIES_EMITS))


NET_WORTH_PERIODS: dict[str, int | None] = {"90d": 90, "1y": 365, "all": None}


def named_net_worth_window(session: Session, period: str, *, today: date) -> tuple[date, date]:
    """A named net-worth period as its (start, end) window, ending today.

    The ONE resolution of a named period: the net_worth tool runs it for its
    series and the resource below for its body, which is the ``all`` window,
    so the series_uri serves the window the tool summarized (issue #343, the
    net-worth twin of #322). ``all`` starts at the ledger's earliest activity,
    never later than today (only future-dated rows means no history yet) and
    never more than ``networth.SERIES_CAP_DAYS`` before it: unlike a balance
    history, each day is a full net-worth valuation, computed live when it is
    not stored."""
    if period not in NET_WORTH_PERIODS:
        raise ValueError(f"period must be one of {' | '.join(NET_WORTH_PERIODS)}")
    days = NET_WORTH_PERIODS[period]
    if days is None:
        from purser import networth

        earliest = networth.earliest_activity_date(session) or today
        floor = today - timedelta(days=networth.SERIES_CAP_DAYS)
        return max(min(earliest, today), floor), today
    return today - timedelta(days=days), today


@mcp.resource(
    "purser://net-worth/daily",
    name="net-worth-daily",
    description=(
        "Full daily net-worth series (JSON) over the net_worth tool's all "
        "window: from the earliest ledger activity, at most ten years back, to "
        "today, with assets, liabilities, and net per day. The net_worth "
        "tool's downsampled series references this via series_uri."
    ),
    mime_type="application/json",
)
async def net_worth_daily_series() -> str:
    def op(session: Session) -> str:
        from purser import networth

        _require(security.can_read, "read")
        start, today = named_net_worth_window(session, "all", today=datetime.now(UTC).date())
        rows = networth.series(session, start=start, end=today)
        return json.dumps(
            {
                "start": start.isoformat(),
                "end": today.isoformat(),
                "days": [
                    {
                        "date": r["as_of"].isoformat(),
                        "assets": str(r["assets"]),
                        "liabilities": str(r["liabilities"]),
                        "net": str(r["net_worth"]),
                    }
                    for r in rows
                ],
            }
        )

    return await _run(op)


BALANCE_PERIODS: dict[str, int | None] = {"30d": 30, "90d": 90, "1y": 365, "all": None}
_BALANCE_POINT_CAP = 5000
_WINDOW_SEPARATOR = "_"


def explicit_balance_window(start: str, end: str) -> tuple[date, date]:
    """An explicit balance-history window from two ISO dates, validated.

    The ONE check for such a window: the balance_history tool runs it on its
    start/end and the resource below on the bounds in its URI, so every
    window the tool accepts is a window the resource serves (issue #289)."""
    window_start, window_end = date.fromisoformat(start), date.fromisoformat(end)
    if window_end < window_start:
        raise ValueError("end must be >= start")
    return window_start, window_end


def balance_window_segment(start: date, end: date) -> str:
    """An explicit window as the resource's period segment, START_END: a
    stable identifier for the same two dates, with nothing in it a URI path
    would treat specially."""
    return f"{start.isoformat()}{_WINDOW_SEPARATOR}{end.isoformat()}"


def named_balance_window(session: Session, period: str, *, today: date) -> tuple[date, date]:
    """A named balance-history period as its (start, end) window, ending today.

    The ONE resolution of a named period: the balance_history tool runs it
    for its window and the resource below for the period in its URI, so the
    series_uri serves the window the tool summarized (issue #322). ``all``
    starts at the ledger's earliest activity, never later than today, with no
    clamp: a per-change series costs one bounded read whatever the window's
    length, so the 5,000-point cap with ``truncated`` bounds every window —
    the explicit ones already relied on it alone."""
    if period not in BALANCE_PERIODS:
        raise ValueError(f"period must be one of {' | '.join(BALANCE_PERIODS)}")
    days = BALANCE_PERIODS[period]
    if days is None:
        from purser import networth

        # Only future-dated rows (schedule instances) means no history yet.
        earliest = networth.earliest_activity_date(session) or today
        return min(earliest, today), today
    return today - timedelta(days=days), today


def _parse_balance_period(raw: str) -> str | tuple[date, date]:
    """'30d' | '90d' | '1y' | 'all' -> that period name, resolved against the
    ledger by named_balance_window; an explicit 'START_END' window -> its
    (start, end) dates."""
    if raw in BALANCE_PERIODS:
        return raw
    start, separator, end = raw.partition(_WINDOW_SEPARATOR)
    if not separator:
        allowed = " | ".join(BALANCE_PERIODS)
        raise ValueError(f"period must be one of {allowed} or START_END dates, got {raw!r}")
    try:
        return explicit_balance_window(start, end)
    except ValueError as exc:
        raise ValueError(f"window {raw!r} is not START_END ISO dates: {exc}") from exc


@mcp.resource(
    "purser://balance-history/{account}/{period}",
    name="balance-history",
    description=(
        "Full-resolution balance history for one account (JSON): every stored "
        "change in the window with the balance after it, the delta, and the "
        "transaction id to drill into, plus the opening balance carried in. "
        "Per-transaction IS the full resolution here — that is the grain the "
        "running-balance store keeps and the register shows. The compact "
        "balance_history tool references this via series_uri; period is "
        "30d | 90d | 1y | all (ending today; all is the whole history), or an "
        "explicit START_END window of ISO dates such as 2026-01-01_2026-03-31, "
        "which is what the tool returns when it is called with start and end. "
        "Every window serves at most 5,000 points, oldest first, and sets "
        "truncated past that."
    ),
    mime_type="application/json",
)
async def balance_history_series(account: str, period: str) -> str:
    window = _parse_balance_period(period)

    withheld = await withheld_now()

    def op(session: Session) -> str:
        from purser import balances, ledger, networth

        _require(security.can_read, "read")
        target = ledger.resolve_account(session, account)
        if target.is_virtual:
            raise ValueError(f"{target.path} is a category; balances apply to real accounts")
        # Exactly the window the tool summarized, whichever kind: no cap beyond
        # the ones it applies, or the URI would serve a different series.
        if isinstance(window, tuple):
            start, end = window
        else:
            start, end = named_balance_window(session, window, today=datetime.now(UTC).date())

        rows, truncated = balances.change_points(
            session, target.id, start=start, end=end, limit=_BALANCE_POINT_CAP
        )
        opening_map, method = networth.account_balance_series(
            session, target, dates=[start - timedelta(days=1)]
        )
        points = [
            {
                "date": r.date.isoformat(),
                "balance": str(r.balance),
                "delta": str(r.delta),
                "transaction_id": str(r.transaction_id),
            }
            for r in rows
        ]
        extra: dict[str, Any] = {}
        if AMOUNT in withheld:
            # Per transaction, the series is the register: its deltas are the
            # amounts and its balances give them away by subtraction. With
            # amounts withheld it collapses to end-of-day balances (#437).
            daily: dict[str, str] = {}
            for point in points:
                daily[point["date"]] = point["balance"]
            points = [{"date": d, "balance": b} for d, b in daily.items()]
            extra["withheld"] = ["amount"]
        return json.dumps(
            {
                "account": target.path,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "currency": target.currency.strip(),
                "method": method,
                "opening": str(next(iter(opening_map.values()), Decimal("0"))),
                "points": points,
                "truncated": truncated,
                **extra,
            }
        )

    return await _run(op)


@audited_tool(kind="read", entity="series", scope="read", emits=Emits(passthrough=True))
async def read_series(uri: str) -> CallToolResult:
    """Read a purser:// series URI another tool handed out —
    forecast_balance's daily_uri, balance_history's and net_worth's
    series_uri — for clients and gateways that forward tools but not
    resources. Returns the resource body byte for byte (JSON text, no
    structuredContent): exactly what resources/read serves. Exempt from the
    response budget like the resources themselves, so call it only when
    the compact answer is not enough — a 365-day forecast is about 6k
    tokens. A body over 200,000 characters is refused; narrow the window (a
    shorter horizon or period). purser:// URIs only."""
    if not uri.startswith(SERIES_SCHEME):
        raise ValueError(
            f"read_series reads {SERIES_SCHEME} series only (got {uri!r}); "
            "widgets (ui://) and other URIs are not data series"
        )
    # FastMCP's own read path — the resource handler, its serializer and its
    # in-handler read-scope check — so the body cannot drift from what
    # resources/read returns.
    result = await mcp.read_resource(uri)
    body = result.contents[0].content if result.contents else ""
    if not isinstance(body, str):
        raise ValueError(f"{uri} is not a text series")
    if len(body) > _READ_SERIES_MAX_CHARS:
        raise ValueError(
            f"{uri} is {len(body):,} characters, over read_series' "
            f"{_READ_SERIES_MAX_CHARS:,}-character ceiling; narrow the window "
            "(a shorter horizon or period, or an explicit START_END window)"
        )
    return CallToolResult(content=[TextContent(type="text", text=body)])
