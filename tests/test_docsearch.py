"""search_docs corpus + ranking unit tests (issue #156, DESIGN decision 41).

Pure — no DB, no server. The MCP-transport path (scopes, audit row, live
index) is covered in test_mcp.py.
"""

from typing import Any

import pytest

from purser.mcp import docsearch

MINI_INDEX = {
    "version": 1,
    "pages": [
        {
            "path": "how-to/backups",
            "title": "Backups",
            "description": "Back up and restore",
            "sections": [
                {"anchor": "", "heading": "", "text": "Overview of backup strategy."},
                {
                    "anchor": "restore",
                    "heading": "Restore",
                    "text": "Restore the database with pg_restore and the original encryption key.",
                },
            ],
        },
        {
            "path": "reference/tokens",
            "title": "Tokens",
            "description": "API tokens",
            "sections": [
                {
                    "anchor": "scopes",
                    "heading": "Scopes",
                    "text": "Tokens carry read propose commit admin scopes. " * 20,
                },
            ],
        },
        {
            "path": "zz/tie",
            "title": "Tokens",
            "description": "",
            "sections": [
                {
                    "anchor": "scopes",
                    "heading": "Scopes",
                    "text": "Tokens carry read propose commit admin scopes. " * 20,
                },
            ],
        },
    ],
}


@pytest.fixture
def mini_index(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(docsearch, "_load_index", lambda: MINI_INDEX)


def test_ranking_prefers_title_and_heading_hits(mini_index: None) -> None:
    hits = docsearch.search("token scopes", 3, None)
    assert hits is not None
    # Identical scores -> deterministic (path, anchor) tie-break.
    assert [h["url"] for h in hits[:2]] == [
        "/reference/tokens/#scopes",
        "/zz/tie/#scopes",
    ]


def test_phrase_bonus_beats_scattered_tokens(mini_index: None) -> None:
    hits = docsearch.search("original encryption key", 1, None)
    assert hits is not None and hits
    assert hits[0]["url"] == "/how-to/backups/#restore"


def test_absolute_urls_from_docs_url(mini_index: None) -> None:
    hits = docsearch.search("restore", 1, "https://docs.example.com/")
    assert hits is not None
    assert hits[0]["url"] == "https://docs.example.com/how-to/backups/#restore"


def test_intro_section_links_to_page_top(mini_index: None) -> None:
    hits = docsearch.search("backup strategy overview", 1, None)
    assert hits is not None
    assert hits[0]["url"] == "/how-to/backups/"
    assert hits[0]["section"] == "Backups"  # falls back to the page title


def test_snippet_is_bounded_and_clean(mini_index: None) -> None:
    hits = docsearch.search("propose", 1, None)
    assert hits is not None
    snippet = hits[0]["snippet"]
    assert len(snippet) <= docsearch._SNIPPET_MAX + len("......")
    assert "\n" not in snippet


def test_no_matches_returns_empty_list(mini_index: None) -> None:
    assert docsearch.search("quux zzyzx", 3, None) == []


def test_stopword_only_query_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(docsearch, "_load_index", lambda: MINI_INDEX)
    # Phrase matching still applies, so pick stopwords that appear nowhere.
    assert docsearch.search("do you", 3, None) == []


def test_missing_index_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(docsearch, "_load_index", lambda: None)
    assert docsearch.search("anything", 3, None) is None


def test_bundled_index_is_present_and_searchable() -> None:
    """The checked-in index ships as package data (wheel + image + dev)."""
    docsearch._load_index.cache_clear()
    index: dict[str, Any] | None = docsearch._load_index()
    assert index is not None and index["version"] == 1
    hits = docsearch.search("encryption key", 3, None)
    assert hits is not None and hits
    assert all(h["url"].startswith("/") for h in hits)
    assert {"title", "section", "url", "snippet"} == set(hits[0])
