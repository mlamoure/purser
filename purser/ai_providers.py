"""BYO model providers for categorization (DESIGN §11).

Two code paths only: any OpenAI-compatible endpoint and native Anthropic.
Two provider choices share the OpenAI-compatible path (decision 133):
``openai`` for a hosted service (OpenAI, OpenRouter, a cloud gateway) and
``local`` for a model the user runs (Ollama, LM Studio, vLLM, a llama.cpp
server, a LiteLLM gateway in front of one). ``local`` never falls back to the
cloud default URL, its key is optional, and its privacy wording says the data
stays on the user's network. Off by default; the model never sees more than
one transaction at a time (``DISCLOSURES`` says exactly what).
"""

import ipaddress
import json
import socket
import time
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx

from purser.config import Settings

PROVIDERS = ("none", "openai", "local", "anthropic")
OPENAI_COMPATIBLE = frozenset({"openai", "local"})
ANTHROPIC_BASE_URL = "https://api.anthropic.com/v1"
# The env default of ai_base_url: right for `openai`, never used for `local`.
CLOUD_DEFAULT_BASE_URL = str(Settings.model_fields["ai_base_url"].default)
# A model list is shown to a person picking one; a server listing thousands
# (a gateway in front of a catalog) is cut here.
MAX_MODELS = 500
MAX_MODEL_ID_LENGTH = 200


@dataclass(frozen=True)
class Sent:
    """One plain-language item of what a capability sends to the provider,
    and the prompt inputs it accounts for. tests/test_ai_local_provider.py
    pins the union of ``fields`` to the prompt builders' inputs, so a prompt
    cannot gain an input without this wording naming it (#640)."""

    text: str
    fields: tuple[str, ...]


@dataclass(frozen=True)
class Disclosure:
    capability: Literal["categorize"]
    when: str
    sends: tuple[Sent, ...]


DISCLOSURES = (
    Disclosure(
        capability="categorize",
        when="To categorize a transaction that no rule or earlier choice covers",
        sends=(
            Sent("its payee, description and amount", ("payee", "description", "amount")),
            Sent(
                "your categories and their descriptions",
                ("categories", "category_descriptions"),
            ),
            Sent(
                "the names and paths of your open accounts, offered as transfer targets",
                ("categories", "category_descriptions"),
            ),
            # Memories (§3.6) are "financial notes" in the user docs.
            Sent(
                "your financial notes that apply everywhere, and the ones linked to its "
                "account or payee",
                ("memories",),
            ),
        ),
    ),
)


@dataclass(frozen=True)
class Suggestion:
    category_path: str
    confidence: float
    model: str


ErrorKind = Literal["config", "transport", "timeout", "status", "malformed"]


class AiProviderError(Exception):
    """A provider call that did not produce an answer. The message is
    sanitized: a status code, an exception type or a fixed sentence, never
    the key, the URL's credentials or the provider's response body."""

    def __init__(
        self, message: str, *, kind: ErrorKind = "status", status: int | None = None
    ) -> None:
        super().__init__(message)
        self.kind: ErrorKind = kind
        self.status = status


def _prompt(
    payee: str | None,
    description: str | None,
    amount: Decimal,
    categories: list[str],
    category_descriptions: dict[str, str] | None = None,
    memories: list[str] | None = None,
) -> str:
    """Category descriptions (DESIGN §3.2) ride along as ``path — description``
    lines — they are written for exactly this classifier.

    Entries wrapped in brackets (``[assets.savings]``) are the caller's own
    accounts, offered so a transfer has a correct answer available at all
    (DESIGN §11.1, issue #183). The reply is still constrained to the list.
    """
    described = category_descriptions or {}
    lines = "\n".join(
        f"- {path} — {described[path]}" if described.get(path) else f"- {path}"
        for path in categories
    )
    bracketed = any(path.startswith("[") for path in categories)
    transfer_note = (
        "Entries in [brackets] are the user's OWN accounts. Choose one only "
        "when this row moves money between the user's accounts rather than "
        "spending or earning it — a card payment, a transfer, an investment "
        "contribution. Be conservative: pick a bracket entry only when the "
        "evidence is clear, and lower your confidence when it is not.\n"
        if bracketed
        else ""
    )
    context = (
        "Things the user has told Purser to remember:\n"
        + "\n".join(f"- {m}" for m in memories)
        + "\n"
        if memories
        else ""
    )
    return (
        "Categorize this personal-finance transaction into exactly one of the "
        "allowed categories.\n"
        f"{context}"
        f"Payee: {payee or '(unknown)'}\n"
        f"Description: {description or '(none)'}\n"
        f"Amount: {amount} (negative = money out)\n"
        f"{transfer_note}"
        f"Allowed categories:\n{lines}\n"
        'Reply with ONLY a JSON object: {"category": "<one allowed path>", '
        '"confidence": <0.0-1.0>}'
    )


def _parse_reply(text: str, categories: list[str], model: str) -> Suggestion | None:
    try:
        start, end = text.index("{"), text.rindex("}") + 1
        data = json.loads(text[start:end])
        category = str(data["category"])
        confidence = float(data.get("confidence", 0.5))
    except (ValueError, KeyError, TypeError):
        return None
    if category not in categories:
        return None
    return Suggestion(category_path=category, confidence=confidence, model=model)


def suggest(
    settings: Settings,
    *,
    payee: str | None,
    description: str | None,
    amount: Decimal,
    categories: list[str],
    category_descriptions: dict[str, str] | None = None,
    memories: list[str] | None = None,
    client: httpx.Client | None = None,
) -> Suggestion | None:
    """Ask the configured provider for a category. None when unconfigured,
    unparseable, or out-of-list — never a guess outside the tree.

    Raises AiProviderError on transport failures, non-200 responses, and
    malformed provider envelopes. Messages are sanitized (status code or
    exception type only — never the API key)."""
    if settings.ai_provider == "none" or not settings.ai_model:
        return None
    prompt = _prompt(payee, description, amount, categories, category_descriptions, memories)
    text = _complete(settings, prompt, client)
    return _parse_reply(text, categories, model=str(settings.ai_model))


def _request(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    timeout: float,
    client: httpx.Client | None,
    body: dict[str, Any] | None = None,
    params: dict[str, str] | None = None,
) -> httpx.Response:
    """One provider request; any transport failure (connect, timeout, bad
    URL, a header value HTTP cannot carry) becomes AiProviderError —
    sanitized, never the API key. The timeout is per request, so it holds
    for an injected client too."""
    try:
        with nullcontext(client) if client is not None else httpx.Client() as http:
            resp = http.request(
                method, url, json=body, params=params, headers=headers, timeout=timeout
            )
    except httpx.TimeoutException as exc:
        raise AiProviderError(
            f"provider did not answer within {timeout:g} s", kind="timeout"
        ) from exc
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        raise AiProviderError(
            f"provider request failed: {type(exc).__name__}", kind="transport"
        ) from exc
    except UnicodeEncodeError as exc:
        raise AiProviderError(
            "the API key or URL holds a character an HTTP request cannot carry", kind="config"
        ) from exc
    return resp


def _post(
    url: str,
    body: dict[str, Any],
    headers: dict[str, str],
    client: httpx.Client | None,
    timeout: float = 30,
) -> httpx.Response:
    """POST to the provider; a non-200 answer is an AiProviderError naming
    only its status code."""
    resp = _request("POST", url, headers=headers, timeout=timeout, client=client, body=body)
    if resp.status_code != 200:
        raise AiProviderError(f"provider returned HTTP {resp.status_code}", status=resp.status_code)
    return resp


def _envelope_text(resp: httpx.Response, path: tuple[str | int, ...]) -> str:
    """Extract the reply text from the provider envelope; invalid JSON or an
    unexpected shape raises AiProviderError."""
    try:
        node: Any = resp.json()
        for key in path:
            node = node[key]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise AiProviderError(
            f"provider returned malformed response: {type(exc).__name__}", kind="malformed"
        ) from exc
    if not isinstance(node, str):
        raise AiProviderError(
            "provider returned malformed response: non-string content", kind="malformed"
        )
    return node


def base_url(settings: Settings) -> str:
    """The URL the configured provider is called at, without a trailing
    slash. ``local`` refuses the cloud default (decision 133): a model the
    user declared local is never silently sent to a hosted service."""
    if settings.ai_provider == "anthropic":
        return ANTHROPIC_BASE_URL
    url = (settings.ai_base_url or "").strip().rstrip("/")
    if settings.ai_provider == "local" and needs_base_url(url):
        raise AiProviderError(
            "a local model needs its server's base URL (for example http://localhost:11434/v1)",
            kind="config",
        )
    return url


def destination(settings: Settings) -> tuple[str, str] | None:
    """The provider and the base URL its requests go to, or None while the
    setup has no address (a local provider without one). The stored key
    goes only to the saved destination (#667)."""
    try:
        return settings.ai_provider, base_url(settings)
    except AiProviderError:
        return None


def host_of(url: str) -> str | None:
    """A URL's host and port, for a message or an audit row: never its
    credentials, path or query."""
    parts = urlsplit(url)
    host = parts.hostname
    if not host:
        return None
    if ":" in host:  # an IPv6 literal
        host = f"[{host}]"
    try:
        port = parts.port
    except ValueError:
        port = None
    return f"{host}:{port}" if port else host


def needs_base_url(url: str | None) -> bool:
    """True when ``url`` cannot serve as a local model's address: unset, or
    the hosted default that ``openai`` uses."""
    cleaned = (url or "").strip().rstrip("/")
    return not cleaned or cleaned == CLOUD_DEFAULT_BASE_URL.rstrip("/")


def _timeout(settings: Settings) -> float:
    return float(settings.ai_timeout_seconds)


def _complete(settings: Settings, prompt: str, client: httpx.Client | None) -> str:
    """One prompt in, raw reply text out. Shared by the categorizer and the
    connection test so both inherit the same sanitized-error handling."""
    if settings.ai_provider in OPENAI_COMPATIBLE:
        return _openai_text(settings, prompt, client=client)
    if settings.ai_provider == "anthropic":
        return _anthropic_text(settings, prompt, client=client)
    raise AiProviderError(f"unknown ai_provider {settings.ai_provider!r}", kind="config")


def _openai_headers(settings: Settings) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if settings.ai_api_key:
        headers["Authorization"] = f"Bearer {settings.ai_api_key}"
    return headers


def _anthropic_headers(settings: Settings) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "x-api-key": settings.ai_api_key or "",
        "anthropic-version": "2023-06-01",
    }


def _openai_text(settings: Settings, prompt: str, *, client: httpx.Client | None) -> str:
    body = {
        "model": settings.ai_model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
    }
    resp = _post(
        f"{base_url(settings)}/chat/completions",
        body,
        _openai_headers(settings),
        client,
        _timeout(settings),
    )
    return _envelope_text(resp, ("choices", 0, "message", "content"))


def _anthropic_text(settings: Settings, prompt: str, *, client: httpx.Client | None) -> str:
    body = {
        "model": settings.ai_model,
        "max_tokens": 200,
        "messages": [{"role": "user", "content": prompt}],
    }
    resp = _post(
        f"{ANTHROPIC_BASE_URL}/messages",
        body,
        _anthropic_headers(settings),
        client,
        _timeout(settings),
    )
    return _envelope_text(resp, ("content", 0, "text"))


# --------------------------------------------------------------------------
# Setup checks (#640, decision 133): the model list and the connection test.
# Admin-only callers; both reuse the request plumbing above, so a failure is
# reported in the same sanitized terms the categorizer logs.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelList:
    listed: bool  # the server answered a model list (False: it has none to offer)
    models: list[str]


def list_models(settings: Settings, *, client: httpx.Client | None = None) -> ModelList:
    """The model ids the provider offers: ``GET {base}/models`` for an
    OpenAI-compatible server (Ollama, LM Studio, vLLM, llama.cpp and LiteLLM
    all answer it), Anthropic's ``/v1/models`` otherwise. A server without
    the route (404/405) gives ``listed=False``; every other failure raises
    AiProviderError."""
    if settings.ai_provider in OPENAI_COMPATIBLE:
        url, headers, params = f"{base_url(settings)}/models", _openai_headers(settings), None
    elif settings.ai_provider == "anthropic":
        url, headers = f"{ANTHROPIC_BASE_URL}/models", _anthropic_headers(settings)
        params = {"limit": "1000"}
    else:
        raise AiProviderError(f"unknown ai_provider {settings.ai_provider!r}", kind="config")
    resp = _request(
        "GET", url, headers=headers, timeout=_timeout(settings), client=client, params=params
    )
    if resp.status_code in (404, 405):
        return ModelList(listed=False, models=[])
    if resp.status_code != 200:
        raise AiProviderError(f"provider returned HTTP {resp.status_code}", status=resp.status_code)
    try:
        data = resp.json()["data"]
        ids = {
            item["id"]
            for item in data
            if isinstance(item, dict)
            and isinstance(item.get("id"), str)
            and 0 < len(item["id"]) <= MAX_MODEL_ID_LENGTH
        }
    except (ValueError, KeyError, TypeError) as exc:
        raise AiProviderError(
            f"provider returned a malformed model list: {type(exc).__name__}", kind="malformed"
        ) from exc
    return ModelList(listed=True, models=sorted(ids)[:MAX_MODELS])


Network = Literal["this_machine", "private_network", "public_internet", "unknown"]
# Carrier-grade NAT space: not "private" to the ipaddress module, but where
# overlay networks such as Tailscale put a home server.
_SHARED_ADDRESS_SPACE = ipaddress.ip_network("100.64.0.0/10")
Resolver = Callable[[str], list[str]]


def _resolve(host: str) -> list[str]:
    return [str(info[4][0]) for info in socket.getaddrinfo(host, None)]


def _is_private(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return (
        addr.is_loopback
        or addr.is_private
        or addr.is_link_local
        or (isinstance(addr, ipaddress.IPv4Address) and addr in _SHARED_ADDRESS_SPACE)
    )


def network_of(url: str, *, resolve: Resolver | None = None) -> Network:
    """Where ``url``'s host lives, by the addresses it resolves to: all
    loopback is this machine, all private (RFC 1918, ULA, link-local, shared
    100.64/10) is the private network, anything else is the public internet.
    Purser takes the user's ``local`` declaration and only warns on a public
    answer (decision 133); a name that does not resolve is ``unknown``."""
    host = urlsplit(url).hostname
    if not host:
        return "unknown"
    try:
        addrs = [ipaddress.ip_address(host)]
    except ValueError:
        if host == "localhost" or host.endswith(".localhost"):
            return "this_machine"
        try:
            found = (resolve or _resolve)(host)
            addrs = [ipaddress.ip_address(a.split("%", 1)[0]) for a in found]
        except (OSError, ValueError, UnicodeError):
            return "unknown"
    if not addrs:
        return "unknown"
    if all(a.is_loopback for a in addrs):
        return "this_machine"
    if all(_is_private(a) for a in addrs):
        return "private_network"
    return "public_internet"


@dataclass(frozen=True)
class ConnectionReport:
    ok: bool
    reachable: bool
    authenticated: bool | None  # None: the server's answers do not say
    model_listed: bool | None  # None: no model list to check against
    answered: bool | None  # the model replied with the JSON Purser asks for
    network: Network
    key_sent: bool
    elapsed_ms: int | None
    message: str
    warning: str | None = None


TEST_PROMPT = (
    'This is a connection test from Purser. Reply with ONLY this JSON object: {"ok": true}'
)


def _where(url: str) -> str:
    """Host and port for a message: never the URL's credentials or path."""
    return host_of(url) or "the server"


def _unreachable(exc: AiProviderError, url: str, settings: Settings) -> str:
    where = _where(url)
    if exc.kind == "timeout":
        return (
            f"{where} did not answer within {settings.ai_timeout_seconds} s. "
            "A model on modest hardware can need longer: raise the timeout."
        )
    if exc.kind == "config":
        return str(exc)[:1].upper() + str(exc)[1:] + "."
    hint = (
        " If Purser runs in Docker, localhost is the Purser container itself: "
        "use host.docker.internal or the host's address."
        if urlsplit(url).hostname in ("localhost", "127.0.0.1", "::1")
        else " Check the address and that the server is running and listening "
        "on an interface Purser can reach."
    )
    return f"Could not reach {where} ({str(exc).rsplit(': ', 1)[-1]}).{hint}"


def _refused(status: int | None, key_sent: bool) -> str:
    if key_sent:
        return f"The server refused the API key (HTTP {status}). Check the key."
    return f"The server needs an API key (HTTP {status}). Enter the key it expects."


def failure_message(exc: AiProviderError, settings: Settings) -> str:
    """One sanitized sentence for a failed setup check (the model list)."""
    if exc.kind in ("config", "transport", "timeout"):
        try:
            url = base_url(settings)
        except AiProviderError:
            url = ""
        return _unreachable(exc, url, settings)
    if exc.status in (401, 403):
        return _refused(exc.status, bool(settings.ai_api_key))
    if exc.kind == "malformed":
        return "The server answered, but its model list is not in the shape Purser reads."
    return f"The server answered HTTP {exc.status}."


def _parse_ok(text: str) -> bool:
    try:
        start, end = text.index("{"), text.rindex("}") + 1
        return json.loads(text[start:end]).get("ok") is True
    except (ValueError, AttributeError):
        return False


def check_connection(
    settings: Settings,
    *,
    client: httpx.Client | None = None,
    resolve: Resolver | None = None,
) -> ConnectionReport:
    """Test connection (#640): is the server reachable, does authentication
    work, does the model answer in the shape Purser parses. Two requests at
    most: the model list (where the server has one) and one tiny completion
    through the same path categorization uses. Every message is built from
    status codes, exception types and the host name, never from the key or
    the provider's response body."""
    key_sent = bool(settings.ai_api_key)
    try:
        url = base_url(settings)
    except AiProviderError as exc:
        return ConnectionReport(
            ok=False,
            reachable=False,
            authenticated=None,
            model_listed=None,
            answered=None,
            network="unknown",
            key_sent=key_sent,
            elapsed_ms=None,
            message=_unreachable(exc, "", settings),
        )
    network = network_of(url, resolve=resolve)
    warning = (
        "This server's address is on the public internet, so what Purser sends "
        "leaves your network. Use the hosted choice if that is intended."
        if settings.ai_provider == "local" and network == "public_internet"
        else None
    )

    def report(**fields: Any) -> ConnectionReport:
        defaults: dict[str, Any] = {
            "ok": False,
            "reachable": True,
            "authenticated": None,
            "model_listed": None,
            "answered": None,
            "network": network,
            "key_sent": key_sent,
            "elapsed_ms": None,
            "warning": warning,
        }
        return ConnectionReport(**(defaults | fields))

    authenticated: bool | None = None
    model_listed: bool | None = None
    try:
        listing = list_models(settings, client=client)
    except AiProviderError as exc:
        if exc.kind in ("config", "transport", "timeout"):
            return report(reachable=False, message=_unreachable(exc, url, settings))
        if exc.status in (401, 403):
            return report(authenticated=False, message=_refused(exc.status, key_sent))
        listing = ModelList(listed=False, models=[])  # e.g. a 500: try the completion
    else:
        if listing.listed:
            authenticated = True
    if not settings.ai_model:
        found = f" It lists {len(listing.models)} models." if listing.listed else ""
        return report(
            authenticated=authenticated,
            message=f"Reached {_where(url)}.{found} Choose a model to finish the test.",
        )
    if listing.listed:
        model_listed = settings.ai_model in listing.models
    started = time.monotonic()
    try:
        text = _complete(settings, TEST_PROMPT, client)
    except AiProviderError as exc:
        if exc.kind in ("config", "transport", "timeout"):
            return report(
                reachable=exc.kind == "timeout",
                authenticated=authenticated,
                model_listed=model_listed,
                message=_unreachable(exc, url, settings),
            )
        if exc.status in (401, 403):
            return report(
                authenticated=False,
                model_listed=model_listed,
                message=_refused(exc.status, key_sent),
            )
        if exc.kind == "malformed":
            message = (
                "The server answered, but not in the chat-completions shape Purser "
                "reads. Check that the base URL is the server's OpenAI-compatible "
                "address (it usually ends in /v1)."
            )
        elif exc.status == 404:
            message = (
                "The server answered HTTP 404: check the base URL (it usually ends in "
                f"/v1) and that it has the model {settings.ai_model!r}."
            )
        else:
            message = f"The model did not answer (HTTP {exc.status})."
        return report(
            authenticated=authenticated, model_listed=model_listed, answered=False, message=message
        )
    elapsed_ms = int((time.monotonic() - started) * 1000)
    if not _parse_ok(text):
        return report(
            authenticated=True,
            model_listed=model_listed,
            answered=False,
            elapsed_ms=elapsed_ms,
            message=(
                "The model answered, but not with the JSON Purser asks for. "
                "Categorization needs a model that follows instructions; try an "
                "instruction-tuned model."
            ),
        )
    unlisted = (
        f" The server does not list {settings.ai_model!r}, but it answered."
        if model_listed is False
        else ""
    )
    return report(
        ok=True,
        authenticated=True,
        model_listed=model_listed,
        answered=True,
        elapsed_ms=elapsed_ms,
        message=f"Connected: {settings.ai_model} answered in {elapsed_ms / 1000:.1f} s.{unlisted}",
    )
