"""Property tests for a securities trade's cash (issue #390 phase 1b,
decision 103; CLAUDE.md guardrail 3: ledger invariants get property tests).

Over random sequences of sells and buys — moving cash, moving none, linking
a bank row, forcing past the probe — interleaved with reversals and unrelated
bank rows:

- postings per (transaction, currency) sum to zero;
- every correction of a posted row is a reversal pair linked through
  ``reverses_id`` (a reversal negates its row and has its replacement), and
  no trade's cash is undone by an unlinked offsetting row;
- the account's cash is the sum of its live rows — its live trade-cash rows
  plus its other live rows — and equals the model: the bank rows plus the
  cash Purser posted for the trades still standing; the stored running
  balance agrees;
- once every trade is reversed, ``equity.securities`` nets to zero and the
  account holds exactly its bank rows.

A second property runs the projection's own history strategy with sells
that move cash again (the projection property passes ``proceeds_to='none'``
for its share mechanics): the projection stays regenerable and agrees with
the timeline, and the cash is exactly the sales' net proceeds.

Synthetic data only: made-up symbols, prices and amounts.
"""

import uuid
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from purser import balances, db, ledger
from purser.core import holdings as core_holdings
from purser.core import ledger as core
from purser.holdings import cash as trade_cash
from purser.holdings import projection, service
from purser.ledger import LedgerError
from purser.models import Account, Position, PositionEvent, Posting, Transaction
from tests.test_holdings_projection import _feed_snapshot, _manual_rows, histories

TODAY = datetime.now(UTC).date()

_SETTINGS = settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)


def _weekday_before(days: int) -> date:
    day = TODAY - timedelta(days=days)
    while day.weekday() >= 4:
        day -= timedelta(days=1)
    return day


def _bank_row(session: Session, account: Account, amount: Decimal, on: date) -> Transaction:
    """A row the bank sent: import origin, uncategorized."""
    from purser.feed import importer

    category = importer._uncategorized(session, income=amount > 0)
    return ledger.create_flat_transaction(
        session,
        account_ref=str(account.id),
        category_ref=str(category.id),
        amount=amount,
        date=on,
        description="BANK",
        requested_status=core.Status.POSTED,
        origin=core.Origin.IMPORT,
        token=None,
        content_hash=f"prop-{uuid.uuid4().hex}",
    )


_quantity = st.decimals(min_value=Decimal("0.5"), max_value=Decimal("40"), places=1)
_price = st.decimals(min_value=Decimal("1"), max_value=Decimal("150"), places=2)
_fees = st.none() | st.decimals(min_value=Decimal("0"), max_value=Decimal("4"), places=2)
_days = st.integers(min_value=8, max_value=50)  # trade dates T-50..T-8: T+1+5 is past
_operation = st.one_of(
    st.tuples(
        st.just("sell"),
        _quantity,
        _price,
        _fees,
        _days,
        st.sampled_from(["cash", "none", "link", "force", "copied"]),
    ),
    st.tuples(
        st.just("buy"),
        _quantity,
        _price,
        _fees,
        _days,
        st.sampled_from(["cash", "none", "link", "copied"]),
    ),
    st.tuples(st.just("reverse"), st.integers(min_value=0, max_value=30)),
    st.tuples(
        st.just("bank"),
        st.builds(
            lambda magnitude, out: -magnitude if out else magnitude,
            st.decimals(min_value=Decimal("0.01"), max_value=Decimal("400"), places=2),
            st.booleans(),
        ),
        _days,
    ),
)


class _Model:
    """What the account's cash must be: its bank rows, plus the cash
    Purser posted for each trade still standing (a linked trade re-books a
    bank row, so it adds nothing)."""

    def __init__(self) -> None:
        self.bank = Decimal(0)
        self.live: dict[uuid.UUID, tuple[str, Decimal]] = {}  # event -> (kind, posted cash)

    def cash(self) -> Decimal:
        return self.bank + sum((posted for _kind, posted in self.live.values()), Decimal(0))


def _trade(
    session: Session, account: Account, position: Position, model: _Model, op: tuple
) -> None:
    kind, quantity, price, fees, days, mode = op
    day = _weekday_before(days)
    field = "proceeds_to" if kind == core_holdings.SELL else "funded_from"
    fields: dict[str, object] = {}
    net = trade_cash.cash_amount(kind, quantity, price, fees)
    signed = net if kind == core_holdings.SELL else -net
    bank = Decimal(0)
    posted = signed
    if mode == "none":
        fields[field] = trade_cash.NONE
        posted = Decimal(0)
    elif mode == "cash" and kind == core_holdings.BUY:
        fields[field] = trade_cash.CASH
    elif mode == "force":
        fields["force_cash"] = True
    elif mode == "link":
        if net <= 0:
            return
        fields["transaction_id"] = _bank_row(session, account, signed, day).id
        bank, posted = signed, Decimal(0)
    elif mode == "copied":
        # The bank already shows exactly this trade's cash, inside the probe's
        # window [trade, T+1+5]: the trade is refused (409) naming a row of
        # that amount, and force_cash posts the cash over it.
        if net <= 0:
            return
        if kind == core_holdings.BUY:
            fields[field] = trade_cash.CASH
        _start, window_end = trade_cash.probe_window(day, None)
        copy = _bank_row(session, account, signed, min(day + timedelta(days=days % 7), window_end))
        try:
            service.record_event(
                session,
                position_id=position.id,
                kind=kind,
                occurred_on=day,
                quantity=quantity,
                price=price,
                fees=fees,
                token=None,
                **fields,  # type: ignore[arg-type]
            )
        except trade_cash.CashAlreadyRecorded as found:
            assert found.amount == signed and found.date <= window_end, (found, copy.id)
            event("copied: 409")
        else:
            raise AssertionError(f"the probe missed bank row {copy.id} of {signed}")
        fields["force_cash"] = True
        bank = signed
    recorded = service.record_event(
        session,
        position_id=position.id,
        kind=kind,
        occurred_on=day,
        quantity=quantity,
        price=price,
        fees=fees,
        token=None,
        **fields,  # type: ignore[arg-type]
    )
    model.bank += bank
    model.live[recorded.id] = (kind, posted)


def _run(session: Session, account: Account, position: Position, ops: list) -> _Model:
    model = _Model()
    for op in ops:
        before = (model.bank, dict(model.live))
        try:
            with session.begin_nested():
                if op[0] in (core_holdings.SELL, core_holdings.BUY):
                    _trade(session, account, position, model, op)
                elif op[0] == "reverse":
                    if model.live:
                        target = list(model.live)[op[1] % len(model.live)]
                        service.reverse_event(session, event_id=target, token=None)
                        del model.live[target]
                else:
                    _amount, days = op[1], op[2]
                    _bank_row(session, account, _amount, _weekday_before(days))
                    model.bank += _amount
        except LedgerError as exc:
            model.bank, model.live = before  # refused: the savepoint took it all back
            event(f"{op[0]} refused: {type(exc).__name__}")
        else:
            mode = op[-1] if op[0] in (core_holdings.SELL, core_holdings.BUY) else ""
            event(f"{op[0]} {mode} ok")
    event(f"standing trades: {min(len(model.live), 5)}")
    return model


def _rows_of(session: Session, account: Account) -> list[Transaction]:
    touching = select(Posting.transaction_id).where(Posting.account_id == account.id)
    return list(session.execute(select(Transaction).where(Transaction.id.in_(touching))).scalars())


def _legs(tx: Transaction) -> Counter[tuple[uuid.UUID, Decimal, str]]:
    return Counter((p.account_id, Decimal(p.amount), p.currency.strip()) for p in tx.postings)


def _assert_ledger_invariants(
    session: Session, account: Account, position: Position, model: _Model
) -> None:
    session.flush()
    rows = _rows_of(session, account)
    by_id = {t.id: t for t in rows}

    # Postings per (transaction, currency) sum to zero.
    for tx in rows:
        if tx.status in ("pending", "posted"):
            totals: dict[str, Decimal] = {}
            for p in tx.postings:
                totals[p.currency.strip()] = totals.get(p.currency.strip(), Decimal(0)) + Decimal(
                    p.amount
                )
            assert all(total == 0 for total in totals.values()), (tx.id, totals)

    # Every correction is a reversal pair: walking a chain from its original,
    # odd depths are reversals (each the exact negation of the row it
    # reverses, and each with its replacement), even depths replacements.
    replaced = {t.reverses_id for t in rows if t.reverses_id is not None}
    for tx in rows:
        depth, node = 0, tx
        while node.reverses_id is not None:
            node = by_id[node.reverses_id]  # a chain never leaves the account
            depth += 1
        if depth % 2 == 1:
            parent = by_id[tx.reverses_id]  # type: ignore[index]
            negated = Counter({(a, -amount, c): n for (a, amount, c), n in _legs(parent).items()})
            assert _legs(tx) == negated, tx.id
            assert tx.id in replaced, f"reversal {tx.id} has no replacement"

    # No trade's cash is undone by an unlinked row: every system-leg row
    # outside a correction chain is the cash of a standing trade event.
    events = list(
        session.execute(
            select(PositionEvent).where(PositionEvent.position_id == position.id)
        ).scalars()
    )
    cash_of_trades = {e.transaction_id for e in events if e.reverses_id is None}
    for tx in rows:
        if ledger.has_system_leg(tx.postings) and tx.reverses_id is None:
            assert tx.id in cash_of_trades, f"unlinked trade-cash row {tx.id}"

    # The account's cash: every row's sum, every live row's sum (trade cash
    # plus the rest), and the model all agree; so does the stored running
    # balance.
    def on_account(tx: Transaction) -> Decimal:
        return sum(
            (Decimal(p.amount) for p in tx.postings if p.account_id == account.id), Decimal(0)
        )

    countable = [t for t in rows if t.status in ("pending", "posted")]
    every_row = sum((on_account(t) for t in countable), Decimal(0))
    heads = [t for t in countable if t.id not in replaced]
    trade_heads = sum(
        (on_account(t) for t in heads if ledger.has_system_leg(t.postings)), Decimal(0)
    )
    other_heads = sum(
        (on_account(t) for t in heads if not ledger.has_system_leg(t.postings)), Decimal(0)
    )
    assert every_row == trade_heads + other_heads == model.cash()
    assert ledger.account_balances(session, [account.id]).get(account.id, Decimal(0)) == every_row
    balances.recompute_running(session)
    assert balances.balance_asof(session, account.id, TODAY + timedelta(days=30)) == every_row


@_SETTINGS
@given(st.lists(_operation, min_size=1, max_size=12))
def test_trade_cash_keeps_the_ledger_invariants(migrated_engine: Engine, ops: list) -> None:
    with db.get_sessionmaker()() as session:
        account = ledger.create_account(
            session,
            name="Plan",
            type_="brokerage",
            path=f"assets.prop_{uuid.uuid4().hex[:10]}",
            actor_token_id=None,
        )
        ledger.update_account(
            session, account_id=account.id, holdings_source="manual", actor_token_id=None
        )
        position = service.record_position(
            session,
            account_ref=str(account.id),
            symbol=f"P{uuid.uuid4().hex[:8].upper()}",
            quantity=Decimal("150"),  # small enough that some sales oversell
            as_of=TODAY - timedelta(days=60),
            token=None,
        ).position
        model = _run(session, account, position, ops)
        _assert_ledger_invariants(session, account, position, model)

        # Reverse every trade still standing: sells first (shares come back),
        # then buys (only the opening count is left under them). None may be
        # refused, and afterwards the system account nets to zero.
        standing = sorted(model.live.items(), key=lambda item: item[1][0] != core_holdings.SELL)
        for event_id, _ in standing:
            service.reverse_event(session, event_id=event_id, token=None)
            del model.live[event_id]
        _assert_ledger_invariants(session, account, position, model)
        assert model.cash() == model.bank
        securities = ledger.securities_account(session)
        system_side = sum(
            (
                Decimal(p.amount)
                for tx in _rows_of(session, account)
                if tx.status in ("pending", "posted")
                for p in tx.postings
                if p.account_id == securities.id
            ),
            Decimal(0),
        )
        assert system_side == 0
        session.rollback()


@_SETTINGS
@given(histories())
def test_cash_moving_sells_keep_the_projection_regenerable_and_the_cash_exact(
    migrated_engine: Engine, spec: dict
) -> None:
    """The projection property's histories with priced sells that move their
    proceeds (the default): rows regenerate identically, the newest set at
    any day is the timeline, and the account's cash is the accepted sales'
    net proceeds, every one booked against equity.securities."""
    from purser.models import SecurityPrice

    with db.get_sessionmaker()() as session:
        symbols = [f"Q{uuid.uuid4().hex[:8].upper()}" for _ in range(spec["positions"])]
        account = ledger.create_account(
            session,
            name="Broker",
            type_="brokerage",
            path=f"assets.cashprop_{uuid.uuid4().hex[:10]}",
            actor_token_id=None,
        )
        if spec["feed"] is not None:
            _feed_snapshot(
                session, account.id, TODAY - timedelta(days=spec["feed"]), symbols[0], "1", "10.00"
            )
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
        proceeds = Decimal(0)
        for index, kind, days_ago, quantity in spec["events"]:
            price = Decimal("12.34") if kind != core_holdings.SET_QUANTITY else None
            try:
                with session.begin_nested():
                    service.record_event(
                        session,
                        position_id=positions[index].id,
                        kind=kind,
                        occurred_on=TODAY - timedelta(days=days_ago),
                        quantity=quantity,
                        price=price,
                        token=None,
                    )
            except LedgerError:
                continue  # refused: the service keeps only valid histories
            if kind == core_holdings.SELL:
                assert price is not None
                proceeds += trade_cash.cash_amount(kind, quantity, price, None)

        # The cash is exactly the accepted sales' proceeds, against the system
        # account (buys here are funded from outside, the default).
        cash = ledger.account_balances(session, [account.id]).get(account.id, Decimal(0))
        assert cash == proceeds
        securities = ledger.securities_account(session)
        touching = select(Posting.transaction_id).where(Posting.account_id == account.id)
        system_side = session.execute(
            select(Posting.amount).where(
                Posting.account_id == securities.id, Posting.transaction_id.in_(touching)
            )
        ).scalars()
        assert sum((Decimal(a) for a in system_side), Decimal(0)) == -proceeds

        # The projection is untouched by the cash: regenerable, and the
        # newest set at or before any day is the timeline on that day.
        first = _manual_rows(session, account.id)
        locked = projection.lock_account(session, account.id)
        assert projection.project_account(session, locked) == projection.ProjectionResult(
            None, 0, 0
        )
        session.execute(
            text("DELETE FROM holdings_snapshots WHERE account_id = :a AND source = 'manual'"),
            {"a": account.id},
        )
        projection.project_account(session, locked)
        assert _manual_rows(session, account.id) == first
        events = (
            session.execute(
                select(PositionEvent)
                .join(Position, Position.id == PositionEvent.position_id)
                .where(Position.account_id == account.id)
            )
            .scalars()
            .all()
        )
        by_position: dict[uuid.UUID, list[core_holdings.Event]] = {}
        for e in events:
            by_position.setdefault(e.position_id, []).append(projection.event_fact(e))
        timelines = {
            pid: core_holdings.Timeline(core_holdings.quantity_timeline(evs))
            for pid, evs in by_position.items()
        }
        dates = sorted({row[0] for row in first})
        if dates:
            for offset in range((TODAY - dates[0]).days + 1):
                day = dates[0] + timedelta(days=offset)
                newest = max(d for d in dates if d <= day)
                latest = {row[1]: row[2] for row in first if row[0] == newest}
                for pid, timeline in timelines.items():
                    assert latest.get(pid, Decimal(0)) == timeline.quantity_on(day)
        session.rollback()
