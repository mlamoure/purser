"""Deterministic rule predicates (DESIGN decision 46, issue #174).

Pure: no I/O, no session, no ORM. The service layer decrypts a rule row into
a `Predicate` and asks `matches()`. Everything here is `mypy --strict`.

The predicate is **structured, never regex**. Three reasons, all load-bearing:
it runs at the import chokepoint, where an AI-authored catastrophic-backtracking
pattern would stall every sync; it has to be explainable in an audit row and in
the reviewer guidance (`payee contains "amazon"` says something,
`/^(?:AMZN|AMAZON).*\\d{3}/` says nothing); and a regex invites patterns written
against raw bank bytes, which `normalize_payee`, `feed_invert` (decision 43) and
the masked-reference guard (decision 44) all move underneath.
"""

from dataclasses import dataclass
from datetime import date as date_type
from decimal import Decimal
from enum import StrEnum

from purser.core.ledger import normalize_payee


class MatchField(StrEnum):
    PAYEE = "payee"
    DESCRIPTION = "description"
    PAYEE_OR_DESCRIPTION = "payee_or_description"


class MatchMode(StrEnum):
    EQUALS = "equals"
    PREFIX = "prefix"
    CONTAINS = "contains"


class Direction(StrEnum):
    DEBIT = "debit"
    CREDIT = "credit"


@dataclass(frozen=True)
class Candidate:
    """The facts a predicate may read about a row.

    ``amount`` is the real leg's signed amount **after** ``feed_invert``
    (decision 43), so a rule is written in the user's sign world rather than
    the bank's.
    """

    account_id: str
    amount: Decimal
    date: date_type
    payee: str | None = None
    description: str | None = None


@dataclass(frozen=True)
class Predicate:
    """Every field optional; the AND of whatever is set. All-None matches
    everything — deliberately, so a bare `categorize` rule scoped only by
    account is expressible."""

    account_ids: frozenset[str] | None = None
    match_field: MatchField | None = None
    match_mode: MatchMode | None = None
    match_text: str | None = None
    amount_exact: Decimal | None = None
    amount_min: Decimal | None = None
    amount_max: Decimal | None = None
    direction: Direction | None = None
    effective_from: date_type | None = None


def _haystack(candidate: Candidate, field: MatchField) -> str:
    if field is MatchField.PAYEE:
        return normalize_payee(candidate.payee or "")
    if field is MatchField.DESCRIPTION:
        return normalize_payee(candidate.description or "")
    both = f"{candidate.payee or ''} {candidate.description or ''}"
    return normalize_payee(both)


def text_matches(haystack: str, needle: str, mode: MatchMode) -> bool:
    """Token-boundary-aware matching over already-normalized strings.

    The space-padding trick is the one `has_transfer_signal` already uses, and
    it is why `chase` cannot match inside `purchase return` — the exact footgun
    `core/matching.py` documents. `prefix` anchors at the start AND ends on a
    boundary, so `star` does not match `starbucks` while `star` does match
    `star market`.
    """
    if not needle:
        return False
    if not haystack:
        return False
    if mode is MatchMode.EQUALS:
        return haystack == needle
    if mode is MatchMode.CONTAINS:
        return f" {needle} " in f" {haystack} "
    padded = f"{haystack} "
    return padded.startswith(f"{needle} ")


def matches(predicate: Predicate, candidate: Candidate) -> bool:
    """True when every constraint the predicate sets holds for the candidate.

    Monotone by construction: adding a constraint can only ever turn a match
    into a non-match, never the reverse. The property tests pin that.
    """
    if predicate.effective_from is not None and candidate.date < predicate.effective_from:
        return False
    if predicate.account_ids is not None and candidate.account_id not in predicate.account_ids:
        return False
    if predicate.direction is not None:
        # Zero is neither; the importer skips zero-amount rows anyway.
        if predicate.direction is Direction.DEBIT and candidate.amount >= 0:
            return False
        if predicate.direction is Direction.CREDIT and candidate.amount <= 0:
            return False
    if predicate.amount_exact is not None and candidate.amount != predicate.amount_exact:
        return False
    if predicate.amount_min is not None and candidate.amount < predicate.amount_min:
        return False
    if predicate.amount_max is not None and candidate.amount > predicate.amount_max:
        return False
    if predicate.match_field is not None:
        # The DB CHECK keeps these three set or unset together; assert rather
        # than silently treating a half-built predicate as unconstrained.
        assert predicate.match_mode is not None
        assert predicate.match_text is not None
        haystack = _haystack(candidate, predicate.match_field)
        if not text_matches(haystack, normalize_payee(predicate.match_text), predicate.match_mode):
            return False
    return True
