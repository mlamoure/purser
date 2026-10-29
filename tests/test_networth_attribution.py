"""Net-worth movement attribution (issue #75): bucket deltas partition the
period change exactly — pinned example + property test."""

from datetime import date, timedelta
from decimal import Decimal
from uuid import uuid4

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy.orm import Session

from purser import ledger as ledger_service
from purser import networth
from purser.core import ledger as core
from purser.models import HoldingsSnapshot, Valuation

START = date(2026, 4, 1)
END = date(2026, 6, 30)

_names = st.uuids().map(lambda u: f"acct_{u.hex[:10]}")
_money = st.decimals(
    min_value=Decimal("-50000.00"),
    max_value=Decimal("50000.00"),
    allow_nan=False,
    allow_infinity=False,
    places=2,
)
_account_specs = st.lists(
    st.fixed_dictionaries(
        {
            "type": st.sampled_from(
                ["checking", "savings", "credit_card", "brokerage", "property", "loan"]
            ),
            "start_value": _money,
            "end_delta": _money,
        }
    ),
    min_size=1,
    max_size=5,
)


def _post(session: Session, account, amount: Decimal, on: date) -> None:
    """Balance an account posting against an expense category (zero-sum)."""
    category = ledger_service.resolve_account(session, "expenses.attribution_test")
    ledger_service.create_flat_transaction(
        session,
        account_ref=str(account.id),
        category_ref=str(category.id),
        amount=amount,
        date=on,
        description="attribution fixture",
        requested_status=core.Status("posted"),
        token=None,
    )


def _category(session: Session) -> None:
    try:
        ledger_service.resolve_account(session, "expenses.attribution_test")
    except Exception:
        ledger_service.create_account(
            session,
            name="Attribution Test",
            type_="category_expense",
            path="expenses.attribution_test",
            actor_token_id=None,
        )


def _nw(session: Session, on: date) -> Decimal:
    return Decimal(networth.net_worth_at(session, on)["net_worth"])


class TestAttributionExample:
    def test_buckets_sum_exactly_and_pin_known_movers(
        self, clean_db: None, db_session: Session
    ) -> None:
        _category(db_session)
        checking = ledger_service.create_account(
            db_session,
            name="Checking",
            type_="checking",
            path="assets.checking",
            actor_token_id=None,
        )
        brokerage = ledger_service.create_account(
            db_session,
            name="Brokerage",
            type_="brokerage",
            path="assets.brokerage",
            actor_token_id=None,
        )
        mortgage = ledger_service.create_account(
            db_session,
            name="Mortgage",
            type_="loan",
            path="liabilities.mortgage",
            actor_token_id=None,
        )

        # cash: +1100 between the dates
        _post(db_session, checking, Decimal("2000.00"), START - timedelta(days=5))
        _post(db_session, checking, Decimal("1100.00"), START + timedelta(days=10))
        # market: holdings 20000 -> 25000
        db_session.add_all(
            [
                HoldingsSnapshot(
                    account_id=brokerage.id,
                    as_of=START,
                    symbol="VTI",
                    quantity=Decimal("10"),
                    market_value=Decimal("20000.00"),
                ),
                HoldingsSnapshot(
                    account_id=brokerage.id,
                    as_of=END - timedelta(days=1),
                    symbol="VTI",
                    quantity=Decimal("10"),
                    market_value=Decimal("25000.00"),
                ),
            ]
        )
        # loan principal: -291000 -> -288900 (paying principal shrinks the liability)
        db_session.add_all(
            [
                Valuation(
                    account_id=mortgage.id,
                    as_of=START,
                    value=Decimal("-291000.00"),
                    source="manual",
                ),
                Valuation(
                    account_id=mortgage.id,
                    as_of=END - timedelta(days=1),
                    value=Decimal("-288900.00"),
                    source="manual",
                ),
            ]
        )
        db_session.flush()

        buckets = networth.attribution(db_session, start=START, end=END)
        by_name = {b["bucket"]: b["delta"] for b in buckets}
        assert by_name["cash"] == Decimal("1100.00")
        assert by_name["market"] == Decimal("5000.00")
        assert by_name["loan_principal"] == Decimal("2100.00")
        total = sum((b["delta"] for b in buckets), Decimal(0))
        assert total == _nw(db_session, END) - _nw(db_session, START)


@settings(
    max_examples=20, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(specs=_account_specs)
def test_attribution_sums_to_period_delta(
    clean_db: None, db_session: Session, specs: list[dict]
) -> None:
    """Property: whatever mix of account classes, valuations, holdings, and
    postings exists, sum(bucket deltas) == net_worth(end) - net_worth(start),
    exactly (Decimal equality). Holds over the whole ledger, so example
    accumulation across hypothesis runs cannot break it."""
    _category(db_session)
    for i, spec in enumerate(specs):
        account = ledger_service.create_account(
            db_session,
            name=f"A{i}",
            type_=spec["type"],
            path=f"assets.h{uuid4().hex[:12]}_{i}",  # unique across accumulated examples
            actor_token_id=None,
        )
        if spec["type"] in ("property", "loan"):
            db_session.add(
                Valuation(
                    account_id=account.id, as_of=START, value=spec["start_value"], source="manual"
                )
            )
            db_session.add(
                Valuation(
                    account_id=account.id,
                    as_of=END - timedelta(days=1),
                    value=spec["start_value"] + spec["end_delta"],
                    source="manual",
                )
            )
        elif spec["type"] == "brokerage":
            db_session.add(
                HoldingsSnapshot(
                    account_id=account.id, as_of=START, symbol="X", market_value=spec["start_value"]
                )
            )
            db_session.add(
                HoldingsSnapshot(
                    account_id=account.id,
                    as_of=END - timedelta(days=1),
                    symbol="X",
                    market_value=spec["start_value"] + spec["end_delta"],
                )
            )
        else:
            if spec["start_value"] != 0:
                _post(db_session, account, spec["start_value"], START - timedelta(days=3))
            if spec["end_delta"] != 0:
                _post(db_session, account, spec["end_delta"], START + timedelta(days=7))
    db_session.flush()

    buckets = networth.attribution(db_session, start=START, end=END)
    total = sum((b["delta"] for b in buckets), Decimal(0))
    assert total == _nw(db_session, END) - _nw(db_session, START)
