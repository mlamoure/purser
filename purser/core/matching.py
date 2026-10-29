"""Pure matching logic: scoring, tiers, variance (DESIGN §5).

Amount is weighted SOFTLY by design — payee + date agreement with a
different amount is still a confident match; the difference is recorded as
variance, which is signal (estimates improve, threshold alerts fire).
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date as date_type
from decimal import Decimal
from difflib import SequenceMatcher
from enum import StrEnum
from uuid import UUID

from purser.core import ledger

DATE_WINDOW_DAYS = 5
AUTO_THRESHOLD = 0.80
PROPOSE_THRESHOLD = 0.55

WEIGHT_PAYEE = Decimal("0.45")
WEIGHT_DATE = Decimal("0.35")
WEIGHT_AMOUNT = Decimal("0.20")

# |variance| / |expected| above this emits a variance.threshold event.
VARIANCE_EVENT_THRESHOLD = Decimal("0.25")


class Tier(StrEnum):
    AUTO = "auto"
    PROPOSED = "proposed"
    NONE = "none"


@dataclass(frozen=True)
class MatchInput:
    """A transaction reduced to what matching needs (real-account leg)."""

    id: UUID
    date: date_type
    amount: Decimal  # the real-account delta
    normalized_payee: str
    # Real-leg account (issue #173): transfer pairing must never consume an
    # incoming row on the SAME account (a reversal/return) — greedy pairing
    # with a post-hoc filter silently lost the true cross-account
    # counterpart. None (expected↔posted matching) skips the check.
    account_id: UUID | None = None
    # The other real account of a two-leg row (issue #232): an expected
    # transfer and a merged transfer that agree on it agree on "who" as
    # surely as two payee strings ever could — merged rows carry no payee.
    counterparty_account_id: UUID | None = None
    # Transfer pairing only (issue #270, decision 65): a bank leg with a live
    # match (proposed or confirmed) to an expected TRANSFER is still half of
    # that transfer, so it stays in the pairing pool — but may pair only with
    # a row on this account (the expected's other side), and never with
    # another held row (one merged transfer cannot settle two expecteds).
    # None = unconstrained.
    pair_account_id: UUID | None = None


@dataclass(frozen=True)
class Scored:
    expected_id: UUID
    candidate_id: UUID
    score: float
    variance: Decimal  # posted - expected: the posted amount wins
    tier: Tier


def payee_similarity(a: str, b: str) -> float:
    """Similarity of two already-normalized payee strings in [0, 1]."""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    seq = SequenceMatcher(None, a, b).ratio()
    tokens_a, tokens_b = set(a.split()), set(b.split())
    jaccard = len(tokens_a & tokens_b) / len(tokens_a | tokens_b)
    return max(seq, jaccard)


def date_score(expected: date_type, candidate: date_type) -> float:
    delta = abs((candidate - expected).days)
    if delta > DATE_WINDOW_DAYS:
        return 0.0
    return 1.0 - (delta / (DATE_WINDOW_DAYS + 1))


def amount_score(expected: Decimal, candidate: Decimal) -> float:
    """Soft closeness in [0, 1]; opposite signs never score."""
    if expected == 0 or candidate == 0:
        return 0.0
    if (expected > 0) != (candidate > 0):
        return 0.0
    denominator = max(abs(expected), abs(candidate))
    relative_gap = abs(candidate - expected) / denominator
    return float(max(Decimal(0), 1 - relative_gap))


def score(expected: MatchInput, candidate: MatchInput) -> Scored:
    d = date_score(expected.date, candidate.date)
    if d == 0.0:
        tier_score = 0.0
    else:
        if (
            expected.counterparty_account_id is not None
            and expected.counterparty_account_id == candidate.counterparty_account_id
        ):
            p = 1.0  # same counterparty account = same "who" (issue #232)
        else:
            p = payee_similarity(expected.normalized_payee, candidate.normalized_payee)
        a = amount_score(expected.amount, candidate.amount)
        tier_score = float(
            WEIGHT_PAYEE * Decimal(str(p))
            + WEIGHT_DATE * Decimal(str(d))
            + WEIGHT_AMOUNT * Decimal(str(a))
        )
    if tier_score >= AUTO_THRESHOLD:
        tier = Tier.AUTO
    elif tier_score >= PROPOSE_THRESHOLD:
        tier = Tier.PROPOSED
    else:
        tier = Tier.NONE
    return Scored(
        expected_id=expected.id,
        candidate_id=candidate.id,
        score=round(tier_score, 4),
        variance=candidate.amount - expected.amount,
        tier=tier,
    )


def best_match(expected: MatchInput, candidates: list[MatchInput]) -> Scored | None:
    scored = [score(expected, c) for c in candidates]
    viable = [s for s in scored if s.tier is not Tier.NONE]
    if not viable:
        return None
    return max(viable, key=lambda s: s.score)


def variance_ratio(expected_amount: Decimal, variance: Decimal) -> Decimal | None:
    if expected_amount == 0:
        return None
    return (variance / abs(expected_amount)).quantize(Decimal("0.0001"))


def exceeds_variance_threshold(expected_amount: Decimal, variance: Decimal) -> bool:
    ratio = variance_ratio(expected_amount, variance)
    return ratio is not None and abs(ratio) > VARIANCE_EVENT_THRESHOLD


@dataclass(frozen=True)
class TransferCandidate:
    outgoing_id: UUID
    incoming_id: UUID
    amount: Decimal
    date_gap_days: int


TRANSFER_WINDOW_DAYS = 3
# The widest window a transfer_link rule may declare for its own account pair
# (decision 66). Two calendar weeks covers slow ACH and holiday settlement, and
# it is at most half the shortest month, so a monthly pair's leg can be inside
# the window of only one occurrence of its counterpart. Past that, pairing by
# exact amount alone is a categorization decision, not a pairing one.
TRANSFER_WINDOW_MAX_DAYS = 14

# The date window for one (outgoing, incoming) pair, or None when that pair
# may not pair at all. ``detect_transfer_pairs`` defaults to the global
# TRANSFER_WINDOW_DAYS for every pair.
PairWindow = Callable[[MatchInput, MatchInput], int | None]

# Tokens (after normalize_payee) that mark a description as transfer-like.
# Matched as whole tokens, never substrings — "repayment" must not hit.
TRANSFER_KEYWORDS: frozenset[str] = frozenset(
    {"transfer", "xfer", "tfr", "payment", "pmt", "pymt", "autopay"}
)
_MIN_COUNTERPARTY_LEN = 4


def has_transfer_signal(*, text: str, counterparty_name: str) -> bool:
    """True when a transaction's text names the transfer for us: it carries a
    transfer/payment-style keyword token, or it mentions the counterparty
    account by name ("CITICARD ONLINE" seen from checking, when the card
    account is named "Citi Card").

    Counterparty matching is boundary-aware, never bare substring — an
    account named "Chase" must not match "PURCHASE RETURN". Two forms hit:
    the name as a whole-token phrase, or a text token that IS (or starts
    with) the space-squashed name ("citicard" ~ "Citi Card").

    The amount+window pairing stays the primary gate; this signal is what
    promotes a candidate pair to a deterministic auto-link (DESIGN §5).
    """
    normalized = ledger.normalize_payee(text)
    if not normalized:
        return False
    tokens = normalized.split()
    if TRANSFER_KEYWORDS & set(tokens):
        return True
    return mentions_counterparty(text, counterparty_name)


# Tokens too generic to identify an account on their own ("Citi Card" is
# identified by "citi", never by "card").
_GENERIC_NAME_TOKENS: frozenset[str] = frozenset(
    {"card", "cards", "credit", "bank", "loan", "auto", "account", "savings", "checking", "the"}
)


def mentions_counterparty(text: str, counterparty_name: str, *, loose: bool = False) -> bool:
    """Does the text name the counterparty account? Strict (the auto-link
    test): the whole name as a phrase, or a token that is/starts with the
    space-squashed name ("citicard" ~ "Citi Card"). ``loose`` (the
    new-liability scan, #234) additionally accepts the name's first
    identifying token as a whole token — "CITI AUTOPAY" names "Citi Card" —
    still boundary-aware, never a bare substring, and never on a generic
    token like "card" or "bank"."""
    normalized = ledger.normalize_payee(text)
    counterparty = ledger.normalize_payee(counterparty_name)
    if not normalized or not counterparty:
        return False
    tokens = normalized.split()
    squashed = counterparty.replace(" ", "")
    if len(squashed) >= _MIN_COUNTERPARTY_LEN:
        if f" {counterparty} " in f" {normalized} ":
            return True
        if any(token.startswith(squashed) for token in tokens):
            return True
    if not loose:
        return False
    identifying = next(
        (
            t
            for t in counterparty.split()
            if len(t) >= _MIN_COUNTERPARTY_LEN and t not in _GENERIC_NAME_TOKENS
        ),
        None,
    )
    return identifying is not None and identifying in tokens


def _may_pair(out: MatchInput, inc: MatchInput) -> bool:
    """The structural pairing constraints, before amount and window: never
    the same account (issue #173), and a held leg (``pair_account_id``,
    issue #270) only with an unheld row on the account it is held for."""
    if out.account_id is not None and out.account_id == inc.account_id:
        return False
    if out.pair_account_id is not None and inc.pair_account_id is not None:
        return False
    if out.pair_account_id is not None and inc.account_id != out.pair_account_id:
        return False
    return inc.pair_account_id is None or out.account_id == inc.pair_account_id


def detect_transfer_pairs(
    transactions: list[MatchInput], *, window: PairWindow | None = None
) -> list[TransferCandidate]:
    """Opposite-amount pairs within the window -> merge candidates.

    Pairs by amount and date, and — when both sides carry ``account_id`` —
    never across the SAME account (issue #173: greedy consumption before
    the caller's cross-account filter silently retired an out-row whose
    true counterpart lived elsewhere). A held leg (``pair_account_id``)
    pairs only with its expected's other side, checked here for the same
    reason: inside the greedy loop, never as a post-filter (decision 65).
    Never merges — candidates only.

    ``window`` gives each pair its own date window, or None to rule the pair
    out; omitted, every pair gets the global ±TRANSFER_WINDOW_DAYS. The only
    caller that passes one is the widened pass over the account pairs that
    transfer_link rules declare (decision 66), run on the rows the standard
    window left unpaired. The greedy order is the same either way: outgoing
    rows by date, each taking the earliest-dated incoming row that fits.
    """
    candidates: list[TransferCandidate] = []
    outs = [t for t in transactions if t.amount < 0]
    ins = [t for t in transactions if t.amount > 0]
    used: set[UUID] = set()
    for out in sorted(outs, key=lambda t: t.date):
        for inc in sorted(ins, key=lambda t: t.date):
            if inc.id in used:
                continue
            if not _may_pair(out, inc):
                continue
            if inc.amount != -out.amount:
                continue
            limit = TRANSFER_WINDOW_DAYS if window is None else window(out, inc)
            if limit is None:
                continue
            gap = abs((inc.date - out.date).days)
            if gap <= limit:
                candidates.append(
                    TransferCandidate(
                        outgoing_id=out.id,
                        incoming_id=inc.id,
                        amount=inc.amount,
                        date_gap_days=gap,
                    )
                )
                used.add(inc.id)
                break
    return candidates
