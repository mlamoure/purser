"""Requests page at the Bridge's recommended 45 days (#490): every fetch path
asks for at most BRIDGE_PAGE_DAYS, a first sync reaches 88 days in two
pages, and counts cover every page exactly once."""

from datetime import date, timedelta
from itertools import pairwise
from typing import Any

import pytest
from sqlalchemy import Engine, text

from purser.feed import service, simplefin
from tests import test_feed
from tests.test_feed import FeedHarness
from tests.test_feed_outage import ACCOUNT, PATH, Bank, _descriptions, _row

feed_env = test_feed.feed_env
harness = test_feed.harness

PAGE = simplefin.BRIDGE_PAGE_DAYS


def _span(request: dict[str, Any]) -> int:
    """Whole days a request covers; an open-ended one runs to now, so its
    whole days must stay below the span."""
    end = request["end"] or date.today()
    return (end - request["start"]).days + (0 if request["end"] else 1)


class TestPages:
    @pytest.mark.parametrize("days", [1, 2, 44, 45, 46, 89, 90, 135])
    def test_pages_tile_the_range_within_the_span(self, days: int) -> None:
        start = date(2026, 1, 1)
        end = start + timedelta(days=days - 1)
        got = service.pages(start, end)
        assert got[0][0] == start and got[-1][1] == end
        for page_start, page_end in got:
            # As requested: start-date inclusive, end-date = page_end + 1 exclusive.
            assert 1 <= (page_end + timedelta(days=1) - page_start).days <= PAGE
        for (_, prev_end), (next_start, _) in pairwise(got):
            assert next_start == prev_end  # one day of overlap, no gap

    def test_one_page_when_it_fits(self) -> None:
        start = date(2026, 1, 1)
        assert len(service.pages(start, start + timedelta(days=PAGE - 1))) == 1


class TestFirstSync:
    def test_a_first_sync_reaches_88_days_in_two_pages(
        self, harness: FeedHarness, monkeypatch: pytest.MonkeyPatch, migrated_engine: Engine
    ) -> None:
        # The harness keeps a first sync to one page for its static mocks;
        # this test runs the production reach against a bank honouring windows.
        monkeypatch.setattr(service, "FIRST_SYNC_DAYS", 88)
        bank = Bank(rows=[_row(80, "-40.00", "Old page"), _row(3, "-5.00", "New page")])
        harness.use_transport(bank.transport())
        connection_id = harness.connect()
        harness.map_new(connection_id, ACCOUNT, PATH)
        bank.requests.clear()
        result = harness.sync(connection_id)

        assert len(bank.requests) == 2
        assert all(_span(r) <= PAGE for r in bank.requests)
        assert bank.requests[-1]["end"] is None  # the live window stays open-ended
        assert result["gap_windows"] == 1
        # Counts cover both pages, each row once.
        assert result["imported"] == 2
        assert result["gap_stats"]["imported"] == 1
        assert _descriptions(migrated_engine) == ["New page", "Old page"]

    def test_mapping_after_a_paged_first_sync_imports_both_pages(
        self, harness: FeedHarness, monkeypatch: pytest.MonkeyPatch, migrated_engine: Engine
    ) -> None:
        """Found by the round's end-to-end run: the map re-import read only
        the latest sync's final page, so history on its older page never
        reached an account mapped after a paged first sync."""
        monkeypatch.setattr(service, "FIRST_SYNC_DAYS", 88)
        bank = Bank(rows=[_row(80, "-40.00", "Old page"), _row(3, "-5.00", "New page")])
        harness.use_transport(bank.transport())
        connection_id = harness.connect()
        harness.sync(connection_id)  # account not mapped yet: nothing imported
        assert _descriptions(migrated_engine) == []
        harness.map_new(connection_id, ACCOUNT, PATH)
        assert _descriptions(migrated_engine) == ["New page", "Old page"]


class TestEveryPathPages:
    def test_backfill_windows_are_one_page(self, harness: FeedHarness) -> None:
        bank = Bank(rows=[_row(150, "-1.00", "Older")])
        harness.use_transport(bank.transport())
        connection_id = harness.connect()
        harness.sync(connection_id)
        bank.requests.clear()
        harness.backfill(connection_id, windows=3)
        assert len(bank.requests) == 3
        assert all(_span(r) <= PAGE for r in bank.requests)

    def test_an_outage_repair_pages_its_range(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        bank = Bank(balance_days_ago=70)
        harness.use_transport(bank.transport())
        connection_id = harness.connect()
        harness.sync(connection_id)
        harness.map_new(connection_id, ACCOUNT, PATH)
        harness.sync(connection_id)
        bank.balance_days_ago = 0
        bank.rows = [_row(60, "-60.00", "Early outage"), _row(20, "-20.00", "Late outage")]
        bank.requests.clear()
        repair = harness.sync(connection_id)["gap_repair"]
        focused = [r for r in bank.requests if r["accounts"]]
        assert repair["pages"] == len(focused) == 2
        assert all(_span(r) <= PAGE for r in bank.requests)
        assert repair["imported"] == 2
        assert _descriptions(migrated_engine) == ["Early outage", "Late outage"]

    def test_a_90_day_refetch_is_three_pages_and_idempotent(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        bank = Bank()
        harness.use_transport(bank.transport())
        connection_id = harness.connect()
        harness.sync(connection_id)
        harness.map_new(connection_id, ACCOUNT, PATH)
        bank.rows = [_row(d, f"-{d}.00", f"Row {d}") for d in (85, 50, 10)]
        bank.requests.clear()
        today = date.today()
        body = {
            "start": (today - timedelta(days=89)).isoformat(),  # 90 days inclusive
            "end": today.isoformat(),
        }
        out = harness.client.post(f"/connections/{connection_id}/refetch", json=body).json()
        assert out["pages"] == 3 and len(bank.requests) == 3
        assert all(_span(r) <= PAGE for r in bank.requests)
        assert out["imported"] == 3
        again = harness.client.post(f"/connections/{connection_id}/refetch", json=body).json()
        assert again["imported"] == 0
        assert _descriptions(migrated_engine) == ["Row 10", "Row 50", "Row 85"]

    def test_a_refetch_needing_more_pages_than_the_budget_is_refused(
        self, harness: FeedHarness, migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bank = Bank()
        harness.use_transport(bank.transport())
        connection_id = harness.connect()
        harness.sync(connection_id)
        harness.map_new(connection_id, ACCOUNT, PATH)
        with migrated_engine.connect() as conn:
            fetched = conn.execute(
                text("SELECT COUNT(*) FROM raw_fetches WHERE connection_id = :c"),
                {"c": connection_id},
            ).scalar_one()
        monkeypatch.setattr(simplefin, "BRIDGE_MAX_FETCHES_PER_DAY", fetched + 2)
        today = date.today()
        body = {  # 90 days: three pages, two fetches left
            "start": (today - timedelta(days=89)).isoformat(),
            "end": today.isoformat(),
        }
        resp = harness.client.post(f"/connections/{connection_id}/refetch", json=body)
        assert resp.status_code == 422
        assert "needs 3 fetch(es)" in resp.text
