"""Stored running balances + the balance watermark (issues #146/#145,
decisions 37/38).

The core property: after ANY random sequence of backdated mutations driven
through the service layer, the stored table equals an INDEPENDENT Python
reference (ORM chain-heads -> sorted prefix sums — deliberately not the SQL
under test), the register walk is additive, and each account's newest stored
balance equals ``account_balances()``. Examples pin the row-37 semantics
(date-edited corrections), the as-of endpoint, the delete-CLI cascade, the
crash path, and rebuild==incremental parity (the migration backfill path).
"""

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session, selectinload

from purser import balances, ledger
from purser.core import ledger as core
from purser.core import sampling
from purser.db import get_sessionmaker
from purser.models import (
    Account,
    AccountRunningBalance,
    ApiToken,
    Posting,
    Transaction,
    User,
)
from tests.conftest import reset_db


def _commit_token(session: Session, suffix: str) -> ApiToken:
    user = User(email=f"user_{suffix}@example.com")
    session.add(user)
    session.flush()
    token = ApiToken(
        user_id=user.id, name=f"tok_{suffix}", token_hash=uuid.uuid4().hex, scopes=["write"]
    )
    session.add(token)
    session.flush()
    return token


def _reference(session: Session) -> dict[tuple[uuid.UUID, uuid.UUID], Decimal]:
    """Independent recompute: chain-head pending|posted rows -> per-real-account
    prefix sums in (date, created_at, id) order, in plain Python."""
    reversed_ids = set(
        session.execute(
            select(Transaction.reverses_id).where(Transaction.reverses_id.is_not(None))
        ).scalars()
    )
    txns = (
        session.execute(
            select(Transaction)
            .options(selectinload(Transaction.postings).selectinload(Posting.account))
            .where(Transaction.status.in_(["pending", "posted"]))
        )
        .scalars()
        .all()
    )
    heads = sorted(
        (t for t in txns if t.id not in reversed_ids),
        key=lambda t: (t.date, t.created_at, t.id),
    )
    running: dict[uuid.UUID, Decimal] = {}
    out: dict[tuple[uuid.UUID, uuid.UUID], Decimal] = {}
    for t in heads:
        per_account: dict[uuid.UUID, Decimal] = {}
        for p in t.postings:
            if p.account is not None and not p.account.is_virtual:
                per_account[p.account_id] = per_account.get(p.account_id, Decimal(0)) + Decimal(
                    p.amount
                )
        for account_id, delta in per_account.items():
            running[account_id] = running.get(account_id, Decimal(0)) + delta
            out[(account_id, t.id)] = running[account_id]
    return out


def _stored(session: Session) -> dict[tuple[uuid.UUID, uuid.UUID], Decimal]:
    return {
        (row.account_id, row.transaction_id): Decimal(row.balance)
        for row in session.execute(select(AccountRunningBalance)).scalars()
    }


def _seed_world(session: Session) -> tuple[ApiToken, list, object]:
    suffix = uuid.uuid4().hex[:10]
    token = _commit_token(session, suffix)
    accounts = [
        ledger.create_account(
            session,
            name=f"Acct{i} {suffix}",
            type_="checking",
            path=f"assets.acct{i}_{suffix}",
            actor_token_id=token.id,
        )
        for i in range(2)
    ]
    category = ledger.ensure_category_path(session, "expenses.misc", actor_token_id=token.id)
    session.commit()
    return token, accounts, category


_amounts = st.decimals(
    min_value=Decimal("-500.00"),
    max_value=Decimal("500.00"),
    allow_nan=False,
    allow_infinity=False,
    places=2,
).filter(lambda d: d != 0)
_days_ago = st.integers(min_value=0, max_value=90)


class TestRunningBalanceProperty:
    @settings(max_examples=20, deadline=None)
    @given(data=st.data())
    def test_random_backdated_mutations_keep_stored_balances_true(
        self, migrated_engine: Engine, data: st.DataObject
    ) -> None:
        reset_db(migrated_engine)
        today = date.today()
        with get_sessionmaker()() as session:
            token, accounts, category = _seed_world(session)
            pending: list[uuid.UUID] = []
            posted: list[uuid.UUID] = []

            n_ops = data.draw(st.integers(min_value=2, max_value=8), label="n_ops")
            for _ in range(n_ops):
                op = data.draw(
                    st.sampled_from(["flat", "transfer", "edit", "post", "void", "correct"]),
                    label="op",
                )
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
                elif op == "transfer":
                    tx = ledger.create_transfer(
                        session,
                        from_account_ref=str(accounts[0].id),
                        to_account_ref=str(accounts[1].id),
                        amount=abs(data.draw(_amounts)),
                        date=today - timedelta(days=data.draw(_days_ago)),
                        description="prop transfer",
                        requested_status=core.Status.POSTED,
                        token=token,
                    )
                elif op == "edit" and pending:
                    ledger.update_transaction(
                        session,
                        tx_id=data.draw(st.sampled_from(pending)),
                        token=token,
                        date=today - timedelta(days=data.draw(_days_ago)),
                        amount=data.draw(_amounts),
                    )
                elif op == "post" and pending:
                    tx_id = data.draw(st.sampled_from(pending))
                    ledger.transition_status(
                        session, tx_id=tx_id, new_status=core.Status.POSTED, token=token
                    )
                    pending.remove(tx_id)
                    posted.append(tx_id)
                elif op == "void" and pending:
                    tx_id = data.draw(st.sampled_from(pending))
                    ledger.transition_status(
                        session, tx_id=tx_id, new_status=core.Status.VOID, token=token
                    )
                    pending.remove(tx_id)
                elif op == "correct" and posted:
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
                session.commit()  # each op commits -> the hook recomputes

            # 1) Stored table == the independent reference, exactly.
            assert _stored(session) == _reference(session)
            # 2) Newest stored balance per account == live account_balances().
            live = ledger.account_balances(session, [a.id for a in accounts])
            for account in accounts:
                assert balances.balance_asof(session, account.id, today) == live.get(
                    account.id, Decimal("0")
                )
            # 3) The register walk is additive over the visible set.
            stored = _stored(session)
            for account in accounts:
                rows, _total = ledger.search_transactions(
                    session, account_ref=str(account.id), limit=200, offset=0
                )
                running = Decimal("0")
                for t in reversed(rows):  # register is DESC; accumulate oldest-first
                    mine = sum(
                        (Decimal(p.amount) for p in t.postings if p.account_id == account.id),
                        Decimal("0"),
                    )
                    running += mine
                    assert stored[(account.id, t.id)] == running
            # 4) The watermark is clean after every commit consumed it.
            assert (
                session.execute(
                    text("SELECT running_dirty_from FROM balance_watermark")
                ).scalar_one_or_none()
                is None
            )

    @settings(max_examples=20, deadline=None)
    @given(data=st.data())
    def test_series_reads_agree_with_the_reference_at_every_date(
        self, migrated_engine: Engine, data: st.DataObject
    ) -> None:
        """#198: the sampled series is a READ over the same store, so every
        point must equal the independent reference's prefix sum at that date,
        the extremes must be the end-of-day extremes of the whole window
        (decision 63), and sampling must never invent a value."""
        reset_db(migrated_engine)
        today = date.today()
        with get_sessionmaker()() as session:
            token, accounts, category = _seed_world(session)
            for _ in range(data.draw(st.integers(min_value=3, max_value=8), label="n_rows")):
                ledger.create_flat_transaction(
                    session,
                    account_ref=str(accounts[0].id),
                    category_ref=str(category.id),
                    amount=data.draw(_amounts),
                    date=today - timedelta(days=data.draw(_days_ago)),
                    payee_name="Prop",
                    description="prop row",
                    requested_status=data.draw(
                        st.sampled_from([core.Status.PENDING, core.Status.POSTED])
                    ),
                    token=token,
                )
                session.commit()

            account = accounts[0]
            start, end = today - timedelta(days=100), today
            window = [start + timedelta(days=n) for n in range((end - start).days + 1)]

            # 1) Dense series == an INDEPENDENT Python resolution of the same
            #    rows (last row dated <= D in decision 37's total order, else
            #    0) — deliberately not a second SQL path, and one query
            #    instead of one per date.
            rows = sorted(
                session.execute(
                    select(AccountRunningBalance).where(
                        AccountRunningBalance.account_id == account.id
                    )
                ).scalars(),
                key=lambda r: (r.date, r.created_at, r.transaction_id),
            )
            expected: dict[date, Decimal] = {}
            for day in window:
                in_effect = [r for r in rows if r.date <= day]
                expected[day] = Decimal(in_effect[-1].balance) if in_effect else Decimal("0")
            series = balances.balance_series(session, account.id, dates=window)
            assert series == expected

            # 2) Sampling selects, never invents.
            sampled = sampling.sample_dates(start, end, intervals=(1, 3, 7), max_points=31)
            picked = balances.balance_series(session, account.id, dates=sampled)
            assert sampled[0] == start and sampled[-1] == end
            for day in sampled:
                assert picked[day] == series[day]

            # 3) Extremes are END-OF-DAY over the WHOLE window, not just the
            #    sampled points (decision 63): exactly the min/max of the
            #    reference's end-of-day balance on every active day (earliest
            #    date on a tie), so every day of the series from the first
            #    activity on lies between them — what makes downsampling safe
            #    to publish — while an intra-day row never becomes one.
            low, high = balances.window_extremes(session, account.id, start=start, end=end)
            active_days = sorted({r.date for r in rows if start <= r.date <= end})
            if active_days:
                end_of_day = {day: expected[day] for day in active_days}
                floor, peak = min(end_of_day.values()), max(end_of_day.values())
                assert low is not None and high is not None
                assert (low.date, Decimal(low.balance)) == (
                    next(day for day in active_days if end_of_day[day] == floor),
                    floor,
                )
                assert (high.date, Decimal(high.balance)) == (
                    next(day for day in active_days if end_of_day[day] == peak),
                    peak,
                )
                for day in window:
                    if day >= active_days[0]:
                        assert floor <= series[day] <= peak
            else:
                assert low is None and high is None

    def test_empty_window_is_flat_at_the_carried_in_balance(self, migrated_engine: Engine) -> None:
        """A window with no activity reads flat at whatever was carried in —
        never absent, never None leaking into a money field."""
        reset_db(migrated_engine)
        today = date.today()
        with get_sessionmaker()() as session:
            token, accounts, category = _seed_world(session)
            ledger.create_flat_transaction(
                session,
                account_ref=str(accounts[0].id),
                category_ref=str(category.id),
                amount=Decimal("-25.00"),
                date=today - timedelta(days=200),
                payee_name="Old",
                description="long before the window",
                requested_status=core.Status.POSTED,
                token=token,
            )
            session.commit()

            account = accounts[0]
            start, end = today - timedelta(days=30), today
            window = [start + timedelta(days=n) for n in range((end - start).days + 1)]
            series = balances.balance_series(session, account.id, dates=window)
            assert set(series.values()) == {Decimal("-25.00")}

            points, truncated = balances.change_points(session, account.id, start=start, end=end)
            assert points == [] and truncated is False
            assert balances.window_extremes(session, account.id, start=start, end=end) == (
                None,
                None,
            )
            assert balances.largest_moves(session, account.id, start=start, end=end) == []

            # Before ANY activity the store answers 0, not absent.
            early = [today - timedelta(days=400), today - timedelta(days=300)]
            assert set(balances.balance_series(session, account.id, dates=early).values()) == {
                Decimal("0")
            }


class TestEndOfDayExtremes:
    """Decision 63 (#272): a window's low/high are end-of-day balances, so the
    order of one day's rows never creates an extreme. Each scenario first
    checks the per-transaction store really does pass through the intra-day
    spike, so the extreme ignoring it is the behaviour under test."""

    @staticmethod
    def _post(
        session: Session,
        token: ApiToken,
        account: Account,
        category: Account,
        amount: str,
        on: date,
    ) -> None:
        ledger.create_flat_transaction(
            session,
            account_ref=str(account.id),
            category_ref=str(category.id),
            amount=Decimal(amount),
            date=on,
            payee_name="Synthetic payee",
            description="end-of-day extremes",
            requested_status=core.Status.POSTED,
            token=token,
        )
        # One commit per row: created_at is transaction_timestamp, so separate
        # commits put same-day rows in exactly the order written here.
        session.commit()

    def test_gross_pay_before_same_day_deductions_is_not_a_high(
        self, migrated_engine: Engine
    ) -> None:
        reset_db(migrated_engine)
        today = date.today()
        opened, payday, later = (today - timedelta(days=n) for n in (20, 10, 5))
        with get_sessionmaker()() as session:
            token, accounts, category = _seed_world(session)
            account = accounts[0]
            for amount, on in (
                ("1000.00", opened),
                ("3000.00", payday),  # gross pay leg, posted first
                ("-600.00", payday),  # tax withheld, same day
                ("-250.00", payday),  # benefits deduction, same day
                ("-400.00", later),
            ):
                self._post(session, token, account, category, amount, on)

            start, end = today - timedelta(days=30), today
            points, _truncated = balances.change_points(session, account.id, start=start, end=end)
            assert max(Decimal(p.balance) for p in points) == Decimal("4000.00")

            low, high = balances.window_extremes(session, account.id, start=start, end=end)
            assert low is not None and high is not None
            assert (high.date, Decimal(high.balance)) == (payday, Decimal("3150.00"))
            assert (low.date, Decimal(low.balance)) == (opened, Decimal("1000.00"))
            # The high is a value the daily series actually shows on its date.
            assert balances.balance_series(session, account.id, dates=[payday]) == {
                payday: Decimal("3150.00")
            }

    def test_duplicate_charge_and_same_day_reversal_is_not_a_low(
        self, migrated_engine: Engine
    ) -> None:
        reset_db(migrated_engine)
        today = date.today()
        opened, charged, later = (today - timedelta(days=n) for n in (20, 10, 5))
        with get_sessionmaker()() as session:
            token, accounts, category = _seed_world(session)
            account = accounts[0]
            for amount, on in (
                ("1000.00", opened),
                ("-200.00", charged),  # the real charge
                ("-200.00", charged),  # the bank's duplicate of it
                ("200.00", charged),  # the bank reverses the duplicate, same day
                ("100.00", later),
            ):
                self._post(session, token, account, category, amount, on)

            start, end = today - timedelta(days=30), today
            points, _truncated = balances.change_points(session, account.id, start=start, end=end)
            assert min(Decimal(p.balance) for p in points) == Decimal("600.00")

            low, high = balances.window_extremes(session, account.id, start=start, end=end)
            assert low is not None and high is not None
            assert (low.date, Decimal(low.balance)) == (charged, Decimal("800.00"))
            assert (high.date, Decimal(high.balance)) == (opened, Decimal("1000.00"))
            assert balances.balance_series(session, account.id, dates=[charged]) == {
                charged: Decimal("800.00")
            }


class TestSemantics:
    def test_date_edited_posted_correction_pins_heads_only_semantics(
        self, migrated_engine: Engine
    ) -> None:
        """Decision 37: the original stays posted at D1 (demoted, not voided);
        the register/stored balance shows the row only at D3."""
        reset_db(migrated_engine)
        today = date.today()
        d1, d3 = today - timedelta(days=30), today - timedelta(days=5)
        with get_sessionmaker()() as session:
            token, accounts, category = _seed_world(session)
            account = accounts[0]
            tx = ledger.create_flat_transaction(
                session,
                account_ref=str(account.id),
                category_ref=str(category.id),
                amount=Decimal("100.00"),
                date=d1,
                payee_name="Original",
                description="original",
                requested_status=core.Status.POSTED,
                token=token,
            )
            session.commit()
            assert balances.balance_asof(session, account.id, d1) == Decimal("100.00")

            ledger.update_transaction(session, tx_id=tx.id, token=token, date=d3)
            session.commit()
            # Heads-only: nothing counted at/after D1 until D3.
            assert balances.balance_asof(session, account.id, d1) == Decimal("0")
            assert balances.balance_asof(session, account.id, d3 - timedelta(days=1)) == Decimal(
                "0"
            )
            assert balances.balance_asof(session, account.id, d3) == Decimal("100.00")
            assert _stored(session) == _reference(session)

    def test_rebuild_equals_incremental(self, migrated_engine: Engine) -> None:
        """The migration-backfill path (full rebuild) reproduces exactly what
        incremental consumption built up."""
        reset_db(migrated_engine)
        today = date.today()
        with get_sessionmaker()() as session:
            token, accounts, category = _seed_world(session)
            for days, amount in ((40, "25.00"), (20, "-10.00"), (0, "5.50")):
                ledger.create_flat_transaction(
                    session,
                    account_ref=str(accounts[0].id),
                    category_ref=str(category.id),
                    amount=Decimal(amount),
                    date=today - timedelta(days=days),
                    payee_name="Seed",
                    description="seed",
                    requested_status=core.Status.POSTED,
                    token=token,
                )
                session.commit()
            incremental = _stored(session)
            assert incremental  # sanity: rows exist
            session.execute(text("DELETE FROM account_running_balances"))
            balances.rebuild_running(session)
            session.commit()
            assert _stored(session) == incremental == _reference(session)

    def test_mark_then_rollback_leaves_nothing(self, migrated_engine: Engine) -> None:
        reset_db(migrated_engine)
        with get_sessionmaker()() as session:
            token, accounts, category = _seed_world(session)
            ledger.create_flat_transaction(
                session,
                account_ref=str(accounts[0].id),
                category_ref=str(category.id),
                amount=Decimal("42.00"),
                date=date.today(),
                payee_name="Doomed",
                description="rolled back",
                requested_status=core.Status.POSTED,
                token=token,
            )
            session.rollback()
            assert _stored(session) == {}
            assert (
                session.execute(
                    text("SELECT running_dirty_from FROM balance_watermark")
                ).scalar_one_or_none()
                is None
            )


class TestDeleteCli:
    def test_delete_account_cascades_and_recomputes_survivors(
        self, migrated_engine: Engine
    ) -> None:
        from purser.admin import account_delete

        reset_db(migrated_engine)
        today = date.today()
        with get_sessionmaker()() as session:
            token, accounts, category = _seed_world(session)
            keep, doomed = accounts
            ledger.create_flat_transaction(
                session,
                account_ref=str(keep.id),
                category_ref=str(category.id),
                amount=Decimal("10.00"),
                date=today - timedelta(days=10),
                payee_name="Keep",
                description="keep",
                requested_status=core.Status.POSTED,
                token=token,
            )
            ledger.create_transfer(
                session,
                from_account_ref=str(doomed.id),
                to_account_ref=str(keep.id),
                amount=Decimal("50.00"),
                date=today - timedelta(days=8),
                description="cross",
                requested_status=core.Status.POSTED,
                token=token,
            )
            session.commit()
            doomed_id = doomed.id
            account_delete.execute_delete(session, doomed, actor_token_id=None)
            session.commit()
            assert not session.execute(
                select(AccountRunningBalance).where(AccountRunningBalance.account_id == doomed_id)
            ).first()
            assert _stored(session) == _reference(session)


@pytest.fixture
def seeded(admin_client: TestClient, migrated_engine: Engine) -> dict[str, str]:
    with migrated_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE balance_watermark SET running_dirty_from = NULL, networth_dirty_from = NULL"
            )
        )
    out: dict[str, str] = {}
    for name, type_, path in (
        ("Checking", "checking", "assets.checking"),
        ("Dining", "category_expense", "expenses.dining"),
    ):
        endpoint = "/categories" if type_.startswith("category_") else "/accounts"
        resp = admin_client.post(endpoint, json={"name": name, "type": type_, "path": path})
        assert resp.status_code == 201, resp.text
        out[name.lower()] = resp.json()["id"]
    return out


class TestApi:
    def _post_row(self, client: TestClient, seeded: dict[str, str], amount: str, d: date) -> str:
        resp = client.post(
            "/transactions",
            json={
                "account": seeded["checking"],
                "category": "expenses.dining",
                "amount": amount,
                "date": d.isoformat(),
                "payee": "Api",
            },
        )
        assert resp.status_code == 201, resp.text
        tx_id = resp.json()["id"]
        assert client.post(f"/transactions/{tx_id}/post").status_code == 200
        return tx_id

    def test_register_rows_carry_stored_balances(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        today = date.today()
        self._post_row(client, seeded, "-40.00", today - timedelta(days=2))
        self._post_row(client, seeded, "100.00", today - timedelta(days=1))
        page = client.get("/transactions", params={"account": seeded["checking"]}).json()
        newest, oldest = page["items"][0], page["items"][1]
        assert Decimal(oldest["balance"]) == Decimal("-40.00")
        assert Decimal(newest["balance"]) == Decimal("60.00")
        # Additivity over the visible page: balance[i] = balance[i+1] + amount[i].
        assert Decimal(newest["balance"]) == Decimal(oldest["balance"]) + Decimal(newest["amount"])
        # Non-perspective queries carry no balance.
        flat = client.get("/transactions").json()
        assert all(t["balance"] is None for t in flat["items"])

    def test_category_perspective_carries_no_balance(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        """A category is virtual and has no running-balance rows by
        construction (#197): asking from its perspective is answered with a
        blank column, not an error and not a wrong number."""
        today = date.today()
        self._post_row(client, seeded, "-40.00", today - timedelta(days=1))
        page = client.get("/transactions", params={"account": "expenses.dining"}).json()
        assert page["items"], "the seeded row posts to expenses.dining"
        assert all(t["balance"] is None for t in page["items"])

    def test_balance_series_endpoint(self, seeded: dict[str, str], client: TestClient) -> None:
        """#198: one point per stored change, plus the opening balance carried
        into the window. Not shadowed by /{account_id}/balance."""
        today = date.today()
        account_id = seeded["checking"]
        self._post_row(client, seeded, "-40.00", today - timedelta(days=40))
        self._post_row(client, seeded, "100.00", today - timedelta(days=5))
        self._post_row(client, seeded, "-10.00", today - timedelta(days=1))

        resp = client.get(f"/accounts/{account_id}/balance/series")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["path"] == "assets.checking"
        assert body["method"] == "postings"
        assert body["truncated"] is False
        # Default window is end - 90d, so all three rows are inside it and
        # nothing was carried in.
        assert Decimal(body["opening"]) == Decimal("0")
        assert [Decimal(p["balance"]) for p in body["points"]] == [
            Decimal("-40.00"),
            Decimal("60.00"),
            Decimal("50.00"),
        ]
        assert [Decimal(p["delta"]) for p in body["points"]] == [
            Decimal("-40.00"),
            Decimal("100.00"),
            Decimal("-10.00"),
        ]
        # Points are oldest-first and each carries a drill target.
        dates = [p["date"] for p in body["points"]]
        assert dates == sorted(dates)
        assert all(p["transaction_id"] for p in body["points"])

        # A window that starts after the first row carries it in as `opening`
        # instead of listing it — opening is the day BEFORE start, so a row
        # dated exactly on `start` is a point, never double-counted.
        narrow = client.get(
            f"/accounts/{account_id}/balance/series",
            params={"start": (today - timedelta(days=5)).isoformat(), "end": today.isoformat()},
        ).json()
        assert Decimal(narrow["opening"]) == Decimal("-40.00")
        assert [Decimal(p["balance"]) for p in narrow["points"]] == [
            Decimal("60.00"),
            Decimal("50.00"),
        ]
        assert Decimal(narrow["opening"]) + sum(
            Decimal(p["delta"]) for p in narrow["points"]
        ) == Decimal(narrow["points"][-1]["balance"])

    def test_balance_series_rejects_a_category_and_a_backwards_window(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        today = date.today()
        categories = client.get("/accounts", params={"include_virtual": True}).json()
        category_id = next(a["id"] for a in categories if a["path"] == "expenses.dining")
        assert client.get(f"/accounts/{category_id}/balance/series").status_code == 422
        assert (
            client.get(
                f"/accounts/{seeded['checking']}/balance/series",
                params={"start": today.isoformat(), "end": (today - timedelta(days=5)).isoformat()},
            ).status_code
            == 422
        )

    def test_balance_asof_endpoint(self, seeded: dict[str, str], client: TestClient) -> None:
        today = date.today()
        self._post_row(client, seeded, "25.00", today - timedelta(days=10))
        account_id = seeded["checking"]
        # Before first activity -> 0.
        early = client.get(
            f"/accounts/{account_id}/balance",
            params={"as_of": (today - timedelta(days=30)).isoformat()},
        ).json()
        assert Decimal(early["balance"]) == Decimal("0")
        # At/after the row -> its running balance; default as_of = today.
        at = client.get(
            f"/accounts/{account_id}/balance",
            params={"as_of": (today - timedelta(days=10)).isoformat()},
        ).json()
        assert Decimal(at["balance"]) == Decimal("25.00")
        default = client.get(f"/accounts/{account_id}/balance").json()
        assert Decimal(default["balance"]) == Decimal("25.00")
        assert default["path"] == "assets.checking"
        # Unknown account -> 404; category -> 422.
        assert client.get(f"/accounts/{uuid.uuid4()}/balance").status_code == 404
        assert client.get(f"/accounts/{seeded['dining']}/balance").status_code == 422

    def test_voided_row_shows_no_balance_and_rewrites_history(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        today = date.today()
        resp = client.post(
            "/transactions",
            json={
                "account": seeded["checking"],
                "category": "expenses.dining",
                "amount": "-15.00",
                "date": (today - timedelta(days=3)).isoformat(),
                "payee": "Void me",
            },
        )
        tx_id = resp.json()["id"]  # pending — counts until voided
        self._post_row(client, seeded, "10.00", today - timedelta(days=1))
        account_id = seeded["checking"]
        assert Decimal(client.get(f"/accounts/{account_id}/balance").json()["balance"]) == Decimal(
            "-5.00"
        )

        assert client.post(f"/transactions/{tx_id}/void").status_code == 200
        # Balance history rewrote in the same request's transaction.
        assert Decimal(client.get(f"/accounts/{account_id}/balance").json()["balance"]) == Decimal(
            "10.00"
        )
        collapsed = client.get(
            "/transactions",
            params={"account": account_id, "include_collapsed": True, "status": "void"},
        ).json()
        assert collapsed["items"] and all(t["balance"] is None for t in collapsed["items"])
