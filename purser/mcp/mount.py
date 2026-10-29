"""Mounting glue: Bearer-token authentication in front of the MCP app.

Same scoped tokens as REST — the wrapper resolves the Authorization header
to a Principal snapshot before any MCP handling runs.
"""

import json
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

import anyio

from purser.mcp.server import Principal, current_principal, mcp

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]


def _authenticate(raw: str) -> Principal | None:
    from purser.admin import service as admin_service
    from purser.db import get_sessionmaker

    with get_sessionmaker()() as session:
        token = admin_service.authenticate_token(session, raw)
        if token is None:
            return None
        principal = Principal(
            token_id=token.id,
            user_id=token.user_id,
            name=token.name,
            scopes=tuple(token.scopes),
        )
        session.commit()  # persist last_used_at
        return principal


def _host_origin_allowlist() -> tuple[list[str], list[str]]:
    """Allow the instance's public host (plus local dev hosts) through
    FastMCP's Host/Origin request guard (DNS-rebinding protection) — otherwise
    every request via a reverse proxy is rejected with 421 Misdirected Request."""
    from urllib.parse import urlparse

    from purser.config import get_settings

    # host:* is the wildcard-port pattern (test servers bind ephemeral ports;
    # the reverse proxy forwards the bare public host).
    hosts = {"localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*"}
    origins = {"http://localhost", "http://127.0.0.1"}
    public = urlparse(get_settings().public_url)
    if public.hostname:
        hosts.add(public.hostname)
        hosts.add(f"{public.hostname}:*")
        origins.add(f"{public.scheme}://{public.netloc}")
    return sorted(hosts), sorted(origins)


class McpAuthWrapper:
    def __init__(self) -> None:
        # Stateless streamable HTTP mounted at "/" of the /mcp mount. One
        # ASGI app per application instance; FastAPI's lifespan enters
        # ``self.app.lifespan`` (api.app.create_app) so the transport's
        # session manager runs inside the server's event loop.
        hosts, origins = _host_origin_allowlist()
        self.app = mcp.http_app(
            path="/",
            stateless_http=True,
            host_origin_protection=True,
            allowed_hosts=hosts,
            allowed_origins=origins,
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        auth = headers.get(b"authorization", b"").decode()
        raw = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else None
        principal = await anyio.to_thread.run_sync(_authenticate, raw) if raw else None
        if principal is None:
            body = json.dumps({"error": "invalid or missing bearer token"}).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        reset = current_principal.set(principal)
        try:
            await self.app(scope, receive, send)
        finally:
            current_principal.reset(reset)
