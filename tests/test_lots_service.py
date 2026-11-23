"""Lots, captures and awards against a real Postgres (issue #393, decision
107): every event entry point respects the lot invariants, lot-aware basis
reaches position states and snapshots, captures are state-bound and
idempotent, vests never double count, net worth ignores awards, and
corrections are audited. Synthetic fixtures only: made-up symbols, prices,
references and dates.
"""

import threading
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from purser import db, ledger, networth
from purser.holdings import awards, captures, lots, service
from purser.holdings import cash as trade_cash
from purser.ledger import LedgerError
from purser.models import (
    Account,
    AuditLog,
    AwardVest,
    HoldingsCapture,
    HoldingsSnapshot,
    Lot,
    LotDisposition,
    PositionEvent,
    RawFetch,
    SecurityPrice,
)
from tests.conftest import now, today


def _as_of() -> date:
    return today() - timedelta(days=20)


def _symbol() -> str:
    return f"L{uuid.uuid4().hex[:7].upper()}"


def _broker(session: Session) -> Account:
    account = ledger.create_account(
        session,
        name="Plan",
        type_="brokerage",
        path=f"assets.plan_{uuid.uuid4().hex[:8]}",
        actor_token_id=None,
    )
    ledger.update_account(
        session, account_id=account.id, holdings_source="manual", actor_token_id=None
    )
    return account


def _lot(ref: str | None, qty: str, acquired: date, per_share: str | None = "10", **kw):
    return captures.LotIn(
        acquired_on=acquired,
        quantity=Decimal(qty),
        external_ref=ref,
        plan=kw.pop("plan", "Plan A"),
        basis_per_share=Decimal(per_share) if per_share is not None else None,
        acquisition=kw.pop("acquisition", "plan_purchase"),
        **kw,
    )


def _capture(
    account: Account,
    symbol: str,
    rows: list[captures.LotIn],
    *,
    as_of: date | None = None,
    complete: bool = True,
    captured_at: datetime | None = None,
    basis_policy: str = "keep",
) -> captures.LotCaptureIn:
    return captures.LotCaptureIn(
        account=str(account.id),
        as_of=as_of or _as_of(),
        captured_at=captured_at or now(),
        completeness="complete" if complete else "partial",
        positions=(captures.SymbolIn(symbol=symbol, lots=tuple(rows)),),
        source_note="synthetic broker page",
        basis_policy=basis_policy,
    )


def _apply(session: Session, capture: captures.LotCaptureIn, **kw) -> captures.Applied:
    preview = captures.preview(session, capture)
    return captures.commit(session, capture, expect_hash=preview.plan_hash, token=None, **kw)


def _position(session: Session, account: Account, symbol: str):
    p = service.open_position(session, account.id, symbol)
    assert p is not None
    return p


def test_multi_symbol_capture_projects_once_with_final_lot_basis(
    clean_db: None,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from purser.holdings import projection

    account = _broker(db_session)
    symbols = [_symbol(), _symbol()]
    db_session.add_all(
        [
            SecurityPrice(symbol=symbol, price_date=today(), source="manual", close=Decimal("100"))
            for symbol in symbols
        ]
    )
    db_session.flush()
    call_count = 0
    original = projection.project_account

    def counted(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(projection, "project_account", counted)
    capture = captures.LotCaptureIn(
        account=str(account.id),
        as_of=_as_of(),
        captured_at=now(),
        completeness="complete",
        positions=tuple(
            captures.SymbolIn(
                symbol=symbol,
                lots=(
                    _lot("SYN-1", "2", _as_of(), "10"),
                    _lot("SYN-2", "3", _as_of(), "20"),
                ),
            )
            for symbol in symbols
        ),
    )
    _apply(db_session, capture)
    assert call_count == 1
    rows = list(
        db_session.scalars(
            select(HoldingsSnapshot).where(
                HoldingsSnapshot.account_id == account.id,
                HoldingsSnapshot.as_of == today(),
            )
        )
    )
    assert len(rows) == 2
    assert all(row.quantity == Decimal("5") and row.cost_basis == Decimal("80") for row in rows)


def _state(session: Session, account: Account, symbol: str) -> service.PositionState:
    return next(
        s
        for s in service.position_states(session, account, stale_days=7)
        if s.position.symbol == symbol
    )


def _three_lots(session: Session) -> tuple[Account, str]:
    account = _broker(session)
    symbol = _symbol()
    _apply(
        session,
        _capture(
            account,
            symbol,
            [
                _lot("R1", "10", date(2023, 1, 5), "10"),
                _lot("R2", "10", date(2024, 6, 5), "30"),
                _lot("R3", "5", date(2024, 6, 5), "20"),  # same day and plan as R2
            ],
        ),
    )
    return account, symbol


class TestCaptureAndBasis:
    def test_complete_capture_sets_quantity_and_lot_basis(self, db_session: Session) -> None:
        account, symbol = _three_lots(db_session)
        state = _state(db_session, account, symbol)
        assert state.quantity == 25
        assert state.cost_basis == Decimal("500.0000")  # 100 + 300 + 100, not an average
        assert state.lots is not None and state.lots.unallocated == 0
        assert state.open_lots == 3
        row = db_session.execute(
            select(HoldingsSnapshot)
            .where(HoldingsSnapshot.account_id == account.id, HoldingsSnapshot.symbol == symbol)
            .order_by(HoldingsSnapshot.as_of.desc())
            .limit(1)
        ).scalar_one()
        assert row.cost_basis == Decimal("500.0000") and row.quantity == 25
        # Raw-first evidence exists and is encrypted.
        cap = db_session.execute(
            select(HoldingsCapture).where(HoldingsCapture.account_id == account.id)
        ).scalar_one()
        raw = db_session.get(RawFetch, cap.raw_fetch_id)
        assert (
            raw is not None and raw.source == "capture" and b"plan_purchase" not in raw.payload_enc
        )

    def test_lot_sale_moves_basis_to_remaining_lots(self, db_session: Session) -> None:
        account, symbol = _three_lots(db_session)
        position = _position(db_session, account, symbol)
        r2 = lots.resolve_lot(db_session, position, "R2")
        sale = service.record_sale(
            db_session,
            account_ref=str(account.id),
            symbol=symbol,
            quantity=Decimal("4"),
            price=Decimal("50"),
            trade_date=today() - timedelta(days=2),
            proceeds_to="none",
            token=None,
            allocation=service.AllocationRequest(lots=((r2, Decimal("4")),)),
        )
        [d] = sale.dispositions
        assert d.cost_basis == Decimal("120.0000") and d.proceeds == Decimal("200.00")
        assert d.realized_gain == Decimal("80.0000") and d.term == "long"
        state = _state(db_session, account, symbol)
        assert state.quantity == 21 and state.cost_basis == Decimal("380.0000")

    def test_unknown_basis_lot_makes_total_unknown(self, db_session: Session) -> None:
        account = _broker(db_session)
        symbol = _symbol()
        _apply(
            db_session,
            _capture(
                account,
                symbol,
                [_lot("A", "5", date(2024, 1, 2)), _lot("B", "5", date(2024, 2, 2), None)],
            ),
        )
        state = _state(db_session, account, symbol)
        assert state.cost_basis is None and state.unrealized_gain is None
        assert state.lots is not None and state.lots.unknown_quantity == 5
        assert state.lots.known == Decimal("50.0000")

    def test_exact_retry_and_repeat_observation(self, db_session: Session) -> None:
        account, symbol = _three_lots(db_session)
        first = _capture(
            account,
            symbol,
            [
                _lot("R1", "10", date(2023, 1, 5), "10"),
                _lot("R2", "10", date(2024, 6, 5), "30"),
                _lot("R3", "5", date(2024, 6, 5), "20"),
            ],
        )
        events_before = db_session.execute(select(func.count(PositionEvent.id))).scalar_one()
        lots_before = db_session.execute(select(func.count(Lot.id))).scalar_one()
        retry = captures.commit(db_session, first, expect_hash="anything", token=None)
        assert retry.retry is True
        assert db_session.execute(select(func.count(HoldingsCapture.id))).scalar_one() >= 1
        # The same holdings observed later: a new capture row, nothing else.
        again = _capture(
            account,
            symbol,
            list(reversed(first.positions[0].lots)),
            as_of=_as_of() + timedelta(days=5),
            captured_at=now() + timedelta(seconds=30),
        )
        applied = _apply(db_session, again)
        assert applied.retry is False and applied.capture.repeat_of is not None
        assert applied.result["counts"] == {}
        assert (
            db_session.execute(select(func.count(PositionEvent.id))).scalar_one() == events_before
        )
        assert db_session.execute(select(func.count(Lot.id))).scalar_one() == lots_before
        fresh = lots.freshness(db_session, account.id, symbol)
        assert fresh is not None and fresh.as_of == _as_of() + timedelta(days=5)

    def test_stale_hash_is_refused(self, db_session: Session) -> None:
        account, symbol = _three_lots(db_session)
        capture = _capture(
            account,
            symbol,
            [
                _lot("R1", "10", date(2023, 1, 5), "10"),
                _lot("R2", "10", date(2024, 6, 5), "30"),
                _lot("R3", "5", date(2024, 6, 5), "20"),
                _lot("R4", "2", date(2024, 9, 5), "40"),
            ],
            as_of=_as_of() + timedelta(days=1),
        )
        preview = captures.preview(db_session, capture)
        assert [c.kind for c in preview.symbols[0].plan.changes] == ["new_lot", "recount"]
        # A sale lands in between: the plan's state changed.
        service.record_sale(
            db_session,
            account_ref=str(account.id),
            symbol=symbol,
            quantity=Decimal("1"),
            price=Decimal("50"),
            trade_date=today() - timedelta(days=1),
            proceeds_to="none",
            token=None,
            allocation=service.AllocationRequest(method="fifo"),
        )
        with pytest.raises(captures.PreviewStale) as exc:
            captures.commit(db_session, capture, expect_hash=preview.plan_hash, token=None)
        assert exc.value.preview.plan_hash != preview.plan_hash

    def test_decrease_missing_and_partial_are_conflicts(self, db_session: Session) -> None:
        account, symbol = _three_lots(db_session)
        later = _as_of() + timedelta(days=3)
        decrease = _capture(
            account,
            symbol,
            [
                _lot("R1", "8", date(2023, 1, 5), "10"),
                _lot("R2", "10", date(2024, 6, 5), "30"),
                _lot("R3", "5", date(2024, 6, 5), "20"),
            ],
            as_of=later,
        )
        preview = captures.preview(db_session, decrease)
        assert [c.code for c in preview.symbols[0].plan.conflicts] == ["quantity_decrease"]
        with pytest.raises(captures.CaptureConflicts):
            captures.commit(db_session, decrease, expect_hash=preview.plan_hash, token=None)
        missing = _capture(account, symbol, [_lot("R1", "10", date(2023, 1, 5), "10")], as_of=later)
        codes = [c.code for c in captures.preview(db_session, missing).symbols[0].plan.conflicts]
        assert codes == ["missing_from_capture", "missing_from_capture"]
        partial = _capture(
            account, symbol, [_lot("R9", "1", date(2024, 9, 1))], as_of=later, complete=False
        )
        codes = [c.code for c in captures.preview(db_session, partial).symbols[0].plan.conflicts]
        assert codes == ["insufficient_unallocated"]
        # Nothing was applied by any of them.
        assert _state(db_session, account, symbol).quantity == 25

    def test_skip_conflicts_records_evidence_only(self, db_session: Session) -> None:
        account, symbol = _three_lots(db_session)
        capture = _capture(
            account,
            symbol,
            [_lot("R1", "8", date(2023, 1, 5), "10")],
            as_of=_as_of() + timedelta(days=2),
        )
        preview = captures.preview(db_session, capture)
        applied = captures.commit(
            db_session, capture, expect_hash=preview.plan_hash, skip_conflicts=True, token=None
        )
        assert applied.skipped_symbols == (symbol,)
        assert applied.capture.symbols == []
        assert _state(db_session, account, symbol).quantity == 25

    def test_out_of_order_capture_is_never_applied(self, db_session: Session) -> None:
        account, symbol = _three_lots(db_session)
        older = _capture(
            account,
            symbol,
            [_lot("R1", "10", date(2023, 1, 5), "10")],
            as_of=_as_of() - timedelta(days=5),
            complete=False,
        )
        plan = captures.preview(db_session, older).symbols[0]
        assert plan.stale and plan.plan.changes == ()

    def test_ambiguous_payload_refused(self, db_session: Session) -> None:
        account = _broker(db_session)
        with pytest.raises(LedgerError, match="distinct ordinal"):
            captures.preview(
                db_session,
                _capture(
                    account,
                    _symbol(),
                    [_lot(None, "1", date(2024, 1, 1)), _lot(None, "2", date(2024, 1, 1))],
                ),
            )


class TestEntryPointsKeepInvariants:
    def test_sell_without_allocation_refused(self, db_session: Session) -> None:
        account, symbol = _three_lots(db_session)
        with pytest.raises(service.LotRuleError) as exc:
            service.record_sale(
                db_session,
                account_ref=str(account.id),
                symbol=symbol,
                quantity=Decimal("1"),
                price=Decimal("5"),
                trade_date=today(),
                proceeds_to="none",
                token=None,
            )
        assert exc.value.code == "allocation_required"

    def test_rest_style_event_paths(self, db_session: Session) -> None:
        account, symbol = _three_lots(db_session)
        position = _position(db_session, account, symbol)
        # A set_quantity below the lots is refused.
        with pytest.raises(service.LotRuleError) as exc:
            service.record_position(
                db_session,
                account_ref=str(account.id),
                symbol=symbol,
                quantity=Decimal("20"),
                as_of=today(),
                token=None,
            )
        assert exc.value.code == "coverage"
        # A transfer out names its lots and moves no cash.
        r1 = lots.resolve_lot(db_session, position, "R1")
        event = service.record_event(
            db_session,
            position_id=position.id,
            kind="transfer_out",
            occurred_on=today(),
            quantity=Decimal("3"),
            token=None,
            allocation=service.AllocationRequest(lots=((r1, Decimal("3")),)),
        )
        assert event.transaction_id is None
        assert _state(db_session, account, symbol).cost_basis == Decimal("470.0000")
        # A buy on a lot-tracked position opens its own lot by default.
        buy = service.record_event(
            db_session,
            position_id=position.id,
            kind="buy",
            occurred_on=today(),
            quantity=Decimal("2"),
            price=Decimal("15"),
            token=None,
        )
        opened = db_session.execute(
            select(Lot).where(Lot.opened_by_event_id == buy.id)
        ).scalar_one()
        assert opened.cost_basis == Decimal("30.0000")
        # ...or adds unallocated shares when asked, and a method never takes them.
        service.record_event(
            db_session,
            position_id=position.id,
            kind="buy",
            occurred_on=today(),
            quantity=Decimal("1"),
            token=None,
            unallocated=True,
        )
        state = _state(db_session, account, symbol)
        assert state.lots is not None and state.lots.unallocated == 1
        assert state.cost_basis is None
        with pytest.raises(service.LotRuleError) as exc:
            service.record_event(
                db_session,
                position_id=position.id,
                kind="sell",
                occurred_on=today(),
                quantity=Decimal("25"),
                price=Decimal("10"),
                proceeds_to="none",
                token=None,
                allocation=service.AllocationRequest(method="fifo"),
            )
        assert exc.value.code == "insufficient_lots"
        service.record_event(
            db_session,
            position_id=position.id,
            kind="sell",
            occurred_on=today(),
            quantity=Decimal("1"),
            price=Decimal("10"),
            proceeds_to="none",
            token=None,
            allocation=service.AllocationRequest(lots=((None, Decimal("1")),)),
        )
        assert _state(db_session, account, symbol).cost_basis is not None

    def test_reversals(self, db_session: Session) -> None:
        account, symbol = _three_lots(db_session)
        position = _position(db_session, account, symbol)
        buy = service.record_event(
            db_session,
            position_id=position.id,
            kind="buy",
            occurred_on=today() - timedelta(days=3),
            quantity=Decimal("2"),
            price=Decimal("15"),
            token=None,
        )
        lot = db_session.execute(select(Lot).where(Lot.opened_by_event_id == buy.id)).scalar_one()
        sell = service.record_event(
            db_session,
            position_id=position.id,
            kind="sell",
            occurred_on=today() - timedelta(days=1),
            quantity=Decimal("1"),
            price=Decimal("20"),
            proceeds_to="none",
            token=None,
            allocation=service.AllocationRequest(lots=((lot.id, Decimal("1")),)),
        )
        # A refused write rolls back with its request; here, its savepoint.
        with pytest.raises(service.LotRuleError) as exc, db_session.begin_nested():
            service.reverse_event(db_session, event_id=buy.id, token=None)
        assert exc.value.code == "dead_lot"
        service.reverse_event(db_session, event_id=sell.id, token=None)
        # The disposition stays, inert; the lot holds its shares again.
        assert (
            db_session.execute(
                select(func.count(LotDisposition.id)).where(LotDisposition.event_id == sell.id)
            ).scalar_one()
            == 1
        )
        assert _state(db_session, account, symbol).cost_basis == Decimal("530.0000")
        service.reverse_event(db_session, event_id=buy.id, token=None)
        assert _state(db_session, account, symbol).cost_basis == Decimal("500.0000")

    def test_void_and_correct_basis(self, db_session: Session) -> None:
        account, symbol = _three_lots(db_session)
        position = _position(db_session, account, symbol)
        r3 = lots.resolve_lot(db_session, position, "R3")
        lots.correct_basis(
            db_session,
            lot_id=r3,
            basis_per_share=Decimal("21"),
            cost_basis=None,
            note="broker fixed basis",
            token=None,
        )
        assert _state(db_session, account, symbol).cost_basis == Decimal("505.0000")
        service.record_sale(
            db_session,
            account_ref=str(account.id),
            symbol=symbol,
            quantity=Decimal("1"),
            price=Decimal("50"),
            trade_date=today(),
            proceeds_to="none",
            token=None,
            allocation=service.AllocationRequest(lots=((r3, Decimal("1")),)),
        )
        with pytest.raises(service.LotRuleError) as exc:
            lots.void_lot(db_session, lot_id=r3, note=None, token=None)
        assert exc.value.code == "lot_has_dispositions"
        with pytest.raises(service.LotRuleError):
            lots.correct_basis(
                db_session,
                lot_id=r3,
                basis_per_share=Decimal("1"),
                cost_basis=None,
                note=None,
                token=None,
            )
        r1 = lots.resolve_lot(db_session, position, "R1")
        lots.void_lot(db_session, lot_id=r1, note="recorded twice", token=None)
        state = _state(db_session, account, symbol)
        assert state.lots is not None and state.lots.unallocated == 10
        actions = set(
            db_session.execute(
                select(AuditLog.action).where(AuditLog.entity_id.in_([r1, r3]))
            ).scalars()
        )
        assert {"lot.void", "lot.basis"} <= actions

    def test_cash_sale_with_lots_keeps_cash_safeguards(self, db_session: Session) -> None:
        """A lot-allocated sale posts its cash like any sale, its
        dispositions' proceeds add up to that cash, and the double-count
        probe still refuses a sale whose cash the account already shows."""
        from purser.core import ledger as core_ledger
        from purser.feed import importer

        account, symbol = _three_lots(db_session)
        trade = today() - timedelta(days=1)
        sale = service.record_sale(
            db_session,
            account_ref=str(account.id),
            symbol=symbol,
            quantity=Decimal("2"),
            price=Decimal("50"),
            fees=Decimal("1"),
            trade_date=trade,
            token=None,
            allocation=service.AllocationRequest(method="hifo"),
        )
        assert sale.cash_amount == Decimal("99.00")
        assert sum(d.proceeds for d in sale.dispositions) == Decimal("99.00")
        category = importer._uncategorized(db_session, income=True)
        ledger.create_flat_transaction(
            db_session,
            account_ref=str(account.id),
            category_ref=str(category.id),
            amount=Decimal("49.00"),
            date=trade,
            description="SOLD",
            requested_status=core_ledger.Status.POSTED,
            origin=core_ledger.Origin.IMPORT,
            token=None,
            content_hash=f"test-{uuid.uuid4().hex}",
        )
        with pytest.raises(trade_cash.CashAlreadyRecorded):
            service.record_sale(
                db_session,
                account_ref=str(account.id),
                symbol=symbol,
                quantity=Decimal("1"),
                price=Decimal("50"),
                fees=Decimal("1"),
                trade_date=trade,
                token=None,
                allocation=service.AllocationRequest(method="fifo"),
            )
        # The refused sale left no dispositions behind.
        position = _position(db_session, account, symbol)
        assert db_session.execute(
            select(func.count(LotDisposition.id)).where(LotDisposition.position_id == position.id)
        ).scalar_one() == len(sale.dispositions)


class TestAwards:
    def _awards(self, session: Session, account: Account, symbol: str, **kw):
        capture = awards.AwardCaptureIn(
            account=str(account.id),
            as_of=kw.pop("as_of", _as_of()),
            captured_at=kw.pop("captured_at", now()),
            completeness=kw.pop("completeness", "complete"),
            awards=(
                awards.AwardIn(
                    grant_ref="G-1",
                    symbol=symbol,
                    granted_on=date(2024, 1, 15),
                    granted_quantity=Decimal("100"),
                    vested_quantity=kw.pop("vested_quantity", Decimal("25")),
                    schedule=kw.pop(
                        "schedule",
                        (
                            awards.VestIn(_as_of() + timedelta(days=5), Decimal("25")),
                            awards.VestIn(today() + timedelta(days=90), Decimal("25")),
                            awards.VestIn(today() + timedelta(days=455), Decimal("25")),
                        ),
                    ),
                ),
            ),
        )
        preview = awards.preview_awards(session, capture)
        return capture, preview

    def test_awards_never_touch_net_worth(self, db_session: Session) -> None:
        account, symbol = _three_lots(db_session)
        db_session.add(
            SecurityPrice(symbol=symbol, price_date=_as_of(), close=Decimal("40"), source="manual")
        )
        db_session.flush()
        before = networth.net_worth_at(db_session, today())["net_worth"]
        capture, preview = self._awards(db_session, account, symbol)
        awards.commit_awards(db_session, capture, expect_hash=preview.plan_hash, token=None)
        assert networth.net_worth_at(db_session, today())["net_worth"] == before
        [u] = awards.unvested(db_session, account)
        assert u.units == 75 and u.next_vest_units == 25
        assert u.next_vest_date == today() + timedelta(days=90)
        assert u.value == Decimal("3000.0000")

    def test_vest_event_idempotent_and_conflicting(self, db_session: Session) -> None:
        account, symbol = _three_lots(db_session)
        capture, preview = self._awards(db_session, account, symbol)
        awards.commit_awards(db_session, capture, expect_hash=preview.plan_hash, token=None)
        quantity = _state(db_session, account, symbol).quantity
        kw = dict(
            award="G-1",
            account=str(account.id),
            vest_date=_as_of() + timedelta(days=5),
            gross_quantity=Decimal("25"),
            withheld_quantity=Decimal("9"),
            fmv=Decimal("40"),
            token=None,
            force_new_lot=True,
        )
        out = awards.record_vest(db_session, **kw)
        assert out.event_id is not None and out.net == 16
        assert _state(db_session, account, symbol).quantity == quantity + 16
        again = awards.record_vest(db_session, **kw)
        assert again.already_recorded and again.lot_id == out.lot_id
        assert _state(db_session, account, symbol).quantity == quantity + 16
        with pytest.raises(awards.VestConflict):
            awards.record_vest(db_session, **{**kw, "withheld_quantity": Decimal("8")})
        assert awards.unvested(db_session, account)[0].units == 50
        # Reversing the vest event hands the units back.
        service.reverse_event(db_session, event_id=out.event_id, token=None)
        vest = db_session.execute(select(AwardVest).where(AwardVest.id == out.vest.id)).scalar_one()
        assert vest.status == "scheduled" and vest.lot_id is None
        assert _state(db_session, account, symbol).quantity == quantity

    def test_vest_links_an_adopted_lot_without_new_shares(self, db_session: Session) -> None:
        account = _broker(db_session)
        symbol = _symbol()
        vest_day = _as_of() - timedelta(days=5)
        _apply(
            db_session,
            _capture(account, symbol, [_lot("V1", "16", vest_day, "40", acquisition="rsu_vest")]),
        )
        # The broker lists the vest as still to come and reports no vested
        # count yet: its baseline is unknown.
        capture, preview = self._awards(
            db_session,
            account,
            symbol,
            vested_quantity=None,
            schedule=(
                awards.VestIn(vest_day, Decimal("25")),
                awards.VestIn(today() + timedelta(days=90), Decimal("25")),
                awards.VestIn(today() + timedelta(days=455), Decimal("25")),
            ),
        )
        awards.commit_awards(db_session, capture, expect_hash=preview.plan_hash, token=None)
        with pytest.raises(awards.VestConflict, match="possible_duplicate"):
            awards.record_vest(
                db_session,
                award="G-1",
                account=str(account.id),
                vest_date=vest_day,
                gross_quantity=Decimal("25"),
                withheld_quantity=Decimal("9"),
                fmv=Decimal("40"),
                token=None,
            )
        with pytest.raises(LedgerError, match="fair market value"):
            awards.record_vest(
                db_session,
                award="G-1",
                account=str(account.id),
                vest_date=vest_day,
                gross_quantity=Decimal("25"),
                withheld_quantity=Decimal("9"),
                fmv=Decimal("41"),
                lot="V1",
                token=None,
            )
        out = awards.record_vest(
            db_session,
            award="G-1",
            account=str(account.id),
            vest_date=vest_day,
            gross_quantity=Decimal("25"),
            withheld_quantity=Decimal("9"),
            fmv=Decimal("40"),
            lot="V1",
            token=None,
        )
        assert out.linked and out.event_id is None
        assert _state(db_session, account, symbol).quantity == 16
        assert awards.unvested(db_session, account)[0].units == 50

    def test_award_capture_conflicts_and_retry(self, db_session: Session) -> None:
        account, symbol = _three_lots(db_session)
        capture, preview = self._awards(db_session, account, symbol)
        awards.commit_awards(db_session, capture, expect_hash=preview.plan_hash, token=None)
        retry = awards.commit_awards(db_session, capture, expect_hash="x", token=None)
        assert retry.retry
        # The next capture drops a future vest: a conflict, never a forfeit.
        dropped, p2 = self._awards(
            db_session,
            account,
            symbol,
            as_of=_as_of() + timedelta(days=1),
            captured_at=now() + timedelta(seconds=5),
            schedule=(awards.VestIn(today() + timedelta(days=90), Decimal("25")),),
        )
        assert {c.code for c in p2.plan.conflicts} == {"scheduled_vest_missing"}
        with pytest.raises(awards.AwardCaptureConflicts):
            awards.commit_awards(db_session, dropped, expect_hash=p2.plan_hash, token=None)


def test_concurrent_duplicate_commits_apply_once(migrated_engine: Engine) -> None:
    """Two writers commit the same previewed capture at once: the second
    waits on the account lock, then finds the first's capture (a retry).
    Exactly one capture, one set of lots, one recount."""
    maker = db.get_sessionmaker()
    with maker() as s:
        account = _broker(s)
        s.commit()
        account_id = account.id
    symbol = _symbol()
    with maker() as s:
        acct = s.get(Account, account_id)
        assert acct is not None
        capture = _capture(
            acct,
            symbol,
            [_lot(f"C{i}", "1", date(2024, 1, 1) + timedelta(days=i)) for i in range(30)],
        )
        plan_hash = captures.preview(s, capture).plan_hash
    results: list[object] = []
    barrier = threading.Barrier(2)

    def work() -> None:
        with maker() as s:
            barrier.wait()
            try:
                results.append(captures.commit(s, capture, expect_hash=plan_hash, token=None))
                s.commit()
            except Exception as exc:
                results.append(exc)

    threads = [threading.Thread(target=work) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(isinstance(r, captures.Applied) for r in results), results
    assert sorted(r.retry for r in results) == [False, True]  # type: ignore[attr-defined]
    with maker() as s:
        assert (
            s.execute(
                select(func.count(HoldingsCapture.id)).where(
                    HoldingsCapture.account_id == account_id
                )
            ).scalar_one()
            == 1
        )
        position = service.open_position(s, account_id, symbol)
        assert position is not None
        assert (
            s.execute(select(func.count(Lot.id)).where(Lot.position_id == position.id)).scalar_one()
            == 30
        )
        assert (
            s.execute(
                select(func.count(PositionEvent.id)).where(PositionEvent.position_id == position.id)
            ).scalar_one()
            == 1
        )


def _threads(work, n: int = 2) -> list[object]:
    results: list[object] = []
    barrier = threading.Barrier(n)

    def run() -> None:
        barrier.wait()
        try:
            results.append(work())
        except Exception as exc:
            results.append(exc)

    threads = [threading.Thread(target=run) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def test_concurrent_identical_vests_record_once(migrated_engine: Engine) -> None:
    """Two writers record the same vest at once: the second waits on the
    account lock, then finds the vest recorded with the same figures."""
    maker = db.get_sessionmaker()
    symbol = _symbol()
    vest_day = today() - timedelta(days=1)
    with maker() as s:
        account = _broker(s)
        capture = awards.AwardCaptureIn(
            account=str(account.id),
            as_of=today() - timedelta(days=3),
            captured_at=now(),
            completeness="complete",
            awards=(
                awards.AwardIn(
                    grant_ref="G-CONC",
                    symbol=symbol,
                    granted_on=date(2024, 1, 2),
                    granted_quantity=Decimal("20"),
                    schedule=(awards.VestIn(vest_day, Decimal("20")),),
                ),
            ),
        )
        preview = awards.preview_awards(s, capture)
        awards.commit_awards(s, capture, expect_hash=preview.plan_hash, token=None)
        s.commit()
        account_id = account.id

    def vest() -> object:
        with maker() as s:
            out = awards.record_vest(
                s,
                award="G-CONC",
                account=str(account_id),
                vest_date=vest_day,
                gross_quantity=Decimal("20"),
                withheld_quantity=Decimal("8"),
                fmv=Decimal("30"),
                token=None,
            )
            s.commit()
            return out

    results = _threads(vest)
    assert all(isinstance(r, awards.VestOutcome) for r in results), results
    assert sorted(r.already_recorded for r in results) == [False, True]  # type: ignore[attr-defined]
    with maker() as s:
        position = service.open_position(s, account_id, symbol)
        assert position is not None
        assert service.quantity_on(s, position, today()) == 12
        assert (
            s.execute(select(func.count(Lot.id)).where(Lot.position_id == position.id)).scalar_one()
            == 1
        )


def test_competing_captures_one_wins_one_is_stale(migrated_engine: Engine) -> None:
    """Two different captures previewed on the same state and committed at
    once: the first applies, the second's plan no longer matches."""
    maker = db.get_sessionmaker()
    symbol = _symbol()
    with maker() as s:
        account = _broker(s)
        s.commit()
        account_id = account.id
        one = _capture(account, symbol, [_lot("A", "5", date(2024, 1, 1))])
        two = _capture(account, symbol, [_lot("B", "7", date(2024, 2, 1))])
        hashes = {
            id(one): captures.preview(s, one).plan_hash,
            id(two): captures.preview(s, two).plan_hash,
        }
    queue = [one, two]

    def commit() -> object:
        capture = queue.pop()
        with maker() as s:
            out = captures.commit(s, capture, expect_hash=hashes[id(capture)], token=None)
            s.commit()
            return out

    results = _threads(commit)
    kinds = sorted(type(r).__name__ for r in results)
    assert kinds == ["Applied", "PreviewStale"], results
    with maker() as s:
        position = service.open_position(s, account_id, symbol)
        assert position is not None
        assert (
            s.execute(select(func.count(Lot.id)).where(Lot.position_id == position.id)).scalar_one()
            == 1
        )


from hypothesis import HealthCheck, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

_steps = st.lists(
    st.tuples(
        st.sampled_from(["sell", "transfer_out", "reverse"]),
        st.integers(min_value=1, max_value=9),  # shares
        st.sampled_from(["fifo", "lifo", "hifo", "specific"]),
        st.integers(min_value=0, max_value=5),  # which lot / which event
    ),
    min_size=1,
    max_size=6,
)


@settings(
    max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(steps=_steps)
def test_random_disposals_keep_lot_invariants(db_session: Session, steps) -> None:
    """Whatever mix of allocated sells, transfers out and reversals the
    service accepts, on real Postgres: every lot stays at or above zero, the
    lots never hold more than the position, each lot's dispositions plus its
    remaining basis are its basis exactly, and a refused write changes
    nothing."""
    from purser.core import lots as core_lots
    from purser.holdings import lotbook, projection

    savepoint = db_session.begin_nested()
    try:
        account, symbol = _three_lots(db_session)
        position = _position(db_session, account, symbol)
        recorded: list[uuid.UUID] = []
        day = _as_of()
        for kind, shares, method, pick in steps:
            day = min(day + timedelta(days=1), today())
            with db_session.begin_nested() as step:
                try:
                    if kind == "reverse":
                        if not recorded:
                            step.rollback()
                            continue
                        service.reverse_event(
                            db_session, event_id=recorded.pop(pick % len(recorded)), token=None
                        )
                        continue
                    if method == "specific":
                        lot_id = lots.resolve_lot(db_session, position, f"R{pick % 3 + 1}")
                        allocation = service.AllocationRequest(lots=((lot_id, Decimal(shares)),))
                    else:
                        allocation = service.AllocationRequest(method=method)
                    event = service.record_event(
                        db_session,
                        position_id=position.id,
                        kind=kind,
                        occurred_on=day,
                        quantity=Decimal(shares),
                        price=Decimal("40") if kind == "sell" else None,
                        proceeds_to="none" if kind == "sell" else None,
                        token=None,
                        allocation=allocation,
                    )
                    recorded.append(event.id)
                except LedgerError:
                    step.rollback()
        facts = [
            projection.event_fact(r)
            for r in projection.position_events(db_session, [position.id])[position.id]
        ]
        book = lotbook.book(lotbook.load(db_session, [position.id]).get(position.id), facts)
        core_lots.check(facts, book)  # raises on any broken invariant
        for lot in book.lots:
            left = book.remaining(lot)
            basis = book.remaining_basis(lot)
            assert left >= 0 and basis is not None and basis >= 0
            spent = sum((d.cost_basis or Decimal(0)) for d in book.of(lot.id))
            assert spent + basis == lot.cost_basis
            if left == 0:
                assert basis == 0
    finally:
        savepoint.rollback()


def test_dry_run_sale_reports_lots_and_writes_nothing(db_session: Session) -> None:
    account, symbol = _three_lots(db_session)
    before = db_session.execute(select(func.count(LotDisposition.id))).scalar_one()
    sale = service.record_sale(
        db_session,
        account_ref=str(account.id),
        symbol=symbol,
        quantity=Decimal("3"),
        price=Decimal("50"),
        trade_date=today(),
        proceeds_to="none",
        token=None,
        dry_run=True,
        allocation=service.AllocationRequest(method="fifo"),
    )
    assert sale.dry_run and [d.quantity for d in sale.dispositions] == [Decimal("3")]
    assert sale.dispositions[0].cost_basis == Decimal("30.0000")
    assert db_session.execute(select(func.count(LotDisposition.id))).scalar_one() == before
