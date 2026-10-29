"""Manual holdings, end to end against real Postgres (issue #390, decision 88):
the position service, its timeline validation, the snapshot projection, and
switching an account's holdings source.

Properties (hypothesis):
- regenerating the projection yields identical rows, and a second pass
  changes nothing;
- every materialized date carries every position held that day, and the
  newest set at or before any day is the timeline on that day;
- feed rows are never touched, and no manual row is dated on or before the
  account's last feed snapshot.

Fixtures are synthetic: TEST / ACME-style symbols, made-up prices.
"""

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from purser import db
from purser.core import holdings as core
from purser.holdings import prices as price_service
from purser.holdings import projection, service
from purser.ledger import LedgerError
from purser.models import Account, HoldingsSnapshot, Position, PositionEvent, SecurityPrice

TODAY = datetime.now(UTC).date()


def _symbol(prefix: str = "T") -> str:
    """A symbol of its own per test/example: security_prices is global."""
    return f"{prefix}{uuid.uuid4().hex[:8].upper()}"


def _brokerage(session: Session, *, manual: bool = True) -> Account:
    from purser import ledger

    account = ledger.create_account(
        session,
        name="Broker",
        type_="brokerage",
        path=f"assets.broker_{uuid.uuid4().hex[:10]}",
        actor_token_id=None,
    )
    if manual:
        ledger.update_account(
            session, account_id=account.id, holdings_source="manual", actor_token_id=None
        )
    session.flush()
    return account


def _manual_rows(session: Session, account_id: uuid.UUID) -> list[tuple]:
    return [
        (r.as_of, r.position_id, r.quantity, r.cost_basis, r.market_value, r.price, r.price_as_of)
        for r in session.execute(
            select(HoldingsSnapshot)
            .where(HoldingsSnapshot.account_id == account_id, HoldingsSnapshot.source == "manual")
            .order_by(HoldingsSnapshot.as_of, HoldingsSnapshot.position_id)
        ).scalars()
    ]


def _price(session: Session, symbol: str, on: date, close: str, source: str = "tiingo") -> None:
    session.add(SecurityPrice(symbol=symbol, price_date=on, source=source, close=Decimal(close)))
    session.flush()


def _mapped(session: Session, account: Account) -> Account:
    """Give ``account`` a feed mapping: only a mapped account can take its
    holdings from the feed (decision 88)."""
    from purser.models import Connection

    connection = Connection(provider="simplefin")
    session.add(connection)
    session.flush()
    account.connection_id = connection.id
    account.external_id = f"SF-{uuid.uuid4().hex[:8]}"
    session.flush()
    return account


def _feed_snapshot(
    session: Session, account_id: uuid.UUID, on: date, symbol: str, quantity: str, market: str
) -> None:
    session.add(
        HoldingsSnapshot(
            account_id=account_id,
            as_of=on,
            symbol=symbol,
            quantity=Decimal(quantity),
            market_value=Decimal(market),
        )
    )
    session.flush()


# ---------------------------------------------------------------------------
# The service: timeline validation, C5, reversals, close
# ---------------------------------------------------------------------------


class TestRecordAndValidate:
    def test_record_position_opens_once_then_records(self, db_session: Session) -> None:
        account = _brokerage(db_session)
        symbol = _symbol()
        first = service.record_position(
            db_session,
            account_ref=str(account.id),
            symbol=symbol.lower(),
            quantity=Decimal("10"),
            as_of=TODAY - timedelta(days=5),
            token=None,
        )
        again = service.record_position(
            db_session,
            account_ref=account.path,
            symbol=symbol,
            quantity=Decimal("12"),
            as_of=TODAY,
            token=None,
        )
        assert first.created and not again.created
        assert first.position.id == again.position.id
        assert first.position.symbol == symbol  # normalized upper case
        assert first.position.opened_on == TODAY - timedelta(days=5)

    def test_future_dated_events_are_refused(self, db_session: Session) -> None:
        """C5: the projection runs to today, so a future event is refused."""
        account = _brokerage(db_session)
        with pytest.raises(LedgerError, match="in the future"):
            service.record_position(
                db_session,
                account_ref=str(account.id),
                symbol=_symbol(),
                quantity=Decimal("1"),
                as_of=TODAY + timedelta(days=1),
                token=None,
            )

    def test_backdated_event_that_breaks_a_later_point_names_the_date(
        self, db_session: Session
    ) -> None:
        account = _brokerage(db_session)
        recorded = service.record_position(
            db_session,
            account_ref=str(account.id),
            symbol=_symbol(),
            quantity=Decimal("10"),
            as_of=TODAY - timedelta(days=20),
            token=None,
        )
        sell_day = TODAY - timedelta(days=5)
        service.record_event(
            db_session,
            position_id=recorded.position.id,
            kind="sell",
            occurred_on=sell_day,
            quantity=Decimal("8"),
            token=None,
        )
        with pytest.raises(LedgerError, match=sell_day.isoformat()):
            service.record_event(
                db_session,
                position_id=recorded.position.id,
                kind="set_quantity",
                occurred_on=TODAY - timedelta(days=10),
                quantity=Decimal("3"),
                token=None,
            )

    def test_backdating_moves_opened_on_earlier(self, db_session: Session) -> None:
        account = _brokerage(db_session)
        recorded = service.record_position(
            db_session,
            account_ref=str(account.id),
            symbol=_symbol(),
            quantity=Decimal("1"),
            as_of=TODAY,
            token=None,
        )
        service.record_event(
            db_session,
            position_id=recorded.position.id,
            kind="buy",
            occurred_on=TODAY - timedelta(days=30),
            quantity=Decimal("1"),
            token=None,
        )
        assert recorded.position.opened_on == TODAY - timedelta(days=30)

    def test_shape_rules(self, db_session: Session) -> None:
        account = _brokerage(db_session)
        recorded = service.record_position(
            db_session,
            account_ref=str(account.id),
            symbol=_symbol(),
            quantity=Decimal("1"),
            as_of=TODAY,
            token=None,
        )
        pid = recorded.position.id
        cases = [
            ({"kind": "set_quantity", "price": Decimal("1")}, "no price or fees"),
            ({"kind": "buy", "cost_basis": Decimal("1")}, "cost_basis applies"),
            ({"kind": "buy", "quantity": Decimal("0")}, "above zero"),
            ({"kind": "sell", "quantity": Decimal("0.0000001")}, "6 decimal places"),
            ({"kind": "split"}, "kind must be one of"),
        ]
        for overrides, message in cases:
            args = {"kind": "buy", "quantity": Decimal("1"), **overrides}
            with pytest.raises(LedgerError, match=message):
                service.record_event(
                    db_session, position_id=pid, occurred_on=TODAY, token=None, **args
                )

    def test_positions_need_a_manual_brokerage_account(self, db_session: Session) -> None:
        feed = _brokerage(db_session, manual=False)
        with pytest.raises(LedgerError, match="takes its holdings from the feed"):
            service.create_position(
                db_session, account_ref=str(feed.id), symbol=_symbol(), token=None
            )
        from purser import ledger

        checking = ledger.create_account(
            db_session,
            name="Chk",
            type_="checking",
            path=f"assets.chk_{uuid.uuid4().hex[:8]}",
            actor_token_id=None,
        )
        with pytest.raises(LedgerError, match="checking account"):
            service.create_position(
                db_session, account_ref=str(checking.id), symbol=_symbol(), token=None
            )

    def test_one_open_position_per_symbol(self, db_session: Session) -> None:
        account = _brokerage(db_session)
        symbol = _symbol()
        service.create_position(db_session, account_ref=str(account.id), symbol=symbol, token=None)
        with pytest.raises(service.PositionConflict):
            service.create_position(
                db_session, account_ref=str(account.id), symbol=symbol, token=None
            )

    def test_events_are_append_only_in_the_database(self, db_session: Session) -> None:
        account = _brokerage(db_session)
        recorded = service.record_position(
            db_session,
            account_ref=str(account.id),
            symbol=_symbol(),
            quantity=Decimal("1"),
            as_of=TODAY,
            token=None,
        )
        db_session.commit()
        from sqlalchemy.exc import DBAPIError

        with pytest.raises(DBAPIError, match="append-only"):
            db_session.execute(
                text("UPDATE position_events SET quantity = 2 WHERE id = :id"),
                {"id": recorded.event.id},
            )
        db_session.rollback()


class TestReplace:
    """#401: correcting a same-day event with ``replaces`` keeps its place."""

    def _day(self, session: Session) -> tuple[Account, Position, PositionEvent, PositionEvent]:
        account = _brokerage(session)
        day = TODAY - timedelta(days=5)
        opening = service.record_position(
            session,
            account_ref=str(account.id),
            symbol=_symbol(),
            quantity=Decimal("100"),
            cost_basis=Decimal("1000"),
            as_of=day,
            token=None,
        )
        buy = service.record_event(
            session,
            position_id=opening.position.id,
            kind="buy",
            occurred_on=day,
            quantity=Decimal("10"),
            price=Decimal("12"),
            token=None,
        )
        return account, opening.position, opening.event, buy

    def _held(self, session: Session, account: Account, position: Position) -> tuple:
        state = next(
            s
            for s in service.position_states(session, account, include_closed=True, stale_days=7)
            if s.position.id == position.id
        )
        return state.quantity, state.cost_basis

    def test_the_issue_example(self, db_session: Session) -> None:
        account, position, opening, _ = self._day(db_session)
        assert self._held(db_session, account, position) == (Decimal("110"), Decimal("1120"))
        service.reverse_event(db_session, event_id=opening.id, token=None)
        fixed = service.record_event(
            db_session,
            position_id=position.id,
            kind="set_quantity",
            occurred_on=opening.occurred_on,
            quantity=Decimal("100"),
            cost_basis=Decimal("900"),
            replaces=opening.id,
            token=None,
        )
        assert fixed.replaces_id == opening.id
        assert self._held(db_session, account, position) == (Decimal("110"), Decimal("1020"))

    def test_without_replaces_it_still_goes_last(self, db_session: Session) -> None:
        account, position, opening, _ = self._day(db_session)
        service.reverse_event(db_session, event_id=opening.id, token=None)
        service.record_event(
            db_session,
            position_id=position.id,
            kind="set_quantity",
            occurred_on=opening.occurred_on,
            quantity=Decimal("100"),
            cost_basis=Decimal("900"),
            token=None,
        )
        assert self._held(db_session, account, position) == (Decimal("100"), Decimal("900"))

    def test_what_replaces_may_name(self, db_session: Session) -> None:
        account, position, opening, buy = self._day(db_session)

        def replace(target: uuid.UUID, on: date | None = None) -> PositionEvent:
            return service.record_event(
                db_session,
                position_id=position.id,
                kind="set_quantity",
                occurred_on=on or opening.occurred_on,
                quantity=Decimal("100"),
                replaces=target,
                token=None,
            )

        with pytest.raises(LedgerError, match="not reversed; reverse it first"):
            replace(opening.id)
        reversal = service.reverse_event(db_session, event_id=opening.id, token=None)
        with pytest.raises(LedgerError, match="names a reversal"):
            replace(reversal.id)
        with pytest.raises(LedgerError, match="carries the same date"):
            replace(opening.id, on=opening.occurred_on - timedelta(days=1))
        with pytest.raises(LedgerError, match="no event"):
            replace(uuid.uuid4())
        replace(opening.id)
        with pytest.raises(LedgerError, match="already replaced"):
            replace(opening.id)
        del account, buy


class TestReverseAndClose:
    def test_reversal_restores_the_history_and_the_rows(self, db_session: Session) -> None:
        account = _brokerage(db_session)
        symbol = _symbol()
        _price(db_session, symbol, TODAY - timedelta(days=10), "10")
        recorded = service.record_position(
            db_session,
            account_ref=str(account.id),
            symbol=symbol,
            quantity=Decimal("5"),
            as_of=TODAY - timedelta(days=10),
            token=None,
        )
        before = _manual_rows(db_session, account.id)
        mistake = service.record_event(
            db_session,
            position_id=recorded.position.id,
            kind="buy",
            occurred_on=TODAY - timedelta(days=4),
            quantity=Decimal("3"),
            price=Decimal("11"),
            token=None,
        )
        assert _manual_rows(db_session, account.id) != before
        reversal = service.reverse_event(db_session, event_id=mistake.id, token=None)
        assert reversal.reverses_id == mistake.id
        assert _manual_rows(db_session, account.id) == before
        with pytest.raises(LedgerError, match="already reversed"):
            service.reverse_event(db_session, event_id=mistake.id, token=None)
        with pytest.raises(LedgerError, match="cannot itself be reversed"):
            service.reverse_event(db_session, event_id=reversal.id, token=None)

    def test_a_reversal_that_breaks_a_later_point_is_refused(self, db_session: Session) -> None:
        account = _brokerage(db_session)
        position = service.create_position(
            db_session,
            account_ref=str(account.id),
            symbol=_symbol(),
            opened_on=TODAY - timedelta(days=9),
            token=None,
        )
        buy = service.record_event(
            db_session,
            position_id=position.id,
            kind="buy",
            occurred_on=TODAY - timedelta(days=9),
            quantity=Decimal("4"),
            token=None,
        )
        service.record_event(
            db_session,
            position_id=position.id,
            kind="sell",
            occurred_on=TODAY - timedelta(days=2),
            quantity=Decimal("1"),
            token=None,
        )
        with pytest.raises(LedgerError, match="fewer than zero"):
            service.reverse_event(db_session, event_id=buy.id, token=None)

    def test_close_needs_zero_today(self, db_session: Session) -> None:
        account = _brokerage(db_session)
        recorded = service.record_position(
            db_session,
            account_ref=str(account.id),
            symbol=_symbol(),
            quantity=Decimal("2"),
            as_of=TODAY - timedelta(days=3),
            token=None,
        )
        with pytest.raises(LedgerError, match="still holds 2"):
            service.update_position(
                db_session, position_id=recorded.position.id, close=True, token=None
            )
        service.record_event(
            db_session,
            position_id=recorded.position.id,
            kind="sell",
            occurred_on=TODAY,
            quantity=Decimal("2"),
            token=None,
        )
        closed = service.update_position(
            db_session, position_id=recorded.position.id, close=True, token=None
        )
        assert closed.closed_at is not None
        with pytest.raises(LedgerError, match="closed"):
            service.record_event(
                db_session,
                position_id=closed.id,
                kind="buy",
                occurred_on=TODAY,
                quantity=Decimal("1"),
                token=None,
            )


# ---------------------------------------------------------------------------
# The projection
# ---------------------------------------------------------------------------


class TestProjection:
    def test_rows_carry_price_and_value_and_a_tombstone_on_sell_out(
        self, db_session: Session
    ) -> None:
        account = _brokerage(db_session)
        symbol = _symbol()
        for n, close in ((12, "10"), (8, "12"), (2, "15")):
            _price(db_session, symbol, TODAY - timedelta(days=n), close)
        recorded = service.record_position(
            db_session,
            account_ref=str(account.id),
            symbol=symbol,
            quantity=Decimal("3"),
            as_of=TODAY - timedelta(days=10),
            cost_basis=Decimal("25.50"),
            token=None,
        )
        service.record_event(
            db_session,
            position_id=recorded.position.id,
            kind="sell",
            occurred_on=TODAY - timedelta(days=5),
            quantity=Decimal("3"),
            token=None,
        )
        rows = _manual_rows(db_session, account.id)
        pid = recorded.position.id
        assert rows == [
            (
                TODAY - timedelta(days=10),
                pid,
                Decimal("3.000000"),
                Decimal("25.5000"),
                Decimal("30.0000"),
                Decimal("10.000000"),
                TODAY - timedelta(days=12),
            ),
            (
                TODAY - timedelta(days=8),
                pid,
                Decimal("3.000000"),
                Decimal("25.5000"),
                Decimal("36.0000"),
                Decimal("12.000000"),
                TODAY - timedelta(days=8),
            ),
            # Sold out: the tombstone, then nothing — the newer close on day -2
            # concerns no held position.
            (TODAY - timedelta(days=5), pid, Decimal("0"), Decimal("0"), Decimal("0"), None, None),
        ]

    def test_no_price_means_null_market_value_never_zero(self, db_session: Session) -> None:
        account = _brokerage(db_session)
        service.record_position(
            db_session,
            account_ref=str(account.id),
            symbol=_symbol(),
            quantity=Decimal("7"),
            as_of=TODAY,
            token=None,
        )
        (row,) = _manual_rows(db_session, account.id)
        assert row[2] == Decimal("7") and row[4] is None

    def test_feed_implied_price_fills_the_gap(self, db_session: Session) -> None:
        """A feed snapshot of the same symbol elsewhere implies a close
        (market value / quantity), computed on read and never stored."""
        symbol = _symbol()
        other = _brokerage(db_session, manual=False)
        _feed_snapshot(db_session, other.id, TODAY - timedelta(days=1), symbol, "4", "50.00")
        account = _brokerage(db_session)
        service.record_position(
            db_session,
            account_ref=str(account.id),
            symbol=symbol,
            quantity=Decimal("2"),
            as_of=TODAY,
            token=None,
        )
        (row,) = _manual_rows(db_session, account.id)
        assert row[5] == Decimal("12.5") and row[4] == Decimal("25.0000")
        stored = db_session.execute(
            select(SecurityPrice).where(SecurityPrice.symbol == symbol)
        ).all()
        assert stored == []

    def test_newest_close_wins_over_an_older_manual_one(self, db_session: Session) -> None:
        account = _brokerage(db_session)
        symbol = _symbol()
        service.record_position(
            db_session,
            account_ref=str(account.id),
            symbol=symbol,
            quantity=Decimal("1"),
            as_of=TODAY - timedelta(days=6),
            token=None,
        )
        price_service.record_price(
            db_session,
            symbol=symbol,
            price_date=TODAY - timedelta(days=6),
            close=Decimal("100"),
            token=None,
        )
        _price(db_session, symbol, TODAY - timedelta(days=1), "90")
        _price(db_session, symbol, TODAY - timedelta(days=6), "99")  # loses the same-day tie
        account = projection.lock_account(db_session, account.id)
        projection.project_account(db_session, account)
        rows = _manual_rows(db_session, account.id)
        assert [(r[0], r[5]) for r in rows] == [
            (TODAY - timedelta(days=6), Decimal("100")),
            (TODAY - timedelta(days=1), Decimal("90")),
        ]

    def test_a_second_pass_changes_nothing_and_marks_nothing(self, db_session: Session) -> None:
        account = _brokerage(db_session)
        symbol = _symbol()
        _price(db_session, symbol, TODAY - timedelta(days=3), "5")
        service.record_position(
            db_session,
            account_ref=str(account.id),
            symbol=symbol,
            quantity=Decimal("1"),
            as_of=TODAY - timedelta(days=3),
            token=None,
        )
        db_session.commit()
        db_session.execute(text("UPDATE balance_watermark SET networth_dirty_from = NULL"))
        account = projection.lock_account(db_session, account.id)
        result = projection.project_account(db_session, account)
        assert result == projection.ProjectionResult(None, 0, 0)
        assert (
            db_session.execute(text("SELECT networth_dirty_from FROM balance_watermark")).scalar()
            is None
        )
        db_session.rollback()

    def test_a_backdated_write_marks_net_worth_from_its_date(self, db_session: Session) -> None:
        account = _brokerage(db_session)
        symbol = _symbol()
        _price(db_session, symbol, TODAY - timedelta(days=40), "5")
        recorded = service.record_position(
            db_session,
            account_ref=str(account.id),
            symbol=symbol,
            quantity=Decimal("1"),
            as_of=TODAY - timedelta(days=2),
            token=None,
        )
        db_session.execute(text("UPDATE balance_watermark SET networth_dirty_from = NULL"))
        service.record_event(
            db_session,
            position_id=recorded.position.id,
            kind="set_quantity",
            occurred_on=TODAY - timedelta(days=30),
            quantity=Decimal("4"),
            token=None,
        )
        cursor = db_session.execute(
            text("SELECT networth_dirty_from FROM balance_watermark")
        ).scalar()
        assert cursor == TODAY - timedelta(days=30)
        db_session.rollback()


class TestSwitchingSource:
    def test_feed_to_manual_keeps_feed_history_and_starts_after_it(
        self, db_session: Session
    ) -> None:
        symbol = _symbol()
        account = _brokerage(db_session, manual=False)
        last_feed = TODAY - timedelta(days=4)
        _feed_snapshot(db_session, account.id, TODAY - timedelta(days=9), symbol, "3", "30.00")
        _feed_snapshot(db_session, account.id, last_feed, symbol, "3", "33.00")
        from purser import ledger

        ledger.update_account(
            db_session, account_id=account.id, holdings_source="manual", actor_token_id=None
        )
        service.record_position(
            db_session,
            account_ref=str(account.id),
            symbol=symbol,
            quantity=Decimal("5"),
            as_of=TODAY - timedelta(days=20),  # backdated into feed territory
            token=None,
        )
        rows = _manual_rows(db_session, account.id)
        assert rows and min(r[0] for r in rows) == last_feed + timedelta(days=1)
        feed = db_session.execute(
            select(HoldingsSnapshot.as_of, HoldingsSnapshot.market_value).where(
                HoldingsSnapshot.account_id == account.id, HoldingsSnapshot.source == "feed"
            )
        ).all()
        assert sorted(feed) == [
            (TODAY - timedelta(days=9), Decimal("30.0000")),
            (last_feed, Decimal("33.0000")),
        ]
        # The first manual set is valued with the feed-implied close (33/3).
        assert rows[0][4] == Decimal("55.0000")

    def test_manual_to_feed_touches_no_row_and_freezes_positions(self, db_session: Session) -> None:
        """The switch back writes and deletes nothing: the hand-tracked set
        stays in effect (no dip) until the feed's first fetch ends the manual
        period. It records the day, the importer's line between the two."""
        from purser import ledger
        from purser.models import AuditLog

        account = _brokerage(db_session)
        symbol = _symbol()
        recorded = service.record_position(
            db_session,
            account_ref=str(account.id),
            symbol=symbol,
            quantity=Decimal("2"),
            as_of=TODAY - timedelta(days=3),
            token=None,
        )
        service.record_event(
            db_session,
            position_id=recorded.position.id,
            kind="buy",
            occurred_on=TODAY,
            quantity=Decimal("1"),
            token=None,
        )
        before = _manual_rows(db_session, account.id)
        assert {r[0] for r in before} == {TODAY - timedelta(days=3), TODAY}
        _mapped(db_session, account)
        ledger.update_account(
            db_session, account_id=account.id, holdings_source="feed", actor_token_id=None
        )
        assert _manual_rows(db_session, account.id) == before
        switched_at = account.holdings_source_changed_at
        assert switched_at is not None
        # The suite can cross midnight UTC after TODAY was taken (#397).
        assert switched_at.date() in {TODAY, TODAY + timedelta(days=1)}
        assert account.manual_period_open is True
        # Both switches ran in this one transaction (one timestamp): pick the
        # manual -> feed row by its content, not by order.
        (row,) = [
            r
            for r in db_session.execute(
                select(AuditLog).where(
                    AuditLog.entity_id == account.id, AuditLog.action == "account.update"
                )
            ).scalars()
            if r.before_after["before"]["holdings_source"] == "manual"
        ]
        assert row.before_after["after"]["holdings_source"] == "feed"
        assert row.before_after["after"]["holdings_source_changed_at"] == switched_at.isoformat()
        with pytest.raises(LedgerError, match="takes its holdings from the feed"):
            service.record_event(
                db_session,
                position_id=recorded.position.id,
                kind="buy",
                occurred_on=TODAY,
                quantity=Decimal("1"),
                token=None,
            )

    def test_a_switch_to_the_feed_needs_a_feed_mapping(self, db_session: Session) -> None:
        """With no feed to take over, the hand-tracked set would stay in effect
        forever: refused (422)."""
        from purser import ledger

        account = _brokerage(db_session)
        with pytest.raises(LedgerError, match="no feed mapping"):
            ledger.update_account(
                db_session, account_id=account.id, holdings_source="feed", actor_token_id=None
            )
        assert account.holdings_source == "manual"

    def test_ending_the_period_with_nothing_reported_holds_nothing(
        self, db_session: Session
    ) -> None:
        """end_manual_period (the importer's, on a cash-only feed's first
        fetch): a MANUAL tombstone ends each hand-tracked position, so nothing
        manual stays current, the feed floor never moves, and one audit row
        records it; the history before stays."""
        from purser import ledger, networth
        from purser.models import AuditLog

        account = _mapped(db_session, _brokerage(db_session))
        symbol = _symbol()
        _price(db_session, symbol, TODAY - timedelta(days=5), "50")
        recorded = service.record_position(
            db_session,
            account_ref=str(account.id),
            symbol=symbol,
            quantity=Decimal("4"),
            as_of=TODAY - timedelta(days=5),
            token=None,
        )
        ledger.update_account(
            db_session, account_id=account.id, holdings_source="feed", actor_token_id=None
        )
        assert networth.holdings_addends(db_session, [account.id], as_of=TODAY)[account.id][0] == (
            Decimal("200")
        )  # the switch alone changes nothing
        deleted, ended = projection.end_manual_period(
            db_session, account, TODAY, reported=set(), raw_fetch_id=None
        )
        assert (deleted, ended) == (0, 1)
        zero = db_session.execute(
            select(
                HoldingsSnapshot.source, HoldingsSnapshot.quantity, HoldingsSnapshot.position_id
            ).where(HoldingsSnapshot.account_id == account.id, HoldingsSnapshot.as_of == TODAY)
        ).all()
        assert [tuple(r) for r in zero] == [("manual", Decimal("0.000000"), recorded.position.id)]
        assert projection.feed_floor(db_session, account.id) is None
        assert account.manual_period_open is False
        (audit,) = db_session.execute(
            select(AuditLog).where(
                AuditLog.entity_id == account.id, AuditLog.action == "account.manual_period_end"
            )
        ).scalars()
        assert audit.before_after["after"]["tombstones_written"] == 1
        for day in (TODAY, TODAY + timedelta(days=90)):
            assert networth.holdings_addends(db_session, [account.id], as_of=day) == {}
            assert networth.latest_holdings(db_session, account.id, as_of=day) == []
        assert networth.holdings_addends(db_session, [account.id], as_of=TODAY - timedelta(days=1))[
            account.id
        ][0] == Decimal("200")

    def test_a_sold_out_position_stays_sold_out_across_the_switch(
        self, db_session: Session
    ) -> None:
        from purser import ledger, networth

        account = _brokerage(db_session)
        symbol = _symbol()
        _price(db_session, symbol, TODAY - timedelta(days=5), "10")
        recorded = service.record_position(
            db_session,
            account_ref=str(account.id),
            symbol=symbol,
            quantity=Decimal("10"),
            as_of=TODAY - timedelta(days=5),
            token=None,
        )
        service.record_event(
            db_session,
            position_id=recorded.position.id,
            kind="sell",
            occurred_on=TODAY,
            quantity=Decimal("10"),
            token=None,
        )
        assert networth.holdings_addends(db_session, [account.id], as_of=TODAY) == {}
        _mapped(db_session, account)
        ledger.update_account(
            db_session, account_id=account.id, holdings_source="feed", actor_token_id=None
        )
        assert networth.holdings_addends(db_session, [account.id], as_of=TODAY) == {}
        assert networth.latest_holdings(db_session, account.id) == []

    def test_only_brokerage_accounts_switch(self, db_session: Session) -> None:
        from purser import ledger

        card = ledger.create_account(
            db_session,
            name="Card",
            type_="credit_card",
            path=f"liabilities.card_{uuid.uuid4().hex[:8]}",
            actor_token_id=None,
        )
        with pytest.raises(LedgerError, match="only to brokerage"):
            ledger.update_account(
                db_session, account_id=card.id, holdings_source="manual", actor_token_id=None
            )


# ---------------------------------------------------------------------------
# Properties against the database
# ---------------------------------------------------------------------------

_kinds = st.sampled_from(["set_quantity", "buy", "sell"])
_qty = st.decimals(min_value=Decimal("0.5"), max_value=Decimal("50"), places=2)


@st.composite
def histories(draw: st.DrawFn) -> dict:
    positions = draw(st.integers(min_value=1, max_value=3))
    events = [
        (
            draw(st.integers(min_value=0, max_value=positions - 1)),
            draw(_kinds),
            draw(st.integers(min_value=0, max_value=25)),  # days ago
            draw(_qty),
        )
        for _ in range(draw(st.integers(min_value=1, max_value=8)))
    ]
    closes = draw(
        st.lists(
            st.tuples(
                st.integers(min_value=0, max_value=positions - 1),
                st.integers(min_value=0, max_value=30),
                st.decimals(min_value=Decimal("1"), max_value=Decimal("500"), places=2),
                st.sampled_from(["manual", "tiingo"]),
            ),
            max_size=6,
        )
    )
    feed_days_ago = draw(st.none() | st.integers(min_value=0, max_value=20))
    return {"positions": positions, "events": events, "closes": closes, "feed": feed_days_ago}


def _build(session: Session, spec: dict) -> Account:
    symbols = [_symbol("P") for _ in range(spec["positions"])]
    account = _brokerage(session, manual=False)
    if spec["feed"] is not None:
        # The account's own feed history: the projection must start after it.
        _feed_snapshot(
            session, account.id, TODAY - timedelta(days=spec["feed"]), symbols[0], "1", "10.00"
        )
    from purser import ledger

    ledger.update_account(
        session, account_id=account.id, holdings_source="manual", actor_token_id=None
    )
    for index, days_ago, close, source in spec["closes"]:
        session.merge(
            SecurityPrice(
                symbol=symbols[index],
                price_date=TODAY - timedelta(days=days_ago),
                source=source,
                close=close,
            )
        )
    session.flush()
    positions = [
        service.create_position(
            session,
            account_ref=str(account.id),
            symbol=s,
            opened_on=TODAY - timedelta(days=30),
            token=None,
        )
        for s in symbols
    ]
    for index, kind, days_ago, quantity in spec["events"]:
        try:
            with session.begin_nested():
                service.record_event(
                    session,
                    position_id=positions[index].id,
                    kind=kind,
                    occurred_on=TODAY - timedelta(days=days_ago),
                    quantity=quantity,
                    token=None,
                )
        except LedgerError:
            continue  # refused: the service keeps only valid histories
    return account


@settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(histories())
def test_projection_is_regenerable_complete_and_respects_the_feed(
    migrated_engine: Engine, spec: dict
) -> None:
    with db.get_sessionmaker()() as session:
        account = _build(session, spec)
        first = _manual_rows(session, account.id)

        # A second pass changes nothing.
        locked = projection.lock_account(session, account.id)
        assert projection.project_account(session, locked) == projection.ProjectionResult(
            None, 0, 0
        )
        # Regenerated from nothing, the rows are identical.
        session.execute(
            text("DELETE FROM holdings_snapshots WHERE account_id = :a AND source = 'manual'"),
            {"a": account.id},
        )
        projection.project_account(session, locked)
        assert _manual_rows(session, account.id) == first

        # Feed rows untouched; no manual row on or before the last feed date.
        feed = (
            session.execute(
                select(HoldingsSnapshot.as_of).where(
                    HoldingsSnapshot.account_id == account.id, HoldingsSnapshot.source == "feed"
                )
            )
            .scalars()
            .all()
        )
        assert len(feed) == (0 if spec["feed"] is None else 1)
        if feed:
            assert all(row[0] > max(feed) for row in first)

        # Every materialized date carries every position held that day, and
        # the newest set at or before any day is the timeline on that day.
        events = (
            session.execute(
                select(PositionEvent)
                .join(Position, Position.id == PositionEvent.position_id)
                .where(Position.account_id == account.id)
            )
            .scalars()
            .all()
        )
        by_position: dict[uuid.UUID, list[core.Event]] = {}
        for e in events:
            by_position.setdefault(e.position_id, []).append(projection.event_fact(e))
        timelines = {
            pid: core.Timeline(core.quantity_timeline(evs)) for pid, evs in by_position.items()
        }
        dates = sorted({row[0] for row in first})
        for day in dates:
            on_day = {row[1]: row for row in first if row[0] == day}
            for pid, timeline in timelines.items():
                if timeline.quantity_on(day) > 0:
                    assert on_day[pid][2] == timeline.quantity_on(day)
        if dates:
            for offset in range((TODAY - dates[0]).days + 1):
                day = dates[0] + timedelta(days=offset)
                newest = max(d for d in dates if d <= day)
                latest = {row[1]: row[2] for row in first if row[0] == newest}
                for pid, timeline in timelines.items():
                    assert latest.get(pid, Decimal(0)) == timeline.quantity_on(day)
        session.rollback()


def test_account_purge_removes_positions_events_and_manual_rows(
    clean_db: None, migrated_engine: Engine
) -> None:
    from purser.admin import account_delete

    with db.get_sessionmaker()() as session:
        account = _brokerage(session)
        recorded = service.record_position(
            session,
            account_ref=str(account.id),
            symbol=_symbol(),
            quantity=Decimal("2"),
            as_of=TODAY,
            token=None,
        )
        price_service.record_price(
            session,
            symbol=recorded.position.symbol,
            price_date=TODAY,
            close=Decimal("3"),
            token=None,
        )
        session.commit()
        plan = account_delete.preflight(session, account)
        assert plan["positions"] == 1 and plan["position_events"] == 1
        summary = account_delete.execute_delete(session, account, actor_token_id=None)
        session.commit()
        assert summary["positions_deleted"] == 1
        assert summary["position_events_deleted"] == 1
        assert summary["holdings_deleted"] == 1
        # Prices are global: they stay.
        assert (
            session.execute(
                select(SecurityPrice).where(SecurityPrice.symbol == recorded.position.symbol)
            )
            .scalars()
            .all()
        )


class TestConcurrentWrites:
    """A write that waits on the account lock acts on the position as the
    writer before it left it (decision 88): the position is read again
    after the lock, so a close committed meanwhile refuses the write, and a
    backdate that moved opened_on is never overwritten."""

    @staticmethod
    def _sold_out(symbol: str) -> tuple[uuid.UUID, uuid.UUID]:
        """A manual account whose position held 10 and was sold out on T-2
        (so it can be closed), committed; returns (position id, sell id)."""
        with db.get_sessionmaker()() as session:
            account = _brokerage(session)
            recorded = service.record_position(
                session,
                account_ref=str(account.id),
                symbol=symbol,
                quantity=Decimal("10"),
                as_of=TODAY - timedelta(days=5),
                token=None,
            )
            sell = service.record_event(
                session,
                position_id=recorded.position.id,
                kind="sell",
                occurred_on=TODAY - timedelta(days=2),
                quantity=Decimal("10"),
                token=None,
            )
            session.commit()
            return recorded.position.id, sell.id

    @staticmethod
    def _race(engine: Engine, first, contender) -> dict[str, object]:
        """Run ``first`` in one session and keep its account lock; start
        ``contender`` in another, wait until it is blocked on that lock, then
        commit the first. Returns the contender's outcome."""
        import threading
        import time

        outcome: dict[str, object] = {}
        maker = db.get_sessionmaker()
        holder = maker()
        try:
            first(holder)

            def run() -> None:
                with maker() as session:
                    try:
                        contender(session)
                        session.commit()
                        outcome["ok"] = True
                    except LedgerError as exc:
                        session.rollback()
                        outcome["ok"] = False
                        outcome["error"] = str(exc)

            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            deadline = time.monotonic() + 15
            blocked = False
            with engine.connect() as conn:
                while time.monotonic() < deadline and not blocked:
                    blocked = (
                        conn.execute(
                            text(
                                "SELECT 1 FROM pg_stat_activity WHERE datname = current_database()"
                                " AND wait_event_type = 'Lock'"
                                " AND query ILIKE '%FROM accounts%FOR NO KEY UPDATE%'"
                            )
                        ).first()
                        is not None
                    )
                    conn.rollback()
                    if not blocked:
                        time.sleep(0.05)
            assert blocked, "the contender never waited on the account lock"
            holder.commit()
        finally:
            holder.close()
        thread.join(timeout=60)
        assert not thread.is_alive()
        return outcome

    def test_a_close_committed_while_a_record_waits_refuses_it(
        self, clean_db: None, migrated_engine: Engine
    ) -> None:
        position_id, _sell = self._sold_out(_symbol("R"))
        outcome = self._race(
            migrated_engine,
            lambda s: service.update_position(s, position_id=position_id, close=True, token=None),
            lambda s: service.record_event(
                s,
                position_id=position_id,
                kind="buy",
                occurred_on=TODAY,
                quantity=Decimal("7"),
                token=None,
            ),
        )
        assert outcome == {"ok": False, "error": outcome.get("error")}
        assert "closed" in str(outcome["error"])

    def test_a_close_committed_while_a_reversal_waits_refuses_it(
        self, clean_db: None, migrated_engine: Engine
    ) -> None:
        position_id, sell_id = self._sold_out(_symbol("V"))
        outcome = self._race(
            migrated_engine,
            lambda s: service.update_position(s, position_id=position_id, close=True, token=None),
            lambda s: service.reverse_event(s, event_id=sell_id, token=None),
        )
        assert outcome["ok"] is False and "closed" in str(outcome["error"])

    def test_a_backdate_committed_while_another_write_waits_keeps_opened_on(
        self, clean_db: None, migrated_engine: Engine
    ) -> None:
        position_id, _sell = self._sold_out(_symbol("O"))
        outcome = self._race(
            migrated_engine,
            lambda s: service.record_event(
                s,
                position_id=position_id,
                kind="buy",
                occurred_on=TODAY - timedelta(days=20),
                quantity=Decimal("1"),
                token=None,
            ),
            lambda s: service.record_event(
                s,
                position_id=position_id,
                kind="buy",
                occurred_on=TODAY - timedelta(days=10),
                quantity=Decimal("1"),
                token=None,
            ),
        )
        assert outcome["ok"] is True
        with db.get_sessionmaker()() as session:
            position = session.get(Position, position_id)
            assert position is not None
            assert position.opened_on == TODAY - timedelta(days=20)


class TestNoOverlap:
    """A symbol's positions on one account never overlap in time: a new one
    starts after the last date of a closed one, so a date never carries two
    positions in the same symbol (which would count the shares twice)."""

    @staticmethod
    def _closed(session: Session) -> tuple[Account, str]:
        """ACME held 10 from T-20, sold out on T-10, closed."""
        account = _brokerage(session)
        symbol = _symbol()
        _price(session, symbol, TODAY - timedelta(days=30), "10")
        first = service.record_position(
            session,
            account_ref=str(account.id),
            symbol=symbol,
            quantity=Decimal("10"),
            as_of=TODAY - timedelta(days=20),
            token=None,
        )
        service.record_event(
            session,
            position_id=first.position.id,
            kind="sell",
            occurred_on=TODAY - timedelta(days=10),
            quantity=Decimal("10"),
            token=None,
        )
        service.update_position(session, position_id=first.position.id, close=True, token=None)
        return account, symbol

    def test_a_reversed_event_does_not_extend_a_closed_position(self, db_session: Session) -> None:
        """Only EFFECTIVE events bound a closed position (the projection's own
        definition): a sell-out first dated today by mistake, reversed and
        recorded on its real date, does not block a later position."""
        account = _brokerage(db_session)
        symbol = _symbol()
        _price(db_session, symbol, TODAY - timedelta(days=60), "10")
        first = service.record_position(
            db_session,
            account_ref=str(account.id),
            symbol=symbol,
            quantity=Decimal("10"),
            as_of=TODAY - timedelta(days=40),
            token=None,
        )
        wrong = service.record_event(
            db_session,
            position_id=first.position.id,
            kind="sell",
            occurred_on=TODAY,
            quantity=Decimal("10"),
            token=None,
        )
        service.reverse_event(db_session, event_id=wrong.id, token=None)
        service.record_event(
            db_session,
            position_id=first.position.id,
            kind="sell",
            occurred_on=TODAY - timedelta(days=30),
            quantity=Decimal("10"),
            token=None,
        )
        service.update_position(db_session, position_id=first.position.id, close=True, token=None)
        again = service.record_position(
            db_session,
            account_ref=str(account.id),
            symbol=symbol,
            quantity=Decimal("5"),
            as_of=TODAY - timedelta(days=20),
            token=None,
        )
        assert again.created
        # The same-day rule stays: nothing may start ON its last effective date.
        with pytest.raises(service.PositionConflict):
            service.record_event(
                db_session,
                position_id=again.position.id,
                kind="set_quantity",
                occurred_on=TODAY - timedelta(days=30),
                quantity=Decimal("5"),
                token=None,
            )

    def test_a_closed_position_with_no_effective_event_blocks_nothing(
        self, db_session: Session
    ) -> None:
        """Recorded on the wrong account, reversed, closed: it never held
        anything, so the real holding may start on any date."""
        account = _brokerage(db_session)
        symbol = _symbol()
        wrong_day = TODAY - timedelta(days=100)
        mistaken = service.record_position(
            db_session,
            account_ref=str(account.id),
            symbol=symbol,
            quantity=Decimal("10"),
            as_of=wrong_day,
            token=None,
        )
        service.reverse_event(db_session, event_id=mistaken.event.id, token=None)
        service.update_position(
            db_session, position_id=mistaken.position.id, close=True, token=None
        )
        real = service.record_position(
            db_session,
            account_ref=str(account.id),
            symbol=symbol,
            quantity=Decimal("7"),
            as_of=TODAY - timedelta(days=200),
            token=None,
        )
        assert real.created and real.position.opened_on == TODAY - timedelta(days=200)

    @pytest.mark.parametrize("days_ago", [15, 10])
    def test_a_new_position_cannot_start_inside_a_closed_ones_history(
        self, db_session: Session, days_ago: int
    ) -> None:
        account, symbol = self._closed(db_session)
        with pytest.raises(service.PositionConflict, match="closed position"):
            service.record_position(
                db_session,
                account_ref=str(account.id),
                symbol=symbol,
                quantity=Decimal("10"),
                as_of=TODAY - timedelta(days=days_ago),
                token=None,
            )

    def test_one_starting_after_it_is_fine_and_counts_once(self, db_session: Session) -> None:
        from purser import networth

        account, symbol = self._closed(db_session)
        service.record_position(
            db_session,
            account_ref=str(account.id),
            symbol=symbol,
            quantity=Decimal("4"),
            as_of=TODAY - timedelta(days=9),
            token=None,
        )
        on = TODAY - timedelta(days=5)
        assert networth.holdings_addends(db_session, [account.id], as_of=on)[account.id][0] == (
            Decimal("40")
        )

    def test_a_backdated_event_cannot_reach_into_it_either(self, db_session: Session) -> None:
        account, symbol = self._closed(db_session)
        second = service.record_position(
            db_session,
            account_ref=str(account.id),
            symbol=symbol,
            quantity=Decimal("4"),
            as_of=TODAY - timedelta(days=5),
            token=None,
        )
        with pytest.raises(service.PositionConflict, match="closed position"):
            service.record_event(
                db_session,
                position_id=second.position.id,
                kind="set_quantity",
                occurred_on=TODAY - timedelta(days=12),
                quantity=Decimal("4"),
                token=None,
            )


class TestOverlapBothWays:
    """The overlap check runs on every event and looks both ways (#390
    round 3, finding 9): what counts is the days each position holds shares
    plus its sell-out day, never its last effective event."""

    @staticmethod
    def _held_then_zeroed(session: Session) -> tuple[Account, str, uuid.UUID]:
        """Held 10 from T-40, set to 0 at T-30, a statement's '0 shares'
        re-recorded at T-10, closed."""
        account = _brokerage(session)
        symbol = _symbol()
        _price(session, symbol, TODAY - timedelta(days=50), "10")
        first = service.record_position(
            session,
            account_ref=str(account.id),
            symbol=symbol,
            quantity=Decimal("10"),
            as_of=TODAY - timedelta(days=40),
            token=None,
        )
        for days in (30, 10):
            service.record_event(
                session,
                position_id=first.position.id,
                kind="set_quantity",
                occurred_on=TODAY - timedelta(days=days),
                quantity=Decimal("0"),
                token=None,
            )
        service.update_position(session, position_id=first.position.id, close=True, token=None)
        return account, symbol, first.position.id

    def test_a_re_buy_after_the_sell_out_is_fine_despite_a_later_zero(
        self, db_session: Session
    ) -> None:
        from purser import networth

        account, symbol, _closed = self._held_then_zeroed(db_session)
        again = service.record_position(
            db_session,
            account_ref=str(account.id),
            symbol=symbol,
            quantity=Decimal("3"),
            as_of=TODAY - timedelta(days=20),
            token=None,
        )
        assert again.created
        for days in (20, 10, 0):
            on = TODAY - timedelta(days=days)
            assert networth.holdings_addends(db_session, [account.id], as_of=on)[account.id][
                0
            ] == Decimal("30")
        # The sell-out day itself stays refused (the same-day re-buy rule).
        with pytest.raises(service.PositionConflict, match="closed position"):
            service.record_event(
                db_session,
                position_id=again.position.id,
                kind="set_quantity",
                occurred_on=TODAY - timedelta(days=30),
                quantity=Decimal("3"),
                token=None,
            )

    def test_an_event_inside_the_span_is_refused_though_not_a_backdate(
        self, db_session: Session
    ) -> None:
        """Opened empty before the closed position's span, then given shares
        inside it: no date moved earlier, yet the two would overlap."""
        account, symbol, _closed = self._held_then_zeroed(db_session)
        empty = service.record_position(
            db_session,
            account_ref=str(account.id),
            symbol=symbol,
            quantity=Decimal("0"),
            as_of=TODAY - timedelta(days=60),
            token=None,
        )
        with pytest.raises(service.PositionConflict, match="closed position"):
            service.record_event(
                db_session,
                position_id=empty.position.id,
                kind="buy",
                occurred_on=TODAY - timedelta(days=35),
                quantity=Decimal("2"),
                token=None,
            )

    def test_a_reversal_that_extends_a_position_into_another_is_refused(
        self, db_session: Session
    ) -> None:
        """A position held T-20 to T-15; a closed one held T-10 to T-6.
        Reversing the first one's sell-out would leave it holding shares on
        the closed one's days: refused."""
        account = _brokerage(db_session)
        symbol = _symbol()
        later = service.record_position(
            db_session,
            account_ref=str(account.id),
            symbol=symbol,
            quantity=Decimal("2"),
            as_of=TODAY - timedelta(days=10),
            token=None,
        )
        service.record_event(
            db_session,
            position_id=later.position.id,
            kind="sell",
            occurred_on=TODAY - timedelta(days=5),
            quantity=Decimal("2"),
            token=None,
        )
        service.update_position(db_session, position_id=later.position.id, close=True, token=None)
        earlier = service.record_position(
            db_session,
            account_ref=str(account.id),
            symbol=symbol,
            quantity=Decimal("0"),
            as_of=TODAY - timedelta(days=25),
            token=None,
        )
        sold = service.record_event(
            db_session,
            position_id=earlier.position.id,
            kind="set_quantity",
            occurred_on=TODAY - timedelta(days=15),
            quantity=Decimal("0"),
            token=None,
        )
        service.record_event(
            db_session,
            position_id=earlier.position.id,
            kind="set_quantity",
            occurred_on=TODAY - timedelta(days=20),
            quantity=Decimal("3"),
            token=None,
        )
        with pytest.raises(service.PositionConflict, match="closed position"):
            service.reverse_event(db_session, event_id=sold.id, token=None)

    def test_an_empty_closed_position_reopened_inside_another_is_refused_shares(
        self, db_session: Session
    ) -> None:
        """1b's reopen relies on this: a closed position that never held
        anything, reopened, may not gain shares inside another position's
        span, though its opened_on lies before it."""
        account = _brokerage(db_session)
        symbol = _symbol()
        mistaken = service.record_position(
            db_session,
            account_ref=str(account.id),
            symbol=symbol,
            quantity=Decimal("10"),
            as_of=TODAY - timedelta(days=100),
            token=None,
        )
        service.reverse_event(db_session, event_id=mistaken.event.id, token=None)
        service.update_position(
            db_session, position_id=mistaken.position.id, close=True, token=None
        )
        held = service.record_position(
            db_session,
            account_ref=str(account.id),
            symbol=symbol,
            quantity=Decimal("5"),
            as_of=TODAY - timedelta(days=50),
            token=None,
        )
        service.record_event(
            db_session,
            position_id=held.position.id,
            kind="sell",
            occurred_on=TODAY - timedelta(days=20),
            quantity=Decimal("5"),
            token=None,
        )
        service.update_position(db_session, position_id=held.position.id, close=True, token=None)
        mistaken.position.closed_at = None  # reopened, as 1b's reopen does
        db_session.flush()
        with (
            pytest.raises(service.PositionConflict, match="closed position"),
            db_session.begin_nested(),
        ):
            service.record_event(
                db_session,
                position_id=mistaken.position.id,
                kind="set_quantity",
                occurred_on=TODAY - timedelta(days=30),
                quantity=Decimal("4"),
                token=None,
            )
        # Holding nothing there is no overlap; holding after the sell-out day is fine.
        service.record_event(
            db_session,
            position_id=mistaken.position.id,
            kind="set_quantity",
            occurred_on=TODAY - timedelta(days=19),
            quantity=Decimal("4"),
            token=None,
        )


def test_timeline_loads_leave_the_encrypted_notes_unread(db_session: Session) -> None:
    """The projection and every timeline check read only the columns a
    timeline needs: the notes are ciphertext, decrypted on load, and none of
    those paths shows them."""
    from sqlalchemy import inspect

    account = _brokerage(db_session)
    recorded = service.record_position(
        db_session,
        account_ref=str(account.id),
        symbol=_symbol(),
        quantity=Decimal("2"),
        as_of=TODAY,
        note="opening count",
        source_note="broker page",
        token=None,
    )
    db_session.expunge_all()
    rows = projection.position_events(db_session, [recorded.position.id])[recorded.position.id]
    unloaded = inspect(rows[0]).unloaded
    assert {"note", "source_note"} <= unloaded
    assert rows[0].note == "opening count"  # still there on demand


class TestEarlierManualPeriods:
    """#402: after manual -> feed -> manual, a correction dated in the FIRST
    manual period reaches its rows. The projection writes manual rows
    outside the feed's runs (stretches of feed-dated sets with no manual-only
    date between them), not only after the last feed row."""

    def _round_trip(self, session: Session) -> tuple[Account, str, Position, date, date]:
        from purser import ledger

        account = _mapped(session, _brokerage(session))
        symbol = _symbol()
        d1, feed_from, feed_to = (TODAY - timedelta(days=n) for n in (20, 12, 8))
        _price(session, symbol, d1, "50")
        recorded = service.record_position(
            session,
            account_ref=str(account.id),
            symbol=symbol,
            quantity=Decimal("4"),
            as_of=d1,
            token=None,
        )
        ledger.update_account(
            session, account_id=account.id, holdings_source="feed", actor_token_id=None
        )
        projection.end_manual_period(
            session, account, feed_from, reported={symbol}, raw_fetch_id=None
        )
        day = feed_from
        while day <= feed_to:
            session.add(
                HoldingsSnapshot(
                    account_id=account.id,
                    as_of=day,
                    symbol=symbol,
                    quantity=Decimal("4"),
                    market_value=Decimal("200"),
                    source="feed",
                )
            )
            day += timedelta(days=1)
        session.flush()
        ledger.update_account(
            session, account_id=account.id, holdings_source="manual", actor_token_id=None
        )
        return account, symbol, recorded.position, feed_from, feed_to

    def _values(self, session: Session, account: Account, source: str) -> dict[date, tuple]:
        return {
            r.as_of: (r.quantity, r.market_value)
            for r in session.execute(
                select(HoldingsSnapshot).where(
                    HoldingsSnapshot.account_id == account.id, HoldingsSnapshot.source == source
                )
            ).scalars()
        }

    def test_a_price_in_the_first_period_reaches_its_rows(self, db_session: Session) -> None:
        account, symbol, _, feed_from, feed_to = self._round_trip(db_session)
        feed_before = self._values(db_session, account, "feed")
        price_service.record_price(
            db_session,
            symbol=symbol,
            price_date=TODAY - timedelta(days=18),
            close=Decimal("60"),
            token=None,
        )
        manual = self._values(db_session, account, "manual")
        first_period = {d: v for d, v in manual.items() if d < feed_from}
        assert first_period[TODAY - timedelta(days=18)] == (
            Decimal("4.000000"),
            Decimal("240.0000"),
        )
        assert not any(feed_from <= d <= feed_to for d in manual)  # the feed's run stays the feed's
        assert self._values(db_session, account, "feed") == feed_before
        # Later sets keep the recorded close: the feed-implied one is unchanged
        # across the run, so it keeps its first day, six days after the recorded
        # close, and a real close that recent outranks it (decision 94).
        assert manual[feed_to + timedelta(days=1)][1] == Decimal("240.0000")

    def test_a_backdated_sell_in_the_first_period_reaches_its_rows(
        self, db_session: Session
    ) -> None:
        account, _, position, feed_from, feed_to = self._round_trip(db_session)
        service.record_event(
            db_session,
            position_id=position.id,
            kind="sell",
            occurred_on=TODAY - timedelta(days=17),
            quantity=Decimal("1"),
            price=Decimal("50"),
            token=None,
        )
        manual = self._values(db_session, account, "manual")
        assert manual[TODAY - timedelta(days=17)] == (Decimal("3.000000"), Decimal("150.0000"))
        assert not any(feed_from <= d <= feed_to for d in manual)
        assert manual[feed_to + timedelta(days=1)][0] == Decimal("3.000000")


class TestValueOutOfRangeLogging:
    def test_logged_on_the_transition_not_on_every_new_set(self, db_session: Session) -> None:
        """#422 item 4: decision 89 logs a value that does not fit an amount
        once. Each day's new set carried it again, so the daily projection
        logged it every day; now only the row where it starts is logged."""
        from structlog.testing import capture_logs

        account = _brokerage(db_session)
        symbol = _symbol()
        start = TODAY - timedelta(days=3)
        _price(db_session, symbol, start, "10000")
        with capture_logs() as logs:
            service.record_position(
                db_session,
                account_ref=str(account.id),
                symbol=symbol,
                quantity=Decimal("10000000000000"),  # x 10000 does not fit NUMERIC(20,4)
                as_of=start,
                token=None,
            )
            for days in (2, 1):
                _price(db_session, symbol, TODAY - timedelta(days=days), "10001")
                projection.project_account(db_session, account, today=TODAY)
        logged = [e for e in logs if e["event"] == "holdings.value_out_of_range"]
        assert len(logged) == 1
        assert logged[0]["first"] == start.isoformat()
