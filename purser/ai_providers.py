"""BYO model providers for categorization (DESIGN §11).

Two code paths only: any OpenAI-compatible endpoint (Ollama, LM Studio,
vLLM, OpenRouter, OpenAI) and native Anthropic. Off by default; the model
never sees more than one transaction's payee/description/amount and the
category list.
"""

import json
from contextlib import nullcontext
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx

from purser.config import Settings


@dataclass(frozen=True)
class Suggestion:
    category_path: str
    confidence: float
    model: str


class AiProviderError(Exception):
    pass


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


def _post(
    url: str, body: dict[str, Any], headers: dict[str, str], client: httpx.Client | None
) -> httpx.Response:
    """POST to the provider; any transport failure (connect, timeout, bad
    URL) becomes AiProviderError — sanitized, never the API key."""
    try:
        with nullcontext(client) if client is not None else httpx.Client(timeout=30) as http:
            resp = http.post(url, json=body, headers=headers)
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        raise AiProviderError(f"provider request failed: {type(exc).__name__}") from exc
    if resp.status_code != 200:
        raise AiProviderError(f"provider returned HTTP {resp.status_code}")
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
            f"provider returned malformed response: {type(exc).__name__}"
        ) from exc
    if not isinstance(node, str):
        raise AiProviderError("provider returned malformed response: non-string content")
    return node


def _complete(settings: Settings, prompt: str, client: httpx.Client | None) -> str:
    """One prompt in, raw reply text out. Shared by the categorizer and the
    backend reviewer so both inherit the same sanitized-error handling."""
    if settings.ai_provider == "openai":
        return _openai_text(settings, prompt, client=client)
    if settings.ai_provider == "anthropic":
        return _anthropic_text(settings, prompt, client=client)
    raise AiProviderError(f"unknown ai_provider {settings.ai_provider!r}")


def _openai_text(settings: Settings, prompt: str, *, client: httpx.Client | None) -> str:
    headers = {"Content-Type": "application/json"}
    if settings.ai_api_key:
        headers["Authorization"] = f"Bearer {settings.ai_api_key}"
    body = {
        "model": settings.ai_model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
    }
    resp = _post(f"{settings.ai_base_url.rstrip('/')}/chat/completions", body, headers, client)
    return _envelope_text(resp, ("choices", 0, "message", "content"))


def _anthropic_text(settings: Settings, prompt: str, *, client: httpx.Client | None) -> str:
    headers = {
        "Content-Type": "application/json",
        "x-api-key": settings.ai_api_key or "",
        "anthropic-version": "2023-06-01",
    }
    body = {
        "model": settings.ai_model,
        "max_tokens": 200,
        "messages": [{"role": "user", "content": prompt}],
    }
    resp = _post("https://api.anthropic.com/v1/messages", body, headers, client)
    return _envelope_text(resp, ("content", 0, "text"))


@dataclass(frozen=True)
class ReviewVerdict:
    """What the backend reviewer decided about one row (issue #175)."""

    escalate: bool
    note: str | None
    model: str


def _review_prompt(row: dict[str, Any], guidance: list[str]) -> str:
    checklist = "\n".join(f"- {line}" for line in guidance)
    return (
        "You are reviewing one personal-finance transaction that landed "
        "without human eyes. Decide whether it looks correct as recorded.\n"
        f"Checklist:\n{checklist}\n"
        f"Transaction: {json.dumps(row, default=str)}\n"
        'Reply with ONLY a JSON object: {"ok": true|false, "note": "<one short '
        'sentence, or null>"}. Use ok=false when a human should look — an '
        "ambiguous transfer pair, a category that contradicts the payee, "
        "anything you cannot settle from what you were given."
    )


def _parse_review(text: str, model: str) -> ReviewVerdict | None:
    try:
        start, end = text.index("{"), text.rindex("}") + 1
        data = json.loads(text[start:end])
        ok = bool(data["ok"])
    except (ValueError, KeyError, TypeError):
        return None
    note = data.get("note")
    return ReviewVerdict(escalate=not ok, note=str(note) if note else None, model=model)


def review_row(
    settings: Settings,
    *,
    row: dict[str, Any],
    guidance: list[str],
    client: httpx.Client | None = None,
) -> ReviewVerdict | None:
    """One reviewer verdict, through the same provider plumbing and the same
    sanitized-error handling the categorizer uses."""
    if settings.ai_provider == "none" or not settings.ai_model:
        return None
    text = _complete(settings, _review_prompt(row, guidance), client)
    return _parse_review(text, str(settings.ai_model))
