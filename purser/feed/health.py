"""Feed freshness and bank errors, per bank login and per account (#480).

One SimpleFIN connection carries many bank logins, and a fetch succeeds
(HTTP 200) while a bank behind it has stopped updating or needs a new login.
After every sync this module records what is wrong *now* and emits an event
when a problem starts or clears:

- ``feed.auth_required`` — a bank login reports a ``con.*`` issue
  (``con.auth``: log in again at the bank);
- ``feed.account_failed`` — an account reports an ``act.*`` issue
  (``act.failed``, ``act.missingdata``) on ``FAILED_STREAK`` syncs in a row;
- ``feed.stale`` — a mapped account's ``balance-date`` is older than
  ``feed_stale_days``;
- ``feed.recovered`` — one of the above clears (``kind`` says which).

The cross-sync memory is a ``feed_health`` block on the ``connection.sync``
audit row (the drift check's no-schema pattern, DESIGN §7.3): backfill audits
as ``connection.backfill`` and a failed sync writes no row, so only completed
syncs count. A stale episode is keyed by its frozen ``balance-date`` with
``events.emit_once``, so it fires once however the syncs interleave.
"""

import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from purser import clock, events
from purser.feed import drift, envelope
from purser.logging import get_logger
from purser.models import Account, AuditLog, Connection, RawFetch

log = get_logger(__name__)

BLOCK_KEY = "feed_health"
# Consecutive syncs an account must report an act.* issue before it is an
# episode: the spec says "try again later", and one-off failures are common.
FAILED_STREAK = 2


@dataclass
class Observation:
    """What is wrong at one sync. Keys are SimpleFIN ids (``conn_id`` for
    logins, the account id for accounts); values carry what an answer needs
    to name the problem without decrypting the fetch again."""

    logins: dict[str, dict[str, str]] = field(default_factory=dict)
    failing: dict[str, dict[str, str]] = field(default_factory=dict)
    stale: dict[str, dict[str, str]] = field(default_factory=dict)
    # Outage repair (#481): every mapped account's balance date at this sync,
    # and the re-fetches still owed (account id -> first date to fetch) when
    # a repair could not run yet (fetch budget, transport error).
    balance_dates: dict[str, str] = field(default_factory=dict)
    repairs: dict[str, str] = field(default_factory=dict)

    def as_block(self) -> dict[str, Any]:
        return {
            "logins": self.logins,
            "failing": self.failing,
            "stale": self.stale,
            "balance_dates": self.balance_dates,
            "repairs": self.repairs,
        }

    @classmethod
    def from_block(cls, block: Any) -> "Observation":
        if not isinstance(block, dict):
            return cls()

        def part(key: str) -> dict[str, dict[str, str]]:
            value = block.get(key)
            return value if isinstance(value, dict) else {}

        def dates(key: str) -> dict[str, str]:
            value = block.get(key)
            if not isinstance(value, dict):
                return {}
            return {str(k): v for k, v in value.items() if isinstance(v, str)}

        return cls(
            logins=part("logins"),
            failing=part("failing"),
            stale=part("stale"),
            balance_dates=dates("balance_dates"),
            repairs=dates("repairs"),
        )


def observe(
    session: Session,
    *,
    connection: Connection,
    payload: dict[str, Any],
    today: date,
    stale_days: int,
) -> Observation:
    """Problems in one fetch. Only accounts mapped to an open Purser account
    count for ``failing`` and ``stale``: an unmapped account is not tracked.
    Login issues count whether or not their accounts are mapped."""
    mapped = {
        account.external_id: account
        for account in session.execute(
            select(Account).where(
                Account.connection_id == connection.id,
                Account.closed_at.is_(None),
                Account.external_id.is_not(None),
            )
        ).scalars()
    }
    by_id = {
        sf_account.get("id"): sf_account
        for sf_account in payload.get("accounts") or []
        if isinstance(sf_account, dict)
    }

    def institution_name(sf_account: dict[str, Any]) -> str:
        found = envelope.institution_of(payload, sf_account)
        return (found.name or found.conn_id or "") if found else ""

    seen = Observation()
    for issue in envelope.issues(payload):
        if issue.prefix == "con" and issue.conn_id:
            # Keyed by the issue's own id, as every earlier sync's block is:
            # the key is the episode memory. Only the name is looked up
            # through the login (the two ids can differ by a prefix).
            login = envelope.login_of_issue(payload, issue)
            seen.logins[issue.conn_id] = {
                "institution": (login.name if login else None) or issue.conn_id,
                "code": issue.code,
                "message": issue.msg,
            }
        elif issue.prefix == "act" and issue.account_id and issue.account_id in mapped:
            failing_account = mapped[issue.account_id]
            sf_account = by_id.get(issue.account_id) or {}
            seen.failing[issue.account_id] = {
                "account": failing_account.path,
                "institution": institution_name(sf_account),
                "code": issue.code,
                "message": issue.msg,
            }
    for external_id, sf_account in by_id.items():
        account = mapped.get(external_id)
        balance_date = drift.parse_balance_date(sf_account.get("balance-date"))
        if account is None or balance_date is None:
            continue
        seen.balance_dates[str(external_id)] = balance_date.isoformat()
        if (today - balance_date).days > stale_days:
            seen.stale[str(external_id)] = {
                "account": account.path,
                "institution": institution_name(sf_account),
                "balance_date": balance_date.isoformat(),
            }
    return seen


def previous_observations(
    session: Session, connection_id: uuid.UUID, *, limit: int = FAILED_STREAK
) -> list[Observation]:
    """The ``feed_health`` blocks of the connection's most recent completed
    syncs, newest first; a row from before #480 reads as healthy."""
    rows = session.execute(
        select(AuditLog)
        .where(
            AuditLog.action == "connection.sync",
            AuditLog.entity_type == "connection",
            AuditLog.entity_id == connection_id,
        )
        .order_by(AuditLog.at.desc(), AuditLog.id.desc())
        .limit(limit)
    ).scalars()
    return [
        Observation.from_block(((row.before_after or {}).get("after") or {}).get(BLOCK_KEY))
        for row in rows
    ]


def check_after_sync(
    session: Session,
    *,
    connection: Connection,
    payload: dict[str, Any],
    raw_fetch: RawFetch,
    today: date,
    stale_days: int,
    window_start: date | None = None,
) -> dict[str, Any]:
    """Observe this fetch, emit the transitions against the previous syncs,
    and return the block the caller stores on the ``connection.sync`` audit
    row. Called before that row is written, so the lookup sees only earlier
    syncs. Never raises: the work runs in a savepoint, and a failure rolls
    back its events and returns an empty block, so a bug here cannot fail a
    sync."""
    try:
        with session.begin_nested():
            return _check(
                session,
                connection=connection,
                payload=payload,
                today=today,
                stale_days=stale_days,
                window_start=window_start,
            )
    except Exception:
        log.exception("feed health check failed; sync unaffected")
        return {}


def _check(
    session: Session,
    *,
    connection: Connection,
    payload: dict[str, Any],
    today: date,
    stale_days: int,
    window_start: date | None,
) -> dict[str, Any]:
    now = observe(
        session, connection=connection, payload=payload, today=today, stale_days=stale_days
    )
    history = previous_observations(session, connection.id)
    prev = history[0] if history else Observation()
    prev2 = history[1] if len(history) > 1 else Observation()

    def emit(type_: str, payload_: dict[str, Any]) -> None:
        events.emit(
            session,
            type_=type_,
            entity_type="connection",
            entity_id=connection.id,
            payload=payload_,
        )

    for conn_id, login in now.logins.items():
        if conn_id not in prev.logins:
            emit("feed.auth_required", {"conn_id": conn_id, **login})
    for conn_id, login in prev.logins.items():
        if conn_id not in now.logins:
            emit(
                "feed.recovered",
                {"kind": "auth", "conn_id": conn_id, "institution": login.get("institution")},
            )

    # An account failing on this sync and the previous one opens an episode
    # the first time the streak reaches FAILED_STREAK; it recovers on the
    # first clean sync after one.
    for account_id, failing in now.failing.items():
        if account_id in prev.failing and account_id not in prev2.failing:
            emit("feed.account_failed", failing)
    for account_id, failing in prev.failing.items():
        if account_id not in now.failing and account_id in prev2.failing:
            emit(
                "feed.recovered",
                {
                    "kind": "account_failed",
                    "account": failing.get("account"),
                    "institution": failing.get("institution"),
                },
            )

    for external_id, stale in now.stale.items():
        lag = (today - date.fromisoformat(stale["balance_date"])).days
        events.emit_once(
            session,
            type_="feed.stale",
            # One episode per frozen balance date: a bank that updates and
            # then freezes again opens a new one.
            dedup_key=f"{connection.id}:{external_id}:{stale['balance_date']}",
            entity_type="connection",
            entity_id=connection.id,
            payload={**stale, "stale_days": lag},
        )
    by_id = {
        sf_account.get("id"): sf_account
        for sf_account in payload.get("accounts") or []
        if isinstance(sf_account, dict)
    }
    for external_id, stale in prev.stale.items():
        if external_id in now.stale or external_id not in by_id:
            continue  # still frozen, or not in this fetch (unmapped, closed, gone)
        fresh = drift.parse_balance_date(by_id[external_id].get("balance-date"))
        emit(
            "feed.recovered",
            {
                "kind": "stale",
                "account": stale.get("account"),
                "institution": stale.get("institution"),
                "balance_date": fresh.isoformat() if fresh else None,
                "stale_since": stale.get("balance_date"),
            },
        )
    now.repairs = repairs_due(prev, now, window_start)
    session.flush()
    return now.as_block()


def repairs_due(prev: Observation, now: Observation, window_start: date | None) -> dict[str, str]:
    """Accounts whose outage left rows this sync's window did not reach (#481).

    A bank that stops updating still answers every fetch, so ``last_fetch_at``
    keeps moving and the next sync's window (``last_fetch_at`` less the resync
    overlap) never reaches back to the outage. When an account's balance date
    moves forward from a date the previous sync saw, and that date is before
    this sync's window, the rows between were never requested: the repair
    re-fetches from that date. Repairs a previous sync could not run are
    carried until they do, for accounts still mapped."""
    due = {ext: start for ext, start in prev.repairs.items() if ext in now.balance_dates}
    if window_start is None:
        return due
    for ext, current in now.balance_dates.items():
        before = prev.balance_dates.get(ext)
        if before is None or current <= before:
            continue
        if date.fromisoformat(before) < window_start:
            due[ext] = min(due.get(ext, before), before)
    return due


def open_problems(session: Session) -> dict[str, list[dict[str, Any]]]:
    """What is wrong now across every connection, from each one's latest
    completed sync: logins that need the user, accounts failing, accounts
    stale (with how many days old their balance is)."""
    out: dict[str, list[dict[str, Any]]] = {"needs_login": [], "failing": [], "stale": []}
    today = clock.ledger_today()
    for connection_id in session.execute(
        select(Connection.id).where(Connection.status != "disabled").order_by(Connection.created_at)
    ).scalars():
        latest = previous_observations(session, connection_id, limit=1)
        if not latest:
            continue
        seen = latest[0]
        out["needs_login"].extend(
            {k: login.get(k) for k in ("institution", "code", "message")}
            for login in seen.logins.values()
        )
        out["failing"].extend(
            {k: failing.get(k) for k in ("account", "institution", "code", "message")}
            for failing in seen.failing.values()
        )
        for stale in seen.stale.values():
            balance_date = stale.get("balance_date")
            out["stale"].append(
                {
                    "account": stale.get("account"),
                    "institution": stale.get("institution"),
                    "balance_date": balance_date,
                    "days": (today - date.fromisoformat(balance_date)).days
                    if balance_date
                    else None,
                }
            )
    return out
