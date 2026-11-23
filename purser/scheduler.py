"""In-process job scheduler.

ALL jobs are registered centrally in `_register_builtin_jobs()` below —
there is no decentralized import-time registration by feature modules.
Adding a job means adding a `register_job` line there (and updating the
contract test in tests/test_scheduler.py). `purser serve` runs this
scheduler in-process; `purser worker` runs it alone.

Every job runs on UTC times except one. The SimpleFIN sync job
(`feed.sync_daily` — the id is a stable contract even when the configured
cadence is sub-daily) takes its cron from the persisted config
(`appsettings.sync_schedule()`) at build time, and its daily preset reads in
the household time zone so it keeps its time on the clock across
daylight-saving changes (decision 111). `reschedule_sync()` applies a change
to the schedule or to the household time zone to the running scheduler. In the split serve/worker
topology the API process runs no scheduler, so changes there apply at worker
restart — the /config API reports which via `sync_reschedule_live`.

A run that never happened is run once when Purser discovers it (issue #584,
decision 124), with nothing to configure:

- **The host slept through it.** A laptop asleep past a job's grace window
  wakes to a scheduler that drops the run. `_on_job_missed` hears that, and
  `run_missed` runs each dropped job once, in the registry's order, half a
  minute later (so every job the same wake dropped is collected first).
  `coalesce` has already folded several missed days into one.
- **Purser was not running.** A scheduler that starts after a slot never sees
  it. `check_sync_at_startup` asks the one question it can answer from stored
  facts: has every syncing connection seen a sync attempt (a fetch, or a
  recorded failure) since the latest sync slot before this process started?
  If not, it runs the sync and the matching pass that follows it, once.
"""

import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from apscheduler.events import EVENT_JOB_MISSED, JobEvent
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from purser.logging import get_logger

log = get_logger(__name__)

DEFAULT_SYNC_CRON: dict[str, str | int] = {"hour": 6, "minute": 30}
# The SimpleFIN Bridge asks apps to spread their fetches instead of all
# hitting it on the same minute (#483). APScheduler's jitter only delays, by
# 0..N seconds, so every preset's :30 run lands between :30 and :40 — still
# before the 06:45 matching job.
SYNC_JITTER_SECONDS = 600

# APScheduler's default `misfire_grace_time` is ONE SECOND: a job whose fire
# time slips past it — a loaded box, a paused container, a worker restart that
# straddles the slot — is dropped with no run and no error, only an
# APScheduler-level log line nothing surfaces (issue #185). An hour of grace
# absorbs that without resurrecting a job from a multi-day outage; the daily
# train is spaced 15 minutes apart, so a late run still lands in order.
# `coalesce` collapses a backlog into ONE run rather than a burst, and
# `max_instances` keeps a long pass (a big sync) from overlapping itself.
# All three are stated explicitly so an upstream default change cannot
# silently reintroduce the 1s window; tests/test_scheduler.py pins them.
JOB_DEFAULTS: dict[str, Any] = {
    "misfire_grace_time": 3600,
    "coalesce": True,
    "max_instances": 1,
}

# Catching up on runs that never happened (#584). The two one-off jobs are
# the scheduler's own, not part of the registry.
CATCH_UP_JOB = "scheduler.catch_up"
CATCH_UP_DELAY = timedelta(seconds=30)
STARTUP_CHECK_JOB = "feed.sync_startup_check"
STARTUP_CHECK_DELAY = timedelta(seconds=60)
# How far back a sync slot is looked for: the longest preset is daily.
_SLOT_LOOKBACK = timedelta(hours=25)
# Jobs that run again within the hour anyway: a dropped run needs no catch-up.
FREQUENT_JOBS = frozenset({"webhooks.delivery", "sessions.cleanup"})
# What a sync missed while Purser was down is followed by: the pass that
# matches what it imported. The rest of the morning train runs on its own
# schedule, and nothing stored says whether it ran.
_AFTER_A_LATE_SYNC = ("feed.sync_daily", "matching.run_daily")

_missed: set[str] = set()
_missed_lock = threading.Lock()
_started_at: datetime | None = None

_active: BackgroundScheduler | None = None

# (job_id, cron kwargs, callable) tuples registered by feature modules.
_JOBS: list[tuple[str, dict[str, str | int], Callable[[], None]]] = []


def register_job(job_id: str, cron: dict[str, str | int], fn: Callable[[], None]) -> None:
    _JOBS.append((job_id, cron, fn))


def _register_builtin_jobs() -> None:
    if _JOBS:
        return
    from purser.feed.service import sync_all_active
    from purser.schedules import detect_job, materialize_all_job, shortfall_scan_job

    # SimpleFIN fetch; the default (daily 06:30) is well inside the 24/day
    # Bridge budget. The actual cron comes from config at build time.
    register_job("feed.sync_daily", dict(DEFAULT_SYNC_CRON), sync_all_active)

    from purser.holdings.prices import fetch_daily as fetch_prices_daily

    # Daily closes for manually tracked positions (#390, decision 89), then a
    # re-projection of every manual account: after the 06:30 sync (its feed
    # snapshots feed the feed-implied prices) and well before the 07:30
    # net-worth materialization.
    register_job("prices.fetch_daily", {"hour": 6, "minute": 40}, fetch_prices_daily)

    from purser.matching import run_matching_job

    # Match imported rows against expecteds so late-bill aging and forecasts stay truthful.
    register_job("matching.run_daily", {"hour": 6, "minute": 45}, run_matching_job)

    # Materialize expected schedule instances, then scan projections.
    register_job("schedules.materialize_daily", {"hour": 7, "minute": 0}, materialize_all_job)
    # Recurrence detection (#234): after matching/auto-link has merged the
    # night's transfers (06:45) and before the shortfall scan, so a freshly
    # suggested pattern and a freshly materialized one never race.
    register_job("schedules.detect_daily", {"hour": 7, "minute": 5}, detect_job)
    register_job("forecast.shortfall_scan", {"hour": 7, "minute": 15}, shortfall_scan_job)

    from purser.assets import stale_check_job

    # An asset whose value is older than its revalue_after_days (#568).
    register_job("assets.stale_check", {"hour": 7, "minute": 20}, stale_check_job)
    from purser.networth import materialize_job

    # Nightly net_worth_daily materialization (Grafana's flagship source).
    register_job("networth.materialize_daily", {"hour": 7, "minute": 30}, materialize_job)

    from purser.webhooks import delivery_job

    # Late-bill aging + webhook fan-out/delivery with retries.
    register_job("webhooks.delivery", {"minute": "*/2"}, delivery_job)

    from purser.admin.service import cleanup_sessions_job

    # Hourly session hygiene: hard-delete dead login sessions nothing
    # references; stamp the attribution-anchored ones expired (DESIGN §9).
    register_job("sessions.cleanup", {"minute": 20}, cleanup_sessions_job)

    from purser import audit

    # Retention: prune audit rows past audit_retention_days (issue #122).
    # 08:15 stays clear of the 06:30-07:30 job train.
    register_job("audit.prune_daily", {"hour": 8, "minute": 15}, audit.prune_job)

    from purser.review import aged_sweep_job

    # The unreviewed set must not grow without bound (#175). 08:30 keeps it
    # clear of the 06:30-08:15 train; the sweep FIRING is itself the signal
    # that review has lapsed, so its count rides into whats_new.
    register_job("review.aged_sweep", {"hour": 8, "minute": 30}, aged_sweep_job)


def _sync_trigger() -> tuple[dict[str, str | int], str]:
    """The configured sync cron and the zone it reads in. Falls back to the
    default schedule when config is unreachable (no database in DB-less
    contexts, unmigrated schema)."""
    from purser import appsettings

    try:
        schedule = appsettings.sync_schedule()
    except Exception:
        log.warning("sync schedule unavailable; using default", cron=DEFAULT_SYNC_CRON)
        schedule = appsettings.DEFAULT_SYNC_SCHEDULE
    return schedule.to_cron(), _sync_zone(schedule.follows_household_zone)


def _sync_zone(household: bool) -> str:
    """The household time zone for the daily preset, UTC otherwise. Without
    the database the env default (PURSER_TIMEZONE) stands in for it."""
    if not household:
        return "UTC"
    from purser import clock
    from purser.config import get_settings

    try:
        return clock.timezone_name()
    except Exception:
        try:
            return get_settings().timezone
        except Exception:
            log.warning("household time zone unavailable; sync time read in UTC")
            return "UTC"


def _utc_now() -> datetime:
    """The current instant. The seam tests patch."""
    return datetime.now(UTC)


def build_scheduler() -> BackgroundScheduler:
    global _active, _started_at
    _register_builtin_jobs()
    scheduler = BackgroundScheduler(timezone="UTC", job_defaults=JOB_DEFAULTS)
    for job_id, cron, fn in _JOBS:
        extra: dict[str, int | str] = {}
        if job_id == "feed.sync_daily":
            cron, extra["timezone"] = _sync_trigger()
            extra["jitter"] = SYNC_JITTER_SECONDS
        scheduler.add_job(fn, "cron", id=job_id, **cron, **extra)
        log.info("scheduled job", job_id=job_id, cron=cron, timezone=extra.get("timezone", "UTC"))
    _started_at = _utc_now()
    with _missed_lock:
        _missed.clear()
    scheduler.add_listener(_on_job_missed, EVENT_JOB_MISSED)
    # Once, shortly after start: a sync slot that passed while Purser was not
    # running (#584). Slots after the start are the scheduler's own.
    scheduler.add_job(
        check_sync_at_startup,
        "date",
        id=STARTUP_CHECK_JOB,
        run_date=_started_at + STARTUP_CHECK_DELAY,
    )
    _active = scheduler
    return scheduler


def _on_job_missed(event: JobEvent) -> None:
    """A registered job's run was dropped for lateness (the host slept past
    its grace window): note it and run it once, shortly."""
    job_id = str(event.job_id)
    if job_id in FREQUENT_JOBS or job_id not in {name for name, _, _ in _JOBS}:
        return
    with _missed_lock:
        _missed.add(job_id)
    log.warning("scheduled run was missed; it will run now", job_id=job_id)
    if _active is not None:
        # replace_existing: one catch-up for everything the same wake dropped.
        _active.add_job(
            run_missed,
            "date",
            id=CATCH_UP_JOB,
            run_date=_utc_now() + CATCH_UP_DELAY,
            replace_existing=True,
        )


def run_missed() -> None:
    """Run every noted job once, in the registry's order (the sync before
    the matching that reads it, and so on). One failing never stops the rest."""
    with _missed_lock:
        due = set(_missed)
        _missed.clear()
    for job_id, _cron, fn in _JOBS:
        if job_id not in due:
            continue
        log.info("running a missed scheduled job", job_id=job_id)
        try:
            fn()
        except Exception:
            log.exception("missed scheduled job failed", job_id=job_id)


def sync_slot_before(moment: datetime) -> datetime | None:
    """The latest time the configured sync schedule names at or before
    ``moment`` (jitter aside), or None when the last day holds none."""
    cron, zone = _sync_trigger()
    trigger = CronTrigger(timezone=zone, **cron)
    slot: datetime | None = None
    fire = trigger.get_next_fire_time(None, moment - _SLOT_LOOKBACK)
    while fire is not None and fire <= moment:
        slot = fire
        # previous + a far-off "now": the next time strictly after ``fire``.
        fire = trigger.get_next_fire_time(fire, moment + _SLOT_LOOKBACK)
    return slot


def check_sync_at_startup() -> None:
    """Run the sync once when a slot passed while Purser was not running and
    a connection has seen no attempt since. Never raises."""
    if _started_at is None:
        return
    try:
        slot = sync_slot_before(_started_at)
        if slot is None:
            return
        from purser.feed.service import sync_owed_since

        if not sync_owed_since(slot):
            return
    except Exception:
        log.exception("startup sync check failed")
        return
    log.warning("a scheduled sync was missed while Purser was not running", slot=slot.isoformat())
    with _missed_lock:
        _missed.update(_AFTER_A_LATE_SYNC)
    run_missed()


def is_live() -> bool:
    """True when this process runs the scheduler (reschedules apply live)."""
    return _active is not None and _active.running


def reschedule_sync() -> bool:
    """Apply the configured sync schedule, read in the current household time
    zone, to the running scheduler (after a schedule or time zone change).

    Returns False when this process runs no scheduler (split serve/worker
    topology) — the new schedule then applies at worker restart.
    """
    if not is_live():
        return False
    assert _active is not None
    cron, zone = _sync_trigger()
    _active.reschedule_job(
        "feed.sync_daily",
        trigger=CronTrigger(timezone=zone, jitter=SYNC_JITTER_SECONDS, **cron),
    )
    log.info("rescheduled job", job_id="feed.sync_daily", cron=cron, timezone=zone)
    return True
