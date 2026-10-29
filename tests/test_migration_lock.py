"""Startup migrations serialize on a Postgres advisory lock (issue #215).

`purser serve` and `purser worker` both call run_migrations() at startup;
started together they raced two Alembic upgrades and one died on a duplicate
alembic_version. Every test runs against a fresh scratch database in the
session's Postgres container (like test_migration_backfill), so the suite-wide
head database is never touched. Threads stand in for processes: the lock
belongs to a database session, and each run_migrations() call opens its own.
"""

import os
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.pool import NullPool

import purser.config
from purser import cli
from tests.conftest import TEST_ENCRYPTION_KEY

SCRATCH_DB = "purser_migration_lock"
JOIN_TIMEOUT = 120  # seconds — generous; a full upgrade from empty takes a few


@pytest.fixture
def scratch_url(database_url: str) -> Iterator[str]:
    os.environ["PURSER_ENCRYPTION_KEY"] = TEST_ENCRYPTION_KEY
    purser.config.get_settings.cache_clear()

    admin = sa.create_engine(database_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(sa.text(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)"))
        conn.execute(sa.text(f"CREATE DATABASE {SCRATCH_DB}"))
    try:
        yield database_url.rsplit("/", 1)[0] + f"/{SCRATCH_DB}"
    finally:
        # FORCE also ends the session of a startup thread a failed test left behind.
        with admin.connect() as conn:
            conn.execute(sa.text(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)"))
        admin.dispose()


def _query(url: str, sql: str, **params: Any) -> list[Any]:
    """One statement on a short-lived session of its own."""
    engine = sa.create_engine(url, poolclass=NullPool)
    try:
        with engine.connect() as conn:
            return list(conn.execute(sa.text(sql), params).scalars())
    finally:
        engine.dispose()


def _execute(url: str, sql: str) -> None:
    """One committed statement on a short-lived session of its own."""
    engine = sa.create_engine(url, poolclass=NullPool)
    try:
        with engine.begin() as conn:
            conn.execute(sa.text(sql))
    finally:
        engine.dispose()


def _head() -> str:
    root = Path(cli.__file__).resolve().parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    head = ScriptDirectory.from_config(cfg).get_current_head()
    assert head is not None
    return head


def _versions(url: str) -> list[str]:
    return _query(url, "SELECT version_num FROM alembic_version")


def _lock_is_free(url: str) -> bool:
    """Probe from a separate session. pg_try_advisory_lock never waits; a won
    probe unlocks explicitly, because a closing session's backend releases its
    locks asynchronously and could still hold it for the next probe."""
    key = {"key": cli.MIGRATION_LOCK_KEY}
    engine = sa.create_engine(url, poolclass=NullPool)
    try:
        with engine.connect() as conn:
            got = bool(conn.execute(sa.text("SELECT pg_try_advisory_lock(:key)"), key).scalar())
            if got:
                conn.execute(sa.text("SELECT pg_advisory_unlock(:key)"), key)
            return got
    finally:
        engine.dispose()


def _advisory_waiters(url: str) -> int:
    return int(
        _query(
            url,
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
            " AND wait_event_type = 'Lock' AND wait_event = 'advisory'",
        )[0]
    )


def _wait_for(predicate: Callable[[], bool], timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _start(
    url: str, errors: list[BaseException], barrier: threading.Barrier | None = None
) -> threading.Thread:
    """run_migrations(url) on a daemon thread (a hung one cannot stall the
    suite); any exception lands in ``errors`` for the test to assert on."""

    def target() -> None:
        try:
            if barrier is not None:
                barrier.wait()
            cli.run_migrations(url)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread


def test_simultaneous_startups_take_turns_and_end_at_head(
    scratch_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two startups against an EMPTY database at the same instant — the split
    compose / Helm worker topology: both succeed, the upgrades never overlap,
    and the schema ends at head (the later one waits, then has nothing to do)."""
    real_upgrade = command.upgrade
    guard = threading.Lock()
    in_flight = 0
    entries: list[int] = []  # upgrades in flight at each entry

    def tracked_upgrade(*args: Any, **kwargs: Any) -> None:
        nonlocal in_flight
        with guard:
            in_flight += 1
            entries.append(in_flight)
        try:
            real_upgrade(*args, **kwargs)
        finally:
            with guard:
                in_flight -= 1

    monkeypatch.setattr(command, "upgrade", tracked_upgrade)

    errors: list[BaseException] = []
    barrier = threading.Barrier(2)
    threads = [_start(scratch_url, errors, barrier) for _ in range(2)]
    for thread in threads:
        thread.join(JOIN_TIMEOUT)

    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert entries == [1, 1]  # both upgraded, strictly one at a time
    assert _versions(scratch_url) == [_head()]
    assert _lock_is_free(scratch_url)


def test_startup_waits_while_another_session_holds_the_lock(
    scratch_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Blocking acquire: while a peer holds the lock (mid-upgrade, say), a
    starting process logs that it is waiting and leaves the schema alone; once
    the lock is released it proceeds and upgrades to head."""
    messages: list[str] = []

    class _Log:
        def info(self, event: str, **_: Any) -> None:
            messages.append(event)

    monkeypatch.setattr(cli, "log", _Log())

    holder_engine = sa.create_engine(scratch_url, poolclass=NullPool)
    holder = holder_engine.connect()
    errors: list[BaseException] = []
    try:
        holder.execute(sa.text("SELECT pg_advisory_lock(:key)"), {"key": cli.MIGRATION_LOCK_KEY})
        holder.commit()

        thread = _start(scratch_url, errors)
        assert _wait_for(lambda: _advisory_waiters(scratch_url) == 1)
        assert thread.is_alive()
        assert any("waiting" in message for message in messages)
        # Waiting, not migrating: not even alembic_version exists yet.
        assert _query(scratch_url, "SELECT to_regclass('alembic_version') IS NULL") == [True]
    finally:
        holder.execute(sa.text("SELECT pg_advisory_unlock(:key)"), {"key": cli.MIGRATION_LOCK_KEY})
        holder.commit()
        holder.close()
        holder_engine.dispose()

    thread.join(JOIN_TIMEOUT)
    assert not thread.is_alive()
    assert errors == []
    assert _versions(scratch_url) == [_head()]


def test_lock_is_released_after_a_successful_startup(
    scratch_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unlock sits in ``finally`` and runs on the still-open migration
    session, not merely when that session ends, so the next startup never
    blocks. The canary check runs on that same session after the finally:
    another session can already take the lock at that point."""
    free_during_canary: list[bool] = []
    real_verify = cli.crypto.verify_canary

    def probing_verify(connection: sa.Connection) -> None:
        free_during_canary.append(_lock_is_free(scratch_url))
        real_verify(connection)

    monkeypatch.setattr(cli.crypto, "verify_canary", probing_verify)
    cli.run_migrations(scratch_url)
    assert free_during_canary == [True]
    assert _lock_is_free(scratch_url)

    # A second startup afterwards does not block: nothing to upgrade, no wait.
    errors: list[BaseException] = []
    second = _start(scratch_url, errors)
    second.join(JOIN_TIMEOUT)
    assert not second.is_alive()
    assert errors == []
    assert _versions(scratch_url) == [_head()]


def test_lock_is_released_after_a_failed_upgrade(
    scratch_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing upgrade surfaces its own error, rolls back as a whole (every
    revision runs in one transaction) and leaves the lock free."""
    # A real failing revision: 0001 creates `users`, which already exists.
    _execute(scratch_url, "CREATE TABLE users (id int)")
    with pytest.raises(sa.exc.ProgrammingError, match="already exists"):
        cli.run_migrations(scratch_url)
    assert _lock_is_free(scratch_url)
    assert _query(scratch_url, "SELECT to_regclass('alembic_version') IS NULL") == [True]

    # Worst case: an error that leaves the locked connection's transaction
    # aborted on the way out (Alembic normally rolls back its own first). The
    # startup must still raise THAT error, not a failed unlock.
    def failing_upgrade(cfg: Config, revision: str, **_: Any) -> None:
        cfg.attributes["connection"].execute(sa.text("SELECT 1 / 0"))

    monkeypatch.setattr(command, "upgrade", failing_upgrade)
    with pytest.raises(sa.exc.DBAPIError, match="division by zero"):
        cli.run_migrations(scratch_url)
    assert _lock_is_free(scratch_url)
