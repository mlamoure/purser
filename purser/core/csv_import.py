"""CSV bank-export parser (issue #138): a second manual-import front door that
rides the QIF import-v2 planner unchanged.

CSV emits the very same ``core.qif`` dataclasses (:class:`QifFile` /
:class:`QifTransaction`) so the mapping-first, cutoff-aware, content-hash-dedup
pipeline in ``purser.qif_import`` is shared verbatim — CSV is only a different
way to produce the parsed file the planner already consumes.

Two layouts auto-detect by header row:

* **Amex activity export** — ``Date, Description, Amount[, Extended Details, …]``
  → flat rows, a single ``__default__`` account, no categories or transfers
  (Amex's own MCC "Category" column, when present, is ignored; the §5 auto-link
  is the transfer mechanism there).
* **Quicken CSV export** — ``Date, Payee, Amount[, Category, Memo, Account,
  Clr, Scheduled, …]`` → an optional ``Account`` column groups rows per file
  account (the :class:`QifFile.transactions` dict supports this natively) and
  the ``Category`` column's ``[Account]`` bracket notation becomes a transfer,
  reusing :func:`core.qif.parse_category_label` so CSV inherits the full
  2-leg/cutoff/split-degradation behavior for free. A ``Clr``/``Cleared``
  column carries each row's cleared mark (no column = no row marked, which the
  planner reads as an account without clearing information: all cleared), and
  a mark in a ``Scheduled`` column flags a Quicken reminder row, which the
  planner skips (issue #249).

When neither header matches, an explicit ``column_map``
(``{date, amount, payee, memo?, category?, account?, cleared?, scheduled?}`` →
header names) drives parsing; without one a :class:`CsvLayoutError` (surfaced
as 422, carrying the detected columns) tells the UI to collect a mapping.

No opening-balance convention: Quicken's self-transfer opening-balance encoding
has no CSV equivalent, so CSV never emits opening balances (DESIGN §7.2).

Pure and mypy-strict (``purser.core``): parses an in-memory string, no I/O.
"""

import csv
import io
import re
from datetime import date as date_type
from decimal import Decimal, InvalidOperation

from purser.core import qif as core_qif
from purser.core.qif import DEFAULT_ACCOUNT, QifFile, QifTransaction

# Logical import fields a column map may name. date/amount/payee are required;
# the rest are optional (category, account, cleared and scheduled are only
# auto-detected for the Quicken layout).
REQUIRED_FIELDS = ("date", "amount", "payee")
OPTIONAL_FIELDS = ("memo", "category", "account", "cleared", "scheduled")

_ISO_DATE = re.compile(r"^\d{4}-\d{1,2}-\d{1,2}$")

# Header aliases per logical field (matched case-insensitively, trimmed).
_ALIASES: dict[str, tuple[str, ...]] = {
    "date": ("date", "transaction date", "posted date", "post date"),
    "amount": ("amount",),
    "payee": ("payee", "name", "merchant"),
    "description": ("description",),
    "category": ("category",),
    "memo": ("memo", "notes", "note"),
    "account": ("account", "account name"),
    # Quicken's register calls its cleared-status column "Clr".
    "cleared": ("clr", "cleared"),
    "scheduled": ("scheduled",),
}

# Cleared-column values, matched case-insensitively. Quicken writes ``c``
# (cleared), ``R`` (reconciled) or blank (uncleared); the QIF ``C`` marks and
# spelled-out words are accepted too. Anything else is a clear error: a guessed
# status would either post a row the bank has not seen or pull cleared history
# out of the balance (issue #249).
_CLEARED_VALUES = core_qif.CLEARED_MARKS | {"cleared", "reconciled", "yes", "y", "true"}
_UNCLEARED_VALUES = frozenset({"", "u", "uncleared", "no", "n", "false"})

# A Scheduled column marks Quicken reminder rows; blank or an explicit "no" is
# an ordinary register row.
_NOT_SCHEDULED_VALUES = frozenset({"", "no", "n", "false", "0"})


class CsvParseError(Exception):
    """A CSV file that cannot be parsed (bad date/amount, missing column).

    Surfaced as HTTP 422 at the API, exactly like ``QifParseError``.
    """


class CsvLayoutError(CsvParseError):
    """The layout could not be auto-detected and no ``column_map`` was supplied.

    Carries the detected header columns so the API can hand them to the UI's
    column-mapping sub-form.
    """

    def __init__(self, message: str, columns: list[str]) -> None:
        super().__init__(message)
        self.columns = columns


def _norm(header: str) -> str:
    return header.strip().lower()


def _detect_column_map(headers: list[str]) -> dict[str, str] | None:
    """Sniff a column map from the header row, or None if unrecognized.

    Quicken is distinguished by an explicit ``Payee`` column; Amex activity by a
    ``Description`` column with no ``Payee``.
    """
    by_norm = {_norm(h): h for h in headers}

    def find(field: str) -> str | None:
        for alias in _ALIASES[field]:
            if alias in by_norm:
                return by_norm[alias]
        return None

    date_col = find("date")
    amount_col = find("amount")
    if date_col is None or amount_col is None:
        return None

    payee_col = find("payee")
    if payee_col is not None:
        # Quicken layout: pull the optional columns too.
        cmap = {"date": date_col, "amount": amount_col, "payee": payee_col}
        for field in ("category", "memo", "account", "cleared", "scheduled"):
            col = find(field)
            if col is not None:
                cmap[field] = col
        return cmap

    desc_col = find("description")
    if desc_col is not None:
        # Amex activity layout: description is the payee; rows are flat.
        return {"date": date_col, "amount": amount_col, "payee": desc_col}

    return None


def _resolve_indices(column_map: dict[str, str], headers: list[str]) -> dict[str, int]:
    header_index = {h: i for i, h in enumerate(headers)}
    missing = [f for f in REQUIRED_FIELDS if f not in column_map]
    if missing:
        raise CsvParseError(f"column map is missing required field(s): {', '.join(missing)}")
    indices: dict[str, int] = {}
    for field, col in column_map.items():
        if field not in REQUIRED_FIELDS and field not in OPTIONAL_FIELDS:
            raise CsvParseError(f"unknown column-map field {field!r}")
        if col not in header_index:
            raise CsvParseError(f"mapped column {col!r} is not in the CSV header")
        indices[field] = header_index[col]
    return indices


def _cell(row: list[str], indices: dict[str, int], field: str) -> str:
    idx = indices.get(field)
    if idx is None or idx >= len(row):
        return ""
    return row[idx]


def _parse_date(raw: str, date_format: str) -> date_type:
    """ISO ``YYYY-MM-DD`` is accepted unambiguously; slashed/other dates go
    through the QIF date parser (mdy/dmy toggle). QIF's parser reads
    ``-``-separated dates as ``M-D-Y``, so ISO must be handled first."""
    text = raw.strip()
    if not text:
        raise CsvParseError("empty date cell")
    if _ISO_DATE.match(text):
        year, month, day = (int(part) for part in text.split("-"))
        try:
            return date_type(year, month, day)
        except ValueError as exc:
            raise CsvParseError(f"invalid date: {raw!r}") from exc
    try:
        return core_qif.parse_date(text, date_format=date_format)
    except core_qif.QifParseError as exc:
        raise CsvParseError(f"unparseable date: {raw!r}") from exc


def _parse_amount(raw: str) -> Decimal:
    """Decimal only. Strips ``$``, thousands commas, and whitespace; reads
    ``(1,234.56)`` accountant negatives. Anything else is a clear error — never
    a float."""
    text = raw.strip()
    if not text:
        raise CsvParseError("empty amount cell")
    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1]
    text = text.replace("$", "").replace(",", "").replace(" ", "").strip()
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise CsvParseError(f"invalid amount: {raw!r}") from exc
    return -value if negative else value


def _parse_cleared(raw: str) -> bool:
    """A cleared-column cell: True for cleared/reconciled, False for blank or
    uncleared; any other value is a :class:`CsvParseError` (never a guess)."""
    value = raw.strip().lower()
    if value in _CLEARED_VALUES:
        return True
    if value in _UNCLEARED_VALUES:
        return False
    raise CsvParseError(
        f"unrecognized cleared flag {raw!r} (expected blank, c, R, *, X, cleared or reconciled)"
    )


def _is_scheduled(raw: str) -> bool:
    """Any mark in a Scheduled cell flags a Quicken reminder row."""
    return raw.strip().lower() not in _NOT_SCHEDULED_VALUES


def parse_csv(
    content: str,
    *,
    date_format: str = "mdy",
    column_map: dict[str, str] | None = None,
) -> QifFile:
    """Parse a CSV bank export into a :class:`QifFile`.

    ``column_map`` (``{date, amount, payee, memo?, category?, account?,
    cleared?, scheduled?}`` → header names) overrides auto-detection; when it is
    None and no layout is recognized, a :class:`CsvLayoutError` is raised
    carrying the header columns.
    """
    reader = csv.reader(io.StringIO(content))
    rows = [row for row in reader if any(cell.strip() for cell in row)]
    if not rows:
        raise CsvParseError("CSV is empty")

    headers = [h.strip() for h in rows[0]]
    detected = column_map if column_map is not None else _detect_column_map(headers)
    if detected is None:
        raise CsvLayoutError("could not recognize the CSV layout; supply a column map", headers)
    indices = _resolve_indices(detected, headers)
    has_account = "account" in indices
    has_cleared = "cleared" in indices

    result = QifFile()
    account_types: dict[str, str] = {}
    for row in rows[1:]:
        account_name = DEFAULT_ACCOUNT
        if has_account:
            account_name = _cell(row, indices, "account").strip() or DEFAULT_ACCOUNT
        when = _parse_date(_cell(row, indices, "date"), date_format)
        amount = _parse_amount(_cell(row, indices, "amount"))
        payee = _cell(row, indices, "payee").strip() or None
        memo = _cell(row, indices, "memo").strip() or None
        category, transfer = core_qif.parse_category_label(_cell(row, indices, "category"))
        txn = QifTransaction(
            date=when,
            amount=amount,
            payee=payee,
            memo=memo,
            category=category,
            transfer_account=transfer,
            # No cleared column: no row carries a mark. The planner reads an
            # account without marks as all-cleared (decision 60), so these rows
            # still import posted — but none is evidence that an earlier
            # expected row has since cleared (#310).
            cleared=_parse_cleared(_cell(row, indices, "cleared")) if has_cleared else False,
            scheduled=_is_scheduled(_cell(row, indices, "scheduled")),
        )
        result.transactions.setdefault(account_name, []).append(txn)
        # CSV carries no account type; default to checking. The sign diagnostic
        # keys on the MAPPED Purser account's type once mapping is chosen, so
        # this default only shows before a target is picked.
        account_types.setdefault(account_name, "checking")

    result.accounts = list(account_types.items())
    return result
