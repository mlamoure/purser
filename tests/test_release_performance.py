"""Opt-in household-scale lot projection measurement for issue #404.

RUN_RELEASE_BENCHMARK=1 uv run pytest tests/test_release_performance.py -s
Timings are evidence, not a flaky wall-clock pass/fail threshold.
"""

import json
import os
import statistics
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from purser import ledger
from purser.holdings import captures, projection
from purser.models import HoldingsSnapshot, SecurityPrice


@pytest.mark.skipif(not os.environ.get("RUN_RELEASE_BENCHMARK"), reason="opt-in measurement")
def test_lots_with_five_years_of_prices(clean_db: None, db_session: Session) -> None:
    today = datetime.now(UTC).date()
    start = today - timedelta(days=365 * 5)
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
        captured_at=datetime.now(UTC),
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
    for _ in range(5):
        began = time.perf_counter()
        projection.project_account(db_session, account, today=today)
        timings.append((time.perf_counter() - began) * 1000)
    rows = db_session.scalar(
        select(func.count())
        .select_from(HoldingsSnapshot)
        .where(
            HoldingsSnapshot.account_id == account.id,
        )
    )
    assert rows == 20 * (365 * 5 + 1)
    print(
        json.dumps(
            {
                "positions": 20,
                "lots": 100,
                "daily_prices": rows,
                "initial_capture_ms": round(import_ms, 1),
                "reprojection_median_ms": round(statistics.median(timings), 1),
                "samples_ms": [round(t, 1) for t in timings],
            }
        )
    )
