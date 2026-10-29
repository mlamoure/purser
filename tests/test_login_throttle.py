"""Password sign-in throttling (issue #212, DESIGN decision 59).

The tracker takes an injected clock, so nothing here sleeps or depends on
wall time: lockouts expire only when a test advances the fake clock.
"""

import json
import threading
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

import purser.config
from purser.admin import security
from purser.admin.throttle import LoginThrottle, LoginThrottled, client_key
from purser.models import AuditLog
from tests.conftest import ADMIN_CREDENTIALS

WRONG = "not-the-password-7f3a"
ALICE = "alice@example.com"
ADDR = "198.51.100.7"


class FakeClock:
    """Monotonic seconds that move only when a test says so."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _throttle(clock: FakeClock, **overrides: int) -> LoginThrottle:
    limits = {
        "max_failures": 3,
        "ip_max_failures": 10,
        "backoff_seconds": 30,
        "backoff_max_seconds": 120,
        "reset_seconds": 600,
    }
    limits.update(overrides)
    return LoginThrottle(clock=clock, **limits)


def _fail(throttle: LoginThrottle, email: str = ALICE, ip: str | None = ADDR) -> None:
    # Admission charges the failure; a wrong password needs no further call.
    throttle.begin(email, ip)


def _refused(throttle: LoginThrottle, email: str = ALICE, ip: str | None = ADDR) -> LoginThrottled:
    with pytest.raises(LoginThrottled) as exc:
        throttle.begin(email, ip)
    return exc.value


# ---------------------------------------------------------------------------
# The tracker
# ---------------------------------------------------------------------------


class TestTracker:
    def test_locks_after_max_failures_with_retry_after(self) -> None:
        throttle = _throttle(FakeClock())
        for _ in range(3):
            _fail(throttle)
        refused = _refused(throttle)
        assert refused.retry_after == 30
        assert refused.limited_by == ["email"]

    def test_refused_attempts_neither_count_nor_extend(self) -> None:
        clock = FakeClock()
        throttle = _throttle(clock)
        for _ in range(3):
            _fail(throttle)
        for _ in range(20):
            _refused(throttle)
            clock.advance(1)
        assert _refused(throttle).retry_after == 10  # still the first 30 s lockout
        clock.advance(10)
        throttle.begin(ALICE, ADDR)  # admitted the moment it ends

    def test_backoff_doubles_up_to_the_cap(self) -> None:
        clock = FakeClock()
        throttle = _throttle(clock)
        for _ in range(3):
            _fail(throttle)
        waits = []
        for _ in range(4):
            wait = _refused(throttle).retry_after
            waits.append(wait)
            clock.advance(wait)
            _fail(throttle)
        assert waits == [30, 60, 120, 120]

    def test_success_clears_the_email_count(self) -> None:
        throttle = _throttle(FakeClock())
        _fail(throttle)
        _fail(throttle)
        throttle.succeeded(throttle.begin(ALICE, ADDR))
        _fail(throttle)
        _fail(throttle)  # 2 since the success: still under the limit of 3
        _fail(throttle)
        assert _refused(throttle).limited_by == ["email"]

    def test_success_refunds_only_its_own_charge_on_the_address(self) -> None:
        throttle = _throttle(FakeClock(), max_failures=100, ip_max_failures=3)
        _fail(throttle, "a@example.com")
        _fail(throttle, "b@example.com")
        # This attempt's charge reaches the limit (and locks the address)
        # until the password verifies and the charge is refunded.
        throttle.succeeded(throttle.begin("c@example.com", ADDR))
        _fail(throttle, "d@example.com")  # two earlier failures still count
        refused = _refused(throttle, "e@example.com")
        assert refused.limited_by == ["ip"]
        assert refused.retry_after == 30

    def test_per_address_limit_spans_emails(self) -> None:
        throttle = _throttle(FakeClock(), max_failures=100, ip_max_failures=3)
        for n in range(3):
            _fail(throttle, f"user{n}@example.com")
        assert _refused(throttle, "fresh@example.com").limited_by == ["ip"]
        throttle.begin("fresh@example.com", "203.0.113.9")  # another address is fine

    def test_per_email_limit_spans_addresses(self) -> None:
        throttle = _throttle(FakeClock())
        for n in range(3):
            _fail(throttle, ALICE, f"203.0.113.{n}")
        assert _refused(throttle, ALICE, "203.0.113.99").limited_by == ["email"]

    def test_both_limits_reported_when_both_lock(self) -> None:
        throttle = _throttle(FakeClock(), max_failures=2, ip_max_failures=2)
        _fail(throttle)
        _fail(throttle)
        assert _refused(throttle).limited_by == ["email", "ip"]

    def test_email_is_casefolded(self) -> None:
        throttle = _throttle(FakeClock(), ip_max_failures=0)
        _fail(throttle, "Alice@example.com")
        _fail(throttle, "ALICE@example.com")
        _fail(throttle, " alice@example.com")
        assert _refused(throttle, "aLiCe@example.com").limited_by == ["email"]

    def test_quiet_period_forgets_counts(self) -> None:
        clock = FakeClock()
        throttle = _throttle(clock)
        _fail(throttle)
        _fail(throttle)
        clock.advance(601)
        _fail(throttle)
        _fail(throttle)  # the slate was wiped: 2 of 3
        _fail(throttle)
        _refused(throttle)

    def test_capped_lockout_is_not_forgotten_the_moment_it_ends(self) -> None:
        clock = FakeClock()
        throttle = _throttle(clock)
        for _ in range(3):
            _fail(throttle)
        for wait in (30, 60, 120):
            clock.advance(wait)
            _fail(throttle)
        # The quiet period runs from the END of the 120 s lockout, so 599 s
        # after it (719 s after the last failure) the count still stands.
        clock.advance(120 + 599)
        _fail(throttle)
        assert _refused(throttle).retry_after == 120

    def test_concurrent_attempts_cannot_race_past_the_limit(self) -> None:
        throttle = _throttle(FakeClock())
        start = threading.Barrier(40)
        admitted: list[bool] = []
        lock = threading.Lock()

        def attempt() -> None:
            start.wait()
            try:
                throttle.begin(ALICE, ADDR)
                ok = True
            except LoginThrottled:
                ok = False
            with lock:
                admitted.append(ok)

        threads = [threading.Thread(target=attempt) for _ in range(40)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # Charged on admission: exactly the limit gets through, however the
        # (slow) password checks of the admitted ones would have interleaved.
        assert admitted.count(True) == 3
        assert admitted.count(False) == 37

    def test_zero_switches_a_limit_off(self) -> None:
        throttle = _throttle(FakeClock(), max_failures=0, ip_max_failures=0)
        for n in range(50):
            _fail(throttle, ALICE, f"203.0.113.{n % 2}")
        assert len(throttle) == 0  # nothing is even tracked

    def test_missing_client_address_still_limits_the_email(self) -> None:
        throttle = _throttle(FakeClock())
        for _ in range(3):
            _fail(throttle, ALICE, None)
        assert _refused(throttle, ALICE, None).limited_by == ["email"]

    def test_ipv6_is_grouped_by_64(self) -> None:
        assert client_key("2001:db8::1") == client_key("2001:db8::2:3") == "2001:db8::/64"
        assert client_key("2001:db8:0:1::1") != client_key("2001:db8::1")
        assert client_key("::ffff:198.51.100.7") == "198.51.100.7"
        assert client_key(ADDR) == ADDR
        assert client_key("testclient") == "testclient"

        throttle = _throttle(FakeClock(), max_failures=100, ip_max_failures=3)
        for n in range(3):
            _fail(throttle, f"user{n}@example.com", f"2001:db8::{n + 1}")
        assert _refused(throttle, "fresh@example.com", "2001:db8::ffff").limited_by == ["ip"]

    def test_memory_is_bounded_and_lockouts_survive_eviction(self) -> None:
        clock = FakeClock()
        throttle = _throttle(clock, max_keys=50, ip_max_failures=0)
        for _ in range(3):
            _fail(throttle, "target@example.com")
        for n in range(500):
            clock.advance(0.01)
            _fail(throttle, f"spray{n}@example.com")
            assert len(throttle) <= 50
        # The locked key was never the eviction victim.
        assert _refused(throttle, "target@example.com").limited_by == ["email"]


# ---------------------------------------------------------------------------
# POST /auth/login
# ---------------------------------------------------------------------------


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def app(clean_db: None, migrated_engine: Engine, clock: FakeClock) -> FastAPI:
    from purser.api.app import create_app

    app = create_app()
    # Small limits keep the argon2 round-trips few: 3 per email, 6 per address
    # (high enough that the per-email tests never trip the address limit).
    app.state.login_throttle = _throttle(clock, ip_max_failures=6)
    return app


@pytest.fixture
def login_client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        assert c.post("/auth/setup", json=ADMIN_CREDENTIALS).status_code == 201
        c.cookies.clear()
        yield c


def _login(
    client: TestClient, email: str = ADMIN_CREDENTIALS["email"], password: str = WRONG
) -> Response:
    return client.post("/auth/login", json={"email": email, "password": password})


def _audit_rows(db_session: Session, action: str) -> list[AuditLog]:
    stmt = select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.at, AuditLog.id)
    return list(db_session.execute(stmt).scalars())


class TestLoginEndpoint:
    def test_failures_then_429_with_retry_after(
        self, login_client: TestClient, clock: FakeClock
    ) -> None:
        for _ in range(3):
            assert _login(login_client).status_code == 401
        # Locked out: even the right password is refused.
        resp = _login(login_client, password=ADMIN_CREDENTIALS["password"])
        assert resp.status_code == 429
        assert resp.headers["Retry-After"] == "30"
        assert "30 seconds" in resp.json()["detail"]
        assert "set-cookie" not in resp.headers

        clock.advance(30)
        resp = _login(login_client, password=ADMIN_CREDENTIALS["password"])
        assert resp.status_code == 200
        assert login_client.get("/auth/me").status_code == 200

    def test_throttled_attempts_never_check_the_password(
        self, login_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        checks: list[str] = []
        real_verify = security.verify_password

        def counting_verify(password_hash: str, password: str) -> bool:
            checks.append(password)
            return real_verify(password_hash, password)

        monkeypatch.setattr(security, "verify_password", counting_verify)
        for _ in range(3):
            assert _login(login_client).status_code == 401
        assert len(checks) == 3
        for password in (WRONG, ADMIN_CREDENTIALS["password"], WRONG):
            assert _login(login_client, password=password).status_code == 429
        assert len(checks) == 3  # no oracle while locked out

    def test_success_resets_the_email_count(self, login_client: TestClient) -> None:
        assert _login(login_client).status_code == 401
        assert _login(login_client).status_code == 401
        assert _login(login_client, password=ADMIN_CREDENTIALS["password"]).status_code == 200
        login_client.cookies.clear()
        assert _login(login_client).status_code == 401
        assert _login(login_client).status_code == 401  # would be 429 without the reset
        assert _login(login_client).status_code == 401
        assert _login(login_client).status_code == 429

    def test_per_address_limit_spans_emails(self, app: FastAPI, login_client: TestClient) -> None:
        for n in range(6):
            assert _login(login_client, f"guess{n}@example.com").status_code == 401
        # The admin's own correct password from the sprayed address: refused.
        assert _login(login_client, password=ADMIN_CREDENTIALS["password"]).status_code == 429
        # The app believes no forwarding header itself (uvicorn's
        # FORWARDED_ALLOW_IPS decides that), so a spoofed one changes nothing.
        spoofed = login_client.post(
            "/auth/login",
            json={"email": ADMIN_CREDENTIALS["email"], "password": ADMIN_CREDENTIALS["password"]},
            headers={"X-Forwarded-For": "203.0.113.50"},
        )
        assert spoofed.status_code == 429
        # Another client address is unaffected. (No `with`: the app's lifespan
        # is already running under login_client.)
        elsewhere = TestClient(app, client=("203.0.113.50", 50000))
        try:
            assert _login(elsewhere, password=ADMIN_CREDENTIALS["password"]).status_code == 200
        finally:
            elsewhere.close()

    def test_failed_and_throttled_attempts_are_audited(
        self, login_client: TestClient, db_session: Session
    ) -> None:
        # Case as entered is what gets recorded; the throttle key is casefolded.
        for email in ("Mallory@example.com", "MALLORY@example.com", "mallory@example.com"):
            assert _login(login_client, email).status_code == 401
        assert _login(login_client, "Mallory@example.com").status_code == 429

        failed = _audit_rows(db_session, "auth.login_failed")
        assert [r.before_after["after"]["email"] for r in failed if r.before_after] == [
            "Mallory@example.com",
            "MALLORY@example.com",
            "mallory@example.com",
        ]
        (throttled,) = _audit_rows(db_session, "auth.login_throttled")
        assert throttled.before_after == {
            "before": None,
            "after": {
                "email": "Mallory@example.com",
                "ip": "testclient",
                "retry_after": 30,
                "limited_by": ["email"],
            },
        }
        for row in [*failed, throttled]:
            assert row.actor_token_id is None  # no principal resolved
            assert row.entity_type == "user"
            assert row.entity_id is None
            assert row.kind == "change"
            assert row.before_after is not None
            assert row.before_after["after"]["ip"] == "testclient"

        # The password is never written anywhere in the audit trail.
        everything = db_session.execute(select(AuditLog)).scalars().all()
        dumped = json.dumps([[r.before_after, r.detail] for r in everything])
        assert WRONG not in dumped
        assert ADMIN_CREDENTIALS["password"] not in dumped

    def test_successful_login_is_audited(
        self, login_client: TestClient, db_session: Session
    ) -> None:
        assert _login(login_client, password=ADMIN_CREDENTIALS["password"]).status_code == 200
        # One from the setup wizard, one from this login — each attributed to
        # the session it minted.
        rows = _audit_rows(db_session, "auth.login")
        assert len(rows) == 2
        assert all(r.actor_token_id is not None for r in rows)
        assert rows[-1].before_after == {"before": None, "after": {"method": "password"}}
        assert _audit_rows(db_session, "auth.login_failed") == []


def test_limits_come_from_settings(
    clean_db: None, migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PURSER_LOGIN_MAX_FAILURES", "2")
    monkeypatch.setenv("PURSER_LOGIN_BACKOFF_SECONDS", "45")
    purser.config.get_settings.cache_clear()
    try:
        from purser.api.app import create_app

        with TestClient(create_app()) as client:
            assert client.post("/auth/setup", json=ADMIN_CREDENTIALS).status_code == 201
            client.cookies.clear()
            assert _login(client).status_code == 401
            assert _login(client).status_code == 401
            resp = _login(client)
            assert resp.status_code == 429
            # Real clock here (the point is create_app's wiring): 45 s minus
            # whatever elapsed, and still above the 30 s default.
            assert 30 < int(resp.headers["Retry-After"]) <= 45
    finally:
        purser.config.get_settings.cache_clear()
