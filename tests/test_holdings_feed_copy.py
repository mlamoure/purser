"""The broker's own copy of a trade's cash that Purser already posted (issue
#408, decision 103): at import it takes over as the trade's cash instead of
counting the money twice — on a sync, a gap-window import (outage repair,
manual refetch), and a re-import of the same fetch, which adds nothing.

Synthetic fixtures only: made-up symbols, prices and amounts.
"""

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from purser import db, ledger
from purser.holdings import cash as trade_cash
from purser.holdings import service
from purser.models import Account, AuditLog, PositionEvent, Posting, Transaction
from tests import test_feed
from tests.test_feed import FeedHarness
from tests.test_feed_outage import Bank
from tests.test_holdings_cash import (
    _broker,
    _epoch,
    _hold,
    _postings,
    _symbol,
    _sync,
    _weekday_before,
)

TODAY = datetime.now(UTC).date()

feed_env = test_feed.feed_env
harness = test_feed.harness


def _sell(session: Session, position_id: uuid.UUID, on: date, quantity: str, price: str, **kw):
    return service.record_event(
        session,
        position_id=position_id,
        kind="sell",
        occurred_on=on,
        quantity=Decimal(quantity),
        price=Decimal(price),
        token=None,
        **kw,
    )


def _sold(row_id: str, on: date, amount: str, *, pending: bool = False) -> dict[str, Any]:
    return {
        "id": row_id,
        "posted": _epoch(on),
        "amount": amount,
        "description": f"SOLD {row_id.upper()}",
        **({"pending": True} if pending else {}),
    }


def _rows(session: Session, account: Account) -> list[Transaction]:
    return list(
        session.execute(
            select(Transaction)
            .join(Posting, Posting.transaction_id == Transaction.id)
            .where(Posting.account_id == account.id)
            .order_by(Transaction.created_at)
        )
        .scalars()
        .unique()
    )


class TestTakeOver:
    def test_the_broker_copy_takes_over_purser_cash_and_a_reimport_adds_nothing(
        self, db_session
    ) -> None:
        account = _broker(db_session)
        position = _hold(db_session, account, _symbol(), "100", TODAY - timedelta(days=40))
        trade = _weekday_before(12)
        event = _sell(db_session, position.id, trade, "20", "50")
        purser_row = ledger.get_transaction(db_session, event.transaction_id)
        assert purser_row.date == trade

        copy = _sold("s1", trade + timedelta(days=1), "1000.00")
        stats = _sync(db_session, account, [copy])
        assert stats["imported"] == 1 and stats["linked_trade_cash"] == 1
        assert _postings(db_session, account) == Decimal("1000.0000")  # once, not twice

        db_session.refresh(purser_row)
        assert purser_row.status == "void" and purser_row.superseded_by is not None
        # The bank's row is linked as the trade's cash, as transaction_id
        # links one: a reversal pair whose replacement books equity.securities.
        holder = ledger.get_transaction(db_session, purser_row.superseded_by)
        assert trade_cash.live_cash(db_session, purser_row).id == holder.id
        assert holder.date == trade + timedelta(days=1) and holder.status == "posted"
        assert ledger.has_system_leg(holder.postings) and holder.reverses_id is not None
        reversal = ledger.get_transaction(db_session, holder.reverses_id)
        bank_row = ledger.get_transaction(db_session, reversal.reverses_id)
        assert bank_row.origin == "import" and bank_row.content_hash is not None
        assert bank_row.source_fetch_id is not None and bank_row.description == "SOLD S1"
        assert not ledger.has_system_leg(bank_row.postings)

        audit = db_session.execute(
            select(AuditLog).where(
                AuditLog.action == "position.feed_cash", AuditLog.entity_id == position.id
            )
        ).scalar_one()
        after = audit.before_after["after"]
        assert after["event_id"] == str(event.id)
        assert after["superseded_id"] == str(purser_row.id)
        assert after["feed_transaction_id"] == str(bank_row.id)
        assert after["cash_transaction_id"] == str(holder.id)
        assert after["amount"] == "1000.0000"

        rows_before = len(_rows(db_session, account))
        again = _sync(db_session, account, [copy])
        assert again["imported"] == 0 and again["linked_trade_cash"] == 0
        assert again["skipped"] == 1
        assert len(_rows(db_session, account)) == rows_before
        assert _postings(db_session, account) == Decimal("1000.0000")

    def test_a_funded_buy_is_taken_over_too(self, db_session) -> None:
        account = _broker(db_session)
        position = _hold(db_session, account, _symbol(), "10", TODAY - timedelta(days=40))
        trade = _weekday_before(12)
        service.record_event(
            db_session,
            position_id=position.id,
            kind="buy",
            occurred_on=trade,
            quantity=Decimal("4"),
            price=Decimal("25"),
            fees=Decimal("1.50"),
            funded_from="cash",
            token=None,
        )
        stats = _sync(db_session, account, [_sold("b1", trade + timedelta(days=2), "-101.50")])
        assert stats["linked_trade_cash"] == 1
        assert _postings(db_session, account) == Decimal("-101.5000")

    def test_a_pending_copy_is_not_stored_and_its_posted_row_takes_over(self, db_session) -> None:
        account = _broker(db_session)
        position = _hold(db_session, account, _symbol(), "50", TODAY - timedelta(days=40))
        trade = _weekday_before(12)
        event = _sell(db_session, position.id, trade, "1", "100")
        pending = _sold("p1", trade + timedelta(days=1), "100.00", pending=True)
        stats = _sync(db_session, account, [pending], window_start=trade - timedelta(days=7))
        assert stats["imported"] == 0 and stats["skipped_trade_cash_pending"] == 1
        assert _postings(db_session, account) == Decimal("100.0000")
        assert ledger.get_transaction(db_session, event.transaction_id).status == "posted"

        posted = {**pending}
        posted.pop("pending")
        stats = _sync(db_session, account, [posted], window_start=trade - timedelta(days=7))
        assert stats["linked_trade_cash"] == 1
        assert _postings(db_session, account) == Decimal("100.0000")

    def test_only_a_copy_inside_the_window_and_of_the_exact_amount_takes_over(
        self, db_session
    ) -> None:
        """A row of another amount, dated before the trade or after the copy
        window (five days after T+1 or settle_on) is an ordinary row."""
        account = _broker(db_session)
        position = _hold(db_session, account, _symbol(), "50", TODAY - timedelta(days=40))
        trade = _weekday_before(20)
        _sell(db_session, position.id, trade, "1", "100")
        end = trade_cash.copy_window_end(trade, trade)
        stats = _sync(
            db_session,
            account,
            [
                _sold("x1", trade - timedelta(days=1), "100.00"),
                _sold("x2", trade + timedelta(days=1), "100.01"),
                _sold("x3", end + timedelta(days=1), "100.00"),
            ],
        )
        assert stats["imported"] == 3 and stats["linked_trade_cash"] == 0
        stats = _sync(db_session, account, [_sold("x4", end, "100.00")])
        assert stats["linked_trade_cash"] == 1

    def test_two_sales_of_one_amount_each_take_one_copy_oldest_first(self, db_session) -> None:
        account = _broker(db_session)
        position = _hold(db_session, account, _symbol(), "50", TODAY - timedelta(days=40))
        first_day = _weekday_before(14)
        second_day = first_day + timedelta(days=1)
        first = _sell(db_session, position.id, first_day, "1", "100")
        second = _sell(db_session, position.id, second_day, "1", "100")
        stats = _sync(
            db_session,
            account,
            [
                _sold("c1", second_day, "100.00"),
                _sold("c2", second_day + timedelta(days=1), "100.00"),
                _sold("c3", second_day + timedelta(days=1), "100.00"),
            ],
        )
        assert stats["imported"] == 3 and stats["linked_trade_cash"] == 2
        assert _postings(db_session, account) == Decimal("300.0000")  # two sales + c3
        rows = {e.id: ledger.get_transaction(db_session, e.transaction_id) for e in (first, second)}
        takers = {ledger.get_transaction(db_session, r.superseded_by).date for r in rows.values()}
        assert takers == {second_day, second_day + timedelta(days=1)}
        holder = ledger.get_transaction(db_session, rows[first.id].superseded_by)
        assert holder.description == "SOLD C1"  # the oldest trade takes the first copy


class TestTheTradeStillOwnsItsCash:
    def _taken_over(self, session: Session):
        account = _broker(session)
        position = _hold(session, account, _symbol(), "100", TODAY - timedelta(days=40))
        trade = _weekday_before(12)
        event = _sell(session, position.id, trade, "20", "50")
        _sync(session, account, [_sold("t1", trade + timedelta(days=1), "1000.00")])
        purser_row = ledger.get_transaction(session, event.transaction_id)
        holder = ledger.get_transaction(session, purser_row.superseded_by)
        reversal = ledger.get_transaction(session, holder.reverses_id)
        bank_row = ledger.get_transaction(session, reversal.reverses_id)
        return account, position, event, holder, bank_row

    def test_reversing_the_trade_gives_the_bank_row_its_category_and_keeps_its_cash(
        self, db_session
    ) -> None:
        account, position, event, _holder, bank_row = self._taken_over(db_session)
        reversal = service.reverse_event(db_session, event_id=event.id, token=None)
        assert service.quantity_on(db_session, position, TODAY) == Decimal("100")
        assert reversal.transaction_id is not None
        head = ledger.get_transaction(db_session, reversal.transaction_id)
        assert not ledger.has_system_leg(head.postings)
        assert {p.account.path for p in head.postings if p.account.is_virtual} == {
            "income.uncategorized"
        }
        # The bank's money stays: it is an ordinary deposit again.
        assert _postings(db_session, account) == Decimal("1000.0000")
        assert not ledger.is_trade_cash_lineage(db_session, bank_row)

    def test_review_names_the_trade_and_the_purge_refuses_the_row(self, db_session) -> None:
        from purser import review
        from purser.admin import feed_repair

        account, position, event, holder, bank_row = self._taken_over(db_session)
        batch = review.batch(db_session, limit=50, account_ref=str(account.id))
        (row,) = [r for r in batch["rows"] if r["id"] == str(holder.id)]
        assert "securities_trade" in row["flags"]
        assert row["locked"] == {"event_id": str(event.id), "position_id": str(position.id)}
        assert not [r for r in batch["rows"] if r["id"] == str(event.transaction_id)]
        (described,) = feed_repair._trade_links(db_session, [bank_row.id])
        assert str(event.id) in described

    def test_the_rows_stay_with_the_trade(self, db_session) -> None:
        _account, _position, _event, holder, bank_row = self._taken_over(db_session)
        claimed = ledger.trade_cash_lineage_set(db_session)
        assert {holder.id, bank_row.id} <= claimed


# --------------------------------------------------------------------------
# The gap-window paths (#481): outage repair and a manual refetch import with
# window_start=None; the take-over works there too.
# --------------------------------------------------------------------------


def _manual_broker(harness: FeedHarness, bank: Bank, *, frozen_days: int = 0) -> tuple[str, str]:
    """Connect, map the bank's account as a hand-tracked brokerage account
    holding 100 shares, and sync once more. Returns (connection, account)."""
    bank.balance_days_ago = frozen_days
    harness.use_transport(bank.transport())
    connection_id = harness.connect()
    harness.sync(connection_id)
    resp = harness.client.post(
        f"/connections/{connection_id}/map",
        json={
            "external_id": "Demo Checking",
            "create": {"name": "Plan", "type": "brokerage", "path": "assets.plan"},
        },
    )
    assert resp.status_code == 200, resp.text
    account_id = harness.client.get("/accounts").json()
    (account,) = [a for a in account_id if a["path"] == "assets.plan"]
    resp = harness.client.patch(f"/accounts/{account['id']}", json={"holdings_source": "manual"})
    assert resp.status_code == 200, resp.text
    harness.sync(connection_id)
    return connection_id, account["id"]


def _record_sale(harness: FeedHarness, account_id: str, on: date) -> dict[str, Any]:
    symbol = _symbol()
    resp = harness.client.post(
        f"/accounts/{account_id}/positions",
        json={
            "symbol": symbol,
            "quantity": "100",
            "as_of": (TODAY - timedelta(days=60)).isoformat(),
        },
    )
    assert resp.status_code == 201, resp.text
    resp = harness.client.post(
        f"/positions/{resp.json()['id']}/events",
        json={"kind": "sell", "occurred_on": on.isoformat(), "quantity": "20", "price": "50"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _cash_rows(engine: Engine, account_id: str) -> dict[str, int]:
    with db.get_sessionmaker()() as session:
        rows = session.execute(
            select(Transaction.status, func.count())
            .join(Posting, Posting.transaction_id == Transaction.id)
            .where(Posting.account_id == uuid.UUID(account_id))
            .group_by(Transaction.status)
        ).all()
        return {status: count for status, count in rows}


class TestGapWindowPaths:
    def test_a_manual_refetch_hands_the_cash_over_once(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        bank = Bank()
        connection_id, account_id = _manual_broker(harness, bank)
        trade = _weekday_before(40)
        _record_sale(harness, account_id, trade)
        bank.rows = [
            {
                "id": "gap-sold",
                "posted": _epoch(trade + timedelta(days=1)),
                "amount": "1000.00",
                "description": "SOLD 20",
            }
        ]
        body = {
            "start": (trade - timedelta(days=2)).isoformat(),
            "end": (trade + timedelta(days=5)).isoformat(),
            "accounts": ["assets.plan"],
        }
        out = harness.client.post(f"/connections/{connection_id}/refetch", json=body).json()
        assert out["imported"] == 1 and out["linked_trade_cash"] == 1, out
        assert _cash_rows(migrated_engine, account_id) == {"posted": 3, "void": 1}
        again = harness.client.post(f"/connections/{connection_id}/refetch", json=body).json()
        assert again["imported"] == 0 and again["linked_trade_cash"] == 0
        assert _cash_rows(migrated_engine, account_id) == {"posted": 3, "void": 1}

    def test_an_outage_repair_hands_the_cash_over(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        bank = Bank()
        connection_id, account_id = _manual_broker(harness, bank, frozen_days=30)
        trade = _weekday_before(20)
        _record_sale(harness, account_id, trade)
        # The bank recovers and lists the sale's cash from during the outage.
        bank.balance_days_ago = 0
        bank.rows = [
            {
                "id": "outage-sold",
                "posted": _epoch(trade + timedelta(days=1)),
                "amount": "1000.00",
                "description": "SOLD 20",
            }
        ]
        repair = harness.sync(connection_id)["gap_repair"]
        assert repair is not None and repair["linked_trade_cash"] == 1, repair
        assert _cash_rows(migrated_engine, account_id) == {"posted": 3, "void": 1}
        harness.sync(connection_id)
        assert _cash_rows(migrated_engine, account_id) == {"posted": 3, "void": 1}


# --------------------------------------------------------------------------
# Property (guardrail 3): over random sales and random feed rows — copies
# inside and outside their windows, other amounts — importing is idempotent
# and no trade's cash is ever counted twice.
# --------------------------------------------------------------------------

_SETTINGS = settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)

_sales = st.lists(
    st.tuples(st.integers(min_value=0, max_value=6), st.sampled_from(["1", "2", "3"])),
    min_size=1,
    max_size=4,
)
_feed = st.lists(
    st.tuples(
        st.integers(min_value=-1, max_value=12),  # days after the first sale
        st.sampled_from(["100.00", "200.00", "300.00", "150.00"]),
        st.booleans(),  # pending
    ),
    min_size=0,
    max_size=6,
    # One posted row per (date, amount): two different rows of one date and amount
    # delivered by separate fetches are decision 74's re-description case,
    # whose heuristic is not what this property is about.
    unique_by=lambda row: (row[0], row[1], row[2]),
)


@_SETTINGS
@given(_sales, _feed, st.integers(min_value=1, max_value=3))
def test_importing_feed_copies_is_idempotent_and_counts_each_sale_once(
    migrated_engine: Engine, sales: list, feed: list, chunks: int
) -> None:
    with db.get_sessionmaker()() as session:
        account = _broker(session)
        position = _hold(session, account, _symbol(), "100", TODAY - timedelta(days=60))
        start = _weekday_before(30)
        proceeds = Decimal(0)
        for offset, quantity in sales:
            _sell(session, position.id, start + timedelta(days=offset), quantity, "100")
            proceeds += Decimal(quantity) * 100
        rows = [
            _sold(f"f{chr(97 + i)}", start + timedelta(days=offset), amount, pending=pending)
            for i, (offset, amount, pending) in enumerate(feed)
        ]
        # The rows arrive over one to three fetches, then all of them again.
        size = max(1, -(-len(rows) // chunks))
        for i in range(0, len(rows), size):
            _sync(session, account, rows[i : i + size])
        count = len(_rows(session, account))
        stats = _sync(session, account, rows)
        assert stats["imported"] == 0 and stats["linked_trade_cash"] == 0
        assert len(_rows(session, account)) == count

        live = [t for t in _rows(session, account) if t.status in ("pending", "posted")]
        trade_cash_rows = [t for t in live if ledger.has_system_leg(t.postings)]
        # Each standing sale has exactly one live cash row: Purser's own or
        # the broker's copy that took it over — never both.
        assert (
            sum(
                (
                    Decimal(p.amount)
                    for t in trade_cash_rows
                    for p in t.postings
                    if p.account_id == account.id
                ),
                Decimal(0),
            )
            == proceeds
        )
        events = session.execute(
            select(PositionEvent).where(
                PositionEvent.position_id == position.id, PositionEvent.transaction_id.is_not(None)
            )
        ).scalars()
        holders = [
            trade_cash.live_cash(session, ledger.get_transaction(session, e.transaction_id))
            for e in events
        ]
        assert len({h.id for h in holders}) == len(holders) == len(sales)
        assert all(h.status == "posted" and ledger.has_system_leg(h.postings) for h in holders)
        # A voided Purser row is superseded by exactly one live broker row.
        superseded = [t for t in _rows(session, account) if t.status == "void" and t.superseded_by]
        assert len({t.superseded_by for t in superseded}) == len(superseded)
        # Every live row's postings balance (guardrail 3).
        for t in live:
            assert sum((Decimal(p.amount) for p in t.postings), Decimal(0)) == 0
        # Purser's cash rows superseded = the broker rows linked in their place.
        assert len(superseded) == sum(1 for t in trade_cash_rows if t.reverses_id is not None)
        session.rollback()
