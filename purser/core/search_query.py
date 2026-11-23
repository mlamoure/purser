"""The search query grammar (#141, decision 135): one syntax for the
register's search box, REST ``q`` and MCP ``q``. Pure: no I/O.

    query  := clause ( WS clause )*        every clause must hold (AND)
    clause := [ "-" ] ( field ":" value | word | '"' phrase '"' )

- A plain word or a ``"phrase"`` is found in the payee, the description or a
  memo, as a substring of the folded text (``star`` finds "Starbucks").
  ``-`` in front excludes the rows that match instead.
- ``payee:`` ``desc:`` ``memo:`` look in that field only. ``payee:py_...``
  (or a bare ``py_...``) is a payee reference: exactly that payee.
- ``account:`` and ``category:`` match account and category names or paths
  (a category's subtree included); ``amount:`` compares the magnitude of the
  row's account leg (``42.10``, ``>100``, ``<=20``, ``10..20``); ``date:``
  takes a year, a month or a day, a comparison or a range; ``status:`` one or
  more statuses; ``is:`` ``unreviewed`` ``reviewed`` ``debit`` ``credit``.
- Case, accents and punctuation are folded everywhere (``7-eleven`` is
  ``7 eleven``); letters and digits of every script are kept.
- A ``word:`` that is not a field is ordinary text (bank descriptions
  contain colons), unless it is one letter away from a field's name, which
  is an error naming the fields. An empty value (``payee:`` as you type) is
  ignored. There is no ``OR``.
"""

import calendar
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

TEXT_FIELDS = ("payee", "desc", "memo")
FIELDS = ("payee", "desc", "memo", "account", "category", "amount", "date", "status", "is")
ALIASES = {"description": "desc"}
STATUSES = ("expected", "pending", "posted", "void")
IS_VALUES = ("unreviewed", "reviewed", "debit", "credit")
PAYEE_REF_RE = re.compile(r"py_[a-z2-7]{10}")

_ASCII_OTHER = re.compile(r"[^a-z0-9]+")
_OTHER = re.compile(r"[\W_]+")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_DATE = re.compile(r"(\d{4})(?:-(\d{1,2})(?:-(\d{1,2}))?)?")


class QueryError(ValueError):
    """A query Purser cannot read; the message says how to write it."""


def fold(text: str) -> str:
    """The searchable form of a text: accents removed, case folded, every
    run of characters that are not letters or digits made one space."""
    if text.isascii():
        return _ASCII_OTHER.sub(" ", text.lower()).strip()
    decomposed = unicodedata.normalize("NFKD", text)
    bare = "".join(c for c in decomposed if not unicodedata.combining(c))
    return _OTHER.sub(" ", bare.casefold()).strip()


@dataclass(frozen=True)
class Text:
    """Folded text to find: ``field`` is ``any``, ``payee``, ``desc`` or
    ``memo``."""

    field: str
    text: str
    negated: bool = False


@dataclass(frozen=True)
class AmountRange:
    """Bounds on a magnitude; ``None`` is open."""

    minimum: Decimal | None = None
    maximum: Decimal | None = None
    min_exclusive: bool = False
    max_exclusive: bool = False


@dataclass(frozen=True)
class Query:
    text: tuple[Text, ...] = ()
    payee_refs: tuple[tuple[str, bool], ...] = ()  # (reference, negated)
    accounts: tuple[tuple[str, bool], ...] = ()  # (name or path part, negated)
    categories: tuple[tuple[str, bool], ...] = ()
    amounts: tuple[AmountRange, ...] = ()
    start: date | None = None
    end: date | None = None
    statuses: frozenset[str] | None = None
    reviewed: bool | None = None
    direction: str | None = None  # "debit" | "credit"
    # The clauses contradict each other (is:debit is:credit, disjoint
    # status lists, an empty date range): nothing can match.
    never: bool = False

    @property
    def positive_text(self) -> tuple[Text, ...]:
        return tuple(c for c in self.text if not c.negated)


@dataclass
class _Builder:
    text: list[Text] = field(default_factory=list)
    payee_refs: list[tuple[str, bool]] = field(default_factory=list)
    accounts: list[tuple[str, bool]] = field(default_factory=list)
    categories: list[tuple[str, bool]] = field(default_factory=list)
    amounts: list[AmountRange] = field(default_factory=list)
    start: date | None = None
    end: date | None = None
    statuses: frozenset[str] | None = None
    reviewed: bool | None = None
    direction: str | None = None
    never: bool = False


def _distance(a: str, b: str) -> int:
    """Edit distance with adjacent transpositions (optimal string alignment)."""
    rows = [list(range(len(b) + 1))]
    for i in range(1, len(a) + 1):
        row = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            row[j] = min(rows[i - 1][j] + 1, row[j - 1] + 1, rows[i - 1][j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                row[j] = min(row[j], rows[i - 2][j - 2] + 1)
        rows.append(row)
    return rows[-1][-1]


def _field_list() -> str:
    return ", ".join(f"{name}:" for name in FIELDS)


def _near_field(name: str) -> str | None:
    if len(name) < 3:
        return None
    for known in (*FIELDS, *ALIASES):
        if len(known) >= 3 and _distance(name, known) == 1:
            return known
    return None


def _amount(value: str) -> Decimal:
    plain = value.replace("$", "").replace(",", "").strip()
    if plain.startswith("-"):
        raise QueryError(
            "amount: compares magnitudes, so it takes no sign; "
            "use is:debit or is:credit for the direction"
        )
    if not _NUMBER.fullmatch(plain):
        raise QueryError(
            f"amount:{value} is not a number; write amount:42.10, amount:>100, "
            "amount:<=20 or amount:10..20"
        )
    return Decimal(plain)


def _amount_clause(value: str) -> AmountRange:
    if ".." in value:
        low, _, high = value.partition("..")
        bounds = AmountRange(
            minimum=_amount(low) if low.strip() else None,
            maximum=_amount(high) if high.strip() else None,
        )
        if bounds.minimum is None and bounds.maximum is None:
            raise QueryError("amount:.. needs at least one bound, as in amount:10..20")
        return bounds
    for op in (">=", "<=", ">", "<"):
        if value.startswith(op):
            number = _amount(value[len(op) :])
            if op[0] == ">":
                return AmountRange(minimum=number, min_exclusive=op == ">")
            return AmountRange(maximum=number, max_exclusive=op == "<")
    number = _amount(value)
    return AmountRange(minimum=number, maximum=number)


def _span(value: str) -> tuple[date, date]:
    """The first and last day a year, month or day names."""
    match = _DATE.fullmatch(value.strip())
    if match is None:
        raise QueryError(
            f"date:{value} is not a date; write date:2026, date:2026-10, "
            "date:2026-10-05, date:>=2026-01-01 or date:2026-01..2026-03"
        )
    year = int(match.group(1))
    try:
        if match.group(3) is not None:
            day = date(year, int(match.group(2)), int(match.group(3)))
            return day, day
        if match.group(2) is not None:
            month = int(match.group(2))
            return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])
        return date(year, 1, 1), date(year, 12, 31)
    except ValueError:
        raise QueryError(f"date:{value} is not a real date") from None


def _date_clause(value: str) -> tuple[date | None, date | None]:
    if ".." in value:
        low, _, high = value.partition("..")
        if not low.strip() and not high.strip():
            raise QueryError("date:.. needs at least one bound, as in date:2026-01..2026-03")
        return (
            _span(low)[0] if low.strip() else None,
            _span(high)[1] if high.strip() else None,
        )
    for op in (">=", "<=", ">", "<"):
        if value.startswith(op):
            first, last = _span(value[len(op) :])
            if op == ">=":
                return first, None
            if op == ">":
                return date.fromordinal(last.toordinal() + 1), None
            if op == "<=":
                return None, last
            return None, date.fromordinal(first.toordinal() - 1)
    return _span(value)


def _empty_range(amount: AmountRange) -> bool:
    low, high = amount.minimum, amount.maximum
    if low is None or high is None:
        return False
    return low > high or (low == high and (amount.min_exclusive or amount.max_exclusive))


def _later(a: date | None, b: date | None) -> date | None:
    return b if a is None else a if b is None else max(a, b)


def _earlier(a: date | None, b: date | None) -> date | None:
    return b if a is None else a if b is None else min(a, b)


def _add_text(out: _Builder, where: str, raw: str, negated: bool) -> None:
    folded = fold(raw)
    if folded:
        out.text.append(Text(where, folded, negated))


def _apply(out: _Builder, name: str, value: str, negated: bool, quoted: bool) -> None:
    if name in ("amount", "date", "status", "is") and negated:
        raise QueryError(f"-{name}: is not supported; write the {name}: you want instead")
    if name == "payee" and not quoted and PAYEE_REF_RE.fullmatch(value):
        out.payee_refs.append((value, negated))
    elif name in TEXT_FIELDS:
        _add_text(out, name, value, negated)
    elif name in ("account", "category"):
        target = out.accounts if name == "account" else out.categories
        if value.strip():
            target.append((value.strip(), negated))
    elif name == "amount":
        out.amounts.append(_amount_clause(value))
    elif name == "date":
        start, end = _date_clause(value)
        out.start, out.end = _later(out.start, start), _earlier(out.end, end)
    elif name == "status":
        values = frozenset(v.strip().lower() for v in value.split(",") if v.strip())
        unknown = sorted(values - set(STATUSES))
        if unknown:
            raise QueryError(f"status:{unknown[0]} is not a status; use {', '.join(STATUSES)}")
        out.statuses = values if out.statuses is None else out.statuses & values
    else:  # is:
        flag = value.strip().lower()
        if flag not in IS_VALUES:
            raise QueryError(f"is:{flag} is not supported; use {', '.join(IS_VALUES)}")
        if flag in ("reviewed", "unreviewed"):
            wanted = flag == "reviewed"
            if out.reviewed is not None and out.reviewed != wanted:
                out.never = True
            out.reviewed = wanted
        else:
            if out.direction is not None and out.direction != flag:
                out.never = True
            out.direction = flag


def _read_quoted(q: str, i: int) -> tuple[str, int]:
    """The text after the quote at ``i`` up to the closing quote (or the
    end: an unfinished quote, as you type), and the index after it."""
    end = q.find('"', i + 1)
    if end == -1:
        return q[i + 1 :], len(q)
    return q[i + 1 : end], end + 1


def parse(q: str) -> Query:
    """Read a query; raises ``QueryError`` with the fix in its message."""
    out = _Builder()
    i, n = 0, len(q)
    while i < n:
        if q[i].isspace():
            i += 1
            continue
        negated = False
        if q[i] == "-" and i + 1 < n and not q[i + 1].isspace():
            negated = True
            i += 1
        if q[i] == '"':
            phrase, i = _read_quoted(q, i)
            _add_text(out, "any", phrase, negated)
            continue
        start = i
        while i < n and not q[i].isspace() and q[i] != ":":
            i += 1
        name = q[start:i].lower()
        if i < n and q[i] == ":" and name.isalpha():
            known = ALIASES.get(name, name)
            if known in FIELDS:
                i += 1
                quoted = i < n and q[i] == '"'
                if quoted:
                    value, i = _read_quoted(q, i)
                else:
                    value_start = i
                    while i < n and not q[i].isspace():
                        i += 1
                    value = q[value_start:i]
                if value.strip():
                    _apply(out, known, value, negated, quoted)
                continue
            near = _near_field(name)
            if near is not None:
                raise QueryError(
                    f'unknown field "{name}:" (did you mean {near}:?); the fields are '
                    f"{_field_list()}; put it in quotes to search the text itself"
                )
        while i < n and not q[i].isspace():
            i += 1
        word = q[start:i]
        if PAYEE_REF_RE.fullmatch(word):
            out.payee_refs.append((word, negated))
        else:
            _add_text(out, "any", word, negated)
    never = out.never
    if out.statuses is not None and not out.statuses:
        never = True
    if out.start is not None and out.end is not None and out.start > out.end:
        never = True
    for amount in out.amounts:
        if _empty_range(amount):
            never = True
    return Query(
        text=tuple(out.text),
        payee_refs=tuple(out.payee_refs),
        accounts=tuple(out.accounts),
        categories=tuple(out.categories),
        amounts=tuple(out.amounts),
        start=out.start,
        end=out.end,
        statuses=out.statuses,
        reviewed=out.reviewed,
        direction=out.direction,
        never=never,
    )


def text_matches(
    clauses: Sequence[Text], *, payee: str, description: str, memos: Sequence[str]
) -> bool:
    """Whether a row's folded payee, description and memos satisfy every text
    clause: the reference semantics the in-memory scan must equal."""
    for clause in clauses:
        if clause.field == "payee":
            hit = clause.text in payee
        elif clause.field == "desc":
            hit = clause.text in description
        elif clause.field == "memo":
            hit = any(clause.text in memo for memo in memos)
        else:
            hit = (
                clause.text in payee
                or clause.text in description
                or any(clause.text in memo for memo in memos)
            )
        if hit == clause.negated:
            return False
    return True
