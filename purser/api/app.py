"""FastAPI application factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

import purser
from purser.config import get_settings
from purser.db import get_session
from purser.logging import configure_logging

DbSession = Annotated[Session, Depends(get_session)]

API_DESCRIPTION = """\
Purser is an AI-native personal finance backend: a double-entry ledger behind a
flat API. Agents are first-class clients — prefer the MCP endpoint at `/mcp`
(same Bearer tokens), or use these REST endpoints directly.

Conventions: amounts are decimal strings (negative = money out); accounts and
categories accept a UUID or an ltree path; posted transactions are immutable
(edits become linked reversal pairs); writes need the `write` scope and are
audited and reversible. Aggregate questions (spending, net worth,
forecasts) have dedicated endpoints — never sum transaction rows client-side.
"""


async def ltree_refusal_handler(request: Request, exc: Exception) -> JSONResponse:
    """BACKSTOP (issue #214), registered for ``DBAPIError``: Postgres refusing
    ltree text answers 422 in the routes' own ``{"detail": "..."}`` shape
    instead of a 500.

    Every path boundary validates first (``ledger.check_path``), so this is
    only reached by text the validator admits but the database's locale still
    refuses. Any other database error is re-raised untouched — it stays a
    500, with its traceback logged.
    """
    from purser.ledger import ltree_input_refusal

    message = ltree_input_refusal(exc) if isinstance(exc, DBAPIError) else None
    if message is None:
        raise exc
    return JSONResponse(status_code=422, content={"detail": message})


def create_app() -> FastAPI:
    from purser import crypto

    crypto.ensure_key()  # encryption at rest is required — fail before serving
    settings = get_settings()
    configure_logging(settings.log_level)

    from purser.mcp.mount import McpAuthWrapper

    mcp_wrapper = McpAuthWrapper()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # Starlette does not propagate lifespan into mounted apps; enter the
        # MCP app's own lifespan so its session manager runs in this loop.
        async with mcp_wrapper.app.lifespan(mcp_wrapper.app):
            yield

    app = FastAPI(
        title="Purser",
        summary="The finance backend for the personal agent stack.",
        description=API_DESCRIPTION,
        version=purser.__version__,
        license_info={"name": "AGPL-3.0-only", "identifier": "AGPL-3.0-only"},
        lifespan=lifespan,
    )

    from purser.admin import security
    from purser.admin.throttle import LoginThrottle

    # Password sign-in throttling (issue #212, decision 59): one in-memory
    # tracker per app — per process in production, fresh per test client.
    app.state.login_throttle = LoginThrottle.from_settings(settings)
    # Derive the sign-in dummy hash now (issue #304): made lazily on the first
    # unknown email instead, that one attempt would cost two hashes and stand
    # out from every other.
    security.dummy_password_hash()

    from purser.api.middleware import AuditMiddleware, BodyLimitMiddleware

    # Outermost layer: sees the final status, flushes one call row after the
    # response body (issue #122). /mcp short-circuits inside the middleware —
    # tool calls audit themselves via audited_tool.
    app.add_middleware(AuditMiddleware)
    # Outside even that: an oversized body is refused before any layer reads
    # it (#429). FastAPI reads a body before it resolves authentication.
    app.add_middleware(BodyLimitMiddleware, limit=settings.max_request_bytes)
    app.add_exception_handler(DBAPIError, ltree_refusal_handler)

    from purser.admin.routes import audit_router, auth_router, tokens_router, users_router
    from purser.api.accounts import categories_router
    from purser.api.accounts import router as accounts_router
    from purser.api.categorize import router as categorize_router
    from purser.api.config import router as config_router
    from purser.api.csv_import import router as csv_router
    from purser.api.feed import router as feed_router
    from purser.api.matching import router as matching_router
    from purser.api.memories import router as memories_router
    from purser.api.networth import holdings_router, valuations_router
    from purser.api.networth import router as networth_router
    from purser.api.positions import prices_router
    from purser.api.positions import router as positions_router
    from purser.api.qif import router as qif_router
    from purser.api.rules import router as rules_router
    from purser.api.schedules import forecast_router
    from purser.api.schedules import router as schedules_router
    from purser.api.suggestions import router as suggestions_router
    from purser.api.transactions import router as transactions_router
    from purser.api.webhooks import events_router
    from purser.api.webhooks import router as webhooks_router

    app.include_router(auth_router)
    app.include_router(users_router)
    app.include_router(tokens_router)
    app.include_router(audit_router)
    app.include_router(accounts_router)
    app.include_router(categories_router)
    app.include_router(transactions_router)
    app.include_router(feed_router)
    app.include_router(matching_router)
    app.include_router(schedules_router)
    app.include_router(suggestions_router)
    app.include_router(forecast_router)
    app.include_router(networth_router)
    app.include_router(holdings_router)
    app.include_router(valuations_router)
    app.include_router(positions_router)
    app.include_router(prices_router)
    app.include_router(events_router)
    app.include_router(webhooks_router)
    app.include_router(qif_router)
    app.include_router(csv_router)
    app.include_router(categorize_router)
    app.include_router(rules_router)
    app.include_router(memories_router)
    app.include_router(config_router)

    @app.api_route("/mcp", methods=["GET", "POST", "DELETE"], include_in_schema=False)
    async def mcp_no_slash() -> RedirectResponse:
        # Bare /mcp only matches the mount via a trailing-slash redirect;
        # the SPA catch-all would otherwise shadow it with a 405.
        return RedirectResponse("/mcp/", status_code=307)

    app.mount("/mcp", mcp_wrapper)

    @app.get("/llms.txt", tags=["meta"], response_class=PlainTextResponse)
    async def llms_txt() -> str:
        """Agent-facing tool guide, generated from the same source as the
        MCP describe tool."""
        from purser.mcp.guide import build_llms_txt

        return await build_llms_txt()

    @app.get(
        "/source",
        tags=["meta"],
        response_class=RedirectResponse,
        status_code=307,
        responses={307: {"description": "Redirect to the Corresponding Source"}},
    )
    def source() -> RedirectResponse:
        """Where to get this instance's source code. Purser is AGPL-3.0-only
        and offers its Corresponding Source to everyone who uses it over a
        network (section 13); this redirects to `PURSER_SOURCE_URL`. Public."""
        return RedirectResponse(get_settings().source_url, status_code=307)

    @app.get("/healthz", tags=["meta"])
    def healthz(session: DbSession) -> JSONResponse:
        try:
            session.execute(text("SELECT 1"))
        except Exception:
            return JSONResponse(status_code=503, content={"status": "error", "database": "down"})
        return JSONResponse(content={"status": "ok", "database": "ok"})

    _mount_spa(app)

    return app


def _mount_spa(app: FastAPI) -> None:
    """Serve the built admin SPA (issue #12). The bundle is a build artifact
    (no Node at runtime); client-side routes fall back to index.html."""
    import os
    from pathlib import Path

    from fastapi.responses import FileResponse
    from starlette.staticfiles import StaticFiles

    static_dir = Path(os.environ.get("PURSER_STATIC_DIR", "/app/static"))
    if not (static_dir / "index.html").exists():
        dev_dist = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"
        if (dev_dist / "index.html").exists():
            static_dir = dev_dist
        else:
            return
    static_dir = static_dir.resolve()
    app.mount("/assets", StaticFiles(directory=static_dir / "assets"), name="assets")

    @app.get("/", include_in_schema=False)
    @app.get("/{spa_path:path}", include_in_schema=False)
    def spa(spa_path: str = "") -> FileResponse:
        # Contain to static_dir: percent-encoded `..` reaches spa_path decoded,
        # so resolve and reject anything (incl. symlink escapes) that lands
        # outside the bundle — unknown paths fall back to the SPA shell.
        candidate = (static_dir / spa_path).resolve()
        if spa_path and candidate.is_relative_to(static_dir) and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(static_dir / "index.html")
