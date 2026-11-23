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
    from purser.sharing import identity

    with get_sessionmaker()() as session:
        token = admin_service.authenticate_token(session, raw)
        if token is None:
            return None
        principal = Principal(
            token_id=token.id,
            user_id=token.user_id,
            name=token.name,
            scopes=tuple(token.scopes),
            identity=identity(session, token),
        )
        session.commit()  # persist last_used_at
        return principal


def _host_origin_allowlist() -> tuple[list[str], list[str]]:
    """The names FastMCP's Host/Origin guard (DNS-rebinding protection)
    admits: PURSER_PUBLIC_URL's host (what a reverse proxy forwards), the
    exact names in PURSER_ALLOWED_HOSTS (#633) and the loopback names.

    FastMCP owns the guard (it switches the MCP SDK's own
    ``TransportSecuritySettings`` check off), so this only feeds it lists.
    Entries are written the SDK's way: a bare name plus ``name:*`` for any
    port, or ``name:port`` when the setting pins one. FastMCP compares host
    names, ports aside, and also admits its built-in loopback names. Nothing
    here is a host pattern: ``parse_allowed_hosts`` refuses wildcards."""
    from urllib.parse import urlparse

    from purser import network
    from purser.config import get_settings

    settings = get_settings()
    # host:* is the wildcard-port form (test servers bind ephemeral ports;
    # the reverse proxy forwards the bare public host).
    hosts = {"localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*"}
    origins = {"http://localhost", "http://127.0.0.1"}
    public = urlparse(settings.public_url)
    if public.hostname:
        hosts.add(public.hostname)
        hosts.add(f"{public.hostname}:*")
        origins.add(f"{public.scheme}://{public.netloc}")
    for entry in settings.allowed_hosts:
        host, port = network.parse_entry(entry)
        hosts.update({entry} if port else {host, f"{host}:*"})
    origins.update(network.allowed_origins(settings.public_url, settings.allowed_hosts))
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
            await self.app(scope, receive, _explain_misdirected(headers, send))
        finally:
            current_principal.reset(reset)


def _explain_misdirected(headers: dict[bytes, bytes], send: Send) -> Send:
    """Replace the Host guard's bare ``421 Misdirected Request`` with the
    reason and the remedy (#581, #633). An agent meets this the first time it
    connects by a name Purser was not told about (a home server reached by
    another of its names), and the bare status says neither what is wrong nor
    which address works. Only an authenticated caller gets this far, so the
    instance's own addresses are not told to strangers."""
    replaced = False

    async def wrapped(message: MutableMapping[str, Any]) -> None:
        nonlocal replaced
        if message["type"] == "http.response.start" and message["status"] == 421:
            replaced = True
            from purser.config import get_settings

            settings = get_settings()
            body = json.dumps(
                {
                    "error": "misdirected request",
                    "detail": _misdirected_detail(
                        settings.public_url.rstrip("/"),
                        settings.allowed_hosts,
                        headers.get(b"host", b"").decode(errors="replace"),
                    ),
                }
            ).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 421,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        if replaced:
            return  # the guard's own body
        await send(message)

    return wrapped


def _misdirected_detail(public: str, allowed: tuple[str, ...], named: str) -> str:
    """What the 421 says: the names this Purser answers, the address to use,
    and how to make it answer the name this request used as well."""
    from purser import network

    answers = f"{public}, to localhost"
    if allowed:
        answers += f" or to {', '.join(allowed)} ({network.SETTING})"
    try:
        host, _ = network.parse_entry(network.host_of(named))
    except ValueError:
        host = ""
    allow = (
        f"To accept {host!r} too, add it to {network.SETTING} (exact names, comma-separated)"
        if host
        else f"To accept another name, add it to {network.SETTING} (exact names, comma-separated)"
    )
    return (
        f"This Purser answers MCP requests addressed to {answers}, and this one was "
        f"addressed to {named!r}. Connect to {public}/mcp. {allow} and restart Purser; "
        "PURSER_PUBLIC_URL sets the address Purser gives out."
    )
