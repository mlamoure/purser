"""QIF import service (v2): parse -> the same content-hash dedup pipeline as
SimpleFIN, with transfer-pair linking instead of duplicate rows.

v2 (issue #125) makes the import mapping-first and cutoff-aware so a manual
QIF export and the SimpleFIN feed can share one account without duplicating
the overlap window:

- Per-file-account **cutoffs**: a row is imported only when its date is
  *strictly older* than the account's cutoff (the feed owns everything from
  the cutoff forward). The server never imposes a cutoff that isn't in the
  request — the preview *suggests* one (`default_cutoff`), the caller decides.
- Per-file-account **invert**: flips every row's amount at consumption time,
  before hashing / category-root selection / transfer direction. `_transfer_hash`
  is abs()-based, so inversion is transfer-hash-neutral.
- **Split transfers** (older than one side's cutoff, inside the other) degrade
  to a one-leg flat transaction on the older side; the post-import
  ``auto_link_pass`` claims the feed counterpart later (DESIGN §5, §7.2).
- **Cleared flag + reminders** (issue #249, decision 60): a Quicken reminder
  row is not history and is skipped (``skipped_scheduled``); a row its file
  marks uncleared lands ``expected`` (counted ``uncleared``) — the bank has not
  seen it, so it forecasts and is matched like a schedule instance instead of
  inflating the posted balance. ``pending`` is deliberately not used: the feed's
  pending lifecycle (``feed/reconcile.py``) voids any import-origin pending in a
  sync window, whatever its source. A later file that marks such a row cleared
  posts it in place (counted ``promoted``, #310) — on accounts without a feed,
  whose matcher would otherwise settle it against the bank's row.
- **Identical rows** (issue #275, decision 68): the n-th row of a file that is
  identical to an earlier one (same content hash) is occurrence n, a distinct
  identity — two same-day coffees at one price import as two. Occurrence 1
  hashes exactly as before, so history already imported keeps its identity.

- **Split rows** (issue #274, decision 87): a QIF row's ``S``/``E``/``$``
  lines categorize it. Category lines become the category legs of one split
  transaction (one real leg plus N category legs; one line left is an
  ordinary flat row), identified by the row's flat hash over ``T`` so a file
  imported before splits were read dedups. Each transfer line ``S[X]`` is
  carved out as an ordinary transfer leg to X and pairs with X's mirror row
  through the symmetric transfer hash, so the money moves once (it used to
  move twice: the mirror formed its own transfer while the row stayed whole).
  A blank line, and whatever the lines leave unexplained, goes to the
  uncategorized placeholder, never to payee memory.
- **Investment sections** (issue #274, decision 84): records from a QIF
  ``!Type:Invst`` section are investment actions (Buy, Sell, Div, XIn, ...), not
  bank rows, so they are skipped and counted ``skipped_investment``. An
  investment account the request does not map is left out, like a skipped
  account: bank legs into it degrade to one-leg rows on the bank side. Mapped,
  bank legs into it still form two-leg transfers.

Preview is DB-aware but strictly read-only: it resolves the mapping, computes
per-account default cutoffs + a sign-diagnostic, and returns REAL dedup counts
by batch-checking content hashes. Investment lot history is explicitly OUT
(issue #8); investments are snapshot-forward from go-live.
"""

import hashlib
import uuid
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date as date_type
from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from purser import audit, balances, crypto, events
from purser import ledger as ledger_service
from purser.core import csv_import as core_csv
from purser.core import ledger as core
from purser.core import qif as core_qif
from purser.ledger import LedgerError, NotFoundError
from purser.models import Account, ApiToken, Posting, RawFetch, Transaction

OPENING_BALANCE_CATEGORY = "income.opening_balances"

# The two supported manual-import formats share this planner; only the parse
# step differs. The fallback description is the placeholder shown for a row with
# no payee/memo, and the audit action is ``{fmt}.import`` (issue #138).
_FALLBACK_DESCRIPTION = {"qif": "QIF import", "csv": "CSV import"}


def _parse_content(
    content: str, *, fmt: str, date_format: str, column_map: dict[str, str] | None
) -> core_qif.QifFile:
    """Dispatch to the QIF or CSV parser — both emit ``core.qif`` dataclasses."""
    if fmt == "csv":
        return core_csv.parse_csv(content, date_format=date_format, column_map=column_map)
    if fmt == "qif":
        return core_qif.parse(content, date_format=date_format)
    raise LedgerError(f"unknown import format {fmt!r}")


# QIF carries transaction dates; SimpleFIN posted dates run 1-3 business days
# later, so the suggested cutoff backs off the feed's earliest date by a margin.
DEFAULT_CUTOFF_MARGIN = timedelta(days=3)

# Sign convention is "suspect" (likely needs inverting) only for accounts whose
# everyday activity is expenses-negative in a normal file: cards and loans.
SUSPECT_TYPES = frozenset({"credit_card", "loan"})

_HASH_CHUNK = 500


# ---------------------------------------------------------------------------
# Per-account options
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QifAccountOptions:
    """Per-file-account import knobs. ``cutoff`` = import only rows strictly
    older than this date (None = import everything). ``invert`` = flip every
    amount at consumption time."""

    cutoff: date_type | None = None
    invert: bool = False


_DEFAULT_OPTIONS = QifAccountOptions()


def _archive_upload(session: Session, content: str, *, source: str) -> RawFetch | None:
    """Archive the verbatim uploaded file to ``raw_fetches`` BEFORE any
    ledger write (raw-first, DESIGN §7 / #127, decision 53).

    The ledger rows this import creates keep ``source_fetch_id`` NULL. That
    NULL is doubly load-bearing — PR-7's cutoff queries AND the #175 review
    scope (``purser/review.py``) both read a non-NULL ``source_fetch_id`` as
    "this row came from the feed" — so the archive link is content hash +
    audit trail, never a foreign key from transactions.

    Re-running the same file is a no-op here too (idempotency mirror of the
    ledger's content-hash dedup): an identical prior archive row short-
    circuits, returning ``None``.
    """
    content_hash = hashlib.sha256(content.encode()).hexdigest()
    existing = session.execute(
        select(RawFetch.id).where(RawFetch.content_hash == content_hash, RawFetch.source == source)
    ).first()
    if existing is not None:
        return None
    raw = RawFetch(
        connection_id=None,
        source=source,
        content_hash=content_hash,
        payload_enc=crypto.encrypt(content),
    )
    session.add(raw)
    session.flush()
    return raw


def _options_for(options: dict[str, QifAccountOptions], qif_name: str) -> QifAccountOptions:
    """Options for a file account: exact key, then the ``__default__`` fallback,
    then the no-op default. (``__default__`` is also the parser's name for a
    single-account file, so an exact hit handles that case too.)"""
    if qif_name in options:
        return options[qif_name]
    if core_qif.DEFAULT_ACCOUNT in options:
        return options[core_qif.DEFAULT_ACCOUNT]
    return _DEFAULT_OPTIONS


def _effective_amount(amount: Decimal, opts: QifAccountOptions) -> Decimal:
    return -amount if opts.invert else amount


def _is_older(when: date_type, cutoff: date_type | None) -> bool:
    """Strictly older than the cutoff imports; a missing cutoff imports all."""
    return cutoff is None or when < cutoff


# ---------------------------------------------------------------------------
# Hashing (dedup identity, DESIGN §7)
# ---------------------------------------------------------------------------


def _description_of(payee: str | None, memo: str | None, fallback: str = "QIF import") -> str:
    return " / ".join(filter(None, [payee, memo])) or fallback


# The pure sha256 recipes (core.content_hash + the abs()-based transfer hash)
# stay intact; keyed_hash composes them into the dictionary-attack-hardened
# form stored in transactions.content_hash (#137, decision 36). Dedup identity
# is unchanged: the same file re-imported dedups exactly, because the migration
# rehashed every existing stored value with the same key.
#
# ``occurrence`` numbers identical rows within one file (#275, decision 68):
# occurrence 1 is the identity every row had before the ordinal existed, the
# n-th identical row adds ``#n`` (core.occurrence_part). _collect assigns them.
def _flat_hash(
    qif_name: str, when: date_type, amount: Decimal, description: str, occurrence: int = 1
) -> str:
    return crypto.keyed_hash(
        core.content_hash(
            external_account_id=f"qif:{qif_name}",
            date=when,
            amount=amount,
            description=description,
            occurrence=occurrence,
        )
    )


def _opening_hash(qif_name: str, when: date_type, amount: Decimal, occurrence: int = 1) -> str:
    return crypto.keyed_hash(
        core.content_hash(
            external_account_id=f"qif:{qif_name}",
            date=when,
            amount=amount,
            description="opening balance",
            occurrence=occurrence,
        )
    )


def _transfer_hash(
    account_a: str, account_b: str, date_iso: str, amount: Decimal, occurrence: int = 1
) -> str:
    key = "|".join(
        [
            "qif-transfer",
            *sorted([account_a, account_b]),
            date_iso,
            str(abs(amount)),
            *core.occurrence_part(occurrence),
        ]
    )
    return crypto.keyed_hash(hashlib.sha256(key.encode()).hexdigest())


# ---------------------------------------------------------------------------
# Planning (pure, read-only) — shared by preview and commit
# ---------------------------------------------------------------------------


@dataclass
class _Counts:
    """Per-file-account disposition. Every file row lands in exactly one of the
    buckets (``in_file`` is their sum); ``uncleared`` is not a bucket but the
    subset of the created rows (flat, transfer legs, one-leg) that land
    ``expected`` because the file marks them uncleared. ``promoted`` is a
    bucket: a row whose identity already exists as an ``expected`` file row
    the file now marks cleared, which posts that row instead of creating one
    (decision 60, #310) — counted per leg for a transfer, like ``transfers``.

    ``split_transfers`` and ``split_remainders`` are not buckets either
    (decision 87): the transfer lines carved out of split rows, and the split
    rows whose lines did not add up to the row's amount (the difference went
    to the uncategorized placeholder). The split row itself lands in one
    bucket, decided by its category part."""

    in_file: int = 0
    would_import: int = 0
    skipped_dedup: int = 0
    skipped_cutoff: int = 0
    skipped_unmapped: int = 0
    skipped_zero_amount: int = 0
    skipped_scheduled: int = 0
    skipped_investment: int = 0
    transfers: int = 0
    opening_balances: int = 0
    one_leg_transfers: int = 0
    promoted: int = 0
    uncleared: int = 0
    split_transfers: int = 0
    split_remainders: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "in_file": self.in_file,
            "would_import": self.would_import,
            "skipped_dedup": self.skipped_dedup,
            "skipped_cutoff": self.skipped_cutoff,
            "skipped_unmapped": self.skipped_unmapped,
            "skipped_zero_amount": self.skipped_zero_amount,
            "skipped_scheduled": self.skipped_scheduled,
            "skipped_investment": self.skipped_investment,
            "transfers": self.transfers,
            "opening_balances": self.opening_balances,
            "one_leg_transfers": self.one_leg_transfers,
            "promoted": self.promoted,
            "uncleared": self.uncleared,
            "split_transfers": self.split_transfers,
            "split_remainders": self.split_remainders,
        }


def _status_for(cleared: bool) -> core.Status:
    """Cleared rows are bank history (posted); an uncleared row is a forecast
    item the matcher settles against the posted bank row (decision 60)."""
    return core.Status.POSTED if cleared else core.Status.EXPECTED


@dataclass
class _SplitLineItem:
    """One category line of a split row (decision 87), in the real account's
    delta sign. ``category`` None = the uncategorized placeholder."""

    category: str | None
    amount: Decimal
    memo: str | None


@dataclass
class _RowItem:
    """A non-transfer file row (flat), a split row's category part (split),
    or a self-transfer (opening balance)."""

    qif_name: str
    kind: str  # "flat" | "split" | "opening"
    amount: Decimal
    date: date_type
    payee: str | None
    description: str
    qif_category: str | None
    content_hash: str
    cutoff_ok: bool
    cleared: bool  # effective: the row's mark, or True in an unmarked account
    cleared_mark: bool  # the row's own mark: only this promotes (decision 60)
    # A flat row from a split's single blank line: the placeholder category,
    # never payee memory (decision 87).
    placeholder: bool = False
    lines: list[_SplitLineItem] = field(default_factory=list)  # kind "split"


@dataclass
class _Leg:
    """One side of a real transfer as it appears in the file."""

    qif_name: str
    other_name: str
    amount: Decimal
    date: date_type
    description: str
    has_description: bool
    flat_hash: str
    self_older: bool
    other_older: bool
    cleared: bool  # effective, as for _RowItem
    cleared_mark: bool  # the leg's own mark, as for _RowItem
    # A transfer line carved out of a split row (decision 87): classified like
    # any leg, but counted as its row's ``split_transfers``, never in a bucket.
    carved: bool = False


@dataclass
class _TransferGroup:
    transfer_hash: str
    legs: list[_Leg] = field(default_factory=list)


@dataclass
class _Collected:
    account_names: list[str]
    row_items: list[_RowItem]
    groups: list[_TransferGroup]
    all_hashes: set[str]
    # File accounts explicitly skipped (mapping value null): name -> row count.
    # Their rows never reach classification but are counted skipped_unmapped so
    # preview and commit agree on where every file row went (issue #138).
    skipped_counts: dict[str, int] = field(default_factory=dict)
    # Zero-amount rows per file account: the ledger rejects zero postings, so
    # they are non-importable regardless of cutoff/dedup and are filtered before
    # hashing/grouping — commit can never trip over them (issue #144).
    zero_counts: dict[str, int] = field(default_factory=dict)
    # Quicken reminder rows per file account (issue #249): upcoming scheduled
    # entries, not history — filtered before hashing so they never land, never
    # half-form a transfer, and stay out of the dedup identity.
    scheduled_counts: dict[str, int] = field(default_factory=dict)
    # QIF investment-section records per file account (issue #274): filtered
    # first, whether or not the account is mapped — never a bank row.
    investment_counts: dict[str, int] = field(default_factory=dict)
    # Split-row sub-counts per file account (decision 87).
    split_transfer_counts: dict[str, int] = field(default_factory=dict)
    split_remainder_counts: dict[str, int] = field(default_factory=dict)


@dataclass
class _FlatCreate:
    qif_name: str
    amount: Decimal
    date: date_type
    payee: str | None
    description: str
    qif_category: str | None
    content_hash: str
    status: core.Status
    placeholder: bool = False


@dataclass
class _SplitCreate:
    """One split transaction: a real leg of ``amount`` and a category leg per
    line (decision 87)."""

    qif_name: str
    amount: Decimal
    date: date_type
    payee: str | None
    description: str
    lines: list[_SplitLineItem]
    content_hash: str
    status: core.Status


@dataclass
class _OpeningCreate:
    qif_name: str
    amount: Decimal
    date: date_type
    payee: str | None
    content_hash: str


@dataclass
class _TransferCreate:
    from_name: str
    to_name: str
    amount: Decimal  # abs()
    date: date_type
    description: str | None
    content_hash: str
    status: core.Status


@dataclass
class _Promote:
    """Post an existing ``expected`` file row the file now marks cleared."""

    content_hash: str


_Action = _FlatCreate | _SplitCreate | _OpeningCreate | _TransferCreate | _Promote


def _collect(
    parsed: core_qif.QifFile,
    options: dict[str, QifAccountOptions],
    *,
    skip: frozenset[str] = frozenset(),
    fallback: str = "QIF import",
) -> _Collected:
    """Turn the parsed file + options into classifiable items and every content
    hash the plan could touch. Pure: applies invert, computes cutoff eligibility
    and hashes, groups transfer legs by their symmetric hash — no DB, no writes.

    ``skip`` names file accounts the caller mapped to null (don't import): their
    own rows are removed here (counted ``skipped_unmapped`` in ``_classify``) and
    a transfer whose counterpart is skipped degrades to a one-leg flat on the
    imported side via the existing split machinery (``other_older`` forced False).
    Investment-section records (issue #274) are filtered first, in skipped
    accounts too, into ``skipped_investment``.

    A row with split lines (decision 87) is decomposed: each transfer line
    ``S[X]`` becomes an ordinary leg to X (carved, so it counts as the row's
    ``split_transfers`` rather than a row of its own), and the category lines,
    plus a placeholder line for whatever the lines leave unexplained, form the
    row's category part: a split row (two or more lines) or a flat row (one),
    of ``T`` minus the transfer lines, hashed like the flat row over ``T``.
    When the transfer lines account for all of ``T`` there is no category part
    and the first transfer line carries the row's bucket instead (category
    lines that net to zero book nothing).

    Cleared status is settled per file account (decision 60): when at least one
    of its rows is marked cleared/reconciled, an unmarked row is uncleared; an
    account with no marks at all carries no clearing information — bank-generated
    exports never mark rows, and Quicken leaves a never-reconciled account (cash)
    unmarked — so every row in it counts as cleared rather than turning its whole
    history into forecast items.

    Identical rows are numbered in file order (#275, decision 68): the n-th
    flat/opening row with one content hash is occurrence n, and the n-th leg a
    file account carries for one transfer identity (counterpart, date, amount)
    joins transfer n — so two identical coffees, or two identical same-day
    transfers, import as two. Every counter follows the order in which
    ``_classify`` claims hashes, so occurrence 1 is always the row that held the
    unqualified hash before the ordinal existed. A leg's flat hash (the identity
    of its one-leg form, and what the two-leg guard checks) is numbered only
    among its file account's legs of the same transfer: across transfers, which
    leg claimed the unqualified hash depended on file layout and on which
    transfers split, so a wider count could renumber a leg whose one-leg row
    already exists.
    """
    row_items: list[_RowItem] = []
    groups: dict[str, _TransferGroup] = {}
    all_hashes: set[str] = set()
    skipped_counts: dict[str, int] = {}
    zero_counts: dict[str, int] = {}
    scheduled_counts: dict[str, int] = {}
    investment_counts: dict[str, int] = {}
    split_transfer_counts: dict[str, int] = {}
    split_remainder_counts: dict[str, int] = {}
    # Occurrence counters for this file (decision 68). Keys are occurrence-1
    # hashes, so "identical" means exactly what the content hash says it means.
    row_occurrence = core.OccurrenceCounter[str]()
    leg_occurrence = core.OccurrenceCounter[tuple[str, str]]()
    leg_flat_occurrence = core.OccurrenceCounter[tuple[str, str]]()

    def add_row(
        qif_name: str,
        opts: QifAccountOptions,
        t: core_qif.QifTransaction,
        *,
        cleared: bool,
        hash_amount: Decimal,
        amount: Decimal,
        description: str,
        kind: str,
        qif_category: str | None = None,
        placeholder: bool = False,
        lines: list[_SplitLineItem] | None = None,
    ) -> None:
        """A flat row, or a split row's category part: identified by the flat
        hash over the file's ``T`` (``hash_amount``) whatever amount it books."""
        content_hash = _flat_hash(qif_name, t.date, hash_amount, description)
        occurrence = row_occurrence(content_hash)
        if occurrence > 1:
            content_hash = _flat_hash(qif_name, t.date, hash_amount, description, occurrence)
        all_hashes.add(content_hash)
        row_items.append(
            _RowItem(
                qif_name=qif_name,
                kind=kind,
                amount=amount,
                date=t.date,
                payee=t.payee,
                description=description,
                qif_category=qif_category,
                content_hash=content_hash,
                cutoff_ok=_is_older(t.date, opts.cutoff),
                cleared=cleared,
                cleared_mark=t.cleared,
                placeholder=placeholder,
                lines=lines or [],
            )
        )

    def add_leg(
        qif_name: str,
        opts: QifAccountOptions,
        t: core_qif.QifTransaction,
        *,
        cleared: bool,
        other: str,
        amount: Decimal,
        carved: bool,
    ) -> None:
        other_opts = _options_for(options, other)
        date_iso = t.date.isoformat()
        description = _description_of(t.payee, t.memo, fallback)
        first_transfer = _transfer_hash(qif_name, other, date_iso, amount)
        # A carved split line's one-leg identity has its own key, so it can
        # never collide with its row's flat hash over T (decision 87).
        flat_key = f"split:{other}:{description}" if carved else description
        first_flat = _flat_hash(qif_name, t.date, amount, flat_key)
        # The k-th leg this file account carries for one transfer
        # identity belongs to transfer k; the counterpart's k-th leg
        # lands in the same group (the transfer hash is symmetric).
        transfer_occurrence = leg_occurrence((qif_name, first_transfer))
        transfer_hash = (
            first_transfer
            if transfer_occurrence == 1
            else _transfer_hash(qif_name, other, date_iso, amount, transfer_occurrence)
        )
        flat_occurrence = leg_flat_occurrence((first_transfer, first_flat))
        flat_hash = (
            first_flat
            if flat_occurrence == 1
            else _flat_hash(qif_name, t.date, amount, flat_key, flat_occurrence)
        )
        all_hashes.add(transfer_hash)
        all_hashes.add(flat_hash)
        # A skipped counterpart can never form a 2-leg transfer; treat
        # its side as "inside its cutoff" so this leg degrades to a
        # one-leg flat on the imported side (never links to a row we
        # are not importing).
        other_older = False if other in skip else _is_older(t.date, other_opts.cutoff)
        groups.setdefault(transfer_hash, _TransferGroup(transfer_hash=transfer_hash)).legs.append(
            _Leg(
                qif_name=qif_name,
                other_name=other,
                amount=amount,
                date=t.date,
                description=description,
                has_description=bool(t.payee or t.memo),
                flat_hash=flat_hash,
                self_older=_is_older(t.date, opts.cutoff),
                other_older=other_older,
                cleared=cleared,
                cleared_mark=t.cleared,
                carved=carved,
            )
        )

    def add_split(
        qif_name: str,
        opts: QifAccountOptions,
        t: core_qif.QifTransaction,
        *,
        cleared: bool,
        amount: Decimal,
    ) -> None:
        """Decompose a split row (decision 87, model B)."""
        transfer_lines: list[tuple[str, Decimal]] = []
        lines: list[_SplitLineItem] = []
        for line in t.splits:
            share = _effective_amount(line.amount, opts)
            if share == 0:
                continue
            if line.transfer_account is not None and line.transfer_account != qif_name:
                transfer_lines.append((line.transfer_account, share))
            else:
                # A blank line (or a "transfer" to the row's own account)
                # goes to the placeholder, never to payee memory.
                lines.append(_SplitLineItem(category=line.category, amount=share, memo=line.memo))
        transferred = sum((share for _, share in transfer_lines), Decimal(0))
        remainder = amount - transferred - sum((line.amount for line in lines), Decimal(0))
        if remainder != 0:
            lines.append(_SplitLineItem(category=None, amount=remainder, memo=None))
            split_remainder_counts[qif_name] = split_remainder_counts.get(qif_name, 0) + 1
        category_total = amount - transferred
        # No category part: the first transfer line carries the row's bucket.
        carrier = category_total == 0
        for i, (other, share) in enumerate(transfer_lines):
            carved = not (carrier and i == 0)
            add_leg(qif_name, opts, t, cleared=cleared, other=other, amount=share, carved=carved)
            if carved:
                split_transfer_counts[qif_name] = split_transfer_counts.get(qif_name, 0) + 1
        if carrier:
            return
        description = _description_of(t.payee, t.memo, fallback)
        if len(lines) == 1:
            add_row(
                qif_name,
                opts,
                t,
                cleared=cleared,
                hash_amount=amount,
                amount=category_total,
                description=description,
                kind="flat",
                qif_category=lines[0].category,
                placeholder=lines[0].category is None,
            )
        else:
            add_row(
                qif_name,
                opts,
                t,
                cleared=cleared,
                hash_amount=amount,
                amount=category_total,
                description=description,
                kind="split",
                lines=lines,
            )

    for qif_name, all_txns in parsed.transactions.items():
        # Investment actions are never bank rows (issue #274): out before the
        # skip, the reminders and the cleared-mark rule.
        txns = [t for t in all_txns if not t.investment]
        if len(txns) < len(all_txns):
            investment_counts[qif_name] = len(all_txns) - len(txns)
        if qif_name in skip:
            skipped_counts[qif_name] = len(txns)
            continue
        opts = _options_for(options, qif_name)
        marked = any(t.cleared for t in txns)
        for t in txns:
            if t.scheduled:
                # A reminder, not history: never hashed, never imported.
                scheduled_counts[qif_name] = scheduled_counts.get(qif_name, 0) + 1
                continue
            cleared = t.cleared or not marked
            amount = _effective_amount(t.amount, opts)
            if amount == 0:
                # Quicken exports carry $0 marker rows ("Live Opening Balance"
                # etc.); a zero posting is a LedgerError, so bucket them here —
                # a zero transfer leg must also never half-form a pair.
                zero_counts[qif_name] = zero_counts.get(qif_name, 0) + 1
                continue
            if t.splits:
                add_split(qif_name, opts, t, cleared=cleared, amount=amount)
            elif t.transfer_account is None:
                description = _description_of(t.payee, t.memo, fallback)
                add_row(
                    qif_name,
                    opts,
                    t,
                    cleared=cleared,
                    hash_amount=amount,
                    amount=amount,
                    description=description,
                    kind="flat",
                    qif_category=t.category,
                )
            elif t.transfer_account == qif_name:
                # Self-transfer = Quicken's opening balance convention. It
                # shares the flat rows' hash space, so it shares their counter.
                content_hash = _opening_hash(qif_name, t.date, amount)
                occurrence = row_occurrence(content_hash)
                if occurrence > 1:
                    content_hash = _opening_hash(qif_name, t.date, amount, occurrence)
                all_hashes.add(content_hash)
                row_items.append(
                    _RowItem(
                        qif_name=qif_name,
                        kind="opening",
                        amount=amount,
                        date=t.date,
                        payee=t.payee,
                        description="opening balance",
                        qif_category=None,
                        content_hash=content_hash,
                        cutoff_ok=_is_older(t.date, opts.cutoff),
                        cleared=cleared,
                        cleared_mark=t.cleared,
                    )
                )
            else:
                add_leg(
                    qif_name,
                    opts,
                    t,
                    cleared=cleared,
                    other=t.transfer_account,
                    amount=amount,
                    carved=False,
                )

    return _Collected(
        account_names=list(parsed.transactions.keys()),
        row_items=row_items,
        groups=list(groups.values()),
        all_hashes=all_hashes,
        skipped_counts=skipped_counts,
        zero_counts=zero_counts,
        scheduled_counts=scheduled_counts,
        investment_counts=investment_counts,
        split_transfer_counts=split_transfer_counts,
        split_remainder_counts=split_remainder_counts,
    )


def _unmapped_investment(
    parsed: core_qif.QifFile, account_map: dict[str, str] | None
) -> frozenset[str]:
    """Investment accounts the request does not map (issue #274, decision 84):
    left out like a skipped account, so bank legs into one degrade to one-leg
    rows on the bank side instead of requiring a mapping. Preview and commit
    derive it from the same request, so the preview still equals the commit.

    Left out by default because mapping one to a holdings-tracked brokerage
    adds every historical contribution to its postings balance while the
    purchases never land, and net worth (§8) would count them on top of the
    holdings."""
    mapped = account_map or {}
    return frozenset(name for name in parsed.investment_accounts if name not in mapped)


def _classify(
    collected: _Collected, existing: set[str], promotable: set[str]
) -> tuple[dict[str, _Counts], list[_Action]]:
    """Deterministic dry-run: classify every row into exactly one bucket and
    emit the actions commit will execute. ``existing`` is the set of content
    hashes already in the DB, so a re-import produces zero new rows;
    ``planned`` dedups within this run, so a hash is created at most once.
    Identical rows reach this point as distinct occurrences (decision 68).

    ``promotable`` (a subset of ``existing``, see :func:`_promotable_hashes`)
    holds the ``expected`` rows a file import created that may still post: a
    row whose identity is one of them and which the file now MARKS cleared
    promotes that row instead of deduping (decision 60, #310). Only the row's
    own mark counts — an account without marks is cleared by convention, which
    is no evidence that anything cleared since."""
    counts: dict[str, _Counts] = defaultdict(_Counts)
    for name in collected.account_names:
        counts[name]  # materialize so every file account appears in per_account
    # Skipped (mapped-to-null) accounts: every row lands in skipped_unmapped.
    for name, n in collected.skipped_counts.items():
        counts[name].in_file += n
        counts[name].skipped_unmapped += n
    # Zero-amount rows: filtered in _collect, structurally non-importable.
    for name, n in collected.zero_counts.items():
        counts[name].in_file += n
        counts[name].skipped_zero_amount += n
    # Quicken reminder rows: filtered in _collect, never history.
    for name, n in collected.scheduled_counts.items():
        counts[name].in_file += n
        counts[name].skipped_scheduled += n
    # QIF investment-section records: filtered in _collect, never bank rows.
    for name, n in collected.investment_counts.items():
        counts[name].in_file += n
        counts[name].skipped_investment += n
    # Split-row sub-counts (decision 87): never buckets.
    for name, n in collected.split_transfer_counts.items():
        counts[name].split_transfers += n
    for name, n in collected.split_remainder_counts.items():
        counts[name].split_remainders += n
    actions: list[_Action] = []
    planned: set[str] = set()
    claimed: set[str] = set()  # promotions planned in this run

    def _seen(content_hash: str) -> bool:
        return content_hash in existing or content_hash in planned

    def _promote(content_hash: str, cleared_mark: bool) -> bool:
        """Plan the promotion of the expected row holding ``content_hash``;
        False when the row cannot or need not be promoted (it dedups)."""
        if not cleared_mark or content_hash not in promotable or content_hash in claimed:
            return False
        claimed.add(content_hash)
        actions.append(_Promote(content_hash=content_hash))
        return True

    for item in collected.row_items:
        c = counts[item.qif_name]
        c.in_file += 1
        if not item.cutoff_ok:
            c.skipped_cutoff += 1
            continue
        if _seen(item.content_hash):
            # Opening balances always post, so there is never one to promote.
            if item.kind != "opening" and _promote(item.content_hash, item.cleared_mark):
                c.promoted += 1
            else:
                c.skipped_dedup += 1
            continue
        planned.add(item.content_hash)
        if item.kind == "opening":
            # Always posted, whatever its mark: an opening balance is
            # bookkeeping, not a bank line, so no bank row will ever clear it.
            c.opening_balances += 1
            actions.append(
                _OpeningCreate(
                    qif_name=item.qif_name,
                    amount=item.amount,
                    date=item.date,
                    payee=item.payee,
                    content_hash=item.content_hash,
                )
            )
        elif item.kind == "split":
            c.would_import += 1
            if not item.cleared:
                c.uncleared += 1
            actions.append(
                _SplitCreate(
                    qif_name=item.qif_name,
                    amount=item.amount,
                    date=item.date,
                    payee=item.payee,
                    description=item.description,
                    lines=item.lines,
                    content_hash=item.content_hash,
                    status=_status_for(item.cleared),
                )
            )
        else:
            c.would_import += 1
            if not item.cleared:
                c.uncleared += 1
            actions.append(
                _FlatCreate(
                    qif_name=item.qif_name,
                    amount=item.amount,
                    date=item.date,
                    payee=item.payee,
                    description=item.description,
                    qif_category=item.qif_category,
                    content_hash=item.content_hash,
                    status=_status_for(item.cleared),
                    placeholder=item.placeholder,
                )
            )

    for group in collected.groups:
        _classify_group(group, counts, actions, planned, _seen, _promote)

    return counts, actions


def _classify_group(
    group: _TransferGroup,
    counts: dict[str, _Counts],
    actions: list[_Action],
    planned: set[str],
    seen: Callable[[str], bool],
    promote: Callable[[str, bool], bool],
) -> None:
    """Classify one transfer (both legs, or one if the file is single-sided).

    The four cases (both legs evaluated against BOTH mapped cutoffs; a missing
    cutoff passes):
      * older than both   -> the 2-leg transfer, unless a prior split already
                             left a flat leg (flat-hash guard) or it's a re-import.
      * inside both        -> skip; the feed owns that window.
      * split              -> a one-leg flat on the OLDER side; auto_link_pass
                             absorbs the feed counterpart later. Never 2-leg here.

    Status (decision 60): a 2-leg transfer lands ``expected`` only when NO leg
    in the file is cleared — once either bank has seen it the money has moved,
    and posting it keeps the cleared side's balance right (the other side's bank
    row is absorbed when it arrives, §5). A one-leg flat follows its own leg.
    The same rule promotes what an earlier import left ``expected`` (#310): the
    transfer when any leg is now marked cleared, a one-leg flat when its own
    leg is. Every leg is counted ``promoted`` or ``skipped_dedup`` then.

    A leg carved out of a split row (decision 87) is planned exactly like any
    other, but counts nowhere: its row already counts once (``tally``).
    """

    def tally(leg: _Leg) -> _Counts:
        return _Counts() if leg.carved else counts[leg.qif_name]

    for leg in group.legs:
        tally(leg).in_file += 1

    leg0 = group.legs[0]
    both_older = leg0.self_older and leg0.other_older
    both_inside = (not leg0.self_older) and (not leg0.other_older)

    if both_inside:
        for leg in group.legs:
            tally(leg).skipped_cutoff += 1
        return

    def _dedup_or_promoted(leg: _Leg, promoted: bool) -> None:
        if promoted:
            tally(leg).promoted += 1
        else:
            tally(leg).skipped_dedup += 1

    if both_older:
        guard = seen(group.transfer_hash) or any(seen(leg.flat_hash) for leg in group.legs)
        if guard:
            if seen(group.transfer_hash):
                marked = any(leg.cleared_mark for leg in group.legs)
                promoted = promote(group.transfer_hash, marked)
                for leg in group.legs:
                    _dedup_or_promoted(leg, promoted)
            else:
                # A prior split left one-leg rows: each marked leg promotes
                # its own.
                for leg in group.legs:
                    _dedup_or_promoted(
                        leg, seen(leg.flat_hash) and promote(leg.flat_hash, leg.cleared_mark)
                    )
            return
        planned.add(group.transfer_hash)
        cleared = any(leg.cleared for leg in group.legs)
        for leg in group.legs:
            tally(leg).transfers += 1
            if not cleared:
                tally(leg).uncleared += 1
        from_name, to_name = (
            (leg0.qif_name, leg0.other_name)
            if leg0.amount < 0
            else (leg0.other_name, leg0.qif_name)
        )
        description = leg0.description if leg0.has_description else None
        actions.append(
            _TransferCreate(
                from_name=from_name,
                to_name=to_name,
                amount=abs(leg0.amount),
                date=leg0.date,
                description=description,
                content_hash=group.transfer_hash,
                status=_status_for(cleared),
            )
        )
        return

    # Split: the leg on the older side degrades to a one-leg flat; the inside
    # side is skipped (the feed owns it).
    for leg in group.legs:
        c = tally(leg)
        if not leg.self_older:
            c.skipped_cutoff += 1
            continue
        if seen(leg.flat_hash):
            _dedup_or_promoted(leg, promote(leg.flat_hash, leg.cleared_mark))
            continue
        planned.add(leg.flat_hash)
        c.one_leg_transfers += 1
        if not leg.cleared:
            c.uncleared += 1
        actions.append(
            _FlatCreate(
                qif_name=leg.qif_name,
                amount=leg.amount,
                date=leg.date,
                payee=None,
                description=leg.description,
                qif_category=None,
                content_hash=leg.flat_hash,
                status=_status_for(leg.cleared),
            )
        )


# ---------------------------------------------------------------------------
# DB reads (preview + commit share these)
# ---------------------------------------------------------------------------


def _existing_hashes(session: Session, hashes: set[str]) -> set[str]:
    """Which of ``hashes`` already exist in the ledger — chunked IN queries."""
    if not hashes:
        return set()
    found: set[str] = set()
    hash_list = list(hashes)
    for start in range(0, len(hash_list), _HASH_CHUNK):
        chunk = hash_list[start : start + _HASH_CHUNK]
        rows = session.execute(
            select(Transaction.content_hash).where(Transaction.content_hash.in_(chunk))
        ).scalars()
        found.update(h for h in rows if h is not None)
    return found


def _promotable_hashes(session: Session, hashes: set[str]) -> set[str]:
    """Which of ``hashes`` belong to an ``expected`` row a file import created
    that a re-import marking it cleared may post (decision 60, #310).

    Only a file import's own forecast row qualifies: origin ``import`` with no
    source fetch (never a feed row) and no schedule (never a schedule
    instance). Every real account it touches must carry no feed: on a
    feed-connected account the matcher settles the row against the bank's
    posted row when that arrives, and posting it here as well would count that
    bank line twice. It must be open too — closing an account voids its
    expected rows, so this is a backstop: a closed account takes no changes.
    """
    if not hashes:
        return set()
    feed_or_closed = (
        select(Posting.id)
        .join(Account, Account.id == Posting.account_id)
        .where(
            Posting.transaction_id == Transaction.id,
            Account.is_virtual.is_(False),
            or_(Account.connection_id.is_not(None), Account.closed_at.is_not(None)),
        )
        .exists()
    )
    found: set[str] = set()
    hash_list = list(hashes)
    for start in range(0, len(hash_list), _HASH_CHUNK):
        chunk = hash_list[start : start + _HASH_CHUNK]
        rows = session.execute(
            select(Transaction.content_hash).where(
                Transaction.content_hash.in_(chunk),
                Transaction.status == core.Status.EXPECTED.value,
                Transaction.origin == core.Origin.IMPORT.value,
                Transaction.source_fetch_id.is_(None),
                Transaction.schedule_id.is_(None),
                ~feed_or_closed,
            )
        ).scalars()
        found.update(h for h in rows if h is not None)
    return found


def _earliest_feed_date(session: Session, account_id: uuid.UUID) -> date_type | None:
    """Earliest feed-sourced (source_fetch_id set) transaction date on an
    account, any status including void — None when the feed hasn't touched it."""
    return session.execute(
        select(func.min(Transaction.date))
        .join(Posting, Posting.transaction_id == Transaction.id)
        .where(Posting.account_id == account_id, Transaction.source_fetch_id.is_not(None))
    ).scalar_one()


def _sign_block(txns: list[core_qif.QifTransaction], *, mapped_type: str | None) -> dict[str, Any]:
    """Sign diagnostic over the file's NON-transfer rows (raw amounts, so the
    suggestion is stable regardless of the invert option). ``suspect`` drives
    the invert default: true when a card/loan file looks income-positive.
    Investment-section records are not bank rows and never count."""
    positive = Decimal("0")
    negative = Decimal("0")
    pos_count = 0
    neg_count = 0
    for t in txns:
        if t.transfer_account is not None or t.investment:
            continue
        if t.amount > 0:
            positive += t.amount
            pos_count += 1
        elif t.amount < 0:
            negative += t.amount
            neg_count += 1
    net = positive + negative
    suspect = mapped_type in SUSPECT_TYPES and (net > 0 or pos_count > neg_count)
    return {
        "positive": str(positive),
        "negative": str(negative),
        "net": str(net),
        "suspect": suspect,
    }


# ---------------------------------------------------------------------------
# Preview (read-only)
# ---------------------------------------------------------------------------


def preview(
    session: Session,
    content: str,
    *,
    date_format: str = "mdy",
    account_map: dict[str, str] | None = None,
    account_options: dict[str, QifAccountOptions] | None = None,
    fmt: str = "qif",
    column_map: dict[str, str] | None = None,
    skip: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """DB-aware, strictly read-only mapping preview.

    Resolves the provided ``account_map`` (unknown targets -> LedgerError, 422
    at the API, exactly like import), computes per-account ``default_cutoff`` +
    sign diagnostic, and returns REAL dedup/cutoff counts for the given options.
    Writes nothing.

    ``fmt`` selects the parser (``qif``/``csv``); ``column_map`` is threaded to
    the CSV parser; ``skip`` names file accounts mapped to null (their rows are
    counted ``skipped_unmapped``, never imported). QIF and CSV share every step
    from here on — the dry-run is exact for both. An investment account the
    map leaves out is skipped the same way (``_unmapped_investment``).
    """
    parsed = _parse_content(content, fmt=fmt, date_format=date_format, column_map=column_map)
    options = account_options or {}
    account_map = account_map or {}
    skip = skip | _unmapped_investment(parsed, account_map)

    mapped: dict[str, Account] = {}
    for qif_name, ref in account_map.items():
        mapped[qif_name] = ledger_service.resolve_account(session, ref)

    collected = _collect(parsed, options, skip=skip, fallback=_FALLBACK_DESCRIPTION[fmt])
    existing = _existing_hashes(session, collected.all_hashes)
    counts, _actions = _classify(collected, existing, _promotable_hashes(session, existing))

    account_types = dict(parsed.accounts)
    accounts_out: list[dict[str, Any]] = []
    for name in parsed.transactions:
        acct = mapped.get(name)
        file_type = account_types.get(name, "checking")
        mapped_out: dict[str, Any] | None = None
        default_cutoff: str | None = None
        if acct is not None:
            mapped_out = {"id": str(acct.id), "path": acct.path, "type": acct.type}
            earliest = _earliest_feed_date(session, acct.id)
            if earliest is not None:
                default_cutoff = (earliest - DEFAULT_CUTOFF_MARGIN).isoformat()
        sign = _sign_block(
            parsed.transactions[name], mapped_type=acct.type if acct is not None else file_type
        )
        accounts_out.append(
            {
                "name": name,
                "type": file_type,
                "mapped": mapped_out,
                "transactions": len(parsed.transactions[name]),
                "default_cutoff": default_cutoff,
                "counts": counts[name].as_dict(),
                "sign": sign,
            }
        )

    categories = sorted(
        {
            t.category
            for txns in parsed.transactions.values()
            for t in txns
            if t.category and not t.investment
        }
        | {
            line.category
            for txns in parsed.transactions.values()
            for t in txns
            for line in t.splits
            if line.category and not t.investment
        }
    )
    dates = [t.date for txns in parsed.transactions.values() for t in txns if not t.investment]
    return {
        "accounts": accounts_out,
        "categories": categories,
        "date_range": {
            "start": min(dates).isoformat() if dates else None,
            "end": max(dates).isoformat() if dates else None,
        },
        "total_transactions": sum(len(txns) for txns in parsed.transactions.values()),
    }


# ---------------------------------------------------------------------------
# Import (commit)
# ---------------------------------------------------------------------------


def import_qif(
    session: Session,
    *,
    content: str,
    account_map: dict[str, str],
    category_map: dict[str, str] | None = None,
    date_format: str = "mdy",
    account_options: dict[str, QifAccountOptions] | None = None,
    fmt: str = "qif",
    column_map: dict[str, str] | None = None,
    skip: frozenset[str] = frozenset(),
    token: ApiToken | None,
) -> dict[str, Any]:
    """Import a QIF or CSV export. Idempotent under any option combination:
    re-running the SAME request produces zero new rows.

    account_map: file account name -> Purser account ref (id or path). A
    single-account file uses the parser's __default__ name. account_options:
    per-file-account cutoff/invert (keys match account_map; ``__default__``
    is honored as a fallback). ``fmt`` selects the parser (``qif``/``csv``);
    ``column_map`` is threaded to the CSV parser; ``skip`` names file accounts
    mapped to null (their rows are counted ``skipped_unmapped``, never imported,
    and transfers into them degrade to a one-leg flat on the imported side).
    An investment account ``account_map`` leaves out is skipped the same way
    (``_unmapped_investment``, issue #274); its investment-section records are
    counted ``skipped_investment`` either way.
    """
    parsed = _parse_content(content, fmt=fmt, date_format=date_format, column_map=column_map)
    skip = skip | _unmapped_investment(parsed, account_map)
    category_map = category_map or {}
    options = account_options or {}
    actor = token.id if token else None
    raw = _archive_upload(session, content, source=fmt)

    resolved: dict[str, Account] = {}
    for qif_name in parsed.transactions:
        if qif_name in skip:
            continue
        if qif_name not in account_map:
            raise LedgerError(f"no mapping for file account {qif_name!r}")
        resolved[qif_name] = ledger_service.resolve_account(session, account_map[qif_name])

    def _resolve(name: str) -> Account:
        if name not in resolved:
            if name not in account_map:
                raise LedgerError(f"transfer references unmapped file account {name!r}")
            resolved[name] = ledger_service.resolve_account(session, account_map[name])
        return resolved[name]

    collected = _collect(parsed, options, skip=skip, fallback=_FALLBACK_DESCRIPTION[fmt])
    existing = _existing_hashes(session, collected.all_hashes)
    counts, actions = _classify(collected, existing, _promotable_hashes(session, existing))

    created_categories: set[str] = set()
    transfers_created = 0

    for action in actions:
        if isinstance(action, _OpeningCreate):
            account = _resolve(action.qif_name)
            category = _resolve_opening_balance_category(session, actor)
            ledger_service.create_flat_transaction(
                session,
                account_ref=str(account.id),
                category_ref=str(category.id),
                amount=action.amount,
                date=action.date,
                payee_name=action.payee,
                description="Opening balance",
                requested_status=core.Status.POSTED,
                origin=core.Origin.IMPORT,
                token=None,
                content_hash=action.content_hash,
            )
        elif isinstance(action, _FlatCreate):
            account = _resolve(action.qif_name)
            if action.placeholder:
                category = _placeholder_category(session, action.amount)
            else:
                category = _resolve_category(
                    session,
                    action.qif_category,
                    action.amount,
                    category_map,
                    payee_name=action.payee,
                    actor_token_id=actor,
                    created=created_categories,
                )
            ledger_service.create_flat_transaction(
                session,
                account_ref=str(account.id),
                category_ref=str(category.id),
                amount=action.amount,
                date=action.date,
                payee_name=action.payee,
                description=action.description,
                requested_status=action.status,
                origin=core.Origin.IMPORT,
                token=None,
                content_hash=action.content_hash,
            )
        elif isinstance(action, _SplitCreate):
            account = _resolve(action.qif_name)
            lines = [
                core.SplitLine(
                    category_id=(
                        _placeholder_category(session, line.amount)
                        if line.category is None
                        else _resolve_category(
                            session,
                            line.category,
                            line.amount,
                            category_map,
                            actor_token_id=actor,
                            created=created_categories,
                        )
                    ).id,
                    amount=line.amount,
                    memo=line.memo,
                )
                for line in action.lines
            ]
            ledger_service.create_split_transaction(
                session,
                account_ref=str(account.id),
                amount=action.amount,
                lines=lines,
                date=action.date,
                payee_name=action.payee,
                description=action.description,
                requested_status=action.status,
                origin=core.Origin.IMPORT,
                token=None,
                content_hash=action.content_hash,
            )
        elif isinstance(action, _Promote):
            _promote_cleared(session, action.content_hash, actor=actor, fmt=fmt)
        else:
            outgoing = _resolve(action.from_name)
            incoming = _resolve(action.to_name)
            ledger_service.create_transfer(
                session,
                from_account_ref=str(outgoing.id),
                to_account_ref=str(incoming.id),
                amount=action.amount,
                date=action.date,
                description=action.description,
                requested_status=action.status,
                token=None,
                origin=core.Origin.IMPORT,
                content_hash=action.content_hash,
            )
            transfers_created += 1

    per_account = {name: counts[name].as_dict() for name in parsed.transactions}
    stats: dict[str, Any] = {
        "imported": sum(c.would_import for c in counts.values()),
        "skipped": sum(c.skipped_dedup for c in counts.values()),
        "transfers_created": transfers_created,
        "opening_balances": sum(c.opening_balances for c in counts.values()),
        "skipped_cutoff": sum(c.skipped_cutoff for c in counts.values()),
        "skipped_unmapped": sum(c.skipped_unmapped for c in counts.values()),
        "skipped_zero_amount": sum(c.skipped_zero_amount for c in counts.values()),
        "skipped_scheduled": sum(c.skipped_scheduled for c in counts.values()),
        "skipped_investment": sum(c.skipped_investment for c in counts.values()),
        "one_leg_transfers": sum(c.one_leg_transfers for c in counts.values()),
        # Expected rows the file now marks cleared, posted in place (#310);
        # counted per leg like `uncleared`.
        "promoted": sum(c.promoted for c in counts.values()),
        # File rows that landed `expected`, summed like `skipped`: a 2-leg
        # transfer counts once per leg the file carries.
        "uncleared": sum(c.uncleared for c in counts.values()),
        # Split rows (decision 87): transfer lines carved out as transfers,
        # and rows whose unexplained remainder went to the placeholder.
        "split_transfers": sum(c.split_transfers for c in counts.values()),
        "split_remainders": sum(c.split_remainders for c in counts.values()),
    }

    # Cross-source transfer pass: same-file pairs were linked above via the
    # [account] convention; this claims counterparts that arrived earlier
    # through SimpleFIN, and absorbs the one-leg degraded splits (DESIGN §5).
    from purser import transfers

    stats.update(transfers.auto_link_pass(session))

    result = {
        **stats,
        "categories_created": sorted(created_categories),
        "per_account": per_account,
    }
    audit.record(
        session,
        actor_token_id=actor,
        action=f"{fmt}.import",
        entity_type="system",
        after={**result, "raw_fetch_id": str(raw.id) if raw is not None else None},
    )
    return result


def _promote_cleared(
    session: Session, content_hash: str, *, actor: uuid.UUID | None, fmt: str
) -> None:
    """Post the ``expected`` row ``content_hash`` names, because its file now
    marks it cleared (decision 60, #310). The side effects of posting it by
    hand: balances dirtied from its date, a ``transaction.posted`` audit row
    (with the reason and source) and event."""
    tx = session.execute(
        select(Transaction)
        .where(Transaction.content_hash == content_hash)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one()
    if tx.status != core.Status.EXPECTED.value:
        # The plan was read in this transaction a moment ago; only a concurrent
        # writer (the matcher settling the row, a hand edit) changes it. Refuse
        # rather than post a row someone else has just decided about.
        raise LedgerError(f"transaction {tx.id} changed during the import; run it again")
    tx.status = core.Status.POSTED.value
    session.flush()
    # expected -> posted joins the balances (decision 38).
    balances.mark_dirty(session, tx.date)
    audit.record(
        session,
        actor_token_id=actor,
        action="transaction.posted",
        entity_type="transaction",
        entity_id=tx.id,
        before={"status": core.Status.EXPECTED.value},
        after={
            "status": core.Status.POSTED.value,
            "reason": "cleared_in_file",
            "source": f"{fmt}.import",
        },
    )
    events.emit(
        session,
        type_="transaction.posted",
        entity_type="transaction",
        entity_id=tx.id,
        payload={"date": tx.date.isoformat(), "description": tx.description},
    )


# ---------------------------------------------------------------------------
# Category resolution (writes; commit path only)
# ---------------------------------------------------------------------------


def _resolve_category(
    session: Session,
    qif_category: str | None,
    amount: Decimal,
    category_map: dict[str, str],
    *,
    payee_name: str | None = None,
    actor_token_id: uuid.UUID | None,
    created: set[str],
) -> Account:
    from purser.feed.importer import category_for_new_row

    if qif_category is None:
        # Payee memory first — uncategorized only when history has no answer.
        # (The batch-level qif.import audit covers provenance here.)
        category, _ = category_for_new_row(session, payee_name=payee_name, income=amount > 0)
        return category
    if qif_category in category_map:
        path = category_map[qif_category]
    else:
        root = "income" if amount > 0 else "expenses"
        try:
            labels = [core.path_label(part) for part in qif_category.split(":")]
        except ValueError as exc:  # '-', '???', 'Food:' — a 422, not a 500 (#214)
            raise LedgerError(
                f"file category {qif_category!r} yields no path ({exc}); "
                "map it to a category path with category_map"
            ) from exc
        path = ".".join([root, *labels])
    try:
        return ledger_service.account_by_path(session, path)
    except NotFoundError:
        parts = path.split(".")
        parent_id: uuid.UUID | None = None
        account: Account | None = None
        for depth in range(1, len(parts) + 1):
            subpath = ".".join(parts[:depth])
            try:
                account = ledger_service.account_by_path(session, subpath)
            except NotFoundError:
                account = ledger_service.create_account(
                    session,
                    name=parts[depth - 1].replace("_", " ").title(),
                    type_="category_income" if parts[0] == "income" else "category_expense",
                    parent_id=parent_id,
                    path=subpath,
                    actor_token_id=actor_token_id,
                )
                created.add(subpath)
            parent_id = account.id
        assert account is not None
        return account


def _placeholder_category(session: Session, amount: Decimal) -> Account:
    """The uncategorized placeholder for a split line no category names
    (decision 87) — deliberately not payee memory, which knows the payee's
    whole-row category, not this line's."""
    from purser.feed.importer import _uncategorized

    return _uncategorized(session, income=amount > 0)


def _resolve_opening_balance_category(
    session: Session, actor_token_id: uuid.UUID | None
) -> Account:
    try:
        return ledger_service.account_by_path(session, OPENING_BALANCE_CATEGORY)
    except NotFoundError:
        return ledger_service.create_account(
            session,
            name="Opening balances",
            type_="category_income",
            path=OPENING_BALANCE_CATEGORY,
            actor_token_id=actor_token_id,
        )
