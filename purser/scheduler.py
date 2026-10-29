"""In-process job scheduler.

ALL jobs are registered centrally in `_register_builtin_jobs()` below —
there is no decentralized import-time registration by feature modules.
Adding a job means adding a `register_job` line there (and updating the
contract test in tests/test_scheduler.py). `purser serve` runs this
scheduler in-process; `purser worker` runs it alone.

The SimpleFIN sync job (`feed.sync_daily` — the id is a stable contract even
when the configured cadence is sub-daily) takes its cron from the persisted
config (`appsettings.sync_schedule()`) at build time, and `reschedule_sync()`
applies a config change to the running scheduler. In the split serve/worker
topology the API process runs no scheduler, so changes there apply at worker
restart — the /config API reports which via `sync_reschedule_live`.
"""

from collections.abc import Callable
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler

from purser.logging import get_logger

log = get_logger(__name__)

DEFAULT_SYNC_CRON: dict[str, str | int] = {"hour": 6, "minute": 30}

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


def _sync_cron() -> dict[str, str | int]:
    """The configured sync cron; falls back to the default when config is
    unreachable (no database in DB-less contexts, unmigrated schema)."""
    try:
        from purser import appsettings

        return appsettings.sync_schedule().to_cron()
    except Exception:
        log.warning("sync schedule unavailable; using default", cron=DEFAULT_SYNC_CRON)
        return dict(DEFAULT_SYNC_CRON)


def build_scheduler() -> BackgroundScheduler:
    global _active
    _register_builtin_jobs()
    scheduler = BackgroundScheduler(timezone="UTC", job_defaults=JOB_DEFAULTS)
    for job_id, cron, fn in _JOBS:
        if job_id == "feed.sync_daily":
            cron = _sync_cron()
        scheduler.add_job(fn, "cron", id=job_id, **cron)
        log.info("scheduled job", job_id=job_id, cron=cron)
    _active = scheduler
    return scheduler


def is_live() -> bool:
    """True when this process runs the scheduler (reschedules apply live)."""
    return _active is not None and _active.running


def reschedule_sync() -> bool:
    """Apply the configured sync schedule to the running scheduler.

    Returns False when this process runs no scheduler (split serve/worker
    topology) — the new schedule then applies at worker restart.
    """
    if not is_live():
        return False
    from apscheduler.triggers.cron import CronTrigger

    assert _active is not None
    cron = _sync_cron()
    _active.reschedule_job("feed.sync_daily", trigger=CronTrigger(timezone="UTC", **cron))
    log.info("rescheduled job", job_id="feed.sync_daily", cron=cron)
    return True
