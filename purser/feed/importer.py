"""Importer: raw SimpleFIN payloads -> ledger rows (DESIGN §7, §4).

Identity is content_hash (external account + date + amount + normalized
description, plus the row's occurrence among identical rows in the payload)
— SimpleFIN ids are unstable and never trusted. Idempotent by construction:
a hash that already exists is skipped, so re-running any import produces zero
new rows. Two identical same-day charges in one fetch are occurrences 1 and 2,
two identities (issue #275, decision 68). The one exception to the skip is a
POSTED row whose identity an open pending holds: the bank posted that pending
without changing its date, amount or description, so the identity moves to
the posted row and the pending is superseded by it (#282) — after which the
hash is taken again and the next run skips it as usual.

The description is part of the identity, so a bank that re-describes a posted
row makes it a new identity. In a fetch's tail — rows older than its resync
overlap, which a sync reaches only while widened for an open pending — such a
row is recognised by the original it replaces and skipped as re-described
(#331, decision 74).
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from purser import audit, balances, crypto, ledger
from purser import rules as rules_service
from purser.core import ledger as core
from purser.feed import reconcile
from purser.logging import get_logger
from purser.models import Account, Posting, RawFetch, Transaction

log = get_logger(__name__)

UNCATEGORIZED_EXPENSE = "expenses.uncategorized"
UNCATEGORIZED_INCOME = "income.uncategorized"

# Every sync re-fetches at least this many days behind its previous fetch so
# pending->posted transitions land (DESIGN §7.1). A row older than the overlap
# of the fetch that delivered it is in that fetch's tail (#331, decision 74).
RESYNC_OVERLAP_DAYS = 7


def _uncategorized(session: Session, *, income: bool) -> Account:
    path = UNCATEGORIZED_INCOME if income else UNCATEGORIZED_EXPENSE
    account = session.execute(select(Account).where(Account.path == path)).scalar_one_or_none()
    if account is None:
        account = ledger.create_account(
            session,
            name="Uncategorized income" if income else "Uncategorized",
            type_="category_income" if income else "category_expense",
            path=path,
            actor_token_id=None,
        )
    return account


def category_for_new_row(
    session: Session, *, payee_name: str | None, income: bool
) -> tuple[Account, bool]:
    """Deterministic categorization at import time (DESIGN §11): rows are
    BORN categorized when the payee's accepted history already knows the
    answer — no posted-row correction ever needed. Returns (category,
    from_memory)."""
    if payee_name:
        # Function-local import: categorize imports this module's constants.
        from purser import categorize

        payee = ledger.resolve_payee(session, payee_name)
        remembered = categorize.payee_memory(session, payee.id)
        if remembered is not None:
            category = session.get(Account, remembered)
            if category is not None:
                return category, True
    return _uncategorized(session, income=income), False


def _tx_date(sf_tx: dict[str, Any]) -> datetime:
    ts = sf_tx.get("posted") or sf_tx.get("transacted_at") or 0
    return datetime.fromtimestamp(int(ts), tz=UTC)


def _row_hashes(sf_account: dict[str, Any]) -> list[str]:
    """The stored identity of every transaction in one account's payload, in
    payload order (DESIGN §7.1, decision 68).

    The hash is over the content the bank sent — the raw amount, before any
    ``feed_invert`` (decision 43) — and the n-th row identical to an earlier
    one in this payload is occurrence n, so two same-day charges at one
    merchant for one amount are two rows, not one (#275). Occurrence 1 is
    the identity every row had before the ordinal existed, so history already
    in the ledger keeps its hash; the count restarts in every payload, so a
    re-fetched overlap lands on hashes that already exist.

    The import loop and the vanished-pending sweep both read this one list,
    so a pending's stored identity and the identity the sweep looks for
    cannot drift apart.
    """
    external_id = str(sf_account["id"])

    def identity(sf_tx: dict[str, Any], occurrence: int = 1) -> str:
        return core.content_hash(
            external_account_id=external_id,
            date=_tx_date(sf_tx).date(),
            amount=Decimal(str(sf_tx["amount"])),
            description=str(sf_tx.get("description") or ""),
            occurrence=occurrence,
        )

    transactions = sf_account.get("transactions", [])
    # "Identical" means the same occurrence-1 hash, so the ordinal can never
    # disagree with content_hash about which rows are the same content.
    firsts = [identity(sf_tx) for sf_tx in transactions]
    return [
        # keyed_hash composes the sha256 identity into the hardened form
        # stored in transactions.content_hash (#137); the recipe is unchanged.
        crypto.keyed_hash(first if occurrence == 1 else identity(sf_tx, occurrence))
        for sf_tx, first, occurrence in zip(
            transactions, firsts, core.occurrence_ordinals(firsts), strict=True
        )
    ]


def import_fetch(
    session: Session,
    raw_fetch: RawFetch,
    *,
    window_start: date | None = None,
    replay: bool = False,
    tail_before: date | None = None,
) -> dict[str, Any]:
    """Import every mapped account's transactions from an archived payload.

    Safe to run repeatedly over the same or overlapping payloads.

    ``window_start`` is the first date the payload covers (the ``start`` the
    fetch was made with). When set, vanished-pending reconciliation only
    considers pendings dated inside the window — a pending older than the
    window is absent from the payload because it wasn't fetched, not because
    the bank dropped it. When ``None`` (archive re-imports, e.g. after mapping
    an account), reconciliation is skipped entirely: those re-imports are about
    importing rows for a newly mapped account, not pending lifecycle. A posted
    row that takes over its pending's own identity is not reconciliation — it
    is the row itself saying it posted — so it supersedes the pending on every
    import path (#282); those voids count in ``voided_pendings`` too.

    ``replay`` is set by the archive replay (decision 45) only: it re-derives
    ledger rows and records no holdings snapshot and no balance valuation.
    Those describe the moment the fetch was made, which the live import
    already recorded, and ``_record_balance_valuation`` compares a fetch's
    balance with the account's LATEST valuation, so replaying an old fetch
    would insert its old balance again as a new historical valuation (#328).

    ``tail_before`` starts the payload's tail: posted rows dated before it are
    checked for re-description (#331, decision 74) and counted in
    ``skipped_redescribed``, with one entry per skipped row in ``redescribed``.
    A sync passes the start its resync overlap would have had, so only rows
    its widening reached are checked. Any other import of a fetch (backfill,
    map re-import, replay) takes the overlap behind the fetch itself:
    ``fetched_at`` less ``RESYNC_OVERLAP_DAYS``. That is never earlier than
    the boundary the fetch's sync used, so re-importing an archived fetch
    skips at least what its sync skipped, and import stays idempotent on
    every path.
    """
    payload = crypto.decrypt_json(raw_fetch.payload_enc)  # decrypted once per import
    if tail_before is None:
        tail_before = raw_fetch.fetched_at.date() - timedelta(days=RESYNC_OVERLAP_DAYS)
    stats: dict[str, Any] = {
        "imported": 0,
        "skipped": 0,
        "skipped_zero_amount": 0,
        "skipped_before_feed_start": 0,
        "skipped_masked_duplicate": 0,
        "skipped_redescribed": 0,
        "unmapped_accounts": 0,
        "voided_pendings": 0,
        "holdings_snapshots": 0,
        "holdings_skipped_manual": 0,
        "manual_period_rows_deleted": 0,
        "manual_period_tombstones": 0,
    }
    redescribed: list[dict[str, str]] = []

    mapped: dict[str, Account] = {}
    # Lock order is account before watermark (decision 88, #413): the
    # connection's accounts are locked here, in id order, before anything
    # below marks the balance watermark, so holdings_source and the manual
    # period are read and ended under the same lock a source switch takes.
    from purser.holdings import projection

    projection.lock_accounts(
        session,
        session.execute(
            select(Account.id).where(Account.connection_id == raw_fetch.connection_id)
        ).scalars(),
    )
    # One load per fetch: rules number in the tens and the narrative columns
    # need decrypting, so this must not be per-row (#174, decision 46).
    active_rules = rules_service.load_active(session)

    for sf_account in payload.get("accounts", []):
        account = session.execute(
            select(Account).where(
                Account.connection_id == raw_fetch.connection_id,
                Account.external_id == sf_account["id"],
            )
        ).scalar_one_or_none()
        if account is None:
            stats["unmapped_accounts"] += 1
            continue
        mapped[sf_account["id"]] = account
        stats_account = _import_account(
            session,
            raw_fetch,
            sf_account,
            account,
            active_rules,
            tail_before=tail_before,
            redescribed=redescribed,
        )
        stats["imported"] += stats_account["imported"]
        stats["skipped"] += stats_account["skipped"]
        stats["skipped_zero_amount"] += stats_account["skipped_zero_amount"]
        stats["skipped_before_feed_start"] += stats_account["skipped_before_feed_start"]
        stats["skipped_masked_duplicate"] += stats_account["skipped_masked_duplicate"]
        stats["skipped_redescribed"] += stats_account["skipped_redescribed"]
        stats["voided_pendings"] += stats_account["voided_pendings"]
        if not replay:  # point-in-time facts the live import already recorded (#328)
            held = _import_holdings(session, raw_fetch, sf_account, account)
            stats["holdings_snapshots"] += held.created
            stats["holdings_skipped_manual"] += held.skipped_manual
            stats["manual_period_rows_deleted"] += held.period_rows_deleted
            stats["manual_period_tombstones"] += held.period_tombstones
            _record_balance_valuation(session, raw_fetch, sf_account, account)

    if window_start is not None:
        stats["voided_pendings"] += _reconcile_vanished_pendings(
            session, payload, mapped, window_start
        )

    # Deterministic transfer pass AFTER reconciliation: absorb late-arriving
    # counterparts into existing transfers, then auto-link signal pairs
    # (DESIGN §5). Function-local import keeps the feed layer decoupled.
    from purser import transfers

    stats.update(transfers.auto_link_pass(session))
    stats["redescribed"] = redescribed  # rows, not a count: callers summing counts skip it
    return stats


@dataclass
class _HoldingsResult:
    created: int = 0
    skipped_manual: int = 0
    period_rows_deleted: int = 0
    period_tombstones: int = 0


def _import_holdings(
    session: Session,
    raw_fetch: RawFetch,
    sf_account: dict[str, Any],
    account: Account,
) -> _HoldingsResult:
    """Holdings snapshots from the feed (DESIGN §8): one set per
    (account, as_of, symbol); no lot accounting in v1. The caller holds the
    account's lock (``import_fetch``), so ``holdings_source`` and the period
    state are read fresh.

    An account whose holdings are tracked by hand (``holdings_source =
    'manual'``, decision 88) takes no feed snapshot at all: its holdings come
    from its positions, one source per account, so the newest snapshot set is
    always complete. Its feed holdings are counted as skipped; the raw fetch
    still archives them, and its cash transactions import as usual.

    Back on the feed:

    - a fetch taken before the last switch (``holdings_source_changed_at``),
      re-imported later, adds nothing on a date the set in effect
      (``networth.holdings_set_on``) tracks by hand: that date belongs to the
      manual period;
    - the first fetch taken after the switch, while the manual period is
      still open, ends it (``projection.end_manual_period``, audited): the
      manual rows from its date on go, a manual tombstone ends each
      hand-tracked position it does not report, and its set imports;
    - every fetch imports its set as usual. A date holds the feed's rows
      plus, on the period-end date only, manual tombstones for the symbols
      the feed does not report: a later fetch that day reporting such a
      symbol replaces its tombstone. At most one row per (account, date,
      symbol)."""
    from purser.holdings import projection
    from purser.models import HoldingsSnapshot
    from purser.networth import holdings_set_on

    result = _HoldingsResult()
    holdings = sf_account.get("holdings", []) or []
    if account.holdings_source == "manual":
        result.skipped_manual = len(holdings)
        return result
    as_of = raw_fetch.fetched_at.date()
    symbols = [
        str(holding.get("symbol") or holding.get("description") or "UNKNOWN")
        for holding in holdings
    ]
    switched_at = account.holdings_source_changed_at
    taken_before_switch = switched_at is not None and raw_fetch.fetched_at <= switched_at
    if taken_before_switch:
        in_effect = holdings_set_on(session, account.id, as_of)
        if in_effect and all(h.source == "manual" for h in in_effect):
            result.skipped_manual = len(holdings)
            return result
    elif account.manual_period_open:
        result.period_rows_deleted, result.period_tombstones = projection.end_manual_period(
            session, account, as_of, reported=set(symbols), raw_fetch_id=raw_fetch.id
        )
    changed = bool(result.period_rows_deleted or result.period_tombstones)
    for holding, symbol in zip(holdings, symbols, strict=True):
        existing = list(
            session.execute(
                select(HoldingsSnapshot).where(
                    HoldingsSnapshot.account_id == account.id,
                    HoldingsSnapshot.as_of == as_of,
                    HoldingsSnapshot.symbol == symbol,
                )
            ).scalars()
        )
        if any(row.source == "feed" or row.quantity != 0 for row in existing):
            continue  # the date's row for the symbol: feed rows dedup per symbol
        for tombstone in existing:  # the feed reports a symbol it had ended
            session.delete(tombstone)
            changed = True
        session.flush()
        session.add(
            HoldingsSnapshot(
                account_id=account.id,
                as_of=as_of,
                symbol=symbol,
                quantity=Decimal(str(holding["shares"])) if holding.get("shares") else None,
                cost_basis=Decimal(str(holding["cost_basis"]))
                if holding.get("cost_basis")
                else None,
                market_value=Decimal(str(holding["market_value"]))
                if holding.get("market_value")
                else None,
                raw_fetch_id=raw_fetch.id,
            )
        )
        session.flush()
        result.created += 1
    if result.created or changed:
        balances.mark_dirty(session, as_of, networth_only=True)
    session.flush()
    return result


def _record_balance_valuation(
    session: Session,
    raw_fetch: RawFetch,
    sf_account: dict[str, Any],
    account: Account,
) -> None:
    """SimpleFIN-synced loans/property are valued by their reported balance
    (DESIGN §8: 'SimpleFIN-synced or manual valuations')."""
    from purser.models import Valuation
    from purser.networth import VALUATION_TYPES

    if account.type not in VALUATION_TYPES or sf_account.get("balance") is None:
        return
    value = Decimal(str(sf_account["balance"]))
    as_of = raw_fetch.fetched_at.date()
    latest = session.execute(
        select(Valuation.value)
        .where(Valuation.account_id == account.id)
        .order_by(Valuation.as_of.desc(), Valuation.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if latest is not None and Decimal(latest) == value:
        return
    session.add(Valuation(account_id=account.id, as_of=as_of, value=value, source="api"))
    balances.mark_dirty(session, as_of, networth_only=True)
    session.flush()


def _import_account(
    session: Session,
    raw_fetch: RawFetch,
    sf_account: dict[str, Any],
    account: Account,
    active_rules: list[rules_service.LoadedRule] | None = None,
    *,
    tail_before: date,
    redescribed: list[dict[str, str]],
) -> dict[str, int]:
    stats = {
        "imported": 0,
        "skipped": 0,
        "skipped_zero_amount": 0,
        "skipped_before_feed_start": 0,
        "skipped_masked_duplicate": 0,
        "skipped_redescribed": 0,
        "voided_pendings": 0,
    }
    transactions = sf_account.get("transactions", [])
    # Every row's identity up front: the occurrence ordinal of a row depends
    # on the identical rows before it in the payload (#275, decision 68).
    hashes = _row_hashes(sf_account)
    listing = _PayloadListing.of(transactions, hashes, invert=account.feed_invert)
    replaced: set[uuid.UUID] = set()  # originals already matched to a re-described row
    for sf_tx, tx_hash in zip(transactions, hashes, strict=True):
        tx_date = _tx_date(sf_tx).date()
        if account.feed_start is not None and tx_date < account.feed_start:
            # Per-account feed sync-from (issue #154, decision 40): rows dated
            # strictly before the boundary belong to manual imports (the
            # mirror of decision 35's cutoff — manual < boundary <= feed).
            # Skipping HERE, at the single chokepoint every feed path shares,
            # means the row never exists for auto_link_pass to pair — the
            # 2026-07-14 latch-reset incident's core fix. Window scoping comes
            # before content classification, so a pre-boundary zero-amount row
            # counts here, not below.
            stats["skipped_before_feed_start"] += 1
            continue
        amount = Decimal(str(sf_tx["amount"]))
        if amount == 0:
            # Investment feeds (brokerages) send zero-amount
            # marker rows; the ledger's nonzero invariant rightly refuses
            # them, so filter here — same call the CSV planner makes for
            # Quicken exports (skipped_zero_amount, #144/#147). Issue #151.
            stats["skipped_zero_amount"] += 1
            continue
        description = str(sf_tx.get("description") or "")
        is_pending = bool(sf_tx.get("pending"))
        # Columns only: nearly every row of a re-fetch is a hit, and loading the
        # entity would decrypt its narrative for nothing.
        holder = session.execute(
            select(Transaction.id, Transaction.status, Transaction.origin).where(
                Transaction.content_hash == tx_hash
            )
        ).one_or_none()
        posted_twin: Transaction | None = None
        if holder is not None:
            if is_pending or not _is_open_pending_on(
                session, holder.id, status=holder.status, origin=holder.origin, account=account
            ):
                stats["skipped"] += 1
                continue
            # The bank posted an open pending without changing its date,
            # amount or description (#282): same identity, so the posted row
            # used to be skipped as a duplicate and the pending then voided
            # as vanished — the transaction lost. The identity MOVES to the
            # posted row created below (decision 29's unlink precedent: that
            # row now IS the bank line) and the pending is superseded by it,
            # exactly as when the posted row rehashes.
            posted_twin = session.get(Transaction, holder.id)
            assert posted_twin is not None
            posted_twin.content_hash = None
            session.flush()  # release the unique index before the posted row claims it

        if account.feed_invert:
            # Sign inversion (issue #170, decision 43) applies strictly AFTER
            # hashing: content identity is the bytes the bank sent, so a flag
            # flip never re-imports history, and _current_pending_hashes
            # (raw-amount-based) keeps matching stored hashes. Only the
            # ledger amount — and the income/expense classification below —
            # see the corrected sign.
            amount = -amount

        if posted_twin is None and _is_masked_duplicate(
            session, account, tx_date, amount, description
        ):
            # Same-source masked-reference dedup (issue #171, decision 44):
            # the provider re-delivered this row with its reference digits
            # masked, so the content hash missed. Runs on the post-invert
            # amount — candidates are ledger rows.
            stats["skipped_masked_duplicate"] += 1
            continue

        if posted_twin is None and not is_pending and tx_date < tail_before:
            original = _redescribed_original(
                session,
                raw_fetch,
                account,
                tx_date=tx_date,
                amount=amount,
                description=description,
                listing=listing,
                replaced=replaced,
            )
            if original is not None:
                # The bank re-described a row the ledger already holds
                # (#331, decision 74): skip the copy. Also post-invert, like
                # the masked guard above — the original is a ledger row.
                replaced.add(original.id)
                stats["skipped_redescribed"] += 1
                redescribed.append(
                    {
                        "account_id": str(account.id),
                        "date": tx_date.isoformat(),
                        "amount": str(core.quantize(amount)),
                        "original_id": str(original.id),
                    }
                )
                continue

        payee_name = sf_tx.get("payee") or description or None

        # Deterministic rules shape the row before it is created (#174,
        # decision 46): rename FIRST, so payee memory below is consulted on
        # the friendly payee — several raw descriptors renamed onto one payee
        # make that payee's memory stronger. This sits strictly AFTER hashing
        # and dedup, so a rule can never re-import history (decision 43).
        shaped = rules_service.shape_new_row(
            active_rules if active_rules is not None else [],
            account_id=account.id,
            amount=amount,
            when=tx_date,
            payee_name=payee_name,
            description=description or None,
        )
        payee_name = shaped.payee_name
        rule_category = shaped.category_id

        from_memory = False
        if rule_category is not None:
            category = ledger.resolve_account(session, str(rule_category))
        else:
            category, from_memory = category_for_new_row(
                session, payee_name=payee_name, income=amount > 0
            )
        tx = ledger.create_flat_transaction(
            session,
            account_ref=str(account.id),
            category_ref=str(category.id),
            amount=amount,
            date=tx_date,
            payee_name=payee_name,
            description=description or None,
            requested_status=core.Status.PENDING if is_pending else core.Status.POSTED,
            origin=core.Origin.IMPORT,
            token=None,
            source_fetch_id=raw_fetch.id,
            content_hash=tx_hash,
        )
        if shaped.applied:
            rules_service.mark_applied(session, shaped.applied)
            audit.record(
                session,
                actor_token_id=None,
                action="transaction.categorize" if rule_category else "transaction.rename",
                entity_type="transaction",
                entity_id=tx.id,
                after={
                    "category": category.path if rule_category else None,
                    "payee": payee_name if rule_category is None else None,
                    "source": "rule",
                    "stage": "import",
                    "rule_ids": [str(r) for r in shaped.applied],
                },
            )
        if from_memory:
            # Same audit shape as the categorization pass — the accept/correct
            # corpus and the register's provenance badge stay uniform.
            audit.record(
                session,
                actor_token_id=None,
                action="transaction.categorize",
                entity_type="transaction",
                entity_id=tx.id,
                after={"category": category.path, "source": "deterministic", "stage": "import"},
            )
        if posted_twin is not None:
            reconcile.void_pending(session, posted_twin, successor=tx, identity_moved=True)
            stats["voided_pendings"] += 1
        stats["imported"] += 1
    return stats


def _is_open_pending_on(
    session: Session, tx_id: uuid.UUID, *, status: str, origin: str, account: Account
) -> bool:
    """An import pending still open on ``account`` — the only holder of a
    posted row's identity that the posted row may take over (#282)."""
    if status != core.Status.PENDING.value or origin != core.Origin.IMPORT.value:
        return False
    return (
        session.execute(
            select(Posting.id).where(
                Posting.transaction_id == tx_id, Posting.account_id == account.id
            )
        ).first()
        is not None
    )


def _is_masked_duplicate(
    session: Session,
    account: Account,
    tx_date: date,
    amount: Decimal,
    description: str,
) -> bool:
    """A row already in the ledger whose description is the incoming one
    under digit-masking (issue #171, decision 44). Deterministic and
    same-source: same account, same date, same real-leg amount, feed-origin
    only, ANY status — a voided merge leg still squats the identity.
    """
    candidates = session.execute(
        select(Transaction)
        .join(Posting, Posting.transaction_id == Transaction.id)
        .where(
            Posting.account_id == account.id,
            Posting.amount == amount,
            Transaction.date == tx_date,
            Transaction.origin == "import",
            Transaction.source_fetch_id.is_not(None),
            Transaction.content_hash.is_not(None),
        )
    ).scalars()
    return any(
        core.masked_variant(description, candidate.description or "") for candidate in candidates
    )


@dataclass(frozen=True)
class _PayloadListing:
    """What one account's payload lists, as the re-description guard reads it
    (#331, decision 74)."""

    identities: frozenset[str]  # every row's stored identity (``_row_hashes``)
    pending: frozenset[str]  # the identities it lists as pending
    # Descriptions by (date, ledger amount): a masked re-delivery (decision 44)
    # still lists its original, under a description the hash cannot see.
    descriptions: dict[tuple[date, Decimal], list[str]]

    @classmethod
    def of(
        cls, transactions: list[dict[str, Any]], hashes: list[str], *, invert: bool
    ) -> "_PayloadListing":
        descriptions: dict[tuple[date, Decimal], list[str]] = {}
        for sf_tx in transactions:
            amount = Decimal(str(sf_tx["amount"]))
            key = (_tx_date(sf_tx).date(), -amount if invert else amount)
            descriptions.setdefault(key, []).append(str(sf_tx.get("description") or ""))
        return cls(
            identities=frozenset(hashes),
            pending=frozenset(
                tx_hash
                for sf_tx, tx_hash in zip(transactions, hashes, strict=True)
                if sf_tx.get("pending")
            ),
            descriptions=descriptions,
        )

    def still_lists(self, row: Transaction, amount: Decimal) -> bool:
        """The payload still lists ledger row ``row`` (posting ``amount``):
        under its own identity, or as a masked re-delivery of it."""
        if row.content_hash in self.identities:
            return True
        return any(
            core.masked_variant(listed, row.description or "")
            for listed in self.descriptions.get((row.date, amount), [])
        )


def _redescribed_original(
    session: Session,
    raw_fetch: RawFetch,
    account: Account,
    *,
    tx_date: date,
    amount: Decimal,
    description: str,
    listing: _PayloadListing,
    replaced: set[uuid.UUID],
) -> Transaction | None:
    """The posted feed row that a new posted tail row re-describes, or None
    (#331, decision 74).

    A posted row in a fetch's tail that no ledger row holds by identity is
    either a late-posted row the bank never listed before, or a row the bank
    has since re-described: the content hash includes the description, so a
    new description is a new identity. It is the second when the account
    already holds a posted feed row from an EARLIER fetch with the same date
    and exact ledger amount, a different ``normalize_payee`` description, and
    an identity this payload no longer lists. The bank lists the new row in
    its place. A row next to an original the bank still lists is a distinct
    late row, and each original stands in for one re-described row per import
    (``replaced``).

    The row is kept, never skipped, when an open pending could take it as its
    posted successor (``_pending_may_take``): #282's pairing must still see it.
    """
    normalized = core.normalize_payee(description)
    candidates = session.execute(
        select(Transaction)
        .join(Posting, Posting.transaction_id == Transaction.id)
        .where(
            Posting.account_id == account.id,
            Posting.amount == amount,
            Transaction.date == tx_date,
            Transaction.status == "posted",
            Transaction.origin == "import",
            Transaction.source_fetch_id.is_not(None),
            Transaction.source_fetch_id != raw_fetch.id,
            Transaction.content_hash.is_not(None),
        )
        .order_by(Transaction.created_at, Transaction.id)
    ).scalars()
    original = next(
        (
            candidate
            for candidate in candidates
            if candidate.id not in replaced
            and core.normalize_payee(candidate.description or "") != normalized
            and not listing.still_lists(candidate, amount)
        ),
        None,
    )
    if original is None or _pending_may_take(
        session, account, tx_date=tx_date, amount=amount, listing=listing
    ):
        return None
    return original


def _pending_may_take(
    session: Session,
    account: Account,
    *,
    tx_date: date,
    amount: Decimal,
    listing: _PayloadListing,
) -> bool:
    """An open import pending on ``account`` that the reconciler may pair with
    a posted row dated ``tx_date`` for ``amount`` (#282): the same amount,
    dated within ``PENDING_MATCH_WINDOW_DAYS`` either way, and no longer listed
    as pending by this payload. A pending the payload still lists is not
    voided, so it takes no successor."""
    window = timedelta(days=reconcile.PENDING_MATCH_WINDOW_DAYS)
    identities = session.execute(
        select(Transaction.content_hash)
        .join(Posting, Posting.transaction_id == Transaction.id)
        .where(
            Posting.account_id == account.id,
            Posting.amount == amount,
            Transaction.status == "pending",
            Transaction.origin == "import",
            Transaction.date >= tx_date - window,
            Transaction.date <= tx_date + window,
        )
    ).scalars()
    return any(identity not in listing.pending for identity in identities)


def _current_pending_hashes(payload: dict[str, Any], external_id: str) -> set[str]:
    """Identities the payload still lists as pending — the same per-row hashes
    the import loop stored, occurrence ordinals included (decision 68)."""
    hashes: set[str] = set()
    for sf_account in payload.get("accounts", []):
        if sf_account["id"] != external_id:
            continue
        transactions = sf_account.get("transactions", [])
        for sf_tx, tx_hash in zip(transactions, _row_hashes(sf_account), strict=True):
            if bool(sf_tx.get("pending")):
                hashes.add(tx_hash)
    return hashes


def _reconcile_vanished_pendings(
    session: Session, payload: dict[str, Any], mapped: dict[str, Account], window_start: date
) -> int:
    """Void import pendings that vanished from the feed (DESIGN §4). The
    per-account lifecycle logic lives in ``purser.feed.reconcile``; here we
    only supply the still-present pending hashes computed from the SimpleFIN
    payload shape."""
    voided = 0
    for external_id, account in mapped.items():
        still_pending = _current_pending_hashes(payload, external_id)
        voided += reconcile.reconcile_vanished_pendings(
            session,
            account=account,
            still_pending_hashes=still_pending,
            window_start=window_start,
        )
    return voided
