"""Set-based net worth equals the per-day, per-account engine it replaced
(#610).

`networth.net_worth_on` reads every posting sum, valuation and snapshot set a
range of days needs in three statements and walks the days in order. The
engine before it ran one postings query per account per day (`net_worth_at`
called once per day by every re-materialization), which made a CSV import
issue ~10k balance queries. This module keeps that engine as the REFERENCE —
copied verbatim below, deliberately not shared with the product — and proves
on random ledgers that the new path answers exactly the same, breakdown and
Decimal representation included: postings (pending, posted, voided, date-
edited corrections), brokerage holdings (feed rows, zero-valued feed rows
valued from a stored close or unpriced), loan valuations (two on one
date: the later recorded wins) and closed accounts (nothing from the close
date on, untouched before it). The stored materialization
(`consume_watermark` in chunks, `materialize_range`) and the live series
readers are checked against the same reference.
"""

import uuid
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session

from purser import clock, ledger, networth
from purser.core import ledger as core
from purser.db import get_sessionmaker
from purser.models import (
    Account,
    ApiToken,
    HoldingsSnapshot,
    NetWorthDaily,
    Posting,
    SecurityPrice,
    Transaction,
    User,
    Valuation,
)
from tests.conftest import reset_db

# ---------------------------------------------------------------------------
# The reference: the per-day engine as it stood before #610.
# ---------------------------------------------------------------------------


def _ref_postings_balance(session: Session, account_id: uuid.UUID, as_of: date) -> Decimal:
    total = session.execute(
        select(func.coalesce(func.sum(Posting.amount), 0))
        .join(Transaction, Transaction.id == Posting.transaction_id)
        .where(
            Posting.account_id == account_id,
            Transaction.status.in_(["pending", "posted"]),
            Transaction.date <= as_of,
        )
    ).scalar_one()
    return Decimal(total)


def _ref_latest_valuation(session: Session, account_id: uuid.UUID, as_of: date) -> Decimal | None:
    row = session.execute(
        select(Valuation.value)
        .where(Valuation.account_id == account_id, Valuation.as_of <= as_of)
        .order_by(*networth.VALUATION_NEWEST_FIRST)
        .limit(1)
    ).scalar_one_or_none()
    return Decimal(row) if row is not None else None


def _ref_net_worth_at(session: Session, as_of: date) -> dict[str, Any]:
    accounts = session.execute(
        select(Account).where(Account.is_virtual.is_(False)).order_by(Account.path)
    ).scalars()
    market_overlay = networth.holdings_addends(session, as_of=as_of)
    breakdown: list[dict[str, Any]] = []
    assets = Decimal(0)
    liabilities = Decimal(0)
    for account in accounts:
        value = Decimal(0)
        method_parts: list[str] = []
        closed_cutoff = account.closed_at is not None and as_of >= clock.ledger_date(
            account.closed_at
        )
        if account.type in networth.VALUATION_TYPES and not closed_cutoff:
            valuation = _ref_latest_valuation(session, account.id, as_of)
            if valuation is not None:
                value = valuation
                method_parts.append("valuation")
            else:
                value = _ref_postings_balance(session, account.id, as_of)
                if value != 0:
                    method_parts.append("postings")
        else:
            value = _ref_postings_balance(session, account.id, as_of)
            if value != 0:
                method_parts.append("postings")
            if account.type == "brokerage" and not closed_cutoff:
                market = market_overlay.get(account.id)
                if market is not None:
                    value += market[0]
                    method_parts.append("holdings")
        if value == 0 and not method_parts:
            continue
        breakdown.append(
            {
                "account_id": account.id,
                "path": account.path,
                "type": account.type,
                "value": value,
                "method": "+".join(method_parts) or "postings",
            }
        )
        if value >= 0:
            assets += value
        else:
            liabilities += -value
    return {
        "as_of": as_of,
        "assets": assets,
        "liabilities": liabilities,
        "net_worth": assets - liabilities,
        "breakdown": breakdown,
    }


def _exact(result: dict[str, Any]) -> Any:
    """A comparable form that also pins each Decimal's representation, so a
    "0" where the engine said "0.0000" is a difference too."""

    def norm(v: Any) -> Any:
        if isinstance(v, Decimal):
            return str(v)
        if isinstance(v, dict):
            return {k: norm(x) for k, x in v.items()}
        if isinstance(v, list):
            return [norm(x) for x in v]
        return v

    return norm(result)


# ---------------------------------------------------------------------------
# Random ledgers
# ---------------------------------------------------------------------------


def _token(session: Session) -> ApiToken:
    suffix = uuid.uuid4().hex[:10]
    user = User(email=f"user_{suffix}@example.com")
    session.add(user)
    session.flush()
    token = ApiToken(
        user_id=user.id, name=f"tok_{suffix}", token_hash=uuid.uuid4().hex, scopes=["write"]
    )
    session.add(token)
    session.flush()
    return token


_amounts = st.decimals(
    min_value=Decimal("-900.00"),
    max_value=Decimal("900.00"),
    allow_nan=False,
    allow_infinity=False,
    places=2,
).filter(lambda d: d != 0)
_values = st.decimals(
    min_value=Decimal("0.00"),
    max_value=Decimal("9000.00"),
    allow_nan=False,
    allow_infinity=False,
    places=2,
)
_closes = st.decimals(
    min_value=Decimal("0.50"),
    max_value=Decimal("400.00"),
    allow_nan=False,
    allow_infinity=False,
    places=2,
)
WINDOW_DAYS = 40
_days_ago = st.integers(min_value=0, max_value=WINDOW_DAYS + 20)  # some before the window
_ACCOUNT_TYPES = ("checking", "credit_card", "brokerage", "loan", "property")


def _closed_at(day: date) -> datetime:
    return datetime.combine(day, time(12), tzinfo=clock.ledger_zone())


def _build_ledger(session: Session, data: st.DataObject, today: date) -> list[Account]:
    token = _token(session)
    types = data.draw(
        st.lists(st.sampled_from(_ACCOUNT_TYPES), min_size=1, max_size=4), label="types"
    )
    accounts = [
        ledger.create_account(
            session,
            name=f"Acct {i}",
            type_=type_,
            path=f"assets.acct{i}_{uuid.uuid4().hex[:6]}",
            actor_token_id=token.id,
        )
        for i, type_ in enumerate(types)
    ]
    category = ledger.ensure_category_path(session, "expenses.misc", actor_token_id=token.id)
    session.commit()

    pending: list[uuid.UUID] = []
    posted: list[uuid.UUID] = []
    for _ in range(data.draw(st.integers(min_value=0, max_value=8), label="n_ops")):
        op = data.draw(st.sampled_from(["flat", "flat", "transfer", "void", "correct"]))
        if op == "flat":
            status = data.draw(st.sampled_from([core.Status.PENDING, core.Status.POSTED]))
            tx = ledger.create_flat_transaction(
                session,
                account_ref=str(data.draw(st.sampled_from(accounts)).id),
                category_ref=str(category.id),
                amount=data.draw(_amounts),
                date=today - timedelta(days=data.draw(_days_ago)),
                payee_name="Prop",
                description="prop row",
                requested_status=status,
                token=token,
            )
            (pending if status == core.Status.PENDING else posted).append(tx.id)
        elif op == "transfer" and len(accounts) > 1:
            source, target = data.draw(st.permutations(accounts))[:2]
            ledger.create_transfer(
                session,
                from_account_ref=str(source.id),
                to_account_ref=str(target.id),
                amount=abs(data.draw(_amounts)),
                date=today - timedelta(days=data.draw(_days_ago)),
                description="prop transfer",
                requested_status=core.Status.POSTED,
                token=token,
            )
        elif op == "void" and pending:
            tx_id = data.draw(st.sampled_from(pending))
            ledger.transition_status(session, tx_id=tx_id, new_status=core.Status.VOID, token=token)
            pending.remove(tx_id)
        elif op == "correct" and posted:
            # A date-edited posted correction: the original stays posted at
            # its old date (decision 37), which net worth's sum over all rows
            # still counts — the reference and the new path must agree.
            tx_id = data.draw(st.sampled_from(posted))
            replacement = ledger.update_transaction(
                session,
                tx_id=tx_id,
                token=token,
                date=today - timedelta(days=data.draw(_days_ago)),
                amount=data.draw(_amounts),
            )
            posted.remove(tx_id)
            posted.append(replacement.id)
        session.commit()

    for account in accounts:
        if account.type == "brokerage":
            for days in data.draw(st.sets(_days_ago, max_size=4), label="snapshot_days"):
                on = today - timedelta(days=days)
                for symbol in data.draw(
                    st.sets(st.sampled_from(["VTI", "BND", "PLANX"]), min_size=1),
                    label="symbols",
                ):
                    kind = data.draw(st.sampled_from(["valued", "zero_feed", "null_feed"]))
                    session.add(
                        HoldingsSnapshot(
                            account_id=account.id,
                            as_of=on,
                            symbol=symbol,
                            quantity=Decimal("3"),
                            # A feed row with no value while it holds shares
                            # is valued on read from a stored close (decision
                            # 92: PLANX has some), or adds nothing unpriced.
                            market_value=(
                                data.draw(_values)
                                if kind == "valued"
                                else Decimal(0)
                                if kind == "zero_feed"
                                else None
                            ),
                        )
                    )
        if account.type == "loan":
            for days in data.draw(st.lists(_days_ago, max_size=4), label="valuation_days"):
                # Two on one date happen: the later recorded wins (#565).
                session.add(
                    Valuation(
                        account_id=account.id,
                        as_of=today - timedelta(days=days),
                        value=-data.draw(_values),
                        source="api",
                    )
                )
                session.flush()
        if data.draw(st.booleans(), label="closed"):
            account.closed_at = _closed_at(
                today - timedelta(days=data.draw(st.integers(0, WINDOW_DAYS + 5)))
            )
    for days in data.draw(st.sets(_days_ago, max_size=3), label="price_days"):
        session.merge(
            SecurityPrice(
                symbol="PLANX",
                price_date=today - timedelta(days=days),
                source="manual",
                close=data.draw(_closes),
            )
        )
    session.commit()
    return accounts


@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(data=st.data())
def test_set_based_net_worth_equals_the_per_day_engine(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch, data: st.DataObject
) -> None:
    reset_db(migrated_engine)
    today = clock.ledger_today()
    with get_sessionmaker()() as session:
        _build_ledger(session, data, today)
        start = today - timedelta(days=WINDOW_DAYS)
        days = [start + timedelta(days=n) for n in range(WINDOW_DAYS + 1)]
        reference = {day: _ref_net_worth_at(session, day) for day in days}

        # 1) Every day of the window in one pass, and any subset of it.
        assert _exact(networth.net_worth_on(session, days)) == _exact(reference)
        subset = data.draw(st.lists(st.sampled_from(days), max_size=6), label="subset")
        assert _exact(networth.net_worth_on(session, subset)) == _exact(
            {day: reference[day] for day in subset}
        )
        probe = data.draw(st.sampled_from(days), label="probe")
        assert _exact(networth.net_worth_at(session, probe)) == _exact(reference[probe])

        # 2) The live readers, with every stored row treated as stale.
        session.execute(text("UPDATE balance_watermark SET networth_dirty_from = :d"), {"d": start})
        session.commit()
        totals = [
            {k: str(reference[day][k]) for k in ("assets", "liabilities", "net_worth")}
            for day in days
        ]
        assert [
            {k: str(row[k]) for k in ("assets", "liabilities", "net_worth")}
            for row in networth.series(session, start=start, end=today)
        ] == totals

    # 3) The stored materialization, consumed in small chunks so a window
    #    spans several of them.
    monkeypatch.setattr(networth, "MATERIALIZE_CHUNK_DAYS", data.draw(st.integers(1, 15)))
    assert networth.consume_watermark() == len(days)
    with get_sessionmaker()() as session:
        stored = {
            row.as_of: {
                "assets": str(row.assets),
                "liabilities": str(row.liabilities),
                "net_worth": str(row.net_worth),
            }
            for row in session.execute(
                select(NetWorthDaily).where(NetWorthDaily.as_of >= start)
            ).scalars()
        }
        expected = {
            day: {
                k: str(Decimal(reference[day][k]).quantize(Decimal("0.0001")))
                for k in ("assets", "liabilities", "net_worth")
            }
            for day in days
        }
        assert stored == expected


def test_rematerializing_years_costs_statements_per_chunk_not_per_day(
    migrated_engine: Engine,
) -> None:
    """The #610 regression guard: re-materializing three years for several
    accounts of every kind is a handful of statements per 92-day chunk. The
    per-day engine issued one postings query per account per day (here about
    1,100 days times 6 accounts) on top of the valuation and holdings reads."""
    from sqlalchemy import event
    from sqlalchemy.engine import Engine as EngineClass

    reset_db(migrated_engine)
    today = clock.ledger_today()
    first = today - timedelta(days=3 * 365)
    with get_sessionmaker()() as session:
        token = _token(session)
        category = ledger.ensure_category_path(session, "expenses.misc", actor_token_id=token.id)
        accounts = [
            ledger.create_account(
                session,
                name=f"Scale {i}",
                type_=type_,
                path=f"assets.scale{i}",
                actor_token_id=token.id,
            )
            for i, type_ in enumerate(
                ["checking", "credit_card", "brokerage", "brokerage", "loan", "property"]
            )
        ]
        session.commit()
        for n, account in enumerate(accounts):
            for months in range(0, 36, 6):
                ledger.create_flat_transaction(
                    session,
                    account_ref=str(account.id),
                    category_ref=str(category.id),
                    amount=Decimal("10.00") + n,
                    date=first + timedelta(days=30 * months + n),
                    payee_name="Scale",
                    description="scale row",
                    requested_status=core.Status.POSTED,
                    token=token,
                )
            if account.type == "brokerage":
                for months in range(0, 36, 3):
                    session.add(
                        HoldingsSnapshot(
                            account_id=account.id,
                            as_of=first + timedelta(days=30 * months),
                            symbol="VTI",
                            quantity=Decimal("2"),
                            market_value=Decimal("500.00") + months,
                        )
                    )
            if account.type == "loan":
                for months in range(0, 36, 1):
                    session.add(
                        Valuation(
                            account_id=account.id,
                            as_of=first + timedelta(days=30 * months),
                            value=Decimal("-90000.00") + months,
                            source="api",
                        )
                    )
        session.execute(text("UPDATE balance_watermark SET networth_dirty_from = :d"), {"d": first})
        session.commit()

    statements = [0]

    def count(*_args: object) -> None:
        statements[0] += 1

    event.listen(EngineClass, "before_cursor_execute", count)
    try:
        days = networth.consume_watermark()
    finally:
        event.remove(EngineClass, "before_cursor_execute", count)
    chunks = -(-days // networth.MATERIALIZE_CHUNK_DAYS)
    assert days == (today - first).days + 1
    # Per chunk: the accounts, the posting sums, the valuations, the snapshot
    # sets and one upsert; plus the claim.
    assert statements[0] <= 6 * chunks + 4, statements[0]

    with get_sessionmaker()() as session:
        stored = {
            row.as_of: row.net_worth
            for row in session.execute(
                select(NetWorthDaily).where(NetWorthDaily.as_of >= first)
            ).scalars()
        }
        assert len(stored) == days
        for day in (first, first + timedelta(days=400), today):
            assert stored[day] == _ref_net_worth_at(session, day)["net_worth"]
