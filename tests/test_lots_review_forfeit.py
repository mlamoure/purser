"""Forfeiture must reduce entitlement, not become unscheduled."""

from datetime import timedelta
from decimal import Decimal

import pytest

from purser.holdings import awards
from purser.ledger import LedgerError
from tests.test_lots_review import _grant
from tests.test_lots_service import AS_OF, _broker, _symbol


def test_review_forfeit_is_not_unscheduled_entitlement(db_session):
    account, symbol = _broker(db_session), _symbol()
    _grant(
        db_session,
        account,
        symbol,
        vested="0",
        schedule=(
            awards.VestIn(AS_OF + timedelta(days=100), Decimal(10)),
            awards.VestIn(AS_OF + timedelta(days=200), Decimal(90)),
        ),
    )
    [view] = awards.award_views(db_session, account)
    vest = next(v for v in view.vests if v.quantity == 10)
    awards.update_vest(
        db_session, vest_id=vest.id, status="forfeited", note="synthetic forfeiture", token=None
    )
    [view] = awards.award_views(db_session, account)
    assert view.unvested == Decimal(90)
    awards.update_vest(
        db_session, vest_id=vest.id, status="scheduled", note="synthetic reinstatement", token=None
    )
    [view] = awards.award_views(db_session, account)
    assert view.unvested == Decimal(100)


def test_forfeit_and_broker_cancelled_count_once(db_session):
    """A forfeited vest and the broker's cancelled count describe the same
    units: the larger counts, never both."""
    account, symbol = _broker(db_session), _symbol()
    _grant(
        db_session,
        account,
        symbol,
        vested="0",
        schedule=(
            awards.VestIn(AS_OF + timedelta(days=100), Decimal(10)),
            awards.VestIn(AS_OF + timedelta(days=200), Decimal(90)),
        ),
    )
    [view] = awards.award_views(db_session, account)
    vest = next(v for v in view.vests if v.quantity == 10)
    awards.update_vest(db_session, vest_id=vest.id, status="forfeited", note=None, token=None)
    # The broker then reports the same 10 as cancelled: still 90 unvested.
    awards.update_award(
        db_session, award_id=view.award.id, cancelled_quantity=Decimal(10), token=None
    )
    [view] = awards.award_views(db_session, account)
    assert view.unvested == Decimal(90) and view.unscheduled == 0
    # A further cancellation beyond the forfeited vest reduces it more.
    with pytest.raises(LedgerError):  # 90 still scheduled: 20 cancelled cannot fit
        awards.update_award(
            db_session, award_id=view.award.id, cancelled_quantity=Decimal(20), token=None
        )


def test_entitlement_forfeiture_is_pure():
    from datetime import date

    from purser.core import awards as core_awards

    day = date(2027, 1, 1)
    base = dict(granted=Decimal(100), reported_vested=Decimal(0), reported_as_of=date(2026, 1, 1))
    forfeited = [
        core_awards.VestFact(day, "forfeited", Decimal(10)),
        core_awards.VestFact(day, "scheduled", Decimal(90)),
    ]
    e = core_awards.entitlement(cancelled=Decimal(0), vests=forfeited, **base)
    assert (e.remaining, e.unscheduled, e.cancelled, e.forfeited) == (90, 0, 10, 10)
    e = core_awards.entitlement(cancelled=Decimal(10), vests=forfeited, **base)
    assert (e.remaining, e.cancelled) == (90, 10)
    e = core_awards.entitlement(cancelled=Decimal(5), vests=forfeited, **base)
    assert (e.remaining, e.cancelled) == (90, 10)
