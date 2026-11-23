"""Contract test for the built-in job registry.

Every scheduled job Purser relies on in production is pinned here by id:
a silently dropped (or renamed) registration becomes a CI failure instead
of a deployed instance that quietly stops matching/materializing.
"""

import pytest

import purser.scheduler as scheduler_module

EXPECTED_JOB_IDS = {
    "feed.sync_daily",
    "prices.fetch_daily",
    "matching.run_daily",
    "schedules.materialize_daily",
    "schedules.detect_daily",
    "forecast.shortfall_scan",
    "assets.stale_check",
    "networth.materialize_daily",
    "webhooks.delivery",
    "sessions.cleanup",
    "audit.prune_daily",
    "review.aged_sweep",
}


def test_build_scheduler_registers_exactly_the_builtin_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # _JOBS is module-global with an idempotency guard (`if _JOBS: return`),
    # so registration may already have happened via import order elsewhere.
    # Swap in a fresh list to force a clean registration pass; monkeypatch
    # restores the original object afterwards, leaving other tests untouched.
    monkeypatch.setattr(scheduler_module, "_JOBS", [])

    scheduler = scheduler_module.build_scheduler()
    # Never started, so no shutdown needed; pending jobs are still listed.
    job_ids = {job.id for job in scheduler.get_jobs()}

    # Plus the scheduler's own one-off check for a sync missed while Purser
    # was not running (#584); it is not part of the registry.
    assert job_ids == EXPECTED_JOB_IDS | {scheduler_module.STARTUP_CHECK_JOB}
    assert {job_id for job_id, _, _ in scheduler_module._JOBS} == EXPECTED_JOB_IDS


def test_every_job_carries_the_misfire_grace_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """APScheduler's default grace is ONE SECOND, so a daily job whose fire
    time slips is dropped silently — no run, no error (issue #185). Every
    registered job must inherit a real window, including jobs added later.
    """
    monkeypatch.setattr(scheduler_module, "_JOBS", [])
    scheduler = scheduler_module.build_scheduler()

    grace = scheduler_module.JOB_DEFAULTS["misfire_grace_time"]
    # Guards the constant itself: a window this small is the bug, not the fix.
    assert grace >= 300

    # APScheduler holds jobs in `_pending_jobs` until the scheduler starts, and
    # only applies `job_defaults` when it drains them. Start paused (nothing
    # fires) so the defaults are actually materialized on each Job.
    scheduler.start(paused=True)
    try:
        jobs = scheduler.get_jobs()
        assert jobs
        for job in jobs:
            assert job.misfire_grace_time == grace, job.id
            # A backlog after an outage collapses to one run, not a burst.
            assert job.coalesce is True, job.id
            assert job.max_instances == 1, job.id
    finally:
        scheduler.shutdown(wait=False)


def test_matching_runs_between_feed_sync_and_materialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """matching.run_daily must land after feed.sync_daily (fresh imports to
    match) and before schedules.materialize_daily / the shortfall scan."""
    monkeypatch.setattr(scheduler_module, "_JOBS", [])
    scheduler_module._register_builtin_jobs()

    times = {
        job_id: (int(cron["hour"]), int(cron["minute"]))
        for job_id, cron, _fn in scheduler_module._JOBS
        if "hour" in cron
    }
    assert times["feed.sync_daily"] < times["matching.run_daily"]
    assert times["matching.run_daily"] < times["schedules.materialize_daily"]


def test_price_fetch_runs_after_the_sync_and_before_materialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """prices.fetch_daily (#390, decision 89) lands at 06:40: after the
    default 06:30 sync, whose feed snapshots feed the feed-implied prices,
    and well before the 07:30 net-worth materialization, which consumes the
    watermark its re-projections lower."""
    monkeypatch.setattr(scheduler_module, "_JOBS", [])
    scheduler_module._register_builtin_jobs()

    times = {
        job_id: (int(cron["hour"]), int(cron["minute"]))
        for job_id, cron, _fn in scheduler_module._JOBS
        if "hour" in cron
    }
    assert times["prices.fetch_daily"] == (6, 40)
    assert times["feed.sync_daily"] < times["prices.fetch_daily"]
    assert times["prices.fetch_daily"] < times["networth.materialize_daily"]


def _trigger_fields(scheduler: object, job_id: str) -> dict[str, str]:
    job = scheduler.get_job(job_id)  # type: ignore[attr-defined]
    return {f.name: str(f) for f in job.trigger.fields}


class TestConfigurableSyncSchedule:
    """feed.sync_daily takes its cron from appsettings.sync_schedule()."""

    def test_build_honors_configured_schedule(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from purser import appsettings

        monkeypatch.setattr(scheduler_module, "_JOBS", [])
        monkeypatch.setattr(
            appsettings, "sync_schedule", lambda: appsettings.SyncSchedule(preset="hourly")
        )
        scheduler = scheduler_module.build_scheduler()
        fields = _trigger_fields(scheduler, "feed.sync_daily")
        assert fields["hour"] == "*"
        assert fields["minute"] == "30"
        # Spread across :30-:40 as the Bridge asks (#483), never early.
        assert scheduler.get_job("feed.sync_daily").trigger.jitter == 600

    def test_build_falls_back_to_default_when_config_unreachable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from purser import appsettings

        def boom() -> object:
            raise RuntimeError("no database")

        monkeypatch.setattr(scheduler_module, "_JOBS", [])
        monkeypatch.setattr(appsettings, "sync_schedule", boom)
        scheduler = scheduler_module.build_scheduler()
        fields = _trigger_fields(scheduler, "feed.sync_daily")
        assert fields["hour"] == "6"
        assert fields["minute"] == "30"

    def test_reschedule_sync_applies_live(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from purser import appsettings

        monkeypatch.setattr(scheduler_module, "_JOBS", [])
        monkeypatch.setattr(
            appsettings, "sync_schedule", lambda: appsettings.SyncSchedule(preset="daily")
        )
        scheduler = scheduler_module.build_scheduler()
        scheduler.start(paused=True)
        try:
            monkeypatch.setattr(
                appsettings, "sync_schedule", lambda: appsettings.SyncSchedule(preset="every_6h")
            )
            assert scheduler_module.reschedule_sync() is True
            fields = _trigger_fields(scheduler, "feed.sync_daily")
            assert fields["hour"] == "0-23/6"
            assert fields["minute"] == "30"
            # Rescheduling swaps only the trigger; the grace window a live
            # reschedule would otherwise quietly drop must survive (#185).
            job = scheduler.get_job("feed.sync_daily")
            assert job.misfire_grace_time == scheduler_module.JOB_DEFAULTS["misfire_grace_time"]
        finally:
            scheduler.shutdown(wait=False)

    def test_reschedule_sync_reports_no_live_scheduler(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(scheduler_module, "_active", None)
        assert scheduler_module.reschedule_sync() is False


class TestSyncTimeFollowsHouseholdZone:
    """#556, decision 111: the daily sync time is a clock time in the
    household zone, so it does not move when the clocks change; the interval
    presets and every other job stay in UTC."""

    @staticmethod
    def _build(monkeypatch: pytest.MonkeyPatch, schedule: object, zone: str) -> object:
        from purser import appsettings, clock

        monkeypatch.setattr(scheduler_module, "_JOBS", [])
        monkeypatch.setattr(appsettings, "sync_schedule", lambda: schedule)
        monkeypatch.setattr(clock, "timezone_name", lambda: zone)
        return scheduler_module.build_scheduler()

    def test_daily_reads_in_the_household_zone_across_dst(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from purser import appsettings

        scheduler = self._build(
            monkeypatch,
            appsettings.SyncSchedule(preset="daily", hour=6, minute=30),
            "America/New_York",
        )
        trigger = scheduler.get_job("feed.sync_daily").trigger  # type: ignore[attr-defined]
        assert str(trigger.timezone) == "America/New_York"
        utc = ZoneInfo("UTC")
        # 06:30 on the clock in winter and in summer (11:30 and 10:30 UTC),
        # plus the Bridge's 0-10 minute jitter.
        winter = trigger.get_next_fire_time(None, datetime(2026, 1, 15, tzinfo=utc))
        summer = trigger.get_next_fire_time(None, datetime(2026, 7, 15, tzinfo=utc))
        for fire in (winter, summer):
            local = fire.astimezone(ZoneInfo("America/New_York"))
            assert local.hour == 6 and 30 <= local.minute <= 40
        assert winter.astimezone(utc).hour == 11
        assert summer.astimezone(utc).hour == 10
        # The rest of the train stays on UTC times.
        matching = scheduler.get_job("matching.run_daily").trigger  # type: ignore[attr-defined]
        assert str(matching.timezone) == "UTC"

    def test_interval_presets_stay_in_utc(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from purser import appsettings

        scheduler = self._build(
            monkeypatch, appsettings.SyncSchedule(preset="every_6h"), "America/New_York"
        )
        trigger = scheduler.get_job("feed.sync_daily").trigger  # type: ignore[attr-defined]
        assert str(trigger.timezone) == "UTC"

    def test_zone_unreachable_falls_back_to_the_env_zone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from purser import appsettings, clock

        def boom() -> str:
            raise RuntimeError("no database")

        monkeypatch.setattr(scheduler_module, "_JOBS", [])
        monkeypatch.setattr(appsettings, "sync_schedule", lambda: appsettings.SyncSchedule())
        monkeypatch.setattr(clock, "timezone_name", boom)
        monkeypatch.setenv("PURSER_TIMEZONE", "Europe/Berlin")
        from purser.config import get_settings

        get_settings.cache_clear()
        try:
            scheduler = scheduler_module.build_scheduler()
            trigger = scheduler.get_job("feed.sync_daily").trigger
            assert str(trigger.timezone) == "Europe/Berlin"
        finally:
            monkeypatch.delenv("PURSER_TIMEZONE")
            get_settings.cache_clear()

    def test_reschedule_picks_up_a_new_zone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from purser import appsettings, clock

        scheduler = self._build(monkeypatch, appsettings.SyncSchedule(), "America/New_York")
        scheduler.start(paused=True)  # type: ignore[attr-defined]
        try:
            monkeypatch.setattr(clock, "timezone_name", lambda: "Asia/Tokyo")
            assert scheduler_module.reschedule_sync() is True
            trigger = scheduler.get_job("feed.sync_daily").trigger  # type: ignore[attr-defined]
            assert str(trigger.timezone) == "Asia/Tokyo"
            assert trigger.jitter == scheduler_module.SYNC_JITTER_SECONDS
        finally:
            scheduler.shutdown(wait=False)  # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# Runs that never happened (#584, decision 124)
# --------------------------------------------------------------------------

from datetime import UTC, datetime, timedelta  # noqa: E402

from apscheduler.events import EVENT_JOB_MISSED, JobExecutionEvent  # noqa: E402


def _missed(job_id: str) -> JobExecutionEvent:
    return JobExecutionEvent(EVENT_JOB_MISSED, job_id, "default", datetime.now(UTC))


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """A registry of stand-in jobs that record their runs, in the real
    registry's order, with no live scheduler."""
    ran: list[str] = []

    def job(job_id: str):  # type: ignore[no-untyped-def]
        def run() -> None:
            ran.append(job_id)

        return run

    order = [
        "feed.sync_daily",
        "prices.fetch_daily",
        "matching.run_daily",
        "schedules.materialize_daily",
        "webhooks.delivery",
        "sessions.cleanup",
    ]
    monkeypatch.setattr(scheduler_module, "_JOBS", [(i, {"hour": 6}, job(i)) for i in order])
    monkeypatch.setattr(scheduler_module, "_active", None)
    monkeypatch.setattr(scheduler_module, "_missed", set())
    return ran


class TestMissedRuns:
    def test_a_dropped_run_is_run_once_in_the_registry_order(self, recorded: list[str]) -> None:
        # The host slept: the scheduler drops every due job as it wakes, in
        # whatever order it reaches them, some more than once.
        for job_id in ("matching.run_daily", "feed.sync_daily", "matching.run_daily"):
            scheduler_module._on_job_missed(_missed(job_id))

        scheduler_module.run_missed()
        assert recorded == ["feed.sync_daily", "matching.run_daily"]
        # Nothing is left to run a second time.
        scheduler_module.run_missed()
        assert recorded == ["feed.sync_daily", "matching.run_daily"]

    def test_a_job_that_runs_again_within_the_hour_is_left_alone(self, recorded: list[str]) -> None:
        for job_id in ("webhooks.delivery", "sessions.cleanup", "not.a.job"):
            scheduler_module._on_job_missed(_missed(job_id))
        scheduler_module.run_missed()
        assert recorded == []

    def test_one_failing_job_does_not_stop_the_rest(
        self, recorded: list[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom() -> None:
            raise RuntimeError("synthetic")

        jobs = list(scheduler_module._JOBS)
        jobs[0] = ("feed.sync_daily", {"hour": 6}, boom)
        monkeypatch.setattr(scheduler_module, "_JOBS", jobs)
        for job_id in ("feed.sync_daily", "matching.run_daily"):
            scheduler_module._on_job_missed(_missed(job_id))
        scheduler_module.run_missed()
        assert recorded == ["matching.run_daily"]

    def test_a_live_scheduler_schedules_one_catch_up_for_the_whole_wake(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(scheduler_module, "_JOBS", [])
        scheduler = scheduler_module.build_scheduler()
        scheduler.start(paused=True)
        try:
            for job_id in ("feed.sync_daily", "matching.run_daily", "networth.materialize_daily"):
                scheduler_module._on_job_missed(_missed(job_id))
            catch_ups = [j for j in scheduler.get_jobs() if j.id == scheduler_module.CATCH_UP_JOB]
            assert len(catch_ups) == 1
            assert catch_ups[0].func is scheduler_module.run_missed
        finally:
            scheduler.shutdown(wait=False)


class TestSyncSlot:
    def _daily(self, monkeypatch: pytest.MonkeyPatch, zone: str = "UTC") -> None:
        monkeypatch.setattr(
            scheduler_module, "_sync_trigger", lambda: ({"hour": 6, "minute": 30}, zone)
        )

    def test_the_latest_slot_at_or_before_a_moment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._daily(monkeypatch)
        slot = scheduler_module.sync_slot_before
        assert slot(datetime(2026, 3, 10, 9, 0, tzinfo=UTC)) == datetime(
            2026, 3, 10, 6, 30, tzinfo=UTC
        )
        assert slot(datetime(2026, 3, 10, 6, 30, tzinfo=UTC)) == datetime(
            2026, 3, 10, 6, 30, tzinfo=UTC
        )
        assert slot(datetime(2026, 3, 10, 6, 29, tzinfo=UTC)) == datetime(
            2026, 3, 9, 6, 30, tzinfo=UTC
        )

    def test_the_daily_slot_reads_in_the_household_zone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._daily(monkeypatch, zone="America/New_York")
        # 06:30 in New York is 11:30 UTC in winter.
        assert scheduler_module.sync_slot_before(
            datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
        ) == datetime(2026, 1, 15, 11, 30, tzinfo=UTC)

    def test_an_interval_preset_names_several_slots_a_day(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            scheduler_module, "_sync_trigger", lambda: ({"hour": "*/6", "minute": 30}, "UTC")
        )
        assert scheduler_module.sync_slot_before(
            datetime(2026, 3, 10, 13, 0, tzinfo=UTC)
        ) == datetime(2026, 3, 10, 12, 30, tzinfo=UTC)


class TestStartupCheck:
    START = datetime(2026, 3, 10, 9, 0, tzinfo=UTC)

    @pytest.fixture
    def started(self, recorded: list[str], monkeypatch: pytest.MonkeyPatch) -> list[datetime]:
        asked: list[datetime] = []
        monkeypatch.setattr(scheduler_module, "_started_at", self.START)
        monkeypatch.setattr(
            scheduler_module, "_sync_trigger", lambda: ({"hour": 6, "minute": 30}, "UTC")
        )
        return asked

    def _owed(self, monkeypatch: pytest.MonkeyPatch, asked: list[datetime], answer: bool) -> None:
        import purser.feed.service as feed_service

        def owed(slot: datetime) -> bool:
            asked.append(slot)
            return answer

        monkeypatch.setattr(feed_service, "sync_owed_since", owed)

    def test_a_slot_missed_while_down_runs_the_sync_and_its_matching_once(
        self, recorded: list[str], started: list[datetime], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._owed(monkeypatch, started, True)
        scheduler_module.check_sync_at_startup()
        # It asked about the latest slot before the process started.
        assert started == [datetime(2026, 3, 10, 6, 30, tzinfo=UTC)]
        assert recorded == ["feed.sync_daily", "matching.run_daily"]

    def test_nothing_runs_when_every_connection_was_tried_since_the_slot(
        self, recorded: list[str], started: list[datetime], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._owed(monkeypatch, started, False)
        scheduler_module.check_sync_at_startup()
        assert recorded == []

    def test_a_failing_check_never_raises(
        self, recorded: list[str], started: list[datetime], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import purser.feed.service as feed_service

        def boom(slot: datetime) -> bool:
            raise RuntimeError("database unreachable")

        monkeypatch.setattr(feed_service, "sync_owed_since", boom)
        scheduler_module.check_sync_at_startup()
        assert recorded == []

    def test_the_check_is_scheduled_once_shortly_after_start(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(scheduler_module, "_JOBS", [])
        now = datetime(2026, 3, 10, 9, 0, tzinfo=UTC)
        monkeypatch.setattr(scheduler_module, "_utc_now", lambda: now)
        scheduler = scheduler_module.build_scheduler()
        [check] = [j for j in scheduler.get_jobs() if j.id == scheduler_module.STARTUP_CHECK_JOB]
        assert check.func is scheduler_module.check_sync_at_startup
        assert check.trigger.run_date == now + scheduler_module.STARTUP_CHECK_DELAY
        assert scheduler_module._started_at == now


class TestSyncOwed:
    """`sync_owed_since`: the stored facts the startup check reads."""

    SLOT = datetime(2026, 3, 10, 6, 30, tzinfo=UTC)

    @pytest.fixture
    def session(self, db_session):  # type: ignore[no-untyped-def]
        from sqlalchemy import update

        from purser.models import Connection

        # Other tests leave connections behind (the table is not reset):
        # set them aside inside this transaction, which the fixture rolls back.
        db_session.execute(update(Connection).values(status="disabled"))
        return db_session

    def _connection(self, session, **fields):  # type: ignore[no-untyped-def]
        from purser.models import Connection

        row = Connection(provider="simplefin", **fields)
        session.add(row)
        session.flush()
        return row

    def _owed(self, session) -> bool:  # type: ignore[no-untyped-def]
        from purser.feed.service import sync_owed_since

        return sync_owed_since(self.SLOT, session)

    def test_no_connection_owes_nothing(self, session) -> None:  # type: ignore[no-untyped-def]
        assert self._owed(session) is False

    def test_a_fetch_before_the_slot_is_owed_and_one_after_is_not(self, session) -> None:  # type: ignore[no-untyped-def]
        before = self.SLOT - timedelta(hours=20)
        row = self._connection(session, created_at=before, last_fetch_at=before)
        assert self._owed(session) is True
        row.last_fetch_at = self.SLOT + timedelta(minutes=4)
        session.flush()
        assert self._owed(session) is False

    def test_a_recorded_failure_since_the_slot_counts_as_an_attempt(self, session) -> None:  # type: ignore[no-untyped-def]
        from purser.models import Event

        before = self.SLOT - timedelta(days=3)
        row = self._connection(session, status="error", created_at=before, last_fetch_at=before)
        assert self._owed(session) is True
        session.add(
            Event(
                type="sync.failed",
                entity_type="connection",
                entity_id=row.id,
                at=self.SLOT + timedelta(minutes=2),
                payload={},
            )
        )
        session.flush()
        assert self._owed(session) is False

    def test_a_disabled_or_newer_connection_owes_nothing(self, session) -> None:  # type: ignore[no-untyped-def]
        before = self.SLOT - timedelta(days=3)
        self._connection(session, status="disabled", created_at=before, last_fetch_at=before)
        self._connection(session, created_at=self.SLOT + timedelta(hours=1))
        assert self._owed(session) is False


def test_a_real_scheduler_runs_a_run_it_dropped_for_lateness_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end with APScheduler itself: a job whose run times are days in
    the past (the host was asleep) is dropped as late, reported, and then run
    exactly once by the catch-up, however many slots passed."""
    import threading

    from apscheduler.schedulers.background import BackgroundScheduler

    ran: list[str] = []
    done = threading.Event()

    def sync() -> None:
        ran.append("feed.sync_daily")
        done.set()

    monkeypatch.setattr(scheduler_module, "_JOBS", [("feed.sync_daily", {"hour": 6}, sync)])
    monkeypatch.setattr(scheduler_module, "_missed", set())
    monkeypatch.setattr(scheduler_module, "CATCH_UP_DELAY", timedelta(milliseconds=200))
    scheduler = BackgroundScheduler(timezone="UTC", job_defaults=scheduler_module.JOB_DEFAULTS)
    scheduler.add_listener(scheduler_module._on_job_missed, EVENT_JOB_MISSED)
    monkeypatch.setattr(scheduler_module, "_active", scheduler)
    # Three daily slots ago: far past the hour of grace.
    scheduler.add_job(
        sync,
        "cron",
        id="feed.sync_daily",
        hour=6,
        next_run_time=datetime.now(UTC) - timedelta(days=3),
    )
    scheduler.start()
    try:
        assert done.wait(timeout=15), "the dropped run was never caught up"
        # Give a second, wrong, catch-up the time it would need to show.
        threading.Event().wait(1.0)
        assert ran == ["feed.sync_daily"]
    finally:
        scheduler.shutdown(wait=False)
