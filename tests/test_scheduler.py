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

    assert job_ids == EXPECTED_JOB_IDS


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
