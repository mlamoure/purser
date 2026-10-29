"""search_docs corpus + ranking (DESIGN decision 41).

The corpus is purser/mcp/docs_index.json — generated from the docs-site
markdown by scripts/export_docs_index.py, checked in, drift-gated in CI.
Bundling it as package data means every install mode (image, wheel/uvx,
air-gapped) answers doc searches offline and deterministically; the deployed
docs site only decorates result links (settings.docs_url).

Pure functions, stdlib only. No DB session — the audited_tool wrapper on the
MCP side records the read on its own short session.
"""

import json
import re
from functools import lru_cache
from importlib import resources
from typing import Any, TypedDict

_TOKEN_RE = re.compile(r"[a-z0-9_]{2,}")
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "can",
        "do",
        "does",
        "for",
        "from",
        "how",
        "i",
        "in",
        "is",
        "it",
        "my",
        "of",
        "on",
        "or",
        "the",
        "this",
        "to",
        "what",
        "when",
        "where",
        "which",
        "with",
        "you",
        "your",
    }
)

_TITLE_WEIGHT = 3
_HEADING_WEIGHT = 2
_BODY_WEIGHT = 1
_BODY_TF_CAP = 3
_ALL_TOKENS_BONUS = 2
_PHRASE_BONUS = 3
_SNIPPET_RADIUS = 120
_SNIPPET_MAX = 240


class Hit(TypedDict):
    title: str
    section: str
    url: str
    snippet: str


@lru_cache(maxsize=1)
def _load_index() -> dict[str, Any] | None:
    """The bundled index, or None when absent/corrupt (never raises)."""
    try:
        raw = (resources.files("purser.mcp") / "docs_index.json").read_text(encoding="utf-8")
        index = json.loads(raw)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(index, dict) or not isinstance(index.get("pages"), list):
        return None
    return index


def _tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS]


def _page_url(path: str, anchor: str, docs_url: str | None) -> str:
    tail = f"/{path}/" if path else "/"
    if anchor:
        tail += f"#{anchor}"
    return f"{docs_url.rstrip('/')}{tail}" if docs_url else tail


def _snippet(text: str, tokens: list[str], phrase: str) -> str:
    lowered = text.lower()
    pos = lowered.find(phrase) if phrase else -1
    if pos < 0:
        positions = [p for p in (lowered.find(t) for t in tokens) if p >= 0]
        pos = min(positions) if positions else 0
    start = max(0, pos - _SNIPPET_RADIUS)
    end = min(len(text), pos + _SNIPPET_RADIUS)
    clipped = re.sub(r"\s+", " ", text[start:end]).strip()[:_SNIPPET_MAX]
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(text) else ""
    return f"{prefix}{clipped}{suffix}"


def search(query: str, limit: int, docs_url: str | None) -> list[Hit] | None:
    """Ranked doc sections for query; None means no index is bundled.

    Deterministic: score desc, then (path, anchor) asc. Sections score
    3x title-token hits + 2x heading hits + 1x body hits (tf capped), with
    all-tokens-present and exact-phrase bonuses.
    """
    index = _load_index()
    if index is None:
        return None
    tokens = _tokenize(query)
    phrase = re.sub(r"\s+", " ", query.lower().strip())
    if not tokens and not phrase:
        return []

    scored: list[tuple[int, str, str, Hit]] = []
    for page in index["pages"]:
        title = page["title"]
        title_lower = title.lower()
        sections = page["sections"] or [{"anchor": "", "heading": "", "text": ""}]
        for section in sections:
            heading = section["heading"]
            text = section["text"]
            heading_lower = heading.lower()
            text_lower = text.lower()
            score = 0
            all_present = bool(tokens)
            for token in tokens:
                in_title = token in title_lower
                in_heading = token in heading_lower
                body_tf = min(text_lower.count(token), _BODY_TF_CAP)
                if in_title:
                    score += _TITLE_WEIGHT
                if in_heading:
                    score += _HEADING_WEIGHT
                score += _BODY_WEIGHT * body_tf
                if not (in_title or in_heading or body_tf):
                    all_present = False
            if all_present:
                score += _ALL_TOKENS_BONUS
            if phrase and (
                phrase in text_lower or phrase in heading_lower or phrase in title_lower
            ):
                score += _PHRASE_BONUS
            if score <= 0:
                continue
            hit: Hit = {
                "title": title,
                "section": heading or title,
                "url": _page_url(page["path"], section["anchor"], docs_url),
                "snippet": _snippet(text or page["description"], tokens, phrase),
            }
            scored.append((score, page["path"], section["anchor"], hit))

    scored.sort(key=lambda item: (-item[0], item[1], item[2]))
    return [hit for _, _, _, hit in scored[:limit]]
