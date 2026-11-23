"""One clock per test, and runs that cross UTC midnight (#533).

``tests/conftest.py`` pins purser.clock for every test at the instant it
starts; ``now()`` and ``today()`` read that same instant. These tests pin the
pin itself, keep import-time and live day reads out of tests/, and move the
clock across midnight between building the fixtures and running the code
under test: the case a long run used to hit by accident, when a module's
``TODAY`` stayed on the day it was imported while the code under test had
moved on to the next one.
"""

import ast
import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

import purser.clock
from purser import clock, db, digest, ledger, matching, review, schedules
from purser.core import ledger as core
from purser.holdings import prices
from purser.models import Event, Match, Transaction
from tests import conftest
from tests.conftest import PinnedClock, now, today
from tests.test_feed_overlay import _feed_account
from tests.test_ledger_day import _expected_bill, _system_row

TESTS = Path(__file__).parent


@pytest.fixture
def midnight() -> datetime:
    """The next UTC midnight after this test started: within a day of the
    live clock, which instants (``created_at``, audit ``at``) still read."""
    return datetime.combine(today() + timedelta(days=1), time(), tzinfo=UTC)


def _cross(pinned_clock: PinnedClock, midnight: datetime) -> tuple[date, date]:
    """Stand one second before ``midnight``; return (that day, the next day).
    The caller builds its fixtures, then calls ``pinned_clock.set`` past it."""
    pinned_clock.set(midnight - timedelta(seconds=1))
    return midnight.date() - timedelta(days=1), midnight.date()


class TestPinnedClock:
    def test_the_code_under_test_reads_the_tests_clock(
        self, pinned_clock: PinnedClock, clean_db: None
    ) -> None:
        assert purser.clock._utc_now() == now() == pinned_clock.instant
        assert clock.ledger_today() == today() == now().date()
        assert clock.ledger_now() == now()

    def test_the_clock_holds_still_for_the_whole_test(self, pinned_clock: PinnedClock) -> None:
        first = now()
        assert datetime.now(UTC) >= first  # the live clock moves on; the pin does not
        assert now() == first and today() == first.date()

    def test_moving_the_clock_moves_today(self, pinned_clock: PinnedClock) -> None:
        start = today()
        pinned_clock.advance(timedelta(days=1))
        assert today() == start + timedelta(days=1)
        with pytest.raises(ValueError, match="aware"):
            pinned_clock.set(datetime(2026, 1, 1, 12, 0))

    def test_reading_the_day_outside_a_test_fails_loudly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A module constant would run before any test pins the clock."""
        monkeypatch.setattr(purser.clock, "_utc_now", conftest._LIVE_CLOCK)
        with pytest.raises(RuntimeError, match="import time"):
            today()


# --------------------------------------------------------------------------
# No import-time or live day reads in tests/
# --------------------------------------------------------------------------

_CLOCK_CALLS = {"now", "today", "utcnow", "ledger_today", "ledger_now", "_utc_now"}


def _is_clock_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
    return name in _CLOCK_CALLS


def _import_time_reads(tree: ast.Module) -> list[int]:
    """Lines whose clock read runs when the module is imported: module and
    class bodies, default arguments and decorators (not function bodies)."""
    found: list[int] = []

    def scan(node: ast.AST) -> None:
        stack = [node]
        while stack:
            current = stack.pop()
            if isinstance(current, ast.Lambda):
                continue  # deferred
            if _is_clock_call(current):
                found.append(current.lineno)
            stack.extend(ast.iter_child_nodes(current))

    def body(statements: list[ast.stmt]) -> None:
        for stmt in statements:
            if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
                for expr in [
                    *stmt.decorator_list,
                    *stmt.args.defaults,
                    *(d for d in stmt.args.kw_defaults if d is not None),
                ]:
                    scan(expr)
            elif isinstance(stmt, ast.ClassDef):
                for expr in stmt.decorator_list:
                    scan(expr)
                body(stmt.body)
            else:
                scan(stmt)

    body(tree.body)
    return found


def _live_day_reads(tree: ast.Module) -> list[int]:
    """``date.today()``, ``datetime.utcnow()`` and ``<datetime.now(...)
    expression>.date()``: the day from the live clock, not the test's."""
    found: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        attr, receiver = node.func.attr, node.func.value
        if (attr in {"today", "utcnow"} and ast.unparse(receiver) in {"date", "datetime"}) or (
            attr == "date"
            and any(
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Attribute)
                and sub.func.attr == "now"
                and ast.unparse(sub.func.value) == "datetime"
                for sub in ast.walk(receiver)
            )
        ):
            found.append(node.lineno)
    return found


def _test_modules() -> list[Path]:
    """The pytest modules (tests/e2e drives a live server on its own clock)."""
    return sorted(p for p in TESTS.glob("*.py") if p.name != "conftest.py")


def test_no_test_module_reads_the_clock_at_import() -> None:
    offenders = [
        f"{path.relative_to(TESTS)}:{line}"
        for path in _test_modules()
        for line in _import_time_reads(ast.parse(path.read_text()))
    ]
    assert offenders == [], (
        "a module or class constant from the clock freezes the import's day while the code"
        " under test reads the test's: use today() / now() from tests.conftest inside the"
        f" test (#533): {offenders}"
    )


def test_tests_read_the_day_from_the_pinned_clock() -> None:
    offenders = [
        f"{path.relative_to(TESTS)}:{line}"
        for path in _test_modules()
        for line in _live_day_reads(ast.parse(path.read_text()))
    ]
    assert offenders == [], f"read the day with today() from tests.conftest (#533): {offenders}"


def test_the_guards_catch_what_they_guard() -> None:
    sample = ast.parse(
        "TODAY = datetime.now(UTC).date()\n"
        "class T:\n    AS_OF = date.today()\n"
        "def helper(on=today()):\n    return (datetime.now(UTC) - timedelta(days=1)).date()\n"
        "def fine():\n    return today(), datetime.now(UTC)\n"
    )
    assert _import_time_reads(sample) == [1, 3, 4]
    assert _live_day_reads(sample) == [1, 3, 5]


# --------------------------------------------------------------------------
# Fixtures built before midnight, code run after it
# --------------------------------------------------------------------------


@pytest.fixture
def world(admin_client: TestClient) -> TestClient:
    for name, type_, path in [
        ("Checking", "checking", "assets.checking"),
        ("Housing", "category_expense", "expenses.housing"),
    ]:
        route = "/categories" if type_.startswith("category") else "/accounts"
        resp = admin_client.post(route, json={"name": name, "type": type_, "path": path})
        assert resp.status_code == 201, resp.text
    return admin_client


def test_every_ledger_day_surface_turns_over_at_midnight(
    world: TestClient, migrated_engine: Engine, pinned_clock: PinnedClock, midnight: datetime
) -> None:
    """A bill and an unreviewed row dated the last day before midnight: one
    second later they are a day old everywhere at once, with nothing rebuilt."""
    client = world
    day, next_day = _cross(pinned_clock, midnight)
    assert today() == day
    bill_id = _expected_bill(client, day)
    _system_row(migrated_engine, day)

    def surfaces() -> tuple[int, bool, int | None, str]:
        with db.get_sessionmaker()() as session:
            unmatched = {
                str(r["transaction_id"]): r
                for r in matching.list_unmatched(session, direction="expected")
            }
            upcoming = digest.whats_new(session)["upcoming"]["items"]
            return (
                unmatched[bill_id]["aging_days"],
                bill_id in {str(item["transaction_id"]) for item in upcoming},
                review.oldest_unreviewed_days(session),
                client.get("/networth").json()["as_of"],
            )

    assert surfaces() == (0, True, 0, day.isoformat())
    pinned_clock.set(midnight + timedelta(seconds=1))
    assert today() == next_day
    assert surfaces() == (1, False, 1, next_day.isoformat())


def test_a_schedule_made_before_midnight_still_matches_a_line_posted_after(
    db_session: Session, pinned_clock: PinnedClock, midnight: datetime
) -> None:
    """The #533 failure in test_holdings_cash: a bill scheduled on the last
    day before midnight, and the bank line that pays it imported the next
    day. The occurrence materialized at creation stays expected, and the
    matcher pairs the line with it."""
    day, next_day = _cross(pinned_clock, midnight)
    suffix = uuid.uuid4().hex[:8]
    account = ledger.create_account(
        db_session,
        name="Checking",
        type_="checking",
        path=f"assets.checking_{suffix}",
        actor_token_id=None,
    )
    category = ledger.ensure_category_path(
        db_session, f"expenses.utilities_{suffix}", actor_token_id=None
    )
    schedule = schedules.create_schedule(
        db_session,
        name="Synthetic Utility",
        account_ref=str(account.id),
        rrule="FREQ=MONTHLY",
        amount_type="fixed",
        amount=Decimal("-75.00"),
        payee_name="Synthetic Utility",
        category_path=category.path,
        dtstart=day,
        token=None,
    )
    pinned_clock.set(midnight + timedelta(seconds=1))
    line = ledger.create_flat_transaction(
        db_session,
        account_ref=str(account.id),
        category_ref=str(category.id),
        amount=Decimal("-75.00"),
        date=next_day,
        payee_name="Synthetic Utility",
        description="SYNTHETIC UTILITY",
        requested_status=core.Status.POSTED,
        origin=core.Origin.IMPORT,
        token=None,
        content_hash=f"test-{uuid.uuid4().hex}",
    )
    matching.run_matching(db_session)
    match = db_session.execute(select(Match).where(Match.posted_id == line.id)).scalar_one()
    expected = db_session.get(Transaction, match.expected_id)
    assert expected is not None
    assert expected.schedule_id == schedule.id and expected.date == day


def test_an_open_period_event_dates_the_switch_not_the_run(
    clean_db: None, pinned_clock: PinnedClock, midnight: datetime
) -> None:
    """The #533 failure in test_feed_overlay: the accounts are built in the
    last second before midnight, the price job runs one second after. The
    event's ``since`` is the day of the switch, ten days before the day the
    fixtures were built, whatever day the job runs on."""
    day, next_day = _cross(pinned_clock, midnight)
    with db.get_sessionmaker()() as session:
        account = _feed_account(session)
        account.manual_period_open = True
        account.holdings_source_changed_at = now() - timedelta(days=10)
        path, account_id = account.path, account.id
        fresh = _feed_account(session)  # switched back yesterday: not yet
        fresh.manual_period_open = True
        fresh.holdings_source_changed_at = now() - timedelta(days=1)
        session.commit()
    pinned_clock.set(midnight + timedelta(seconds=1))
    prices.run_fetch(today=next_day)
    with db.get_sessionmaker()() as session:
        (event,) = session.execute(
            select(Event).where(Event.type == "holding.period_open")
        ).scalars()
    assert event.entity_id == account_id
    assert event.payload == {
        "account": path,
        "since": (day - timedelta(days=10)).isoformat(),
        "stale_days": 7,
    }
