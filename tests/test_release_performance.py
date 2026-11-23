"""Opt-in household-scale lot projection measurement for issue #404.

RUN_RELEASE_BENCHMARK=1 uv run pytest tests/test_release_performance.py -s
Timings are evidence, not a flaky wall-clock pass/fail threshold.

The fixture is synthetic: 20 positions of 5 lots each with five years of
daily closes (36,520 manual rows). Besides the bulk capture and a full
reprojection it times the paths #404 names: a write dated near today and
one a year back (each projects from its date), a recorded close, one
position's state against every position's, and the Settings, Prices status.
Each write runs in a savepoint that is rolled back, so every sample sees the
same facts.
"""

import json
import os
import statistics
import time
from collections.abc import Callable
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from purser import ledger
from purser.api import positions as positions_api
from purser.holdings import captures, projection, service
from purser.holdings import prices as price_service
from purser.models import HoldingsSnapshot, Position, SecurityPrice
from tests.conftest import now, today

SAMPLES = 5


def _median_ms(session: Session, fn: Callable[[], object], *, rollback: bool) -> float:
    timings = []
    for _ in range(SAMPLES):
        savepoint = session.begin_nested() if rollback else None
        began = time.perf_counter()
        fn()
        timings.append((time.perf_counter() - began) * 1000)
        if savepoint is not None:
            savepoint.rollback()
    return round(statistics.median(timings), 1)


@pytest.mark.skipif(not os.environ.get("RUN_RELEASE_BENCHMARK"), reason="opt-in measurement")
def test_lots_with_five_years_of_prices(clean_db: None, db_session: Session) -> None:
    start = today() - timedelta(days=365 * 5)
    account = ledger.create_account(
        db_session,
        name="Synthetic benchmark",
        type_="brokerage",
        path="assets.release_benchmark",
        actor_token_id=None,
    )
    ledger.update_account(
        db_session,
        account_id=account.id,
        holdings_source="manual",
        actor_token_id=None,
    )
    symbols = [f"SYN{i}" for i in range(20)]
    db_session.add_all(
        [
            SecurityPrice(
                symbol=symbol,
                price_date=start + timedelta(days=day),
                source="manual",
                close=Decimal("100.00"),
            )
            for symbol in symbols
            for day in range(365 * 5 + 1)
        ]
    )
    db_session.flush()
    capture = captures.LotCaptureIn(
        account=str(account.id),
        as_of=start,
        captured_at=now(),
        completeness="complete",
        positions=tuple(
            captures.SymbolIn(
                symbol=symbol,
                lots=tuple(
                    captures.LotIn(
                        acquired_on=start,
                        quantity=Decimal("2"),
                        external_ref=f"{symbol}-{i}",
                        basis_per_share=Decimal("50"),
                    )
                    for i in range(5)
                ),
            )
            for symbol in symbols
        ),
    )
    plan = captures.preview(db_session, capture)
    began = time.perf_counter()
    captures.commit(db_session, capture, expect_hash=plan.plan_hash, token=None)
    import_ms = (time.perf_counter() - began) * 1000
    timings = []
    for _ in range(SAMPLES):
        began = time.perf_counter()
        projection.project_account(db_session, account, today=today())
        timings.append((time.perf_counter() - began) * 1000)
    rows = db_session.scalar(
        select(func.count())
        .select_from(HoldingsSnapshot)
        .where(
            HoldingsSnapshot.account_id == account.id,
        )
    )
    assert rows == 20 * (365 * 5 + 1)
    position = db_session.execute(
        select(Position).where(Position.account_id == account.id, Position.symbol == "SYN0")
    ).scalar_one()

    def buy(days_ago: int) -> Callable[[], object]:
        return lambda: service.record_event(
            db_session,
            position_id=position.id,
            kind="buy",
            occurred_on=today() - timedelta(days=days_ago),
            quantity=Decimal("1"),
            price=Decimal("100"),
            token=None,
            today=today(),
        )

    write_recent = _median_ms(db_session, buy(2), rollback=True)
    write_year_back = _median_ms(db_session, buy(365), rollback=True)
    record_price = _median_ms(
        db_session,
        lambda: price_service.record_price(
            db_session,
            symbol="SYN0",
            price_date=today() - timedelta(days=2),
            close=Decimal("101"),
            token=None,
            today=today(),
        ),
        rollback=True,
    )
    single_state = _median_ms(
        db_session, lambda: positions_api._state(db_session, position), rollback=False
    )
    all_states = _median_ms(
        db_session,
        lambda: service.position_states(db_session, account, stale_days=7, today=today()),
        rollback=False,
    )
    prices_status = _median_ms(
        db_session, lambda: price_service.status(db_session, today=today()), rollback=False
    )
    print(
        json.dumps(
            {
                "positions": 20,
                "lots": 100,
                "daily_prices": rows,
                "initial_capture_ms": round(import_ms, 1),
                "reprojection_median_ms": round(statistics.median(timings), 1),
                "samples_ms": [round(t, 1) for t in timings],
                "write_2_days_back_ms": write_recent,
                "write_1_year_back_ms": write_year_back,
                "record_price_ms": record_price,
                "single_position_state_ms": single_state,
                "all_position_states_ms": all_states,
                "prices_status_ms": prices_status,
            }
        )
    )
