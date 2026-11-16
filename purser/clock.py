"""The ledger day: one definition of "today" for the whole package (#471).

Purser keeps two kinds of time apart (DESIGN decision 105):

- **Instants** — audit ``at``, ``fetched_at``, ``created_at``, token and
  session expiry — are UTC timestamps (``datetime.now(UTC)``) and stay that
  way everywhere.
- **The ledger day** — "today" for schedules, suggestions, matching, the
  digest, net worth, review, reconciliation, holdings and feed freshness — is
  the calendar date in the household time zone, the ``timezone`` setting
  (``PURSER_TIMEZONE``, runtime-editable, default ``UTC``).

Every calendar-day decision reads ``ledger_today()`` (or turns an instant
into a day with ``ledger_date()``); nothing calls ``date.today()``, which
follows the host's local zone, or ``datetime.now(UTC).date()``, which is the
UTC day. The scheduler's job times stay in UTC.

One frame is deliberately not the ledger day: the SimpleFIN fetch window
(``feed.service`` sync and outage repair). Imported rows are dated by their
epoch's UTC date and the Bridge counts its reach back from now, so that
window math uses the UTC date; feed *staleness* is a ledger-day judgment.
The SQL side (``readmodel.ledger_today()``, migration 0044) computes the
same day from the same setting.

Tests pin the clock by monkeypatching ``_utc_now``.
"""

from datetime import UTC, date, datetime
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

DEFAULT_TIMEZONE = "UTC"


def _utc_now() -> datetime:
    """The current instant (UTC). The single seam tests patch."""
    return datetime.now(UTC)


@lru_cache(maxsize=1)
def _known_zones() -> frozenset[str]:
    return frozenset(available_timezones())


def is_valid_timezone(name: str) -> bool:
    """An IANA zone name this Python knows (``UTC``, ``America/New_York``)."""
    if name not in _known_zones():
        return False
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def timezone_name() -> str:
    """The effective household time zone (env default + runtime override)."""
    from purser import appsettings

    return appsettings.effective().timezone


def ledger_zone() -> ZoneInfo:
    return ZoneInfo(timezone_name())


def ledger_now() -> datetime:
    """The current instant, expressed in the household time zone."""
    return _utc_now().astimezone(ledger_zone())


def ledger_today() -> date:
    """Today's ledger day: the calendar date in the household time zone."""
    return ledger_now().date()


def ledger_date(instant: datetime) -> date:
    """The ledger day an instant falls on (a naive value is taken as UTC)."""
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=UTC)
    return instant.astimezone(ledger_zone()).date()
