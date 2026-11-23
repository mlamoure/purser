"""Shared MCP plumbing: the FastMCP singleton, principal context, and the
``audited_tool`` decorator every tool module registers through.

This module is a LEAF — it imports no tool module, so the tool groups and
``purser.mcp.resources`` can all import from it without a cycle. That is what
lets ``purser/mcp/server.py`` be a composition root rather than a 1,100-line
registry (issue #90).
"""

import copy
import functools
import inspect
import time
import typing
import uuid
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import anyio
import jsonschema
import pydantic_core
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult
from mcp.types import CallToolResult, TextContent
from pydantic import TypeAdapter
from sqlalchemy.orm import Session

from purser import audit, privacy
from purser.admin import security

# --------------------------------------------------------------------------
# Principal plumbing: the ASGI wrapper authenticates the Bearer token and
# stashes a snapshot here; tools re-attach to a fresh session per call.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Principal:
    token_id: uuid.UUID
    user_id: uuid.UUID
    name: str
    scopes: tuple[str, ...]
    identity: dict[str, Any] | None = None


current_principal: ContextVar[Principal | None] = ContextVar("current_principal", default=None)


class McpAuthError(Exception):
    pass


def _principal() -> Principal:
    principal = current_principal.get()
    if principal is None:
        raise McpAuthError("no authenticated principal")
    return principal


def _require(check: Any, needed: str) -> Principal:
    principal = _principal()
    if not check(list(principal.scopes)):
        raise McpAuthError(f"requires scope: {needed}")
    return principal


def _token_row(session: Session, principal: Principal) -> Any:
    from purser.models import ApiToken

    return session.get(ApiToken, principal.token_id)


def _amount(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


async def _run[T](fn: Callable[[Session], T]) -> T:
    """Run a sync service call on a worker thread with its own session.

    Every tool and purser:// resource executes through here, which makes it
    the MCP twin of the REST ltree-refusal handler (issue #214): Postgres
    refusing ltree text the path validator admitted becomes a clean
    LedgerError message instead of a tool error quoting the raw SQL."""

    def wrapper() -> T:
        from sqlalchemy.exc import DBAPIError

        from purser.db import get_sessionmaker
        from purser.ledger import LedgerError, ltree_input_refusal

        with get_sessionmaker()() as session:
            try:
                result = fn(session)
                session.commit()
            except DBAPIError as exc:
                message = ltree_input_refusal(exc)
                if message is None:
                    raise
                raise LedgerError(message) from exc
            return result

    return await anyio.to_thread.run_sync(wrapper)


mcp = FastMCP(
    "purser",
    instructions=(
        "Purser is a double-entry personal-finance ledger presented through a flat "
        "API. Call describe first in a fresh session: it returns the data model, "
        "conventions, category tree, and accounts in one round-trip. Amounts are "
        "decimal strings; negative = money out. Aggregate questions (spending, net "
        "worth, balance history, forecasts) each have a dedicated tool — never "
        "page transactions to compute totals or balances yourself. You act with "
        "the user's authority: write directly when carrying out the user's "
        "request; when acting on your own initiative (a pattern you noticed, a "
        "rule you think would help), propose instead — leave activate false, or "
        "use the suggestions inbox. Privacy is your choice per call: when the "
        "question or action does not need payee names, descriptions or single "
        "amounts (a count, a category, which account, a total), pass withhold "
        "with those fields and work with payee references and transaction ids; "
        "call present to show the user details instead of repeating them. "
        "Nothing is withheld unless you ask, and aggregates are always exact."
    ),
)


SCOPE_CHECKS: dict[str, Callable[[list[str]], bool]] = {
    "read": security.can_read,
    "write": security.can_write,
    "admin": security.is_admin,
}


def _enforce_scope(scope: str) -> Principal:
    """The gate ``audited_tool`` applies before any tool body runs.

    Factored out so it is testable without registering a throwaway tool —
    the toolset is pinned by equality, so a test-only tool would fail CI.
    """
    return _require(SCOPE_CHECKS[scope], scope)


def _declared_output_schema(fn: Callable[..., Any]) -> dict[str, Any] | None:
    """``Annotated[CallToolResult, <TypedDict>]`` -> JSON schema for the tool's
    outputSchema. The SDK-bundled FastMCP derived this implicitly; standalone
    FastMCP needs it passed explicitly (a bare ``CallToolResult`` return infers
    no schema, which is what the non-widget three-tier tools want)."""
    ret = typing.get_type_hints(fn, include_extras=True).get("return")
    if typing.get_origin(ret) is typing.Annotated:
        base, *extras = typing.get_args(ret)
        if base is CallToolResult and extras:
            return TypeAdapter(extras[0]).json_schema()
    return None


def _ensure_text_block(result: Any) -> Any:
    """Every tool result reaches the client with at least one text block (issue #391).

    FastMCP sends an empty list as zero content blocks. ContextForge — the
    gateway agents reach Purser through — rebuilds a result from its content
    blocks, so it then drops ``structuredContent`` too, and a client that
    validates the output schema fails exactly when the honest answer is
    "none". The MCP spec says a tool that returns structured content SHOULD
    also send it serialized as text; this makes that unconditional."""
    if isinstance(result, list) and not result:
        # Same shape FastMCP gives a non-empty list: JSON text plus the
        # wrap-result structured content its output schema declares.
        return ToolResult(
            content=[TextContent(type="text", text="[]")], structured_content={"result": []}
        )
    if (
        isinstance(result, CallToolResult)
        and not result.content
        and result.structured_content is not None
    ):
        text = pydantic_core.to_json(result.structured_content).decode()
        return result.model_copy(update={"content": [TextContent(type="text", text=text)]})
    return result


# Every registered tool's disclosure declaration, by name: the canary test
# walks it to prove each emitting tool is exercised (#437).
EMITS: dict[str, privacy.Emits] = {}

WITHHOLD_DOC = (
    "\n\nwithhold: fields to leave out of this answer — any of payee (payees "
    "become stable references such as py_8k2mq4x7ra), description, amount "
    "(single amounts; totals stay exact). Pass it when the question or action "
    "does not need them; nothing is withheld otherwise. The response names what "
    "it withheld in `withheld`."
)
INCLUDE_DOC = (
    '\n\ninclude: ["external_ids"] returns the bank\'s account and connection '
    "identifiers, which are left out unless asked for."
)


def _with_params(fn: Callable[..., Any], names: list[str]) -> inspect.Signature:
    """The tool's signature plus the disclosure parameters it takes
    (``withhold``, ``include``; #613). Injected here rather than written on
    each tool, so no tool can forget them."""
    sig = inspect.signature(fn)
    for name in names:
        if name in sig.parameters:
            raise TypeError(f"{fn.__name__}: {name} is added by audited_tool, not declared")
    added = [
        inspect.Parameter(
            name, inspect.Parameter.KEYWORD_ONLY, default=None, annotation=list[str] | None
        )
        for name in names
    ]
    return sig.replace(parameters=[*sig.parameters.values(), *added])


def _dictionary() -> privacy.Dictionary:
    from purser.db import get_sessionmaker

    with get_sessionmaker()() as session:
        return privacy.load_dictionary(session)


def _scrub_result(result: Any, scrubber: privacy.Scrubber) -> Any:
    """Apply one call's withheld set to whatever the tool returned, before
    ``_ensure_text_block`` serializes it — so the headline, the text block
    and ``structuredContent`` carry the same disclosure."""
    if isinstance(result, CallToolResult):
        structured = result.structured_content
        if structured is not None:
            structured = copy.deepcopy(structured)
            scrubber.collect(structured)
            structured = scrubber.structure(structured)
        content = [
            block.model_copy(update={"text": scrubber.text(block.text)})
            if isinstance(block, TextContent)
            else block
            for block in result.content
        ]
        named = scrubber.named
        if named:
            if isinstance(structured, dict):
                structured["withheld"] = named
            if content and isinstance(content[0], TextContent):
                content[0] = content[0].model_copy(
                    update={"text": f"{content[0].text} (withheld: {', '.join(named)})"}
                )
        return result.model_copy(update={"structured_content": structured, "content": content})
    if isinstance(result, (dict, list)):
        return privacy.scrub(result, scrubber)
    return result


def withheld_now() -> set[str]:
    """The fields withheld for the running request: the surrounding tool
    call's choice (read_series), else only the external identifiers (a
    purser:// resource read directly asks for nothing)."""
    current = privacy.current_withheld.get()
    return set(privacy.DEFAULT_WITHHELD) if current is None else set(current)


async def disclose(payload: dict[str, Any], emits: privacy.Emits) -> dict[str, Any]:
    """Disclosure for a value served outside a tool's return path — the
    purser:// resources, read directly or through read_series. Uses the
    surrounding tool call's ``withhold``/``include`` when there is one."""
    withheld = withheld_now()
    dictionary = (
        await anyio.to_thread.run_sync(_dictionary)
        if privacy.needs_dictionary(withheld, emits)
        else privacy.Dictionary()
    )
    scrubbed = privacy.scrub(
        payload, privacy.Scrubber(withheld=withheld, emits=emits, dictionary=dictionary)
    )
    return scrubbed  # type: ignore[no-any-return]


def audited_tool(
    *,
    kind: str,
    entity: str,
    scope: str,
    emits: privacy.Emits,
    meta: dict[str, Any] | None = None,
) -> Callable[[Callable[..., Any]], Any]:
    """Register an MCP tool: deny-by-default scope enforcement, minimal
    disclosure, plus an audit call row on every call (issue #122) — tool
    name, clipped args, ok/error, duration, one request_id shared with any
    change rows the tool produces.

    ``scope`` is REQUIRED and has no default (issue #188). A tool that does
    not declare one is a TypeError at import, so a new tool can no longer
    reach the wire with no check at all — which is what hand-rolled
    ``_require`` closures allowed.

    ``emits`` is REQUIRED too (#437, #613): what the tool's output carries
    that a call can ask to withhold (``privacy.NOTHING`` for none). A tool
    that emits payees, narrative or amounts takes ``withhold``; one that
    emits external identifiers takes ``include``. The wrapper scrubs the
    result centrally to the call's withheld set and names what it withheld.
    Payee references in the arguments are resolved, and refused in free text,
    for every tool. Error messages are scrubbed like results. The call row
    records what the call asked to withhold, what the answer withheld, and
    which identifiable fields it returned (``detail.privacy``).

    The check runs INSIDE the try, so a denial still records its ok=false call
    row in the finally below — asserted by test_mcp.py's audit tests.

    functools.wraps preserves the signature FastMCP introspects
    (inspect.signature follows __wrapped__), so tool schemas are unchanged —
    pinned by the tools/list snapshot test; ``include`` is added through
    ``__signature__``. ``meta`` passes through to ``mcp.tool`` for the
    widget-bearing tools.
    """
    if scope not in SCOPE_CHECKS:  # a typo fails at import, not at call time
        raise ValueError(f"unknown scope {scope!r}; expected one of {sorted(SCOPE_CHECKS)}")
    if not isinstance(emits, privacy.Emits):
        raise TypeError("emits must be a privacy.Emits (privacy.NOTHING for none)")
    takes_withhold = emits.withholdable()
    takes_include = emits.includable()
    scrubs = bool(emits.fields()) and not emits.passthrough

    def decorate(fn: Callable[..., Any]) -> Any:
        output_schema = _declared_output_schema(fn)

        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            principal = current_principal.get()
            request_id = uuid.uuid4()
            reset = audit.current_request_id.set(request_id)
            started = time.monotonic()
            result: Any = None
            shown: Any = None
            ok = True
            denied = False
            error: str | None = None
            disclosed: list[str] | None = None
            requested_extra: list[str] | None = None
            requested_withhold: set[str] = set()
            named: list[str] = []
            withheld: set[str] = set()
            dictionary: privacy.Dictionary | None = None
            withhold = kwargs.pop("withhold", None) if takes_withhold else None
            include = kwargs.pop("include", None) if takes_include else None
            try:
                _enforce_scope(scope)
                requested_withhold = privacy.parse_withhold(withhold)
                chosen = privacy.parse_include(include)
                withheld = privacy.withheld_for(withhold=requested_withhold, include=chosen)
                requested_extra = sorted(chosen & emits.fields()) or None
                dictionary = (
                    await anyio.to_thread.run_sync(_dictionary)
                    if privacy.has_ref(kwargs) or privacy.needs_dictionary(withheld, emits)
                    else None
                )
                call_kwargs = privacy.resolve_inputs(kwargs, dictionary or privacy.Dictionary())
                withheld_reset = privacy.current_withheld.set(withheld)
                try:
                    result = await fn(*args, **call_kwargs)
                finally:
                    privacy.current_withheld.reset(withheld_reset)
                if output_schema is not None and isinstance(result, CallToolResult):
                    # Server-side contract check the SDK-bundled FastMCP used to do:
                    # drift between a payload builder and toolschemas fails the call.
                    # The message names the path, never the values (#437).
                    try:
                        jsonschema.validate(result.structured_content, output_schema)
                    except jsonschema.ValidationError as exc:
                        where = "/".join(str(p) for p in exc.absolute_path) or "(root)"
                        raise ToolError(
                            f"{fn.__name__}: output contract drift at {where}"
                        ) from None
                # `result` itself stays the tool's value: the audit row below counts its rows.
                if scrubs:
                    scrubber = privacy.Scrubber(
                        withheld=withheld,
                        emits=emits,
                        dictionary=dictionary or privacy.Dictionary(),
                    )
                    shown = _scrub_result(result, scrubber)
                    named = scrubber.named
                else:
                    shown = result
                shown = _ensure_text_block(shown)
                return shown
            except BaseException as exc:
                ok = False
                denied = isinstance(exc, McpAuthError)
                error = str(exc)[:300]
                if withheld and isinstance(exc, Exception) and not isinstance(exc, McpAuthError):
                    if dictionary is None and withheld & {privacy.PAYEE, privacy.DESCRIPTION}:
                        try:
                            dictionary = await anyio.to_thread.run_sync(_dictionary)
                        except Exception:
                            # The original failure may be the database itself:
                            # never replace it with this one. No names to
                            # scrub then, so amounts and numbers still go.
                            dictionary = None
                    scrubber = privacy.Scrubber(
                        withheld=withheld,
                        emits=privacy.EVERYTHING,
                        dictionary=dictionary or privacy.Dictionary(),
                    )
                    scrubbed = scrubber.error(str(exc))
                    if scrubbed != str(exc):
                        error = scrubbed[:300]
                        raise ToolError(scrubbed) from None
                raise
            finally:
                audit.current_request_id.reset(reset)
                if principal is not None:
                    detail: dict[str, Any] = {
                        "tool": fn.__name__,
                        "ok": ok,
                        "ms": int((time.monotonic() - started) * 1000),
                    }
                    logged = dict(kwargs)
                    if withhold is not None:
                        logged["withhold"] = withhold
                    if include is not None:
                        logged["include"] = include
                    clipped = audit.clip_values(logged) if logged else None
                    if clipped:
                        detail["args"] = clipped
                    if error is not None:
                        detail["error"] = error
                    if ok and isinstance(result, list):
                        detail["rows"] = len(result)
                    from purser import sharing

                    summary = sharing.summarize(shown if ok else None, requested=kwargs)
                    observed = set(summary["sensitive_fields"])
                    if ok:
                        observed |= privacy.present_declared_fields(
                            sharing.payload(shown)[0], emits
                        )
                    disclosed = sorted(set(requested_extra or []) & observed) or None
                    detail.update(
                        identity=principal.identity,
                        sharing=summary,
                        requested_extra=requested_extra or [],
                        privacy=privacy.audit_record(
                            requested=requested_withhold,
                            withheld=set(named) | set(summary["withheld"]),
                            observed=observed if ok else set(),
                        ),
                        completion_point="tool_return",
                    )
                    outcome = (
                        "denied"
                        if denied
                        else "failed"
                        if not ok
                        else "empty"
                        if summary["empty"]
                        else "shared"
                    )
                    event = await anyio.to_thread.run_sync(
                        functools.partial(
                            audit.record_call,
                            actor_token_id=principal.token_id,
                            kind=kind,
                            action=f"mcp.{fn.__name__}",
                            entity_type=entity,
                            entity_id=None,
                            request_id=request_id,
                            detail=detail,
                            disclosed=disclosed,
                            channel="mcp",
                            outcome=outcome,
                            strict=True,
                        )
                    )
                    await anyio.to_thread.run_sync(audit.record_receipt, event)

        EMITS[fn.__name__] = emits
        added = [n for n, on in (("withhold", takes_withhold), ("include", takes_include)) if on]
        if added:
            wrapper.__signature__ = _with_params(fn, added)  # type: ignore[attr-defined]
            wrapper.__doc__ = (
                (fn.__doc__ or "").rstrip()
                + (WITHHOLD_DOC if takes_withhold else "")
                + (INCLUDE_DOC if takes_include else "")
            )
            wrapper.__annotations__ = {
                **fn.__annotations__,
                **dict.fromkeys(added, list[str] | None),
            }
        register_kwargs: dict[str, Any] = {}
        if meta is not None:
            register_kwargs["meta"] = meta
        if output_schema is not None:
            register_kwargs["output_schema"] = output_schema
        return mcp.tool(**register_kwargs)(wrapper)

    return decorate


def audited_resource(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Direct financial resource reads use the same evidence as tools.

    read_series already owns a call record; avoid counting its nested read twice.
    """

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        if audit.current_request_id.get() is not None:
            return await fn(*args, **kwargs)
        principal = current_principal.get()
        request_id = uuid.uuid4()
        reset = audit.current_request_id.set(request_id)
        result: Any = None
        ok = False
        denied = False
        try:
            result = await fn(*args, **kwargs)
            ok = True
            return result
        except McpAuthError:
            denied = True
            raise
        finally:
            audit.current_request_id.reset(reset)
            if principal is not None:
                from purser import sharing

                summary = sharing.summarize(result if ok else None, requested=kwargs)
                event = await anyio.to_thread.run_sync(
                    functools.partial(
                        audit.record_call,
                        actor_token_id=principal.token_id,
                        kind="read",
                        action=f"resource.{fn.__name__}",
                        entity_type="series",
                        entity_id=None,
                        request_id=request_id,
                        channel="resource",
                        strict=True,
                        outcome="denied"
                        if denied
                        else "failed"
                        if not ok
                        else "empty"
                        if summary["empty"]
                        else "shared",
                        detail={
                            "resource": fn.__name__,
                            "ok": ok,
                            "identity": principal.identity,
                            "args": audit.clip_values(kwargs),
                            "sharing": summary,
                            "privacy": privacy.audit_record(
                                requested=set(),
                                withheld=set(summary["withheld"]),
                                observed=set(summary["sensitive_fields"]) if ok else set(),
                            ),
                            "completion_point": "resource_return",
                        },
                    )
                )
                await anyio.to_thread.run_sync(audit.record_receipt, event)

    return wrapper
