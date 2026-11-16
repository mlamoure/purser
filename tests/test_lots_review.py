"""Independent review regressions for #393; synthetic data only."""

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

import pytest

from purser.core import lots as core_lots
from purser.holdings import awards, captures, service
from purser.ledger import LedgerError
from tests.test_lots_service import (
    AS_OF,
    NOW,
    TODAY,
    _apply,
    _broker,
    _capture,
    _lot,
    _state,
    _symbol,
)


def _grant(session, account, symbol, *, vested="0", schedule=()):
    capture = awards.AwardCaptureIn(
        account=str(account.id),
        as_of=AS_OF,
        captured_at=NOW - timedelta(hours=2),
        completeness="complete",
        awards=(
            awards.AwardIn(
                grant_ref="REVIEW-G",
                symbol=symbol,
                granted_on=date(2024, 1, 1),
                granted_quantity=Decimal("100"),
                vested_quantity=Decimal(vested),
                schedule=schedule,
            ),
        ),
    )
    p = awards.preview_awards(session, capture)
    awards.commit_awards(session, capture, expect_hash=p.plan_hash, token=None)
    return capture


def test_review_proceeds_never_negative():
    pieces = core_lots.split_amount(Decimal("0.01"), [Decimal(1)] * 150)
    assert sum(pieces) == Decimal("0.01")
    assert all(p >= 0 for p in pieces)


def test_review_fill_preserves_reported_total(db_session):
    account, symbol = _broker(db_session), _symbol()
    first = _capture(
        account, symbol, [_lot("R", "300", AS_OF, None)], captured_at=NOW - timedelta(hours=2)
    )
    _apply(db_session, first)
    second = _capture(
        account,
        symbol,
        [_lot("R", "300", AS_OF, None, cost_basis=Decimal("1"))],
        captured_at=NOW - timedelta(hours=1),
    )
    _apply(db_session, second)
    assert _state(db_session, account, symbol).cost_basis == Decimal("1.0000")


def test_review_same_day_older_capture_cannot_replace_basis(db_session):
    account, symbol = _broker(db_session), _symbol()
    _apply(
        db_session,
        _capture(
            account, symbol, [_lot("R", "3", AS_OF, "20")], captured_at=NOW - timedelta(hours=1)
        ),
    )
    old = _capture(
        account,
        symbol,
        [_lot("R", "3", AS_OF, "10")],
        captured_at=NOW - timedelta(hours=2),
        basis_policy="replace",
    )
    with pytest.raises(LedgerError):
        _apply(db_session, old)


def test_review_zero_remaining_row_accepts_zero_total_basis(db_session):
    account, symbol = _broker(db_session), _symbol()
    c = _capture(account, symbol, [_lot("R", "0", AS_OF, None, cost_basis=Decimal("0"))])
    p = captures.preview(db_session, c)
    assert not p.conflicts


def test_review_reversal_frees_external_reference(db_session):
    account, symbol = _broker(db_session), _symbol()
    recorded = service.record_position(
        db_session,
        account_ref=str(account.id),
        symbol=symbol,
        quantity=Decimal(0),
        as_of=AS_OF,
        token=None,
    )
    kwargs = dict(
        position_id=recorded.position.id,
        kind="buy",
        occurred_on=AS_OF + timedelta(days=1),
        quantity=Decimal(5),
        price=Decimal(10),
        token=None,
        lot=service.LotSpec(external_ref="CORRECTABLE"),
    )
    event = service.record_event(db_session, **kwargs)
    service.reverse_event(db_session, event_id=event.id, token=None)
    corrected = service.record_event(db_session, **{**kwargs, "quantity": Decimal(4)})
    assert corrected.id != event.id


def test_review_scheduled_vest_cannot_exceed_grant(db_session):
    account, symbol = _broker(db_session), _symbol()
    day = AS_OF + timedelta(days=1)
    _grant(db_session, account, symbol, schedule=(awards.VestIn(day, Decimal(10)),))
    with pytest.raises(LedgerError):
        awards.record_vest(
            db_session,
            award="REVIEW-G",
            account=str(account.id),
            vest_date=day,
            gross_quantity=Decimal(101),
            fmv=Decimal(10),
            token=None,
        )


def test_review_missing_schedule_does_not_mean_no_unvested(db_session):
    account, symbol = _broker(db_session), _symbol()
    _grant(db_session, account, symbol, vested="20")
    views = awards.award_views(db_session, account)
    assert len(views) == 1
    assert views[0].unvested == Decimal(80)


def test_review_omitted_reported_vested_preserves_baseline(db_session):
    account, symbol = _broker(db_session), _symbol()
    old = _grant(db_session, account, symbol, vested="80")
    row = replace(
        old.awards[0],
        vested_quantity=None,
        schedule=(awards.VestIn(TODAY + timedelta(days=90), Decimal(50)),),
    )
    newer = replace(old, captured_at=NOW, awards=(row,))
    p = awards.preview_awards(db_session, newer)
    assert p.plan.conflicts, "80 vested + 50 scheduled cannot fit a grant of 100"


def test_review_full_withholding_can_record_a_vest(db_session):
    account, symbol = _broker(db_session), _symbol()
    day = AS_OF + timedelta(days=1)
    _grant(db_session, account, symbol, schedule=(awards.VestIn(day, Decimal(10)),))
    out = awards.record_vest(
        db_session,
        award="REVIEW-G",
        account=str(account.id),
        vest_date=day,
        gross_quantity=Decimal(10),
        withheld_quantity=Decimal(10),
        fmv=Decimal(10),
        token=None,
    )
    assert out.net == 0


@pytest.mark.parametrize(
    "quantity, per_share, total",
    [
        (Decimal("10000000000000"), Decimal("10000000000000"), None),
        (Decimal("0.000001"), None, Decimal("1000000000000000")),
    ],
)
def test_review_derived_basis_fits_database(quantity, per_share, total):
    with pytest.raises(ValueError):
        core_lots.normalize_basis(quantity, per_share, total)


def test_review_new_vest_respects_historical_reported_vested(db_session):
    account, symbol = _broker(db_session), _symbol()
    _grant(db_session, account, symbol, vested="80")
    with pytest.raises(LedgerError):
        awards.record_vest(
            db_session,
            award="REVIEW-G",
            account=str(account.id),
            vest_date=AS_OF + timedelta(days=1),
            gross_quantity=Decimal(21),
            fmv=Decimal(10),
            token=None,
        )


# --- Further cases around the review findings (#393) ---------------------


def test_split_amount_signs_and_sums_property():
    from hypothesis import given, settings
    from hypothesis import strategies as st

    @settings(max_examples=200, deadline=None)
    @given(
        total=st.decimals(min_value=Decimal("-1000"), max_value=Decimal("1000"), places=4),
        parts=st.lists(
            st.decimals(min_value=Decimal("0.000001"), max_value=Decimal("1000"), places=6),
            min_size=1,
            max_size=200,
        ),
    )
    def check(total, parts):
        out = core_lots.split_amount(total, parts)
        assert sum(out) == total
        assert all((p >= 0) if total >= 0 else (p <= 0) for p in out)

    check()


def test_replace_keeps_reported_total(db_session):
    account, symbol = _broker(db_session), _symbol()
    _apply(
        db_session,
        _capture(
            account, symbol, [_lot("R", "300", AS_OF, "1")], captured_at=NOW - timedelta(hours=2)
        ),
    )
    _apply(
        db_session,
        _capture(
            account,
            symbol,
            [_lot("R", "300", AS_OF, None, cost_basis=Decimal("1"))],
            captured_at=NOW - timedelta(hours=1),
            basis_policy="replace",
        ),
    )
    assert _state(db_session, account, symbol).cost_basis == Decimal("1.0000")


def test_same_day_newer_capture_still_applies(db_session):
    account, symbol = _broker(db_session), _symbol()
    _apply(
        db_session,
        _capture(
            account, symbol, [_lot("R", "3", AS_OF, "10")], captured_at=NOW - timedelta(hours=2)
        ),
    )
    _apply(
        db_session,
        _capture(
            account,
            symbol,
            [_lot("R", "3", AS_OF, "20")],
            captured_at=NOW - timedelta(hours=1),
            basis_policy="replace",
        ),
    )
    assert _state(db_session, account, symbol).cost_basis == Decimal("60.0000")
    # The exact first request again is still a retry, not a stale capture.
    first = _capture(
        account, symbol, [_lot("R", "3", AS_OF, "10")], captured_at=NOW - timedelta(hours=2)
    )
    assert captures.commit(db_session, first, expect_hash="x", token=None).retry


def test_same_day_older_award_capture_is_stale(db_session):
    account, symbol = _broker(db_session), _symbol()
    newer = _grant(db_session, account, symbol, vested="10")
    older = replace(
        newer,
        captured_at=NOW - timedelta(hours=5),
        awards=(replace(newer.awards[0], vested_quantity=Decimal("5")),),
    )
    p = awards.preview_awards(db_session, older)
    assert p.stale and {c.code for c in p.plan.conflicts} == {"stale_capture"}


def test_vest_reversal_frees_lot_reference(db_session):
    account, symbol = _broker(db_session), _symbol()
    day = AS_OF + timedelta(days=1)
    _grant(db_session, account, symbol, schedule=(awards.VestIn(day, Decimal(10)),))
    kw = dict(
        award="REVIEW-G",
        account=str(account.id),
        vest_date=day,
        gross_quantity=Decimal(10),
        withheld_quantity=Decimal(4),
        fmv=Decimal(10),
        lot_ref="VEST-LOT",
        token=None,
    )
    first = awards.record_vest(db_session, **kw)
    service.reverse_event(db_session, event_id=first.event_id, token=None)
    again = awards.record_vest(db_session, **{**kw, "withheld_quantity": Decimal(3)})
    assert again.event_id != first.event_id and again.net == 7
    assert _state(db_session, account, symbol).quantity == 7


def test_full_withholding_lifecycle(db_session):
    account, symbol = _broker(db_session), _symbol()
    day = AS_OF + timedelta(days=1)
    _grant(db_session, account, symbol, schedule=(awards.VestIn(day, Decimal(10)),))
    kw = dict(
        award="REVIEW-G",
        account=str(account.id),
        vest_date=day,
        gross_quantity=Decimal(10),
        withheld_quantity=Decimal(10),
        fmv=Decimal(10),
        token=None,
    )
    out = awards.record_vest(db_session, **kw)
    assert out.lot_id is None and out.event_id is None
    assert service.open_position(db_session, account.id, symbol) is None  # no shares
    assert awards.record_vest(db_session, **kw).already_recorded
    with pytest.raises(awards.VestConflict):
        awards.record_vest(db_session, **{**kw, "withheld_quantity": Decimal(9)})
    with pytest.raises(LedgerError):
        awards.record_vest(db_session, **{**kw, "lot": "anything"})
    # Undone like a linked vest: back to scheduled.
    awards.update_vest(db_session, vest_id=out.vest.id, status="scheduled", note=None, token=None)
    assert awards.award_views(db_session, account)[0].unvested == 100  # baseline 0, nothing vested


def test_every_mutator_respects_the_grant(db_session):
    account, symbol = _broker(db_session), _symbol()
    day = AS_OF + timedelta(days=1)
    _grant(db_session, account, symbol, vested="60", schedule=(awards.VestIn(day, Decimal(40)),))
    [view] = awards.award_views(db_session, account)
    vest = view.vests[0]
    # Shrinking the grant below what is vested and scheduled is refused.
    with pytest.raises(LedgerError):
        awards.update_award(
            db_session, award_id=view.award.id, granted_quantity=Decimal(90), token=None
        )
    with pytest.raises(LedgerError):
        awards.update_award(
            db_session, award_id=view.award.id, cancelled_quantity=Decimal(1), token=None
        )
    # Forfeit, then cancel the units: reinstating the vest no longer fits.
    awards.update_vest(db_session, vest_id=vest.id, status="forfeited", note=None, token=None)
    awards.update_award(
        db_session, award_id=view.award.id, cancelled_quantity=Decimal(40), token=None
    )
    with pytest.raises(LedgerError):
        awards.update_vest(db_session, vest_id=vest.id, status="scheduled", note=None, token=None)


def test_unvested_reports_unscheduled_and_unknown(db_session):
    account, symbol = _broker(db_session), _symbol()
    _grant(
        db_session,
        account,
        symbol,
        vested="20",
        schedule=(awards.VestIn(TODAY + timedelta(days=30), Decimal(30)),),
    )
    [v] = awards.award_views(db_session, account)
    assert (v.unvested, v.scheduled, v.unscheduled, v.unvested_known) == (80, 30, 50, True)
    [u] = awards.unvested(db_session, account)
    assert u.units == 80 and u.unscheduled == 50 and u.complete


def test_unknown_baseline_is_not_reported_as_known(db_session):
    account, symbol = _broker(db_session), _symbol()
    capture = awards.AwardCaptureIn(
        account=str(account.id),
        as_of=AS_OF,
        captured_at=NOW,
        completeness="partial",
        awards=(
            awards.AwardIn(
                grant_ref="NO-BASELINE",
                symbol=symbol,
                granted_on=date(2024, 1, 1),
                granted_quantity=Decimal("100"),
                schedule=(awards.VestIn(TODAY + timedelta(days=30), Decimal(25)),),
            ),
        ),
    )
    p = awards.preview_awards(db_session, capture)
    awards.commit_awards(db_session, capture, expect_hash=p.plan_hash, token=None)
    [v] = awards.award_views(db_session, account)
    assert v.unvested_known is False and v.unvested == 25 and v.unscheduled is None
    assert awards.unvested(db_session, account)[0].complete is False


def test_split_amount_zero_and_small():
    assert core_lots.split_amount(Decimal("0"), [Decimal(1)] * 3) == [Decimal(0)] * 3
    out = core_lots.split_amount(Decimal("-0.0003"), [Decimal(1)] * 5)
    assert sum(out) == Decimal("-0.0003") and all(p <= 0 for p in out)
