# ruff: noqa: F811 - pytest fixtures imported from test_mcp_lots are parameters here
"""Independent transport response-budget regression."""

import json
from datetime import timedelta

from tests.conftest import today
from tests.test_mcp import _mcp_call
from tests.test_mcp_lots import (  # noqa: F401 - fixtures used by name
    _as_of,
    live_server,
    tokens,
)


def test_review_long_vest_schedule_is_paged(live_server, tokens):
    args = {
        "account": "assets.plan",
        "as_of": _as_of().isoformat(),
        "captured_at": "2026-11-01T16:00:00+00:00",
        "completeness": "complete",
        "awards": [
            {
                "grant_ref": "REVIEW-G",
                "symbol": "SYNT",
                "granted_on": "2025-02-01",
                "granted_quantity": "400",
                "schedule": [
                    {
                        "vest_date": (today() + timedelta(days=90 * (i + 1))).isoformat(),
                        "quantity": "10",
                    }
                    for i in range(40)
                ],
            }
        ],
    }
    [preview] = _mcp_call(live_server.url, tokens["agent"], [("upsert_awards", args)])
    [applied, page] = _mcp_call(
        live_server.url,
        tokens["agent"],
        [
            ("upsert_awards", {**args, "dry_run": False, "expect_hash": preview["plan_hash"]}),
            ("list_awards", {"account": "assets.plan", "award": "REVIEW-G"}),
        ],
    )
    assert applied["applied"]
    assert len(json.dumps(page)) <= 2100, "Schedule details must page, not bypass MCP budget"


def _awards_args(n_vests: int, grants: int = 1) -> dict:
    return {
        "account": "assets.plan",
        "as_of": _as_of().isoformat(),
        "captured_at": "2026-11-01T16:00:00+00:00",
        "completeness": "complete",
        "awards": [
            {
                "grant_ref": f"PAGE-G{g:02d}",
                "symbol": f"SY{g:02d}",
                "granted_on": "2025-02-01",
                "granted_quantity": str(10 * n_vests),
                "plan": "A deliberately long synthetic plan label " * 4,
                "schedule": [
                    {
                        "vest_date": (today() + timedelta(days=30 * (i + 1))).isoformat(),
                        "quantity": "10",
                    }
                    for i in range(n_vests)
                ],
            }
            for g in range(grants)
        ],
    }


def _load(live_server, tokens, args):
    [preview] = _mcp_call(live_server.url, tokens["agent"], [("upsert_awards", args)])
    [applied] = _mcp_call(
        live_server.url,
        tokens["agent"],
        [("upsert_awards", {**args, "dry_run": False, "expect_hash": preview["plan_hash"]})],
    )
    assert applied["applied"]


def _pages(live_server, tokens, args):
    pages, cursor = [], None
    while True:
        call = {**args, **({"cursor": cursor} if cursor else {})}
        [page] = _mcp_call(live_server.url, tokens["agent"], [("list_awards", call)])
        assert "error" not in page, page
        assert len(json.dumps(page)) <= 2100
        pages.append(page)
        cursor = page["next_cursor"]
        if cursor is None:
            return pages
        assert len(pages) < 50


def test_review_every_vest_once_across_pages(live_server, tokens):
    _load(live_server, tokens, _awards_args(40))
    pages = _pages(live_server, tokens, {"account": "assets.plan", "award": "PAGE-G00"})
    vests = [v["vest_id"] for p in pages for v in p["vests"]]
    assert len(pages) > 1 and len(vests) == len(set(vests)) == 40
    assert all(p["total"] == 40 for p in pages)
    # Every page describes the same award, and only the first carries the summary.
    assert {p["awards"][0]["award_id"] for p in pages} == {pages[0]["awards"][0]["award_id"]}
    assert pages[0]["unvested"] and all(not p["unvested"] for p in pages[1:])


def test_review_many_awards_page_and_summary_is_bounded(live_server, tokens):
    _load(live_server, tokens, _awards_args(2, grants=25))
    pages = _pages(live_server, tokens, {"account": "assets.plan"})
    ids = [a["award_id"] for p in pages for a in p["awards"]]
    assert len(ids) == len(set(ids)) == 25
    assert pages[0]["unvested_truncated"] is True
    symbol = _pages(live_server, tokens, {"account": "assets.plan", "symbol": "SY07"})
    assert [a["symbol"] for p in symbol for a in p["awards"]] == ["SY07"]
    assert [u["symbol"] for u in symbol[0]["unvested"]] == ["SY07"]
    [holdings] = _mcp_call(
        live_server.url, tokens["agent"], [("list_holdings", {"account": "assets.plan"})]
    )
    entry = holdings["accounts"][0]
    assert len(entry["unvested"]) == 3 and entry["unvested_more"] == 22
    assert len(json.dumps(holdings)) <= 2100
