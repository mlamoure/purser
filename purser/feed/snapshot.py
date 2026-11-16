"""Account financial reads from the archived bank payload (#505).

This is not connection discovery: only the requested ledger account is selected,
and the response is an allowlist of financial facts, never provider metadata.
No fetch, import, basis observation or reconciliation is performed by a read.
"""

from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel
from sqlalchemy.orm import Session

from purser import appsettings, clock, crypto
from purser.feed import drift
from purser.feed.service import latest_sync_fetch
from purser.models import Account, Connection


class BankComparison(BaseModel):
    """Live ledger comparison, not a reconstruction at the bank timestamp."""

    ledger_cutoff: Literal["all_recorded_dates"] = "all_recorded_dates"
    posted_balance: Decimal
    pending_inclusive_balance: Decimal
    ledger_balance: Decimal
    basis: Literal["posted_only", "pending_inclusive"]
    basis_learned: Literal["posted_only", "pending_inclusive"]
    basis_pinned: bool
    # Exact arithmetic on the declared basis. The existing alert policy can
    # consider the account clean when the OTHER basis matches; keep it separate.
    difference: Decimal
    matches_either_basis: bool


class BankSnapshot(BaseModel):
    """Latest archived snapshot, even when the enclosing ledger read is historical.

    Null amounts are unknown; zero is a reported or computed zero. Available
    balance is independent (e.g. remaining credit on a card), never ledger cash.
    """

    kind: Literal["latest"] = "latest"
    status: Literal[
        "ok",
        "not_connected",
        "connection_disabled",
        "account_closed",
        "no_snapshot",
        "account_missing",
        "ambiguous_account",
        "balance_missing",
        "balance_invalid",
    ]
    source: Literal["simplefin"] | None = None
    fetched_at: datetime | None = None
    balance: Decimal | None = None
    available_balance: Decimal | None = None
    available_balance_status: Literal["reported", "missing", "invalid"] = "missing"
    currency: str | None = None
    balance_at: datetime | None = None
    balance_date: date | None = None
    time_precision: Literal["timestamp", "date", "unknown"] = "unknown"
    freshness: Literal["fresh", "stale", "unknown", "future"] = "unknown"
    age_days: int | None = None
    comparison: BankComparison | None = None
    comparison_unavailable: (
        Literal[
            "no_bank_balance",
            "currency_unknown",
            "currency_mismatch",
            "account_type",
            "not_connected",
            "connection_disabled",
            "account_closed",
            "no_snapshot",
            "account_missing",
            "ambiguous_account",
        ]
        | None
    ) = None


def _money(raw: object) -> Decimal | None:
    # The protocol uses decimal strings. Do not turn a JSON float's lost
    # precision into apparently exact financial data.
    if isinstance(raw, bool) or not isinstance(raw, str | int | Decimal):
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None
    if not value.is_finite() or value.adjusted() >= 16:
        return None
    # Bank amounts must fit the ledger's NUMERIC(20,4) without rounding.
    if value != value.quantize(Decimal("0.0001")):
        return None
    return value


def _bank_time(raw: object) -> tuple[datetime | None, date | None]:
    """Keep a real instant; a date-only source never gains a midnight time.

    Epochs are rendered in UTC (the bank's original local zone is not known).
    Offset-bearing ISO timestamps retain their offset. Invalid/out-of-range
    data is unknown, not an exception that breaks an ordinary account read.
    """
    if isinstance(raw, bool) or not isinstance(raw, int | float | str):
        return None, None
    if isinstance(raw, str):
        try:
            if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
                return None, date.fromisoformat(raw)
        except ValueError:
            pass
        try:
            instant = datetime.fromisoformat(raw)
            if instant.tzinfo is not None:
                return instant, instant.astimezone(UTC).date()
        except ValueError:
            pass
    try:
        timestamp = float(raw)  # timestamp only; never used for money
        if timestamp <= 0:
            return None, None
        instant = datetime.fromtimestamp(timestamp, tz=UTC)
        return instant, instant.date()
    except (ValueError, OverflowError, OSError):
        return None, None


def account_snapshot(session: Session, account: Account) -> BankSnapshot:
    """One account's financial data under the same read authority as its ledger."""
    if account.closed_at is not None:
        return BankSnapshot(status="account_closed", comparison_unavailable="account_closed")
    if account.connection_id is None or account.external_id is None:
        return BankSnapshot(status="not_connected", comparison_unavailable="not_connected")
    connection = session.get(Connection, account.connection_id)
    if connection is None or connection.provider != "simplefin":
        return BankSnapshot(status="not_connected", comparison_unavailable="not_connected")
    if connection.status == "disabled":
        return BankSnapshot(
            status="connection_disabled", comparison_unavailable="connection_disabled"
        )
    raw = latest_sync_fetch(session, connection.id)
    if raw is None:
        return BankSnapshot(
            status="no_snapshot", source="simplefin", comparison_unavailable="no_snapshot"
        )
    result = BankSnapshot(status="ok", source="simplefin", fetched_at=raw.fetched_at)
    payload = crypto.decrypt_json(raw.payload_enc)
    matches = [
        item
        for item in (payload.get("accounts") or [])
        if isinstance(item, dict) and item.get("id") == account.external_id
    ]
    if len(matches) != 1:
        state: Literal["account_missing", "ambiguous_account"] = (
            "account_missing" if not matches else "ambiguous_account"
        )
        result.status = state
        result.comparison_unavailable = state
        return result
    bank = matches[0]
    result.balance = _money(bank.get("balance"))
    result.available_balance = _money(bank.get("available-balance"))
    if result.available_balance is not None:
        result.available_balance_status = "reported"
    elif bank.get("available-balance") is not None:
        result.available_balance_status = "invalid"
    currency = bank.get("currency")
    if isinstance(currency, str) and len(currency.strip()) == 3 and currency.strip().isalpha():
        result.currency = currency.strip().upper()
    result.balance_at, result.balance_date = _bank_time(bank.get("balance-date"))
    if result.balance_date is not None:
        result.time_precision = "timestamp" if result.balance_at else "date"
        result.age_days = (clock.ledger_today() - result.balance_date).days
        if result.age_days < 0:
            result.freshness = "future"
        elif result.age_days > appsettings.effective().feed_stale_days:
            result.freshness = "stale"
        else:
            result.freshness = "fresh"
    if result.balance is None:
        result.status = "balance_missing" if bank.get("balance") is None else "balance_invalid"
        result.comparison_unavailable = "no_bank_balance"
    elif result.currency is None:
        result.comparison_unavailable = "currency_unknown"
    elif result.currency != account.currency.strip():
        result.comparison_unavailable = "currency_mismatch"
    elif not drift.checked_type(account.type, feed_balance_is_cash=account.feed_balance_is_cash):
        result.comparison_unavailable = "account_type"
    else:
        # Time parsing above preserves more precision than the legacy drift
        # reader. Its comparison only needs the amounts and sync history.
        comp = drift.live_compare(session, account=account, sf_account={"balance": result.balance})
        assert comp is not None  # eligible account and validated finite balance
        selected = comp.posted if comp.basis == drift.POSTED_ONLY else comp.pending
        result.comparison = BankComparison(
            posted_balance=comp.posted,
            pending_inclusive_balance=comp.pending,
            ledger_balance=selected,
            basis=comp.basis,
            basis_learned=comp.learned_basis,
            basis_pinned=comp.pinned,
            difference=result.balance - selected,
            matches_either_basis=result.balance in (comp.posted, comp.pending),
        )
    return result
