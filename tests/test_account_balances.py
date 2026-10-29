"""Overlay-aware account balances (DESIGN §8, decisions 42/50, issues
#169/#196).

Account surfaces report an open loan/property account's latest valuation as
its balance, and add an open brokerage account's latest holdings market value
on top of its postings basis; postings stay the fallback and keep riding
along for flows that operate on them (close). Both overlays are
presentation-only — the running balance store and
``ledger.account_balances()`` are pinned elsewhere (test_running_balances.py)
and stay postings-sums.
"""

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

# UTC, not local — see the note in tests/test_networth.py (issue #140).
TODAY = datetime.now(UTC).date()


@pytest.fixture
def seeded(admin_client: TestClient) -> dict[str, str]:
    ids: dict[str, str] = {}
    for name, type_, path in [
        ("Checking", "checking", "assets.checking"),
        ("Mortgage", "loan", "liabilities.mortgage"),
        ("Car Loan", "loan", "liabilities.carloan"),
        ("Brokerage", "brokerage", "assets.brokerage"),
    ]:
        resp = admin_client.post("/accounts", json={"name": name, "type": type_, "path": path})
        assert resp.status_code == 201, resp.text
        ids[path] = resp.json()["id"]
    resp = admin_client.post(
        "/categories",
        json={"name": "Interest", "type": "category_expense", "path": "expenses.interest"},
    )
    assert resp.status_code == 201, resp.text
    return ids


def _valuation(client: TestClient, account_id: str, value: str, on: date) -> None:
    resp = client.post(
        f"/accounts/{account_id}/valuations",
        json={"as_of": on.isoformat(), "value": value},
    )
    assert resp.status_code == 201, resp.text


def _holdings(session: Session, account_id: str, on: date, *symbols: tuple[str, str]) -> None:
    """Insert a snapshot set directly — the feed path is covered by
    test_networth.py; here we only need the rows to exist."""
    from purser.models import HoldingsSnapshot

    for symbol, market_value in symbols:
        session.add(
            HoldingsSnapshot(
                account_id=uuid.UUID(account_id),
                as_of=on,
                symbol=symbol,
                quantity=Decimal("1"),
                cost_basis=Decimal(market_value),
                market_value=Decimal(market_value),
            )
        )
    session.commit()


class TestAccountSurfaces:
    def test_loan_with_valuation_and_no_postings(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        """The Harbor-mortgage shape: balance-only feed, zero transactions."""
        mortgage = seeded["liabilities.mortgage"]
        _valuation(client, mortgage, "-665824.83", TODAY - timedelta(days=20))

        single = client.get(f"/accounts/{mortgage}").json()
        assert Decimal(single["balance"]) == Decimal("-665824.83")
        assert single["balance_method"] == "valuation"
        assert single["balance_as_of"] == (TODAY - timedelta(days=20)).isoformat()
        assert Decimal(single["postings_balance"]) == Decimal("0")

        listed = {
            a["path"]: a for a in client.get("/accounts", params={"include_balances": True}).json()
        }
        assert Decimal(listed["liabilities.mortgage"]["balance"]) == Decimal("-665824.83")
        assert listed["liabilities.mortgage"]["balance_method"] == "valuation"
        assert listed["assets.checking"]["balance_method"] == "postings"
        assert listed["assets.checking"]["postings_balance"] is None

    def test_valuation_wins_over_postings(self, seeded: dict[str, str], client: TestClient) -> None:
        """The auto-loan shape: payment-split postings plus a feed valuation."""
        carloan = seeded["liabilities.carloan"]
        resp = client.post(
            "/transactions",
            json={
                "account": "liabilities.carloan",
                "category": "expenses.interest",
                "amount": "421.57",
                "date": (TODAY - timedelta(days=5)).isoformat(),
                "status": "posted",
            },
        )
        assert resp.status_code == 201, resp.text
        _valuation(client, carloan, "-12174.63", TODAY - timedelta(days=1))

        single = client.get(f"/accounts/{carloan}").json()
        assert Decimal(single["balance"]) == Decimal("-12174.63")
        assert single["balance_method"] == "valuation"
        assert Decimal(single["postings_balance"]) == Decimal("421.57")

    def test_balance_endpoint_picks_valuation_asof(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        mortgage = seeded["liabilities.mortgage"]
        _valuation(client, mortgage, "-670000.00", TODAY - timedelta(days=30))
        _valuation(client, mortgage, "-665824.83", TODAY - timedelta(days=2))

        current = client.get(f"/accounts/{mortgage}/balance").json()
        assert Decimal(current["balance"]) == Decimal("-665824.83")
        assert current["method"] == "valuation"

        between = client.get(
            f"/accounts/{mortgage}/balance",
            params={"as_of": (TODAY - timedelta(days=10)).isoformat()},
        ).json()
        assert Decimal(between["balance"]) == Decimal("-670000.00")
        assert between["method"] == "valuation"

        # Before the first valuation: postings fallback (0 — no activity).
        before = client.get(
            f"/accounts/{mortgage}/balance",
            params={"as_of": (TODAY - timedelta(days=40)).isoformat()},
        ).json()
        assert Decimal(before["balance"]) == Decimal("0")
        assert before["method"] == "postings"

    def test_no_valuation_falls_back_to_postings(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        mortgage = seeded["liabilities.mortgage"]
        single = client.get(f"/accounts/{mortgage}").json()
        assert Decimal(single["balance"]) == Decimal("0")
        assert single["balance_method"] == "postings"
        assert single["balance_as_of"] is None

        checking = seeded["assets.checking"]
        endpoint = client.get(f"/accounts/{checking}/balance").json()
        assert endpoint["method"] == "postings"

    def test_brokerage_adds_market_value_on_every_surface(
        self, seeded: dict[str, str], client: TestClient, db_session: Session
    ) -> None:
        """Decision 50: the bug this issue fixes. Brokerage cash lives in
        postings, holdings live in snapshots; every account surface must
        report the SUM, as net worth already did."""
        brokerage = seeded["assets.brokerage"]
        resp = client.post(
            "/transactions",
            json={
                "account": "assets.brokerage",
                "category": "expenses.interest",
                "amount": "-1200.00",
                "date": (TODAY - timedelta(days=5)).isoformat(),
                "status": "posted",
            },
        )
        assert resp.status_code == 201, resp.text
        _holdings(
            db_session, brokerage, TODAY - timedelta(days=1), ("VTI", "2625.00"), ("BND", "1580.00")
        )
        expected = Decimal("-1200.00") + Decimal("4205.00")

        single = client.get(f"/accounts/{brokerage}").json()
        assert Decimal(single["balance"]) == expected
        assert single["balance_method"] == "postings+holdings"
        assert single["balance_as_of"] == (TODAY - timedelta(days=1)).isoformat()
        assert Decimal(single["postings_balance"]) == Decimal("-1200.00")

        listed = {
            a["path"]: a for a in client.get("/accounts", params={"include_balances": True}).json()
        }
        assert Decimal(listed["assets.brokerage"]["balance"]) == expected
        assert listed["assets.brokerage"]["balance_method"] == "postings+holdings"

        endpoint = client.get(f"/accounts/{brokerage}/balance").json()
        assert Decimal(endpoint["balance"]) == expected
        assert endpoint["method"] == "postings+holdings"

    def test_brokerage_addend_matches_net_worth(
        self, seeded: dict[str, str], client: TestClient, db_session: Session
    ) -> None:
        """The account surface and the net-worth engine must add the SAME
        market value. Asserted on the addend rather than the total: decision
        37's stored heads-only balance and net_worth_at's sum-over-all-rows
        legitimately diverge inside a date-edited correction window, and that
        divergence is not what this test is about."""
        from purser import balances as balance_service
        from purser import networth as networth_service

        brokerage = seeded["assets.brokerage"]
        _holdings(db_session, brokerage, TODAY - timedelta(days=1), ("VTI", "2625.00"))

        surface = Decimal(client.get(f"/accounts/{brokerage}/balance").json()["balance"])
        surface_basis = balance_service.balance_asof(db_session, uuid.UUID(brokerage), TODAY)

        engine = client.get("/networth").json()
        line = next(b for b in engine["breakdown"] if b["path"] == "assets.brokerage")
        engine_basis = networth_service._postings_balance(db_session, uuid.UUID(brokerage), TODAY)

        assert surface - surface_basis == Decimal(line["value"]) - engine_basis

    def test_brokerage_before_first_snapshot_is_postings_only(
        self, seeded: dict[str, str], client: TestClient, db_session: Session
    ) -> None:
        brokerage = seeded["assets.brokerage"]
        _holdings(db_session, brokerage, TODAY - timedelta(days=1), ("VTI", "2625.00"))

        before = client.get(
            f"/accounts/{brokerage}/balance",
            params={"as_of": (TODAY - timedelta(days=10)).isoformat()},
        ).json()
        assert Decimal(before["balance"]) == Decimal("0")
        assert before["method"] == "postings"
        assert "balance_as_of" not in before or before.get("balance_as_of") is None

    def test_closed_brokerage_answers_postings_not_stale_snapshot(
        self, seeded: dict[str, str], client: TestClient, db_session: Session
    ) -> None:
        """Same cutoff rule as valuations and net_worth_at: closing zeroed the
        postings, and a lingering snapshot must not resurrect the account."""
        brokerage = seeded["assets.brokerage"]
        _holdings(db_session, brokerage, TODAY - timedelta(days=2), ("VTI", "2625.00"))
        resp = client.post(f"/accounts/{brokerage}/close", json={})
        assert resp.status_code == 200, resp.text

        single = client.get(f"/accounts/{brokerage}").json()
        assert single["balance_method"] == "postings"
        assert Decimal(single["balance"]) == Decimal("0")

        endpoint = client.get(f"/accounts/{brokerage}/balance").json()
        assert endpoint["method"] == "postings"
        assert Decimal(endpoint["balance"]) == Decimal("0")

    def test_forecast_starts_at_the_register_headline(
        self, seeded: dict[str, str], client: TestClient, db_session: Session
    ) -> None:
        """A projection must begin at the number the register headline above
        it shows (decision 50) — so it carries the market value too.

        The headline (`GET /accounts/{id}`) is the right comparison, not
        `GET /accounts/{id}/balance`: the headline's basis is the LIVE
        postings sum and the balance endpoint's is the stored running balance
        at a date. They differ deliberately on future-dated rows, and the
        forecast must use the live basis or a future-dated *pending* row —
        which is not an `expected` row, so the event loop never sees it —
        would vanish from the projection entirely.
        """
        brokerage = seeded["assets.brokerage"]
        resp = client.post(
            "/transactions",
            json={
                "account": "assets.brokerage",
                "category": "expenses.interest",
                "amount": "-500.00",
                "date": (TODAY + timedelta(days=3)).isoformat(),
                "status": "pending",
            },
        )
        assert resp.status_code == 201, resp.text
        _holdings(db_session, brokerage, TODAY - timedelta(days=1), ("VTI", "2625.00"))

        headline = Decimal(client.get(f"/accounts/{brokerage}").json()["balance"])
        forecast = client.get("/forecast", params={"account": "assets.brokerage"}).json()
        assert Decimal(forecast["start_balance"]) == headline
        # The future-dated pending row is inside the live basis, and the
        # market value rode along on top of it.
        assert headline == Decimal("-500.00") + Decimal("2625.00")

    def test_series_branches_on_account_type(
        self, seeded: dict[str, str], client: TestClient, db_session: Session
    ) -> None:
        """#198: an account does not change how it is valued mid-window, so
        the series branches once on type — and reports which branch it took."""
        from purser import ledger as ledger_service
        from purser import networth as networth_service

        mortgage, brokerage = seeded["liabilities.mortgage"], seeded["assets.brokerage"]
        _valuation(client, mortgage, "-665824.83", TODAY - timedelta(days=20))
        _holdings(db_session, brokerage, TODAY - timedelta(days=2), ("VTI", "2625.00"))
        dates = [TODAY - timedelta(days=n) for n in (30, 10, 0)]

        loan = ledger_service.get_account(db_session, uuid.UUID(mortgage))
        loan_series, loan_method = networth_service.account_balance_series(
            db_session, loan, dates=dates
        )
        assert loan_method == "valuation"
        # Before the first valuation the overlay has nothing to say, so the
        # postings basis answers — same fallback as the point-in-time surface.
        assert loan_series[dates[0]] == Decimal("0")
        assert loan_series[dates[1]] == Decimal("-665824.83")

        broker = ledger_service.get_account(db_session, uuid.UUID(brokerage))
        broker_series, broker_method = networth_service.account_balance_series(
            db_session, broker, dates=dates
        )
        assert broker_method == "postings+holdings"
        assert broker_series[dates[0]] == Decimal("0")
        assert broker_series[dates[2]] == Decimal("2625.00")

        checking = ledger_service.get_account(db_session, uuid.UUID(seeded["assets.checking"]))
        _, cash_method = networth_service.account_balance_series(db_session, checking, dates=dates)
        assert cash_method == "postings"

    def test_series_applies_the_close_cutoff_per_date(
        self, seeded: dict[str, str], client: TestClient, db_session: Session
    ) -> None:
        """History BEFORE the close date keeps its overlay; from the close
        date onward the account answers from postings, so a stale valuation
        cannot resurrect it partway through a series."""
        from purser import ledger as ledger_service
        from purser import networth as networth_service

        mortgage = seeded["liabilities.mortgage"]
        _valuation(client, mortgage, "-665824.83", TODAY - timedelta(days=20))
        assert client.post(f"/accounts/{mortgage}/close", json={}).status_code == 200

        db_session.expire_all()
        loan = ledger_service.get_account(db_session, uuid.UUID(mortgage))
        before, after = TODAY - timedelta(days=10), TODAY
        series, _method = networth_service.account_balance_series(
            db_session, loan, dates=[before, after]
        )
        assert series[before] == Decimal("-665824.83")
        assert series[after] == Decimal("0")

    def test_closed_loan_answers_postings_not_stale_valuation(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        """A closed account's postings were zeroed at close; a lingering
        valuation must not resurrect it (same cutoff as net_worth_at)."""
        mortgage = seeded["liabilities.mortgage"]
        _valuation(client, mortgage, "-665824.83", TODAY - timedelta(days=20))
        resp = client.post(f"/accounts/{mortgage}/close", json={})
        assert resp.status_code == 200, resp.text

        single = client.get(f"/accounts/{mortgage}").json()
        assert single["balance_method"] == "postings"
        assert Decimal(single["balance"]) == Decimal("0")

        endpoint = client.get(f"/accounts/{mortgage}/balance").json()
        assert endpoint["method"] == "postings"
        assert Decimal(endpoint["balance"]) == Decimal("0")
