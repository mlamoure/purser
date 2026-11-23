"""The per-change balance series and the reported-basis extremes (#599,
decision 129).

`GET /accounts/{id}/balance/series` and `purser://balance-history/…` are the
POSTINGS history for every account type: `opening` is the stored running
balance the day before the window, so `opening` plus every `delta` equals the
last point — for a brokerage account (whose points are its cash) and a loan
(whose points are its postings) as much as for checking. `method` is always
`postings`.

MCP `balance_history` reports the account's REPORTED balance (holdings added
for brokerage, valuations for loans), and its `low`/`high` rank that same
basis over every day with activity in the window
(`networth.account_window_extremes`), never the postings store's cash.
"""

import uuid
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from purser import clock, ledger, networth
from purser.core import ledger as core
from purser.db import get_sessionmaker
from purser.models import (
    Account,
    AccountRunningBalance,
    ApiToken,
    HoldingsSnapshot,
    User,
    Valuation,
)
from tests.conftest import reset_db, today


@pytest.fixture
def seeded(admin_client: TestClient) -> dict[str, str]:
    ids: dict[str, str] = {}
    for name, type_, path in [
        ("Checking", "checking", "assets.checking"),
        ("Mortgage", "loan", "liabilities.mortgage"),
        ("Brokerage", "brokerage", "assets.brokerage"),
    ]:
        resp = admin_client.post("/accounts", json={"name": name, "type": type_, "path": path})
        assert resp.status_code == 201, resp.text
        ids[path] = resp.json()["id"]
    resp = admin_client.post(
        "/categories",
        json={"name": "Dining", "type": "category_expense", "path": "expenses.dining"},
    )
    assert resp.status_code == 201, resp.text
    return ids


def _post_row(client: TestClient, account: str, amount: str, on: date) -> None:
    resp = client.post(
        "/transactions",
        json={
            "account": account,
            "category": "expenses.dining",
            "amount": amount,
            "date": on.isoformat(),
            "payee": "Example Payee",
            "status": "posted",
        },
    )
    assert resp.status_code == 201, resp.text


def _snapshot(session: Session, account_id: str, on: date, market_value: str) -> None:
    session.add(
        HoldingsSnapshot(
            account_id=uuid.UUID(account_id),
            as_of=on,
            symbol="VTI",
            quantity=Decimal("1"),
            cost_basis=Decimal(market_value),
            market_value=Decimal(market_value),
        )
    )
    session.commit()


def _series(client: TestClient, account_id: str, start: date, end: date) -> dict:
    resp = client.get(
        f"/accounts/{account_id}/balance/series",
        params={"start": start.isoformat(), "end": end.isoformat()},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _assert_postings_contract(body: dict) -> None:
    """The endpoint's documented contract: opening + every delta == the last
    point, and the method says what the numbers are."""
    assert body["method"] == "postings"
    assert body["points"], "the fixture puts activity inside the window"
    total = Decimal(body["opening"]) + sum(Decimal(p["delta"]) for p in body["points"])
    assert total == Decimal(body["points"][-1]["balance"])


def test_brokerage_opening_is_the_cash_its_points_continue(
    seeded: dict[str, str], client: TestClient, db_session: Session
) -> None:
    """The #599 repro: holdings before the window used to land in `opening`
    while every point was cash only, so opening + deltas != the last point."""
    brokerage = seeded["assets.brokerage"]
    _post_row(client, brokerage, "500.00", today() - timedelta(days=40))
    _snapshot(db_session, brokerage, today() - timedelta(days=35), "2000.00")
    _post_row(client, brokerage, "-120.00", today() - timedelta(days=10))
    _post_row(client, brokerage, "30.00", today() - timedelta(days=4))

    body = _series(client, brokerage, today() - timedelta(days=20), today())
    _assert_postings_contract(body)
    assert Decimal(body["opening"]) == Decimal("500.00")  # cash, no 2000.00 of holdings
    assert [Decimal(p["balance"]) for p in body["points"]] == [
        Decimal("380.00"),
        Decimal("410.00"),
    ]
    # The reported balance is still cash plus holdings, on the surfaces that
    # report it.
    assert Decimal(client.get(f"/accounts/{brokerage}").json()["balance"]) == Decimal("2410.00")


def test_loan_opening_is_its_postings_not_its_valuation(
    seeded: dict[str, str], client: TestClient
) -> None:
    mortgage = seeded["liabilities.mortgage"]
    resp = client.post(
        f"/accounts/{mortgage}/valuations",
        json={"as_of": (today() - timedelta(days=50)).isoformat(), "value": "-300000.00"},
    )
    assert resp.status_code == 201, resp.text
    _post_row(client, mortgage, "-1000.00", today() - timedelta(days=45))
    _post_row(client, mortgage, "1500.00", today() - timedelta(days=15))
    _post_row(client, mortgage, "1500.00", today() - timedelta(days=5))

    body = _series(client, mortgage, today() - timedelta(days=30), today())
    _assert_postings_contract(body)
    assert Decimal(body["opening"]) == Decimal("-1000.00")
    assert Decimal(body["points"][-1]["balance"]) == Decimal("2000.00")


def test_postings_account_is_unchanged(seeded: dict[str, str], client: TestClient) -> None:
    checking = seeded["assets.checking"]
    _post_row(client, checking, "250.00", today() - timedelta(days=60))
    _post_row(client, checking, "-75.00", today() - timedelta(days=12))
    _post_row(client, checking, "-25.00", today() - timedelta(days=12))

    body = _series(client, checking, today() - timedelta(days=12), today())
    _assert_postings_contract(body)
    assert Decimal(body["opening"]) == Decimal("250.00")
    assert Decimal(body["points"][-1]["balance"]) == Decimal("150.00")


def test_a_window_with_no_activity_carries_the_opening_alone(
    seeded: dict[str, str], client: TestClient, db_session: Session
) -> None:
    brokerage = seeded["assets.brokerage"]
    _post_row(client, brokerage, "700.00", today() - timedelta(days=40))
    _snapshot(db_session, brokerage, today() - timedelta(days=5), "9000.00")

    body = _series(client, brokerage, today() - timedelta(days=10), today())
    assert body["method"] == "postings"
    assert Decimal(body["opening"]) == Decimal("700.00")
    assert body["points"] == []


# ---------------------------------------------------------------------------
# networth.account_window_extremes: the reported basis, every active day
# ---------------------------------------------------------------------------


def test_brokerage_extremes_rank_the_reported_balance(
    seeded: dict[str, str], client: TestClient, db_session: Session
) -> None:
    """A snapshot-only day is the low and another the high; the postings
    store alone would report the cash's own low and high instead."""
    brokerage = seeded["assets.brokerage"]
    _post_row(client, brokerage, "1000.00", today() - timedelta(days=60))
    _snapshot(db_session, brokerage, today() - timedelta(days=60), "5000.00")
    _snapshot(db_session, brokerage, today() - timedelta(days=31), "300.00")
    _snapshot(db_session, brokerage, today() - timedelta(days=29), "9000.00")
    _post_row(client, brokerage, "-400.00", today() - timedelta(days=10))

    account = db_session.get(Account, uuid.UUID(brokerage))
    assert account is not None
    low, high = networth.account_window_extremes(
        db_session, account, start=today() - timedelta(days=90), end=today()
    )
    assert low == (today() - timedelta(days=31), Decimal("1300.00"))
    assert high == (today() - timedelta(days=29), Decimal("10000.00"))

    # The chart ranks the same days on the same basis.
    history = client.get(f"/accounts/{brokerage}/balance/history", params={"range": "3m"}).json()
    assert history["low"] == {"date": low[0].isoformat(), "balance": "1300.0000"}
    assert history["high"] == {"date": high[0].isoformat(), "balance": "10000.0000"}


def test_loan_extremes_rank_valuations_and_the_close_cutoff(
    seeded: dict[str, str], client: TestClient, db_session: Session
) -> None:
    mortgage = seeded["liabilities.mortgage"]
    for days, value in ((40, "-300000.00"), (20, "-310000.00"), (10, "-305000.00")):
        resp = client.post(
            f"/accounts/{mortgage}/valuations",
            json={"as_of": (today() - timedelta(days=days)).isoformat(), "value": value},
        )
        assert resp.status_code == 201, resp.text
    _post_row(client, mortgage, "2000.00", today() - timedelta(days=15))

    account = db_session.get(Account, uuid.UUID(mortgage))
    assert account is not None
    low, high = networth.account_window_extremes(
        db_session, account, start=today() - timedelta(days=30), end=today()
    )
    # The posting day (15) reads the valuation in effect, -310000.00 from
    # day 20, and loses the tie to day 20.
    assert low == (today() - timedelta(days=20), Decimal("-310000.00"))
    assert high == (today() - timedelta(days=10), Decimal("-305000.00"))

    # From the close date on, the account answers from postings: the close
    # day counts as activity even with no row on it.
    closed_on = today() - timedelta(days=3)
    account.closed_at = datetime.combine(closed_on, time(12), tzinfo=clock.ledger_zone())
    db_session.commit()
    low, high = networth.account_window_extremes(
        db_session, account, start=today() - timedelta(days=30), end=today()
    )
    assert low == (today() - timedelta(days=20), Decimal("-310000.00"))
    assert high == (closed_on, Decimal("2000.00"))


def test_no_activity_in_the_window_has_no_extremes(
    seeded: dict[str, str], client: TestClient, db_session: Session
) -> None:
    brokerage = seeded["assets.brokerage"]
    _post_row(client, brokerage, "100.00", today() - timedelta(days=60))
    _snapshot(db_session, brokerage, today() - timedelta(days=55), "100.00")
    account = db_session.get(Account, uuid.UUID(brokerage))
    assert account is not None
    assert networth.account_window_extremes(
        db_session, account, start=today() - timedelta(days=30), end=today()
    ) == (None, None)


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
    min_value=Decimal("1.00"),
    max_value=Decimal("9000.00"),
    allow_nan=False,
    allow_infinity=False,
    places=2,
)
_days_ago = st.integers(min_value=0, max_value=70)


@settings(max_examples=20, deadline=None)
@given(data=st.data())
def test_extremes_bound_every_day_of_the_reported_series(
    migrated_engine: Engine, data: st.DataObject
) -> None:
    """For random brokerage and loan ledgers (postings, snapshots or
    valuations, sometimes a close inside the window), the extremes are the
    min and max of the dense reported series over the days with activity —
    earliest date on a tie — and every day of that series from the first
    activity on lies between them, so no day the overlay moves on is
    missed."""
    reset_db(migrated_engine)
    today = clock.ledger_today()
    kind = data.draw(st.sampled_from(["brokerage", "loan"]), label="kind")
    with get_sessionmaker()() as session:
        token = _token(session)
        account = ledger.create_account(
            session,
            name="Overlay",
            type_=kind,
            path=f"assets.overlay_{uuid.uuid4().hex[:8]}",
            actor_token_id=token.id,
        )
        category = ledger.ensure_category_path(session, "expenses.misc", actor_token_id=token.id)
        session.commit()
        for _ in range(data.draw(st.integers(min_value=0, max_value=5), label="n_rows")):
            ledger.create_flat_transaction(
                session,
                account_ref=str(account.id),
                category_ref=str(category.id),
                amount=data.draw(_amounts),
                date=today - timedelta(days=data.draw(_days_ago)),
                payee_name="Prop",
                description="prop row",
                requested_status=core.Status.POSTED,
                token=token,
            )
            session.commit()
        overlay_days = data.draw(st.sets(_days_ago, max_size=5), label="overlay_days")
        for days in overlay_days:
            on = today - timedelta(days=days)
            value = data.draw(_values)
            if kind == "brokerage":
                session.add(
                    HoldingsSnapshot(
                        account_id=account.id,
                        as_of=on,
                        symbol="VTI",
                        quantity=Decimal("1"),
                        market_value=value,
                    )
                )
            else:
                session.add(Valuation(account_id=account.id, as_of=on, value=-value, source="api"))
        closed_days_ago = data.draw(st.none() | st.integers(0, 40), label="closed_days_ago")
        closed_on = today - timedelta(days=closed_days_ago) if closed_days_ago is not None else None
        if closed_on is not None:
            account.closed_at = datetime.combine(closed_on, time(12), tzinfo=clock.ledger_zone())
        session.commit()

        start, end = today - timedelta(days=45), today
        window = [start + timedelta(days=n) for n in range((end - start).days + 1)]
        dense, _method = networth.account_balance_series(session, account, dates=window)
        posting_days = set(
            session.execute(
                select(AccountRunningBalance.date).where(
                    AccountRunningBalance.account_id == account.id
                )
            ).scalars()
        )
        active = sorted(
            day
            for day in window
            if day in posting_days
            or (today - day).days in overlay_days
            or (closed_on is not None and day == closed_on)
        )

        low, high = networth.account_window_extremes(session, account, start=start, end=end)
        if not active:
            assert (low, high) == (None, None)
            return
        floor = min(dense[day] for day in active)
        peak = max(dense[day] for day in active)
        assert low == (next(day for day in active if dense[day] == floor), floor)
        assert high == (next(day for day in active if dense[day] == peak), peak)
        for day in window:
            if day >= active[0]:
                assert floor <= dense[day] <= peak
