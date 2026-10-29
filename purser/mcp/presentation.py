"""Three-tier tool responses + widget wiring (issue #72, DESIGN §10).

Viz-worthy tools answer in three tiers so they stay excellent in text-only
agents and carry a rendered experience into MCP Apps hosts (SEP-1865):

1. a one-sentence text headline the agent can speak,
2. compact ``structuredContent`` (< 500 tokens; guardrail 6),
3. ``_meta.ui.resourceUri`` naming a self-contained ``ui://purser/*`` widget.
"""

from typing import Any

from mcp.types import CallToolResult, TextContent

from purser import appsettings

# Widget registry: resource name -> description. The widget build pipeline
# (frontend/widgets/) emits one self-contained HTML bundle per entry;
# purser.mcp.resources serves each as ui://purser/<name>.
WIDGETS: dict[str, str] = {
    "forecast": "Balance forecast step-line with expected events and shortfall callout",
    "recurring": "Recurring bills radar with price-change badges",
    "net-worth": "Net worth area chart with movement attribution",
    "spending": "Spending vs baseline bar rows with top payee movers",
    "digest": "What's-new receipt card summarizing recent ledger activity",
    "balance-history": (
        "Per-account balance step-line with period pills, biggest-mover dots "
        "and an optional dashed forecast tail"
    ),
}

# Grafana dashboards shipped in grafana/dashboards/ (readmodel contract,
# DESIGN decision 14). links.grafana appears only where a view exists.
_DASHBOARD_UIDS: dict[str, str] = {
    "net-worth": "purser-net-worth",
    "cash-flow": "purser-cash-flow",
    "upcoming-bills": "purser-upcoming-bills",
}


def widget_uri(name: str) -> str:
    if name not in WIDGETS:
        raise ValueError(f"unknown widget: {name}")
    return f"ui://purser/{name}"


def widget_meta(name: str) -> dict[str, Any]:
    """Declaration-level tool meta: SEP-1865 hosts read
    ``_meta.ui.resourceUri`` off the tool definition to fetch the widget."""
    return {"ui": {"resourceUri": widget_uri(name)}}


def grafana_links(dashboard: str) -> dict[str, str] | None:
    """``links`` payload fragment for tools backed by a readmodel dashboard.
    None (caller omits the key) when PURSER_GRAFANA_URL is unset."""
    base = appsettings.effective().grafana_url
    uid = _DASHBOARD_UIDS.get(dashboard)
    if not base or uid is None:
        return None
    return {"grafana": f"{base.rstrip('/')}/d/{uid}"}


def three_tier(
    headline: str,
    payload: dict[str, Any],
    *,
    widget: str | None = None,
) -> CallToolResult:
    """Compose the three-tier response. ``payload`` is the structuredContent
    (FastMCP validates it against the tool's declared output schema);
    ``widget`` mirrors the declaration meta onto the result so hosts can
    resolve the bundle from either place."""
    return CallToolResult(
        content=[TextContent(type="text", text=headline)],
        structuredContent=payload,
        _meta=widget_meta(widget) if widget else None,
    )
