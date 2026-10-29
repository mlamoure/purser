"""Read/write call auditing for REST — pure ASGI middleware (issue #122).

One audit call row per authenticated request, written AFTER the final
response body flushes (the client never waits on audit) on a short dedicated
session (``audit.record_call`` swallows failures — an audit hiccup must not
fail a read). Requests that never resolve a principal (401s, public probes,
static/SPA, /mcp — which audits per tool call) produce nothing: the auth
dependency stamps ``request.state.audit_actor`` and its absence is the skip
signal. 403s DO land here — denied attempts are exactly what an audit trail
is for.
"""

import contextlib
import json
import time
import uuid
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any
from urllib.parse import parse_qsl

import anyio

from purser import audit

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]

# Never audited, short-circuited before any capture machinery. /mcp is
# audited per tool call by the server's audited_tool wrapper instead.
_SKIP_PREFIXES = (
    "/mcp",
    "/assets",
    "/healthz",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/llms.txt",
    "/favicon",
)

# POST endpoints that are semantically reads (no ledger effect).
_READ_POSTS = frozenset({"/import/qif/preview", "/import/csv/preview"})

_BODY_CAP = 256 * 1024  # responses are token-budgeted; anything bigger, skip rows count


def _row_count(body: bytes) -> int | None:
    """Best-effort result-size signal: a top-level array's length, or the
    length of a single array member of a top-level object (list envelopes)."""
    if not body or len(body) > _BODY_CAP:
        return None
    try:
        parsed = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    if isinstance(parsed, list):
        return len(parsed)
    if isinstance(parsed, dict):
        arrays = [v for v in parsed.values() if isinstance(v, list)]
        if len(arrays) == 1:
            return len(arrays[0])
    return None


class AuditMiddleware:
    def __init__(self, app: Callable[[Scope, Receive, Send], Awaitable[None]]) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] == "OPTIONS":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if any(path == p or path.startswith(p + "/") or path.startswith(p) for p in _SKIP_PREFIXES):
            await self.app(scope, receive, send)
            return

        request_id = uuid.uuid4()
        reset = audit.current_request_id.set(request_id)
        started = time.monotonic()
        status_code = 0
        chunks: list[bytes] = []
        captured = 0

        async def send_wrapper(message: MutableMapping[str, Any]) -> None:
            nonlocal status_code, captured
            if message["type"] == "http.response.start":
                status_code = message["status"]
            elif message["type"] == "http.response.body":
                body = message.get("body", b"")
                if body and captured <= _BODY_CAP:
                    chunks.append(body)
                    captured += len(body)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            audit.current_request_id.reset(reset)

        actor_token_id = scope.get("state", {}).get("audit_actor")
        if actor_token_id is None:
            return  # no principal resolved: public, unauthenticated, or 401

        route = scope.get("route")
        path_template = getattr(route, "path_format", None) or path
        method = scope["method"]
        kind = (
            audit.KIND_READ
            if method in ("GET", "HEAD") or path_template in _READ_POSTS
            else audit.KIND_WRITE
        )
        entity_id = None
        path_params = scope.get("path_params") or {}
        uuid_params = [v for v in path_params.values() if _as_uuid(v) is not None]
        if len(uuid_params) == 1:
            entity_id = _as_uuid(uuid_params[0])
        query = dict(parse_qsl(scope.get("query_string", b"").decode("latin-1")))
        detail: dict[str, Any] = {
            "method": method,
            "path": path_template,
            "status": status_code,
            "ms": int((time.monotonic() - started) * 1000),
        }
        if query:
            detail["query"] = audit.clip_values(query)
        rows = _row_count(b"".join(chunks)) if kind == audit.KIND_READ else None
        if rows is not None:
            detail["rows"] = rows

        # the response is already sent — an audit hiccup must never resurface
        with contextlib.suppress(Exception):
            await anyio.to_thread.run_sync(
                lambda: audit.record_call(
                    actor_token_id=actor_token_id,
                    kind=kind,
                    action=f"{method} {path_template}",
                    entity_type=audit.entity_for_path(path_template),
                    entity_id=entity_id,
                    request_id=request_id,
                    detail=detail,
                )
            )


def _as_uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


class BodyLimitMiddleware:
    """Refuse a request body larger than ``limit`` bytes with 413 (#429).

    FastAPI reads a route's whole body before it resolves the route's
    dependencies, authentication included, so without a limit an
    unauthenticated client could make the process buffer any amount of
    memory before its 401. A declared Content-Length over the limit is
    refused before anything is read; a chunked body is counted as it
    arrives and refused once it passes the limit. ``limit`` 0 turns it off."""

    def __init__(self, app: Callable[[Scope, Receive, Send], Awaitable[None]], limit: int) -> None:
        self.app = app
        self.limit = limit

    async def _refuse(self, send: Send) -> None:
        body = json.dumps(
            {"detail": f"request body too large: the limit is {self.limit} bytes"}
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"connection", b"close"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self.limit <= 0:
            await self.app(scope, receive, send)
            return
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = 0
                if declared > self.limit:
                    await self._refuse(send)
                    return
        received = 0
        started = False
        refused = False

        async def counted() -> MutableMapping[str, Any]:
            nonlocal received, refused
            if refused:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.limit and not started:
                    # Answer now and tell the app the client went away: the
                    # body parser would turn an exception into a 400.
                    refused = True
                    await self._refuse(send)
                    return {"type": "http.disconnect"}
            return message

        async def tracked(message: MutableMapping[str, Any]) -> None:
            nonlocal started
            if refused:
                return  # the 413 is the answer
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counted, tracked)
        except Exception:
            if not refused:
                raise
