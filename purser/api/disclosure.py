"""Agent-requested privacy on the REST API (#613, DESIGN §9).

The same choice an MCP tool call makes with ``withhold``, for an integration
that calls the API:

- Every read route declares what it returns that a caller can ask to leave
  out, with ``disclose(Emits(...))`` in its ``dependencies`` (or
  ``disclose(NOTHING)``) — no default, like its scope. A route that emits
  payees, narrative or single amounts takes a ``withhold`` query parameter
  (repeat it or comma-separate: ``?withhold=payee,amount``). The declaration
  is structural: tests/test_privacy_rest.py fails for a read route without
  one, and the canary there calls every declared route with everything
  withheld.
- ``DisclosureMiddleware`` scrubs the serialized JSON response with the
  engine MCP uses (``purser.privacy``), after the route built it, so no
  field the route forgot to drop can slip through. The response names what
  it withheld in ``withheld`` (on each item of a bare list).
- Payee references (``py_...``) are accepted wherever a request names a
  payee, in the query string or a JSON body, and refused inside free text.

Nothing is withheld unless the request asks: the web app never passes
``withhold`` and keeps full detail. External account identifiers follow the
token's scope on REST (left out for API tokens without ``admin``), not this
parameter.
"""

import json
from collections.abc import Awaitable, Callable, MutableMapping
from dataclasses import dataclass
from typing import Annotated, Any
from urllib.parse import parse_qsl, urlencode

import anyio
from fastapi import Depends, HTTPException, Query, Request
from starlette.types import Receive, Scope, Send

from purser import privacy
from purser.privacy import NOTHING, Emits

__all__ = ["NOTHING", "Disclosure", "DisclosureMiddleware", "Emits", "disclose"]

WITHHOLD_DESCRIPTION = (
    "Fields to leave out of this response: any of payee (payees become stable "
    "references such as py_8k2mq4x7ra), description, amount (single amounts; "
    "totals and balances stay exact). Repeat the parameter or comma-separate. "
    "Nothing is withheld otherwise; the response names what it withheld in "
    "`withheld`."
)

# Marks a dependency as a disclosure declaration; the structural test reads it.
EMITS_ATTR = "__purser_emits__"


@dataclass(frozen=True)
class Disclosure:
    """One request's choice, set on ``request.state.disclosure`` by the
    route's declaration and read by the middleware and the audit capture."""

    emits: Emits
    requested: frozenset[str]


def disclose(emits: Emits) -> Any:
    """The dependency that declares a read route's emits and, when there is
    anything to withhold, adds the ``withhold`` query parameter."""
    if not isinstance(emits, Emits):
        raise TypeError("disclose takes a privacy.Emits (privacy.NOTHING for none)")

    if not emits.withholdable():

        def declared(request: Request) -> None:
            request.state.disclosure = Disclosure(emits=emits, requested=frozenset())

        setattr(declared, EMITS_ATTR, emits)
        return Depends(declared)

    def withholding(
        request: Request,
        withhold: Annotated[
            list[str] | None, Query(description=WITHHOLD_DESCRIPTION, max_length=3)
        ] = None,
    ) -> None:
        try:
            chosen = privacy.parse_withhold(withhold)
        except privacy.DisclosureError as exc:
            raise HTTPException(422, str(exc)) from None
        request.state.disclosure = Disclosure(emits=emits, requested=frozenset(chosen))

    setattr(withholding, EMITS_ATTR, emits)
    return Depends(withholding)


def _dictionary() -> privacy.Dictionary:
    from purser.db import get_sessionmaker

    with get_sessionmaker()() as session:
        return privacy.load_dictionary(session)


def _is_json(headers: list[tuple[bytes, bytes]]) -> bool:
    for key, value in headers:
        if key.lower() == b"content-type":
            return value.split(b";")[0].strip().lower() == b"application/json"
    return False


async def _reject(send: Send, message: str) -> None:
    body = json.dumps({"detail": message}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 422,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _resolve_query(query: bytes, dictionary: privacy.Dictionary) -> bytes:
    pairs = parse_qsl(query.decode("latin-1"), keep_blank_values=True)
    out = []
    for key, value in pairs:
        arg: Any = [value] if key in privacy.PAYEE_LIST_ARGS else value
        resolved = privacy.resolve_inputs({key: arg}, dictionary)[key]
        out.append((key, resolved[0] if isinstance(resolved, list) else resolved))
    return urlencode(out).encode("latin-1")


def scrub_body(body: bytes, status: int, disclosure: Disclosure) -> tuple[bytes, list[str]]:
    """The response body with the request's withheld fields scrubbed, and the
    fields it named. Runs in a worker thread (it may load the dictionary)."""
    try:
        value = json.loads(body)
    except ValueError:
        return body, []
    withheld = set(disclosure.requested)
    dictionary = (
        _dictionary()
        if status >= 400 or privacy.needs_dictionary(withheld, disclosure.emits)
        else privacy.Dictionary()
    )
    scrubber = privacy.Scrubber(withheld=withheld, emits=disclosure.emits, dictionary=dictionary)
    if status >= 400:
        if isinstance(value, dict) and isinstance(value.get("detail"), str):
            value["detail"] = scrubber.error(value["detail"])
        return json.dumps(value).encode(), []
    scrubbed = privacy.scrub(value, scrubber)
    return json.dumps(scrubbed, separators=(",", ":")).encode(), scrubber.named


class DisclosureMiddleware:
    """Resolves payee references in requests and applies a read route's
    ``withhold`` to its JSON response. Sits inside ``AuditMiddleware``, so
    the audit row inventories the response that actually left Purser."""

    def __init__(self, app: Callable[[Scope, Receive, Send], Awaitable[None]]) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "") if scope["type"] == "http" else ""
        if scope["type"] != "http" or path == "/mcp" or path.startswith("/mcp/"):
            # MCP resolves references and applies withhold in audited_tool;
            # its JSON-RPC body is not a REST request.
            await self.app(scope, receive, send)
            return

        routed = await self._resolve_references(scope, receive, send)
        if routed is None:
            return

        start: MutableMapping[str, Any] | None = None
        chunks: list[bytes] = []
        disclosure: Disclosure | None = None
        passthrough = False

        async def send_wrapper(message: MutableMapping[str, Any]) -> None:
            nonlocal start, disclosure, passthrough
            if message["type"] == "http.response.start":
                disclosure = scope.get("state", {}).get("disclosure")
                if (
                    disclosure is None
                    or not disclosure.requested
                    or not _is_json(list(message.get("headers", [])))
                ):
                    passthrough = True
                    await send(message)
                    return
                start = message
                return
            if message["type"] != "http.response.body" or passthrough or start is None:
                await send(message)
                return
            chunks.append(message.get("body", b""))
            if message.get("more_body"):
                return
            assert disclosure is not None
            body, named = await anyio.to_thread.run_sync(
                scrub_body, b"".join(chunks), int(start["status"]), disclosure
            )
            scope.setdefault("state", {})["disclosure_withheld"] = named
            headers = [
                (k, v) for k, v in start.get("headers", []) if k.lower() != b"content-length"
            ]
            headers.append((b"content-length", str(len(body)).encode()))
            await send({**start, "headers": headers})
            await send({"type": "http.response.body", "body": body})

        await self.app(scope, routed, send_wrapper)

    async def _resolve_references(
        self, scope: Scope, receive: Receive, send: Send
    ) -> Receive | None:
        """Payee references in the query string or a JSON body become the
        payee's name before routing; a reference in free text is refused
        with 422. Returns the receive to route with, or None when refused."""
        query: bytes = scope.get("query_string", b"")
        body: bytes | None = None
        headers = list(scope.get("headers", []))
        if _is_json(headers) and scope.get("method") in ("POST", "PUT", "PATCH"):
            parts: list[bytes] = []
            more = True
            while more:
                message = await receive()
                if message["type"] != "http.request":
                    break
                parts.append(message.get("body", b""))
                more = message.get("more_body", False)
            body = b"".join(parts)
        query_has_ref = privacy.REF_RE.search(query.decode("latin-1")) is not None
        body_has_ref = body is not None and privacy.REF_RE.search(body.decode("utf-8", "replace"))
        if query_has_ref or body_has_ref:
            dictionary = await anyio.to_thread.run_sync(_dictionary)
            try:
                if query_has_ref:
                    # The audit records what the caller sent (the reference),
                    # not the name the route filters by.
                    scope.setdefault("state", {})["audit_query_string"] = query
                    scope["query_string"] = _resolve_query(query, dictionary)
                if body_has_ref and body is not None:
                    parsed = json.loads(body)
                    if isinstance(parsed, dict):
                        parsed = privacy.resolve_inputs(parsed, dictionary)
                    elif isinstance(parsed, list):
                        parsed = [
                            privacy.resolve_inputs(v, dictionary) if isinstance(v, dict) else v
                            for v in parsed
                        ]
                    body = json.dumps(parsed).encode()
                    scope["headers"] = [
                        (k, v) for k, v in headers if k.lower() != b"content-length"
                    ] + [(b"content-length", str(len(body)).encode())]
            except privacy.DisclosureError as exc:
                await _reject(send, str(exc))
                return None
            except ValueError:
                pass  # not JSON after all: let the route refuse it
        if body is None:
            return receive
        replay = body
        sent = False

        async def replay_receive() -> MutableMapping[str, Any]:
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": replay, "more_body": False}
            return await receive()

        return replay_receive
