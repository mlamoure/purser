"""Post-sync balance-drift check (issue #149, DESIGN §7.3).

After every successful sync, compare the feed-reported per-account ``balance``
against the ledger for mapped, open, cash/card accounts and surface drift as a
``balance.drift`` warning event — never a gate, never an auto-adjustment, no
ledger writes. The cross-sync memory (for the consecutive-sync emission gate,
the once-per-episode dedup, and the per-account basis choice of issue #283) is
the ``balance_drift`` observation block the caller stores on the
``connection.sync`` audit row: no schema change.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from purser import events
from purser.logging import get_logger
from purser.models import Account, AuditLog, Connection, Posting, RawFetch, Transaction

log = get_logger(__name__)

# loan/property consume the reported balance as valuations (importer.
# _record_balance_valuation, DESIGN §8); brokerage balances are market value
# (holdings-driven, §8). Drift compares postings sums, so cash/card only.
ELIGIBLE_TYPES = frozenset({"checking", "savings", "cash", "credit_card"})

_QUANT = Decimal("0.0001")  # canonical NUMERIC(20,4) string form

# The two ledger bases a bank's reported balance can track: banks differ on
# whether pendings are in it. Pending-inclusive is Purser's primary balance
# definition everywhere else, hence the tie-break default.
PENDING_INCLUSIVE = "pending_inclusive"
POSTED_ONLY = "posted_only"
BASES = (PENDING_INCLUSIVE, POSTED_ONLY)

# Basis selection (issue #283, decision 64): an account's basis is chosen from
# its observations in this many recent successful syncs, this one included.
BASIS_WINDOW_SYNCS = 20


def canonical(value: Decimal) -> str:
    """One canonical string for a delta — used identically in the observation
    block, the event payload, and the dedup key so they can never disagree."""
    return str(value.quantize(_QUANT))


@dataclass(frozen=True)
class DriftComparison:
    reported: Decimal
    posted: Decimal  # posted-only postings sum
    pending: Decimal  # pending-inclusive postings sum
    delta: Decimal  # reported minus the ledger on ``basis``; 0 == clean
    basis: str  # PENDING_INCLUSIVE | POSTED_ONLY — the account's chosen basis
    balance_date: date | None
    # Issue #307 (decision 83): ``pinned`` = ``basis`` is the account's
    # ``drift_basis_override``; ``learned_basis`` is what the history rule
    # picks either way (== ``basis`` when unpinned).
    pinned: bool = False
    learned_basis: str = PENDING_INCLUSIVE


def parse_balance_date(raw: object) -> date | None:
    """SimpleFIN ``balance-date`` (unix epoch) -> UTC date; None when absent or
    garbage (the field is optional in practice — no fast-path gate then)."""
    if isinstance(raw, bool) or not isinstance(raw, int | float | str):
        return None
    try:
        ts = int(raw)
    except ValueError:
        return None
    if ts <= 0:
        return None
    return datetime.fromtimestamp(ts, tz=UTC).date()


def _parse_reported(sf_account: dict[str, Any]) -> Decimal | None:
    raw = sf_account.get("balance")
    if raw is None:
        return None
    try:
        value = Decimal(str(raw))
    except InvalidOperation:
        return None
    # NaN/Infinity would poison every comparison and the stored history.
    return value if value.is_finite() else None


def _eligible(account: Account) -> bool:
    return account.type in ELIGIBLE_TYPES and account.closed_at is None


def _ledger_sums(
    session: Session, account_ids: list[uuid.UUID]
) -> dict[uuid.UUID, tuple[Decimal, Decimal, date | None]]:
    """(posted_only, pending_inclusive, last_activity) per account in one pass.

    Live SUMs, deliberately not the stored running balances: those are only
    recomputed by the before_commit hook, so they are stale inside the sync
    transaction (and pending-inclusive only).
    """
    if not account_ids:
        return {}
    posted_sum = func.coalesce(func.sum(Posting.amount).filter(Transaction.status == "posted"), 0)
    rows = session.execute(
        select(
            Posting.account_id,
            posted_sum,
            func.coalesce(func.sum(Posting.amount), 0),
            func.max(Transaction.date),
        )
        .join(Transaction, Transaction.id == Posting.transaction_id)
        .where(
            Posting.account_id.in_(account_ids),
            Transaction.status.in_(["pending", "posted"]),
        )
        .group_by(Posting.account_id)
    )
    return {row[0]: (Decimal(row[1]), Decimal(row[2]), row[3]) for row in rows}


def basis_deltas(
    *, reported: Decimal, posted: Decimal, pending: Decimal
) -> tuple[Decimal, Decimal]:
    """``(posted_only, pending_inclusive)`` deltas: reported minus each
    ledger basis."""
    return reported - posted, reported - pending


def choose_basis(history: Sequence[tuple[Decimal, Decimal]], *, previous: str | None) -> str:
    """Choose an account's drift basis from its recent sync history (issue
    #283, decision 64) rather than per sync.

    ``history`` is the account's non-empty ``basis_deltas`` pairs, oldest
    first, the current comparison last. The basis whose delta changed on fewer
    consecutive-sync steps wins: a bank whose balance leaves pendings out holds
    a constant posted-only delta while the pending-inclusive one moves with
    every pending charge (and the reverse for a bank that includes them), and
    a moving reported delta is exactly what opens new ``balance.drift``
    episodes. Changes rather than distinct values, so a pending that keeps
    coming and going counts on every step. Equal counts — no history yet, or
    two bases that never moved apart — keep the basis the previous sync chose;
    with none recorded, the per-sync rule decides: the smaller ``|delta|``, tie
    -> pending-inclusive.
    """
    posted_changes = sum(1 for before, after in pairwise(history) if before[0] != after[0])
    pending_changes = sum(1 for before, after in pairwise(history) if before[1] != after[1])
    if posted_changes != pending_changes:
        return POSTED_ONLY if posted_changes < pending_changes else PENDING_INCLUSIVE
    if previous is not None and previous in BASES:
        return previous
    delta_posted, delta_pending = history[-1]
    return PENDING_INCLUSIVE if abs(delta_pending) <= abs(delta_posted) else POSTED_ONLY


def compare(
    *,
    reported: Decimal,
    posted: Decimal,
    pending: Decimal,
    balance_date: date | None,
    basis: str,
    pinned: bool = False,
    learned_basis: str | None = None,
) -> DriftComparison:
    """Dual-basis comparison: banks differ on whether their reported balance
    includes pendings, so the account is clean iff EITHER basis matches exactly
    (exact Decimal numeric equality — 950.00 == 950.0000). Otherwise the drift
    is the delta on the account's ``basis`` (``choose_basis``, or the pin)."""
    delta_posted, delta_pending = basis_deltas(reported=reported, posted=posted, pending=pending)
    if delta_posted == 0 or delta_pending == 0:
        delta = Decimal(0)
    else:
        delta = delta_posted if basis == POSTED_ONLY else delta_pending
    return DriftComparison(
        reported=reported,
        posted=posted,
        pending=pending,
        delta=delta,
        basis=basis,
        balance_date=balance_date,
        pinned=pinned,
        learned_basis=learned_basis if learned_basis is not None else basis,
    )


def pinned_basis(account: Account) -> str | None:
    """The account's drift-basis pin (issue #307, decision 83), or None when
    the history rule decides. An unknown stored value (the CHECK forbids
    one) is ignored rather than trusted."""
    override = account.drift_basis_override
    return override if override in BASES else None


def _stored_deltas(obs: object) -> tuple[Decimal, Decimal] | None:
    """One stored observation's ``basis_deltas``; None when malformed."""
    if not isinstance(obs, dict):
        return None
    try:
        reported, posted, pending = (
            Decimal(str(obs[key])) for key in ("reported", "posted", "pending")
        )
    except (KeyError, InvalidOperation):
        return None
    if not all(value.is_finite() for value in (reported, posted, pending)):
        return None
    return basis_deltas(reported=reported, posted=posted, pending=pending)


def compare_with_history(
    blocks: Sequence[dict[str, Any]],
    *,
    account_id: uuid.UUID,
    reported: Decimal,
    posted: Decimal,
    pending: Decimal,
    balance_date: date | None,
    pinned_basis: str | None = None,
) -> DriftComparison:
    """``compare`` on the basis the account's history selects, or on
    ``pinned_basis`` when the account has a pin (issue #307, decision 83).
    ``blocks`` are the connection's recent observation blocks, newest first
    (``recent_blocks``); the current comparison is the last history step.

    The history rule runs either way, so a pin never interrupts it: the
    tie-break reads the previous observation's ``learned`` basis when that
    observation was pinned (its ``basis`` was the pin, not evidence), and its
    ``basis`` otherwise. Clearing a pin therefore resumes exactly where the
    history would be, with nothing inherited from the pin."""
    key = str(account_id)
    history: list[tuple[Decimal, Decimal]] = []
    previous: str | None = None
    for block in blocks:  # newest first
        obs = block.get(key)
        stored = _stored_deltas(obs)
        if stored is None:
            continue
        history.append(stored)
        if previous is None and isinstance(obs, dict):
            recorded = obs.get("learned") if obs.get("pinned") else obs.get("basis")
            if recorded in BASES:
                previous = str(recorded)
    history.reverse()  # oldest first
    history.append(basis_deltas(reported=reported, posted=posted, pending=pending))
    learned = choose_basis(history, previous=previous)
    pinned = pinned_basis in BASES
    return compare(
        reported=reported,
        posted=posted,
        pending=pending,
        balance_date=balance_date,
        basis=str(pinned_basis) if pinned else learned,
        pinned=pinned,
        learned_basis=learned,
    )


def live_compare(
    session: Session,
    *,
    account: Account,
    sf_account: dict[str, Any],
    blocks: Sequence[dict[str, Any]] | None = None,
) -> DriftComparison | None:
    """Ungated reported-vs-ledger comparison for the discovery surface. The
    Config tab shows the current raw drift immediately; only the event stream
    is gated (deliberate asymmetry, DESIGN §7.3). The basis comes from the
    same sync history the post-sync check uses, or from the account's pin;
    pass the connection's ``recent_blocks`` when comparing several accounts to
    read them once."""
    if not _eligible(account):
        return None
    reported = _parse_reported(sf_account)
    if reported is None:
        return None
    posted, pending, _ = _ledger_sums(session, [account.id]).get(
        account.id, (Decimal(0), Decimal(0), None)
    )
    if blocks is None:
        blocks = (
            recent_blocks(session, account.connection_id)
            if account.connection_id is not None
            else []
        )
    return compare_with_history(
        blocks,
        account_id=account.id,
        reported=reported,
        posted=posted,
        pending=pending,
        balance_date=parse_balance_date(sf_account.get("balance-date")),
        pinned_basis=pinned_basis(account),
    )


def check_after_sync(
    session: Session,
    *,
    connection: Connection,
    payload: dict[str, Any],
    raw_fetch: RawFetch,
) -> dict[str, Any]:
    """Run the drift check at the end of a successful sync (and only there —
    backfill pages and map re-imports never see it).

    Returns the per-account observation block the caller stores on the
    ``connection.sync`` audit row. Never raises: a drift bug must not fail a
    sync.
    """
    try:
        return _check(session, connection=connection, payload=payload, raw_fetch=raw_fetch)
    except Exception:
        log.exception("balance-drift check failed; sync unaffected")
        return {}


def _check(
    session: Session,
    *,
    connection: Connection,
    payload: dict[str, Any],
    raw_fetch: RawFetch,
) -> dict[str, Any]:
    # Newest first: [0] is the previous sync (emission gate + episode memory);
    # the rest of the window feeds the per-account basis choice (#283).
    blocks = recent_blocks(session, connection.id)
    prev = blocks[0] if blocks else {}

    eligible: list[tuple[dict[str, Any], Account, Decimal]] = []
    for sf_account in payload.get("accounts", []):
        account = session.execute(
            select(Account).where(
                Account.connection_id == connection.id,
                Account.external_id == sf_account["id"],
            )
        ).scalar_one_or_none()
        if account is None or not _eligible(account):
            continue
        reported = _parse_reported(sf_account)
        if reported is None:
            continue
        eligible.append((sf_account, account, reported))

    sums = _ledger_sums(session, [account.id for _, account, _ in eligible])
    block: dict[str, Any] = {}
    for sf_account, account, reported in eligible:
        try:
            posted, pending, last_activity = sums.get(account.id, (Decimal(0), Decimal(0), None))
            comp = compare_with_history(
                blocks,
                account_id=account.id,
                reported=reported,
                posted=posted,
                pending=pending,
                balance_date=parse_balance_date(sf_account.get("balance-date")),
                pinned_basis=pinned_basis(account),
            )
            block[str(account.id)] = _observe(
                session,
                account=account,
                comp=comp,
                last_activity=last_activity,
                prev_obs=prev.get(str(account.id)),
                raw_fetch=raw_fetch,
                currency=str(sf_account.get("currency", "USD")),
            )
        except Exception:
            log.exception("balance-drift check failed for account %s; skipped", account.id)

    # Carry forward episode state for accounts absent from this payload: one
    # payload hiccup must not amnesia an episode (a re-appearing account would
    # otherwise duplicate-emit under the fast path). Clean observations carry
    # no episode state, so only candidate/emitted entries are kept.
    for account_id, obs in prev.items():
        if account_id not in block and (obs.get("delta") is not None or obs.get("emitted")):
            block[account_id] = obs
    return block


def recent_blocks(
    session: Session, connection_id: uuid.UUID, *, limit: int = BASIS_WINDOW_SYNCS - 1
) -> list[dict[str, Any]]:
    """The ``balance_drift`` blocks of the connection's most recent successful
    syncs, newest first (``{}`` for a row that carries none) — the no-schema
    cross-sync memory (DESIGN §7.3). The default window is the previous
    ``BASIS_WINDOW_SYNCS - 1`` syncs, so with the current comparison the basis
    choice sees ``BASIS_WINDOW_SYNCS`` observations. Backfill audits as
    ``connection.backfill``, so it never pollutes this lookup; failed syncs
    write no ``connection.sync`` row at all."""
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
    blocks: list[dict[str, Any]] = []
    for row in rows:
        got = ((row.before_after or {}).get("after") or {}).get("balance_drift")
        blocks.append(got if isinstance(got, dict) else {})
    return blocks


def _previous_observations(session: Session, connection_id: uuid.UUID) -> dict[str, Any]:
    """The ``balance_drift`` block from the previous successful sync's audit
    row (``recent_blocks`` with a window of one)."""
    blocks = recent_blocks(session, connection_id, limit=1)
    return blocks[0] if blocks else {}


def _observe(
    session: Session,
    *,
    account: Account,
    comp: DriftComparison,
    last_activity: date | None,
    prev_obs: dict[str, Any] | None,
    raw_fetch: RawFetch,
    currency: str,
) -> dict[str, Any]:
    """One account's state-machine step: CLEAN / CANDIDATE / DRIFTING.

    Emission gate (in-flight transactions must not false-positive): emit only
    when nothing can be in flight (no ledger activity), the bank's number is
    STRICTLY fresher than all ledger activity (same-day is ambiguous at date
    granularity — onboarding wobbles must not emit per-sync), or the identical
    delta was observed by the previous sync. Episode dedup: within a continuous
    same-delta episode exactly one event is emitted (the carried ``emitted``
    flag); a resolved-then-reappeared delta is a new episode and alerts again.
    """
    obs: dict[str, Any] = {
        "delta": None,
        "emitted": False,
        "reported": str(comp.reported),
        "posted": canonical(comp.posted),
        "pending": canonical(comp.pending),
        "balance_date": comp.balance_date.isoformat() if comp.balance_date else None,
        # Recorded on every observation, clean ones too: the basis choice's
        # tie-break is the previous sync's basis (decision 64).
        "basis": comp.basis,
    }
    if comp.pinned:
        # Only on pinned observations, so unpinned blocks are unchanged: the
        # learned basis keeps the history rule's tie-break running through
        # the pin (issue #307, decision 83).
        obs["pinned"] = True
        obs["learned"] = comp.learned_basis
    if comp.delta == 0:
        return obs
    delta_str = canonical(comp.delta)
    obs["delta"] = delta_str

    prev_delta = prev_obs.get("delta") if prev_obs else None
    prev_emitted = bool(prev_obs.get("emitted")) if prev_obs else False

    confirmed = (
        last_activity is None
        or (comp.balance_date is not None and comp.balance_date > last_activity)
        or prev_delta == delta_str
    )
    if not confirmed:
        return obs  # CANDIDATE: recorded for next sync's consecutive rule, no event
    if prev_emitted and prev_delta == delta_str:
        obs["emitted"] = True  # same episode, already alerted — carry the flag
        return obs

    payload: dict[str, Any] = {
        "account": str(account.path),
        "reported": str(comp.reported),
        "ledger_posted": canonical(comp.posted),
        "ledger_pending": canonical(comp.pending),
        "delta": delta_str,
        "mode": comp.basis,  # the basis behind delta (key predates #283)
        "balance_date": obs["balance_date"],
        "raw_fetch_id": str(raw_fetch.id),
        "currency": currency,
    }
    if comp.pinned:
        payload["basis_pinned"] = True  # ``mode`` is the account's pin (#307)
    events.emit(
        session,
        type_="balance.drift",
        entity_type="account",
        entity_id=account.id,
        payload=payload,
        dedup_key=f"{account.id}:{delta_str}",
    )
    obs["emitted"] = True
    return obs
