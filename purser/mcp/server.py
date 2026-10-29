"""The MCP server — Purser's primary interface (DESIGN §10).

Streamable HTTP from the same process, authenticated with the same scoped
tokens as REST. Every tool answers compactly (token-budgeted, < ~500 tokens
typical) and computes server-side — an agent never sums rows in-context.

This module is the **composition root**. The tools themselves live in the
``tools_*`` modules and register against the shared ``mcp`` singleton at
import time via ``@audited_tool``; importing them here is what assembles the
toolset (issue #90). ``purser.mcp.server`` therefore remains the one import
that yields a fully-populated registry — ``mount``, ``guide``,
``scripts/export_mcp_catalog.py`` and the tests all rely on that.

Adding a tool group means one import below and one line in
``tests/test_mcp.py::EXPECTED_TOOLS``; a dropped import fails that test.
"""

# Tool modules are imported for the registration side effect of @audited_tool;
# `resources` registers the ui:// widget bundles and purser:// series the same
# way. Both import from _registry rather than from this module, so the
# import-last ordering constraint noted in issue #72 is gone.
from purser.mcp import (
    resources,
    tools_analytics,
    tools_holdings,
    tools_memories,
    tools_read,
    tools_review,
    tools_rules,
    tools_suggestions,
    tools_write,
)
from purser.mcp._registry import (
    McpAuthError,
    Principal,
    _amount,
    _principal,
    _require,
    _run,
    _token_row,
    audited_tool,
    current_principal,
    mcp,
)

# Re-exported so existing importers keep working unchanged.
__all__ = [
    "McpAuthError",
    "Principal",
    "_amount",
    "_principal",
    "_require",
    "_run",
    "_token_row",
    "audited_tool",
    "current_principal",
    "mcp",
    "resources",
    "tools_analytics",
    "tools_holdings",
    "tools_memories",
    "tools_read",
    "tools_review",
    "tools_rules",
    "tools_suggestions",
    "tools_write",
]
