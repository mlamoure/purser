"""Pure lot accounting (issue #393, decision 107): conservation of shares and
basis, selection methods, the coverage invariant, holding-period terms and
the capture planner. Synthetic figures only."""

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from purser.core import holdings as core
from purser.core import lots

D0 = date(2025, 1, 6)
_T0 = datetime(2025, 1, 1, tzinfo=UTC)

quantities = st.decimals(
    min_value=Decimal("0.000001"), max_value=Decimal("100000"), places=6, allow_nan=False
)
bases = st.decimals(min_value=Decimal("0"), max_value=Decimal("9999999"), places=4, allow_nan=False)


def _event(kind: str, on: date, quantity: str | Decimal, seq: int, **kw: object) -> core.Event:
    return core.Event(
        id=uuid.UUID(int=seq + 1000),
        occurred_on=on,
        created_at=_T0 + timedelta(seconds=seq),
        kind=kind,
        quantity=Decimal(quantity),
        **kw,  # type: ignore[arg-type]
    )


def _lot(
    n: int,
    quantity: str | Decimal,
    basis: str | Decimal | None,
    *,
    acquired: date = D0,
    effective: date | None = None,
    adopted: bool = True,
    per_share: str | Decimal | None = None,
    opened_by: uuid.UUID | None = None,
) -> lots.LotFact:
    q = Decimal(quantity)
    cb = Decimal(basis) if basis is not None else None
    ps = (
        Decimal(per_share)
        if per_share is not None
        else ((cb / q).quantize(core.PRICE_QUANT) if cb is not None else None)
    )
    return lots.LotFact(
        id=uuid.UUID(int=n),
        acquired_on=acquired,
        effective_on=effective or acquired,
        quantity=q,
        cost_basis=cb,
        basis_per_share=ps,
        ordinal=n,
        adopted=adopted,
        opened_by=opened_by,
    )


class TestConservation:
    @settings(max_examples=200, deadline=None)
    @given(
        quantity=quantities,
        basis=bases,
        cuts=st.lists(st.integers(min_value=1, max_value=1000), min_size=1, max_size=8),
        order=st.randoms(use_true_random=False),
    )
    def test_dispositions_and_remaining_sum_to_the_lot(
        self, quantity: Decimal, basis: Decimal, cuts: list[int], order: object
    ) -> None:
        """However a lot is cut up, recorded in any order, the dispositions'
        basis plus the remaining basis is the lot's basis exactly, nothing is
        negative, and an emptied lot has no basis left."""
        lot = _lot(1, quantity, basis)
        total = sum(cuts)
        pieces = [(quantity * c / total).quantize(core.QUANTITY_QUANT) for c in cuts]
        pieces[-1] = quantity - sum(pieces[:-1])
        pieces = [p for p in pieces if p > 0]
        book = lots.Book(lots=(lot,))
        for i, q in enumerate(pieces):
            q = min(q, lots.available(book, lot))
            if q <= 0:
                continue
            [a] = lots.allocate(book, D0 + timedelta(days=1 + i), [(lot, q)], None)
            assert a.cost_basis is not None and a.cost_basis >= 0
            book = lots.with_disposals(
                book, uuid.UUID(int=500 + i), D0 + timedelta(days=1 + i), [a]
            )
            left = book.remaining_basis(lot)
            assert left is not None and left >= 0
            spent = sum((d.cost_basis or Decimal(0)) for d in book.disposals)
            assert spent + left == basis
        if book.remaining(lot) == 0:
            assert book.remaining_basis(lot) == 0

    @settings(max_examples=100, deadline=None)
    @given(
        quantity=quantities,
        basis=bases,
        keep=st.lists(st.booleans(), min_size=3, max_size=3),
    )
    def test_reversal_restores(self, quantity: Decimal, basis: Decimal, keep: list[bool]) -> None:
        """Reversing any of a lot's disposals (their event no longer
        effective) restores exactly its shares and basis."""
        lot = _lot(1, quantity, basis)
        book = lots.Book(lots=(lot,))
        third = (quantity / 3).quantize(core.QUANTITY_QUANT)
        sizes = [third, third, quantity - 2 * third]
        events = []
        for i, q in enumerate(sizes):
            if q <= 0:
                continue
            [a] = lots.allocate(book, D0 + timedelta(days=i + 1), [(lot, q)], None)
            book = lots.with_disposals(
                book, uuid.UUID(int=900 + i), D0 + timedelta(days=i + 1), [a]
            )
            events.append(uuid.UUID(int=900 + i))
        live_ids = [e for e, k in zip(events, keep, strict=False) if k]
        live = lots.live([lot], book.disposals, live_ids)
        expected = quantity - sum(d.quantity for d in book.disposals if d.event_id in live_ids)
        assert live.remaining(lot) == expected
        left = live.remaining_basis(lot)
        assert left is not None
        assert (
            left
            + sum((d.cost_basis or Decimal(0)) for d in book.disposals if d.event_id in live_ids)
            == basis
        )

    @settings(max_examples=100, deadline=None)
    @given(
        total=st.decimals(min_value=Decimal("-999999"), max_value=Decimal("999999"), places=2),
        parts=st.lists(quantities, min_size=1, max_size=6),
    )
    def test_proceeds_split_exactly(self, total: Decimal, parts: list[Decimal]) -> None:
        split = lots.split_amount(total, parts)
        assert sum(p for p in split if p is not None) == total

    def test_unknown_basis_stays_unknown(self) -> None:
        lot = _lot(1, "10", None)
        book = lots.Book(lots=(lot,))
        [a] = lots.allocate(book, D0 + timedelta(days=2), [(lot, Decimal("4"))], Decimal("100.00"))
        assert a.cost_basis is None and a.realized_gain is None and a.proceeds == Decimal("100.00")
        assert book.remaining_basis(lot) is None
        basis = lots.position_basis(book, D0, Decimal("10"))
        assert basis.basis is None and basis.unknown_quantity == 10


class TestSelection:
    def _book(self) -> lots.Book:
        return lots.Book(
            lots=(
                _lot(1, "10", "100", acquired=date(2024, 1, 5), effective=D0),
                _lot(2, "10", "300", acquired=date(2024, 6, 5), effective=D0),
                _lot(3, "10", "200", acquired=date(2024, 3, 5), effective=D0),
            )
        )

    def test_fifo_lifo_hifo(self) -> None:
        book = self._book()
        day = D0 + timedelta(days=1)
        fifo = lots.select(lots.FIFO, book, day, Decimal("15"))
        assert [(lot.id.int, q) for lot, q in fifo] == [(1, 10), (3, 5)]
        lifo = lots.select(lots.LIFO, book, day, Decimal("15"))
        assert [(lot.id.int, q) for lot, q in lifo] == [(2, 10), (3, 5)]
        hifo = lots.select(lots.HIFO, book, day, Decimal("15"))
        assert [(lot.id.int, q) for lot, q in hifo] == [(2, 10), (3, 5)]

    def test_methods_never_reach_unallocated(self) -> None:
        with pytest.raises(lots.LotError) as exc:
            lots.select(lots.FIFO, self._book(), D0 + timedelta(days=1), Decimal("31"))
        assert exc.value.code == "insufficient_lots"

    def test_adopted_lot_drawn_only_after_its_day(self) -> None:
        book = self._book()
        with pytest.raises(lots.LotError):
            lots.select(lots.FIFO, book, D0, Decimal("1"))  # end of D0 only
        with pytest.raises(lots.LotError) as exc:
            lots.specific(book, D0, Decimal("1"), [(uuid.UUID(int=1), Decimal("1"))])
        assert exc.value.code == "before_lot"

    def test_hifo_refuses_unknown_basis(self) -> None:
        book = lots.with_lots(self._book(), [_lot(4, "5", None, effective=D0)])
        with pytest.raises(lots.LotError) as exc:
            lots.select(lots.HIFO, book, D0 + timedelta(days=1), Decimal("1"))
        assert exc.value.code == "hifo_unknown_basis"

    def test_specific_validation(self) -> None:
        book = self._book()
        day = D0 + timedelta(days=1)
        with pytest.raises(lots.LotError):  # more than the lot holds
            lots.specific(book, day, Decimal("11"), [(uuid.UUID(int=1), Decimal("11"))])
        with pytest.raises(lots.LotError):  # sum differs
            lots.specific(book, day, Decimal("5"), [(uuid.UUID(int=1), Decimal("4"))])
        with pytest.raises(lots.LotError):  # named twice
            lots.specific(
                book,
                day,
                Decimal("2"),
                [(uuid.UUID(int=1), Decimal("1")), (uuid.UUID(int=1), Decimal("1"))],
            )
        out = lots.specific(
            book, day, Decimal("6"), [(uuid.UUID(int=2), Decimal("5")), (None, Decimal("1"))]
        )
        assert out[1] == (None, Decimal("1"))


class TestTerm:
    @pytest.mark.parametrize(
        ("acquired", "disposed", "term"),
        [
            (date(2024, 3, 1), date(2025, 3, 1), "short"),  # the anniversary
            (date(2024, 3, 1), date(2025, 3, 2), "long"),
            (date(2024, 2, 29), date(2025, 2, 28), "short"),  # leap-day anniversary
            (date(2024, 2, 29), date(2025, 3, 1), "long"),
        ],
    )
    def test_boundaries(self, acquired: date, disposed: date, term: str) -> None:
        assert lots.term(acquired, disposed) == term


class TestCheck:
    def test_coverage_refused(self) -> None:
        events = [_event(core.SET_QUANTITY, D0, "10", 1)]
        book = lots.Book(lots=(_lot(1, "12", "1", effective=D0),))
        with pytest.raises(lots.LotError) as exc:
            lots.check(events, book)
        assert exc.value.code == "coverage"

    def test_unallocated_disposal_refused(self) -> None:
        events = [
            _event(core.SET_QUANTITY, D0, "10", 1),
            _event(core.SELL, D0 + timedelta(days=3), "2", 2, price=Decimal("5")),
        ]
        book = lots.Book(lots=(_lot(1, "10", "10", effective=D0),))
        with pytest.raises(lots.LotError) as exc:
            lots.check(events, book)
        assert exc.value.code == "unallocated_disposal"

    def test_simple_sale_before_lots_count_is_fine(self) -> None:
        events = [
            _event(core.SET_QUANTITY, D0, "10", 1),
            _event(core.SELL, D0 + timedelta(days=1), "2", 2, price=Decimal("5")),
            _event(core.SET_QUANTITY, D0 + timedelta(days=5), "8", 3),
        ]
        book = lots.Book(lots=(_lot(1, "8", "8", effective=D0 + timedelta(days=5)),))
        lots.check(events, book)

    def test_dead_lot(self) -> None:
        opener = uuid.UUID(int=77)
        lot = _lot(1, "5", "5", adopted=False, opened_by=opener)
        disposal = lots.Disposal(uuid.UUID(int=1002), lot.id, D0 + timedelta(days=1), Decimal("1"))
        book = lots.live([lot], [disposal], [uuid.UUID(int=1002)])  # opener reversed
        events = [
            _event(core.SET_QUANTITY, D0, "5", 1),
            _event(core.SELL, D0 + timedelta(days=1), "1", 2),
        ]
        with pytest.raises(lots.LotError) as exc:
            lots.check(events, book)
        assert exc.value.code == "dead_lot"

    @settings(max_examples=150, deadline=None)
    @given(
        held=st.integers(min_value=0, max_value=50),
        sizes=st.lists(st.integers(min_value=1, max_value=20), max_size=5),
    )
    def test_check_iff_lots_fit(self, held: int, sizes: list[int]) -> None:
        events = [_event(core.SET_QUANTITY, D0, str(held), 1)]
        book = lots.Book(
            lots=tuple(_lot(i + 1, str(s), None, effective=D0) for i, s in enumerate(sizes))
        )
        if sum(sizes) <= held:
            lots.check(events, book)
            assert lots.position_basis(book, D0, Decimal(held)).unallocated == held - sum(sizes)
        else:
            with pytest.raises(lots.LotError):
                lots.check(events, book)


def _row(
    i: int,
    quantity: str,
    *,
    ref: str | None = None,
    acquired: date = date(2024, 1, 5),
    plan: str | None = "p",
    ordinal: int | None = None,
    per_share: str | None = "10",
) -> lots.CaptureRow:
    q = Decimal(quantity)
    return lots.CaptureRow(
        index=i,
        ref=ref,
        acquired_on=acquired,
        plan=plan,
        ordinal=ordinal,
        quantity=q,
        basis=lots.normalize_basis(q, Decimal(per_share) if per_share else None, None),
        acquisition="plan_purchase",
    )


def _apply(plan: lots.SymbolPlan, rows: list[lots.CaptureRow], as_of: date) -> list[lots.KnownLot]:
    """Simulate a commit of new lots (the service's job) for re-run tests."""
    out = []
    for c in plan.changes:
        if c.kind != "new_lot":
            continue
        row = next(r for r in rows if r.index == c.row)
        fact = lots.LotFact(
            id=uuid.uuid4(),
            acquired_on=row.acquired_on,
            effective_on=as_of,
            quantity=row.quantity,
            cost_basis=row.basis.cost_basis,
            basis_per_share=row.basis.basis_per_share,
            ordinal=c.ordinal or 1,
            adopted=True,
        )
        out.append(lots.KnownLot(fact, row.ref, row.plan, False))
    return out


class TestCapturePlan:
    AS_OF = date(2025, 3, 10)

    def _plan(self, rows, known=(), events=(), complete=True, stale=False, replace=False):
        rows = lots.validate_rows(rows, self.AS_OF)
        book = lots.Book(lots=tuple(k.fact for k in known))
        return lots.plan_capture(
            rows,
            list(known),
            list(events),
            book,
            as_of=self.AS_OF,
            complete=complete,
            stale=stale,
            replace_basis=replace,
        )

    def test_same_day_lots_need_refs_or_ordinals(self) -> None:
        with pytest.raises(lots.LotError) as exc:
            lots.validate_rows([_row(0, "1"), _row(1, "2")], self.AS_OF)
        assert exc.value.code == "ambiguous_identity"
        rows = [_row(0, "1", ordinal=1), _row(1, "2", ordinal=2)]
        plan = self._plan(rows)
        assert [c.kind for c in plan.changes] == ["new_lot", "new_lot", "recount"]
        assert plan.target_quantity == 3

    def test_rerun_is_a_no_op_and_lots_stay_separate(self) -> None:
        rows = lots.validate_rows(
            [_row(i, str(i + 1), ref=f"R{i}") for i in range(100)]
            + [_row(100, "5", ref="S1"), _row(101, "5", ref="S2")],  # same date and plan
            self.AS_OF,
        )
        first = self._plan(rows)
        known = _apply(first, rows, self.AS_OF)
        assert len(known) == 102
        total = sum(r.quantity for r in rows)
        events = [_event(core.SET_QUANTITY, self.AS_OF, total, 1)]
        again = self._plan(rows, known, events)
        assert again.changes == () and again.conflicts == ()
        assert again.unchanged == 102

    def test_decrease_and_increase_are_conflicts(self) -> None:
        rows = lots.validate_rows([_row(0, "10", ref="A")], self.AS_OF)
        known = _apply(self._plan(rows), rows, self.AS_OF)
        events = [_event(core.SET_QUANTITY, self.AS_OF, "10", 1)]
        later = date(2025, 3, 20)
        for qty, code in (("7", "quantity_decrease"), ("12", "quantity_increase")):
            rows2 = lots.validate_rows([_row(0, qty, ref="A")], later)
            book = lots.Book(lots=tuple(k.fact for k in known))
            plan = lots.plan_capture(
                rows2,
                known,
                events,
                book,
                as_of=later,
                complete=True,
                stale=False,
                replace_basis=False,
            )
            assert plan.changes == ()
            assert [c.code for c in plan.conflicts] == [code]

    def test_missing_row(self) -> None:
        rows = lots.validate_rows([_row(0, "10", ref="A"), _row(1, "5", ref="B")], self.AS_OF)
        known = _apply(self._plan(rows), rows, self.AS_OF)
        events = [_event(core.SET_QUANTITY, self.AS_OF, "15", 1)]
        only_a = [_row(0, "10", ref="A")]
        complete = self._plan(only_a, known, events, complete=True)
        assert [c.code for c in complete.conflicts] == ["missing_from_capture"]
        partial = self._plan(only_a, known, events, complete=False)
        assert partial.conflicts == () and partial.not_in_capture == 1 and partial.changes == ()

    def test_partial_new_lot_needs_unallocated_shares(self) -> None:
        events = [_event(core.SET_QUANTITY, date(2025, 3, 1), "4", 1)]
        plan = self._plan([_row(0, "5", ref="A")], (), events, complete=False)
        assert [c.code for c in plan.conflicts] == ["insufficient_unallocated"]
        fits = self._plan([_row(0, "3", ref="A")], (), events, complete=False)
        assert [c.kind for c in fits.changes] == ["new_lot"]

    def test_basis(self) -> None:
        rows = lots.validate_rows([_row(0, "10", ref="A", per_share=None)], self.AS_OF)
        known = _apply(self._plan(rows), rows, self.AS_OF)
        events = [_event(core.SET_QUANTITY, self.AS_OF, "10", 1)]
        fill = self._plan([_row(0, "10", ref="A", per_share="12.5")], known, events)
        assert [c.kind for c in fill.changes] == ["fill_basis"]
        known2 = [
            lots.KnownLot(
                lots.LotFact(
                    k.fact.id,
                    k.fact.acquired_on,
                    k.fact.effective_on,
                    k.fact.quantity,
                    Decimal("125"),
                    Decimal("12.5"),
                    k.fact.ordinal,
                    True,
                ),
                k.ref,
                k.plan,
                False,
            )
            for k in known
        ]
        differs = self._plan([_row(0, "10", ref="A", per_share="13")], known2, events)
        assert [c.code for c in differs.conflicts] == ["basis_differs"]
        replace = self._plan([_row(0, "10", ref="A", per_share="13")], known2, events, replace=True)
        assert [c.kind for c in replace.changes] == ["replace_basis"]
        locked = [lots.KnownLot(k.fact, k.ref, k.plan, True) for k in known2]
        assert [
            c.code
            for c in self._plan(
                [_row(0, "10", ref="A", per_share="13")], locked, events, replace=True
            ).conflicts
        ] == ["basis_locked"]

    def test_stale_capture_applies_nothing(self) -> None:
        plan = self._plan([_row(0, "5", ref="A")], stale=True)
        assert plan.changes == ()
        assert {c.code for c in plan.conflicts} == {"stale_capture"}

    def test_zero_rows_never_seen_are_skipped(self) -> None:
        plan = self._plan([_row(0, "0", ref="Z"), _row(1, "2", ref="A")])
        assert plan.skipped_zero == 1
        assert [c.kind for c in plan.changes] == ["new_lot", "recount"]

    def test_ref_assigned_to_single_bare_lot(self) -> None:
        rows = lots.validate_rows([_row(0, "4")], self.AS_OF)
        known = _apply(self._plan(rows), rows, self.AS_OF)
        events = [_event(core.SET_QUANTITY, self.AS_OF, "4", 1)]
        plan = self._plan([_row(0, "4", ref="NEW")], known, events)
        assert [c.kind for c in plan.changes] == ["assign_ref"]


class TestBasisInput:
    def test_forms(self) -> None:
        q = Decimal("3")
        assert lots.normalize_basis(q, None, None).source is None
        ps = lots.normalize_basis(q, Decimal("10.123456"), None)
        assert ps.cost_basis == Decimal("30.3704") and ps.source == "per_share"
        tot = lots.normalize_basis(q, None, Decimal("30.37"))
        assert tot.basis_per_share == Decimal("10.123333")
        both = lots.normalize_basis(q, Decimal("10.123456"), Decimal("30.37"))
        assert both.source == "both"
        with pytest.raises(lots.LotError):
            lots.normalize_basis(q, Decimal("10"), Decimal("40"))


def test_complete_capture_below_the_count_is_a_conflict() -> None:
    """Shares outside every lot that the broker no longer shows are never
    recounted away: that would be a sale nobody recorded."""
    as_of = date(2025, 3, 10)
    events = [_event(core.SET_QUANTITY, date(2025, 3, 1), "20", 1)]
    rows = lots.validate_rows([_row(0, "15", ref="A")], as_of)
    plan = lots.plan_capture(
        rows,
        [],
        events,
        lots.Book(lots=()),
        as_of=as_of,
        complete=True,
        stale=False,
        replace_basis=False,
    )
    assert [c.code for c in plan.conflicts] == ["unexplained_decrease"]
