"""Pure ledger logic: flat-form expansion, reversals, hashing, lifecycle.

No I/O in this module — it is the mypy --strict core. The service layer
(purser/ledger/) persists what these functions compute.
"""

import hashlib
import re
import unicodedata
from collections.abc import Hashable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date as date_type
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

QUANT = Decimal("0.0001")


class Status(StrEnum):
    EXPECTED = "expected"
    PENDING = "pending"
    POSTED = "posted"
    VOID = "void"


class Origin(StrEnum):
    IMPORT = "import"
    AGENT = "agent"
    HUMAN = "human"
    RULE = "rule"
    SYSTEM = "system"


# expected → pending → posted → void (DESIGN §4). No BARE transition leaves
# posted: corrections are linked reversal pairs, and transfer merges / match
# supersedes void a posted row only together with a superseded_by lineage
# link (service + DB trigger, not this table). Void is terminal.
_TRANSITIONS: dict[Status, frozenset[Status]] = {
    Status.EXPECTED: frozenset({Status.PENDING, Status.POSTED, Status.VOID}),
    Status.PENDING: frozenset({Status.POSTED, Status.VOID}),
    Status.POSTED: frozenset(),
    Status.VOID: frozenset(),
}


def can_transition(old: Status, new: Status) -> bool:
    return new in _TRANSITIONS[old]


@dataclass(frozen=True)
class PostingSpec:
    account_id: UUID
    amount: Decimal
    currency: str
    memo: str | None = None


def quantize(amount: Decimal) -> Decimal:
    return amount.quantize(QUANT)


def expand_flat(
    *,
    account_id: UUID,
    category_id: UUID,
    amount: Decimal,
    currency: str,
    memo: str | None = None,
) -> list[PostingSpec]:
    """Expand the flat form into a balanced pair.

    Sign convention (DESIGN §3.3): the flat amount is the real account's
    delta; the category absorbs the negation. citicard -14.00 dining →
    postings (citicard -14.00, expenses.dining +14.00).
    """
    amt = quantize(amount)
    return [
        PostingSpec(account_id=account_id, amount=amt, currency=currency, memo=memo),
        PostingSpec(account_id=category_id, amount=-amt, currency=currency, memo=memo),
    ]


def expand_transfer(
    *,
    from_account_id: UUID,
    to_account_id: UUID,
    amount: Decimal,
    currency: str,
    memo: str | None = None,
) -> list[PostingSpec]:
    """A transfer is ONE transaction with two real-account postings."""
    amt = quantize(amount)
    return [
        PostingSpec(account_id=from_account_id, amount=-amt, currency=currency, memo=memo),
        PostingSpec(account_id=to_account_id, amount=amt, currency=currency, memo=memo),
    ]


def split_transfer_remainder(outgoing_amount: Decimal, incoming_amount: Decimal) -> Decimal:
    """The interest/fee remainder of a split loan payment (DESIGN §3.3,
    decision 62): what the cash leg paid beyond the principal leg.

    ``outgoing_amount`` is the cash leg on the paying account (negative);
    ``incoming_amount`` is the principal leg on the loan (positive). The
    bounds ``0 < incoming < |outgoing|`` are strict: equal legs are an
    ordinary transfer with nothing to book, and a principal leg larger than
    the cash that paid it is not a loan payment — booking the difference
    would invent negative "interest". Raises ``ValueError`` naming the bound.
    """
    outgoing = quantize(outgoing_amount)
    incoming = quantize(incoming_amount)
    if outgoing >= 0:
        raise ValueError(f"the outgoing (cash) leg must be negative, got {outgoing}")
    if incoming <= 0:
        raise ValueError(f"the incoming (principal) leg must be positive, got {incoming}")
    if incoming == -outgoing:
        raise ValueError(
            f"the legs already cancel ({outgoing} / {incoming}): that is an ordinary "
            "transfer, with no remainder to book"
        )
    if incoming > -outgoing:
        raise ValueError(
            f"the incoming (principal) leg {incoming} exceeds the outgoing (cash) leg "
            f"{-outgoing}: a split loan payment books the interest/fee part of the cash, "
            "so incoming must be less than |outgoing|"
        )
    return -(outgoing + incoming)


def expand_split_transfer(
    *,
    outgoing_account_id: UUID,
    outgoing_amount: Decimal,
    incoming_account_id: UUID,
    incoming_amount: Decimal,
    remainder_category_id: UUID,
    currency: str,
    memo: str | None = None,
) -> list[PostingSpec]:
    """A split loan payment is ONE transaction (DESIGN §3.3, decision 62):
    the exact cash leg, the exact principal leg, and one balancing posting to
    the interest/fee category. Zero-sum by construction — the remainder IS
    the negated sum of the two bank legs — and never a synthetic
    full-payment liability leg. Raises ``ValueError`` outside the bounds of
    :func:`split_transfer_remainder`.
    """
    remainder = split_transfer_remainder(outgoing_amount, incoming_amount)
    return [
        PostingSpec(
            account_id=outgoing_account_id, amount=quantize(outgoing_amount), currency=currency
        ),
        PostingSpec(
            account_id=incoming_account_id, amount=quantize(incoming_amount), currency=currency
        ),
        PostingSpec(
            account_id=remainder_category_id, amount=remainder, currency=currency, memo=memo
        ),
    ]


@dataclass(frozen=True)
class SplitLine:
    """One category line of a split transaction (DESIGN §3.3, decision 87).
    ``amount`` is the share of the real account's delta the line carries, in
    the flat form's sign (negative = money out), so its category leg books the
    negation."""

    category_id: UUID
    amount: Decimal
    memo: str | None = None


def expand_split(
    *,
    account_id: UUID,
    amount: Decimal,
    lines: Sequence[SplitLine],
    currency: str,
    memo: str | None = None,
) -> list[PostingSpec]:
    """A split transaction is exactly one real leg plus two or more category
    legs (DESIGN §3.3, decision 87): the real leg carries ``amount`` and each
    line books its negation to its category, so the postings are zero-sum by
    construction. It is not a transfer (``transfer_shape`` returns None).

    Raises ``ValueError`` unless ``amount`` is nonzero, there are at least two
    lines (one line is a flat row), no line is zero, and the lines sum to
    ``amount`` exactly.
    """
    amt = quantize(amount)
    if amt == 0:
        raise ValueError("a split's amount must be nonzero")
    if len(lines) < 2:
        raise ValueError("a split needs at least two category lines; one line is a flat row")
    shares = [quantize(line.amount) for line in lines]
    if any(share == 0 for share in shares):
        raise ValueError("a split line's amount must be nonzero")
    total = sum(shares, Decimal(0))
    if total != amt:
        raise ValueError(f"the split lines sum to {total}, not to the amount {amt}")
    return [
        PostingSpec(account_id=account_id, amount=amt, currency=currency, memo=memo),
        *(
            PostingSpec(
                account_id=line.category_id, amount=-share, currency=currency, memo=line.memo
            )
            for line, share in zip(lines, shares, strict=True)
        ),
    ]


class TransferShape(StrEnum):
    """The transfer shapes Purser creates (DESIGN §3.3, decision 62)."""

    PLAIN = "plain"  # two real legs that cancel
    SPLIT = "split"  # cash leg + principal leg + one interest/fee category leg


@dataclass(frozen=True)
class Leg:
    """One posting as the transfer classifier sees it (no ORM, no I/O)."""

    account_id: UUID
    amount: Decimal
    is_virtual: bool


def transfer_shape(legs: Sequence[Leg]) -> TransferShape | None:
    """Which transfer shape a transaction's postings form — or None.

    A row is a transfer exactly when its postings are one of the shapes
    Purser itself creates, never merely "it touches two real accounts": a
    multi-posting row (a paycheck split into net pay, deductions and a
    savings leg, #245) is not a transfer however many real accounts it has.

    - PLAIN: exactly two real legs, on different accounts, that cancel.
    - SPLIT: exactly two real legs on different accounts with
      ``0 < incoming < |outgoing|``, plus exactly one category leg carrying
      the remainder ``-(outgoing + incoming)``.
    """
    real = [leg for leg in legs if not leg.is_virtual]
    virtual = [leg for leg in legs if leg.is_virtual]
    if len(real) != 2 or real[0].account_id == real[1].account_id:
        return None
    outgoing, incoming = sorted(real, key=lambda leg: leg.amount)
    if not virtual:
        if outgoing.amount < 0 and outgoing.amount + incoming.amount == 0:
            return TransferShape.PLAIN
        return None
    if len(virtual) != 1 or not 0 < incoming.amount < -outgoing.amount:
        return None
    if virtual[0].amount != -(outgoing.amount + incoming.amount):
        return None
    return TransferShape.SPLIT


def split_line_count(legs: Sequence[Leg]) -> int:
    """The category-line count of a split transaction — one real leg plus two
    or more category legs (decision 87) — and 0 for every other row: a flat
    row, a transfer, a split loan payment."""
    real = sum(1 for leg in legs if not leg.is_virtual)
    virtual = sum(1 for leg in legs if leg.is_virtual)
    return virtual if real == 1 and virtual >= 2 else 0


def reversal_postings(postings: list[PostingSpec]) -> list[PostingSpec]:
    """Negate every posting — the reversal leg of a correction pair."""
    return [
        PostingSpec(
            account_id=p.account_id,
            amount=-p.amount,
            currency=p.currency,
            memo=p.memo,
        )
        for p in postings
    ]


def balance_by_currency(postings: list[PostingSpec]) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = {}
    for p in postings:
        totals[p.currency] = totals.get(p.currency, Decimal(0)) + p.amount
    return totals


def is_balanced(postings: list[PostingSpec]) -> bool:
    return all(total == 0 for total in balance_by_currency(postings).values())


def parse_transfer_target(category_ref: str) -> str | None:
    """Quicken-style bracket category: '[assets.savings]' -> 'assets.savings'.

    Returns the inner account reference (path or id — resolution is the
    caller's job) or None when the ref is not bracket form. An empty
    bracket ('[]') is not a target.
    """
    ref = category_ref.strip()
    if len(ref) >= 3 and ref.startswith("[") and ref.endswith("]"):
        inner = ref[1:-1].strip()
        return inner or None
    return None


_PUNCT_RE = re.compile(r"[^a-z0-9 ]+")
_WS_RE = re.compile(r"\s+")


def normalize_payee(name: str) -> str:
    """Casefold, strip accents/punctuation, collapse whitespace.

    'CHIPOTLE 2419  DENVER CO' and 'Chipotle #2419 Denver, CO' normalize to
    the same string; used for payee identity and match scoring.
    """
    text = unicodedata.normalize("NFKD", name)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.casefold()
    text = _PUNCT_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def occurrence_part(occurrence: int) -> list[str]:
    """The canonical-string part an occurrence ordinal adds (DESIGN §7.1,
    decision 68): nothing for the first occurrence — so every identity hashed
    before the ordinal existed is unchanged — and ``#n`` for the n-th. A
    normalized description never contains ``#`` or ``|``, so a qualified
    canonical string can never equal an unqualified one. Raises ``ValueError``
    below 1."""
    if occurrence < 1:
        raise ValueError(f"occurrence is 1-based, got {occurrence}")
    return [] if occurrence == 1 else [f"#{occurrence}"]


def content_hash(
    *,
    external_account_id: str,
    date: date_type,
    amount: Decimal,
    description: str,
    occurrence: int = 1,
) -> str:
    """Dedup identity for imported rows (DESIGN §7).

    SimpleFIN transaction ids are unstable — identity is the content the
    bank sent: external account + date + amount + normalized description,
    plus the row's ``occurrence`` among identical rows in one payload (see
    :func:`occurrence_ordinals`). Occurrence 1 hashes exactly as before the
    ordinal existed.
    """
    canonical = "|".join(
        [
            external_account_id,
            date.isoformat(),
            str(quantize(amount)),
            normalize_payee(description),
            *occurrence_part(occurrence),
        ]
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


class OccurrenceCounter[K: Hashable]:
    """Numbers equal keys in the order they are seen: calling it with a key
    returns 1 the first time, 2 the second, and so on (DESIGN §7.1, decision
    68).

    An import numbers identical rows within ONE payload this way, so two
    same-day coffees at the same price are two identities (occurrence 1 and
    2) instead of one. A counter lives for one payload: the count restarts in
    every fetch and every file, which is what makes re-fetching an
    overlapping window — or re-importing the same file — land on identities
    that already exist.
    """

    def __init__(self) -> None:
        self._seen: dict[K, int] = {}

    def __call__(self, key: K) -> int:
        occurrence = self._seen.get(key, 0) + 1
        self._seen[key] = occurrence
        return occurrence


def occurrence_ordinals[K: Hashable](keys: Iterable[K]) -> list[int]:
    """The occurrence of each key among the equal keys before it, in sequence
    order: ``[a, b, a, a]`` -> ``[1, 1, 2, 3]`` (see :class:`OccurrenceCounter`)."""
    count = OccurrenceCounter[K]()
    return [count(key) for key in keys]


_MASK_CHARS = frozenset("Xx*")


def masked_variant(a: str, b: str) -> bool:
    """True when ``a`` and ``b`` are the same text except that one side
    masks digits the other side shows (DESIGN §7, decision 44).

    Providers re-deliver rows with the reference number partially masked
    between fetches (``ER13529574`` → ``ERXXXX9574``), which changes the
    content hash. Positional and symmetric: every index must match exactly
    OR pair a mask character (X/x/*) with a digit, with at least one such
    masked-digit position. Distinct visible digits never match — this is
    identity under masking, not fuzzy similarity.
    """
    if len(a) != len(b):
        return False
    masked_positions = 0
    for ca, cb in zip(a, b, strict=True):
        if ca == cb:
            continue
        if (ca in _MASK_CHARS and cb.isdigit()) or (cb in _MASK_CHARS and ca.isdigit()):
            masked_positions += 1
            continue
        return False
    return masked_positions > 0


_SLUG_RE = re.compile(r"[^a-z0-9_]+")


def path_label(name: str) -> str:
    """An ltree label for an account name: lowercase [a-z0-9_]."""
    label = _SLUG_RE.sub("_", name.casefold().strip()).strip("_")
    if not label:
        raise ValueError(f"cannot derive an ltree label from {name!r}")
    return label


# PostgreSQL 16 ltree input limits (contrib/ltree): a label is at most 1000
# characters (longer -> SQLSTATE 42622), a path at most 65535 labels (more ->
# 54000). Everything else malformed is 42601 "ltree syntax error".
LTREE_LABEL_MAX_CHARS = 1000
LTREE_MAX_LABELS = 65535

_LABEL_ASCII = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")


class InvalidPathError(ValueError):
    """A caller-supplied ltree path Postgres would refuse to parse."""


def _label_char(ch: str) -> bool:
    """One character of an ltree label, per PG16's ``ISLABEL``: alphanumeric
    under the database locale, ``_`` or ``-``.

    ASCII is locale-independent, so it is checked exactly. Beyond ASCII,
    Postgres asks the database's LC_CTYPE: the UTF-8 locales Purser ships on
    admit the letters and digits of any script, vowel signs included
    (``café``, ``日本``, ``खाना``); a C-locale database refuses every
    non-ASCII byte. This approximates the UTF-8 set by Unicode category
    (letters, marks, decimal digits, letter numbers) and rejects whitespace,
    punctuation, symbols and controls (curly quotes, NBSP, ``€``) everywhere.
    What it admits but the database still refuses — any non-ASCII under a C
    locale, a bare combining accent from NFD text — meets the ltree-refusal
    backstop: a 422, never a 500.
    """
    if ch.isascii():
        return ch in _LABEL_ASCII
    category = unicodedata.category(ch)
    return category[0] in "LM" or category in ("Nd", "Nl")


def _clip(text: str, limit: int = 60) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _label_problem(label: str) -> str | None:
    """What is wrong with one label, phrased to follow 'segment N', or None."""
    if not label:
        return " is empty; segments are separated by single dots, with no leading or trailing dot"
    if len(label) > LTREE_LABEL_MAX_CHARS:
        return (
            f" ({_clip(label)!r}) is {len(label)} characters long; "
            f"at most {LTREE_LABEL_MAX_CHARS} are allowed"
        )
    bad = next((ch for ch in label if not _label_char(ch)), None)
    if bad is not None:
        return (
            f" ({_clip(label)!r}) contains {bad!r}; "
            "segments allow only letters, digits, '_' and '-'"
        )
    return None


def validate_path(path: str) -> str:
    """Check a caller-supplied dotted ltree path against the PostgreSQL 16
    label grammar before it reaches SQL (issue #214). Returns ``path``
    unchanged; raises ``InvalidPathError`` naming the offending segment.

    A path is one or more labels joined by single dots; a label is 1 to
    ``LTREE_LABEL_MAX_CHARS`` characters, each a letter, digit, ``_`` or
    ``-`` (a leading or trailing hyphen is legal in PG16); at most
    ``LTREE_MAX_LABELS`` labels. The empty string is refused although ltree
    parses it as the zero-label path: no account lives there, and a subtree
    filter of ``''`` would silently match everything.

    This validates, it never repairs — ``path_label`` is the slugifier that
    derives a label from a free-text name.
    """
    if not path:
        raise InvalidPathError("path is empty; expected dot-separated labels like expenses.dining")
    labels = path.split(".")
    if len(labels) > LTREE_MAX_LABELS:
        raise InvalidPathError(
            f"invalid path {_clip(path)!r}: {len(labels)} segments; "
            f"at most {LTREE_MAX_LABELS} are allowed"
        )
    for position, label in enumerate(labels, start=1):
        problem = _label_problem(label)
        if problem is not None:
            raise InvalidPathError(f"invalid path {_clip(path)!r}: segment {position}{problem}")
    return path
