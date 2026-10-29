"""QIF parser (issue #8): the one-time migration path off Quicken.

QIF, not QXF (proprietary/undocumented). QIF has no stable ids and an
ambiguous date format; transfers are encoded as [Account Name] in the
category field — on BOTH sides, which naive importers double-count.
"""

from dataclasses import dataclass, field
from datetime import date as date_type
from decimal import Decimal, InvalidOperation


class QifParseError(Exception):
    pass


@dataclass(frozen=True)
class QifSplit:
    """One split line of a QIF transaction (issue #274, decision 87): ``S``
    opens it (a category, or ``[Account]`` for a transfer line, or blank),
    ``E`` is its memo and ``$`` its amount, in the transaction's sign."""

    category: str | None
    transfer_account: str | None
    amount: Decimal
    memo: str | None


@dataclass(frozen=True)
class QifTransaction:
    date: date_type
    amount: Decimal
    payee: str | None
    memo: str | None
    category: str | None  # 'Utilities:Electric' or None
    transfer_account: str | None  # '[Savings]' -> 'Savings'
    # Marked cleared or reconciled in the file — the row's own mark, False
    # when the file carries none (a CSV without a cleared column included).
    # The import planner reads an unmarked row as uncleared only when its file
    # account marks at least one row: an account with no marks at all carries
    # no clearing information (bank-generated exports never mark rows), so it
    # is treated as cleared. Only a mark promotes an earlier expected row
    # (DESIGN §7.2, decision 60).
    cleared: bool
    # A Quicken reminder (an upcoming scheduled bill/deposit the register shows
    # ahead of time), not history: the planner skips it as `skipped_scheduled`.
    scheduled: bool = False
    # A record from a ``!Type:Invst`` section (issue #274, decision 84): a
    # Buy/Sell/Div/XIn/... investment action, not a bank row. The planner skips
    # it as ``skipped_investment``; its amount is read leniently (0 when absent
    # or garbage) and it never carries a category or transfer.
    investment: bool = False
    # Split lines in file order (decision 87). When present, ``L`` is ignored
    # (``--Split--`` or the first line's category), so ``category`` and
    # ``transfer_account`` are None and the lines carry the categorization.
    splits: tuple[QifSplit, ...] = ()


# QIF ``C`` values (case-insensitive): ``*``/``c`` cleared, ``X``/``R``
# reconciled. Blank or absent = uncleared (issue #249).
CLEARED_MARKS = frozenset({"*", "c", "x", "r"})


def is_cleared_mark(value: str) -> bool:
    """True when a cleared-status value marks the row cleared or reconciled."""
    return value.strip().lower() in CLEARED_MARKS


@dataclass
class QifFile:
    accounts: list[tuple[str, str]] = field(default_factory=list)  # (name, qif type)
    transactions: dict[str, list[QifTransaction]] = field(default_factory=dict)
    # File accounts that are investment accounts (issue #274): those owning a
    # ``!Type:Invst`` section, and those an ``!Account`` block declares with an
    # investment type. The planner leaves one out when the request doesn't map it.
    investment_accounts: set[str] = field(default_factory=set)


_TYPE_MAP = {
    "Bank": "checking",
    "CCard": "credit_card",
    "Cash": "cash",
    "Oth A": "checking",
    "Oth L": "loan",
    # Investment account types (issue #274): shown as brokerage in the preview.
    "Invst": "brokerage",
    "Port": "brokerage",
    "401(k)/403(b)": "brokerage",
    "Mutual": "brokerage",
}

# QIF account types whose register is investment actions, not bank rows.
INVESTMENT_TYPES = frozenset({"invst", "port", "401(k)/403(b)", "mutual"})

DEFAULT_ACCOUNT = "__default__"


def parse_category_label(label: str) -> tuple[str | None, str | None]:
    """Split a Quicken category/transfer label into ``(category, transfer)``.

    ``[Account]`` bracket notation is a transfer to that account; any other
    non-empty text is a plain category; empty is neither. Shared by the QIF and
    CSV parsers so both inherit the exact same transfer semantics (issue #138).
    """
    label = label.strip()
    if label.startswith("[") and label.endswith("]"):
        return None, label[1:-1]
    if label:
        return label, None
    return None, None


def parse_date(raw: str, *, date_format: str = "mdy") -> date_type:
    """Quicken dates: 1/15/04, 01/15/2004, 1/15'04 (apostrophe = 2000s),
    with mdy vs dmy ambiguity resolved by caller preference."""
    cleaned = raw.strip().replace("'", "/")
    for sep in ("/", "-", "."):
        if sep in cleaned:
            parts = [p.strip() for p in cleaned.split(sep) if p.strip()]
            break
    else:
        raise QifParseError(f"unparseable QIF date: {raw!r}")
    if len(parts) != 3:
        raise QifParseError(f"unparseable QIF date: {raw!r}")
    try:
        a, b, year_raw = (int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError as exc:
        raise QifParseError(f"unparseable QIF date: {raw!r}") from exc
    year = year_raw if year_raw > 99 else (2000 + year_raw if year_raw < 70 else 1900 + year_raw)
    month, day = (a, b) if date_format == "mdy" else (b, a)
    if month > 12 and day <= 12:
        month, day = day, month  # the file disagrees with the stated format
    try:
        return date_type(year, month, day)
    except ValueError as exc:
        raise QifParseError(f"invalid QIF date: {raw!r}") from exc


def _parse_amount(raw: str) -> Decimal:
    try:
        return Decimal(raw.replace(",", "").replace("$", "").strip())
    except InvalidOperation as exc:
        raise QifParseError(f"invalid QIF amount: {raw!r}") from exc


def _lenient_amount(record: dict[str, str]) -> Decimal:
    """An investment record's ``T``/``U`` amount, or 0 when it has none or it
    doesn't parse (ShrsIn/ShrsOut carry no amount). The planner never imports
    these rows, so the value is informational only."""
    for code in ("T", "U"):
        raw = record.get(code)
        if raw is None:
            continue
        try:
            value = Decimal(raw.replace(",", "").replace("$", "").strip())
        except InvalidOperation:
            continue
        if value.is_finite():
            return value
    return Decimal(0)


def parse(content: str, *, date_format: str = "mdy") -> QifFile:
    """Parse a full QIF export (single- or multi-account)."""
    result = QifFile()
    current_account = DEFAULT_ACCOUNT
    in_account_block = False
    in_transactions = False
    in_investment = False  # inside a !Type:Invst section (issue #274)
    pending_account_name: str | None = None
    pending_account_type = "checking"
    pending_investment = False
    record: dict[str, str] = {}
    # Split lines of the current record, in order: S opens one, E/$ attach
    # to the open one (a repeated E or $ opens a blank-category line).
    split_lines: list[dict[str, str]] = []

    def add_split_field(code: str, value: str) -> None:
        if code == "S" or not split_lines or code in split_lines[-1]:
            split_lines.append({} if code != "S" else {"S": value})
            if code == "S":
                return
        split_lines[-1][code] = value

    def build_splits() -> tuple[QifSplit, ...]:
        out = []
        for line in split_lines:
            category, transfer = parse_category_label(line.get("S", ""))
            raw = line.get("$", "").strip()
            out.append(
                QifSplit(
                    category=category,
                    transfer_account=transfer,
                    amount=_parse_amount(raw) if raw else Decimal(0),
                    memo=line.get("E", "").strip() or None,
                )
            )
        return tuple(out)

    def flush_record() -> None:
        nonlocal record
        if not record:
            split_lines.clear()
            return
        if in_investment:
            # Every dated investment record is kept, flagged, so the planner
            # can count it; it never raises on a missing or odd amount.
            if "D" in record:
                try:
                    when = parse_date(record["D"], date_format=date_format)
                except QifParseError:
                    when = date_type.min  # counted, never imported or dated
                result.transactions.setdefault(current_account, []).append(
                    QifTransaction(
                        date=when,
                        amount=_lenient_amount(record),
                        payee=record.get("P", "").strip() or None,
                        memo=record.get("M", "").strip() or None,
                        category=None,
                        transfer_account=None,
                        cleared=is_cleared_mark(record.get("C", "")),
                        investment=True,
                    )
                )
            record = {}
            split_lines.clear()
            return
        if "D" not in record or ("T" not in record and "U" not in record):
            record = {}
            split_lines.clear()
            return
        splits = build_splits()
        split_lines.clear()
        # With split lines, L is only a summary (--Split--, or in some
        # exporters the first line's category): the lines categorize the row.
        category, transfer = (None, None) if splits else parse_category_label(record.get("L", ""))
        txn = QifTransaction(
            date=parse_date(record["D"], date_format=date_format),
            amount=_parse_amount(record.get("T") or record["U"]),
            payee=record.get("P", "").strip() or None,
            memo=record.get("M", "").strip() or None,
            category=category,
            transfer_account=transfer,
            cleared=is_cleared_mark(record.get("C", "")),
            splits=splits,
        )
        result.transactions.setdefault(current_account, []).append(txn)
        record = {}

    for raw_line in content.splitlines():
        line = raw_line.rstrip("\r\n")
        if not line:
            continue
        if line.startswith("!"):
            header = line[1:].strip()
            if header.lower() == "account":
                flush_record()
                in_account_block = True
                in_transactions = False
                in_investment = False
                pending_account_name = None
                pending_account_type = "checking"
                pending_investment = False
            elif header.lower().startswith("type:"):
                flush_record()
                type_name = header.split(":", 1)[1]
                if type_name in ("Cat", "Class", "Memorized", "Prices", "Security"):
                    in_transactions = False
                    in_account_block = False
                    in_investment = False
                else:
                    in_transactions = True
                    in_account_block = False
                    in_investment = type_name.strip().lower() == "invst"
                    if in_investment:
                        result.investment_accounts.add(current_account)
                    if current_account == DEFAULT_ACCOUNT and not result.accounts:
                        fallback = "brokerage" if in_investment else "checking"
                        result.accounts.append(
                            (DEFAULT_ACCOUNT, _TYPE_MAP.get(type_name, fallback))
                        )
            elif header.lower() == "clear:autoswitch" or header.lower() == "option:autoswitch":
                pass
            continue
        if in_account_block:
            code, value = line[0], line[1:]
            if code == "N":
                pending_account_name = value.strip()
            elif code == "T":
                pending_account_type = _TYPE_MAP.get(value.strip(), "checking")
                pending_investment = value.strip().lower() in INVESTMENT_TYPES
            elif code == "^" and pending_account_name:
                current_account = pending_account_name
                if pending_account_name not in dict(result.accounts):
                    result.accounts.append((pending_account_name, pending_account_type))
                if pending_investment:
                    result.investment_accounts.add(pending_account_name)
                pending_investment = False
            continue
        if in_transactions:
            code, value = line[0], line[1:]
            if code == "^":
                flush_record()
            elif code in ("S", "E", "$"):
                add_split_field(code, value)
            else:
                record[code] = value
    flush_record()
    if not result.transactions and not any(
        line.lstrip().startswith("!") for line in content.splitlines()
    ):
        # Every QIF export opens with a !Type / !Account / !Option header. A
        # file with neither a header nor a record is not QIF (a CSV, a bank
        # PDF's text, the wrong file): say so rather than preview "nothing".
        raise QifParseError(
            "not a QIF file: no !Type, !Account or !Option header and no transactions"
        )
    return result
