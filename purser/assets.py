"""Hand-valued assets as ledgers (issue #566, DESIGN §8, decision 113).

A ``property`` account — a house, a car, anything valued by hand — is an
ordinary ledger: its value is the sum of its postings. A change in value is an
entry, a two-leg posted transaction between the asset and the system account
``equity.revaluation`` (never a category, so spending and income reports never
see it). It works like any transaction: a backdated change moves every balance
after it, and a wrong one is corrected by its reversal.

"The asset is worth X on D" is reconciliation (``purser.reconciliation``),
which for an asset posts the difference against ``equity.revaluation``: a
calculator over the same entries, not a second model.

Money put into an asset that no tracked account paid (its purchase before
Purser's records, an improvement paid in cash) is an entry against
``equity.capital`` instead (#567), so it is never read as a gain. Each asset
has a kind (a label and an icon), and a loan may say which asset secures it:
the asset's equity is its value plus those loans.
"""

import re
import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from purser import audit, clock, events, ledger, locks
from purser.core import ledger as core
from purser.models import Account, ApiToken, AssetDetails, AssetValueChange, Posting, Transaction

# Account types valued by hand through this ledger. The API keeps the name
# ``property`` (agents and integrations use it); the UI calls it "Asset".
ASSET_TYPES = frozenset({"property"})

# What an asset is: a label and an icon, never behaviour (#567).
ASSET_KINDS = ("house", "land", "car", "boat", "rv", "jewelry", "art", "equipment", "other")
KIND_REVALUATION = "revaluation"  # a change in value, against equity.revaluation
KIND_CAPITAL = "capital"  # money put in, against equity.capital
ENTRY_KINDS = (KIND_REVALUATION, KIND_CAPITAL)
DESCRIPTION_MAX = 500
REVALUE_AFTER_MAX = 3650
STALE_EVENT = "asset.stale"

PAYEE = "Asset value change"
CAPITAL_PAYEE = "Money put in"
_MAX_ABS = Decimal("1e16")  # NUMERIC(20,4)
REFERENCE_MAX = 100
# A reference names a change for retries ("valuation-2026-12"); it is stored in
# plaintext, so it is limited to a token shape that cannot carry a sentence.
_REFERENCE_SHAPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


@dataclass(frozen=True)
class ValueChange:
    transaction: Transaction
    account: Account
    amount: Decimal  # signed, on the asset
    created: bool  # False: a repeated reference answered with the first change
    reversed: bool  # the change has since been reversed


def validated_amount(value: Decimal, *, field: str = "amount") -> Decimal:
    try:
        if not value.is_finite() or abs(value) >= _MAX_ABS:
            raise ledger.LedgerError(f"{field} is out of range")
        if value != value.quantize(core.QUANT):
            raise ledger.LedgerError(f"{field} has more than 4 decimal places")
    except InvalidOperation as exc:
        raise ledger.LedgerError(f"{field} is out of range") from exc
    return core.quantize(value)


def _validated_reference(reference: str | None) -> str | None:
    if reference is None:
        return None
    reference = reference.strip()
    if not reference:
        return None
    if len(reference) > REFERENCE_MAX or not _REFERENCE_SHAPE.match(reference):
        raise ledger.LedgerError(
            f"reference must be 1-{REFERENCE_MAX} letters, digits, '.', '_', ':' or '-' "
            "(for example valuation-2026-12); it is stored in plaintext, so put words in note"
        )
    return reference


def require_asset(account: Account) -> None:
    """Refuse anything but an open hand-valued asset account."""
    if account.is_virtual:
        raise ledger.LedgerError(f"{account.path} is a category, not an asset")
    if account.type not in ASSET_TYPES:
        raise ledger.LedgerError(
            f"{account.path} is a {account.type} account: value changes apply to hand-valued "
            "assets (type property). A loan's balance is a valuation (record_valuation); "
            "a bank or card account is reconciled to a statement (reconcile_account)."
        )
    if account.closed_at is not None:
        raise ledger.LedgerError(f"{account.path} is closed (read-only); reopen it first")


def _is_reversed(session: Session, tx: Transaction) -> bool:
    return (
        session.execute(select(Transaction.id).where(Transaction.reverses_id == tx.id)).first()
        is not None
    )


def _asset_leg(tx: Transaction, account_id: uuid.UUID) -> Decimal:
    return core.quantize(
        sum((Decimal(p.amount) for p in tx.postings if p.account_id == account_id), Decimal(0))
    )


def record_value_change(
    session: Session,
    *,
    account_ref: str,
    as_of: date,
    amount: Decimal,
    token: ApiToken | None,
    note: str | None = None,
    reference: str | None = None,
    origin: core.Origin | None = None,
    today: date | None = None,
    lock: bool = True,
    kind: str = KIND_REVALUATION,
) -> ValueChange:
    """Post a change of ``amount`` (signed: + the asset gained value) on
    ``as_of`` against equity.revaluation. With a ``reference`` already used on
    the account, nothing is posted and the first change is returned
    (``created=False``), so a retried call never posts twice. ``lock=False``
    is the importer's alone: it already holds the account in its own lock
    order."""
    amount = validated_amount(amount)
    if amount == 0:
        raise ledger.LedgerError("amount must be nonzero")
    if kind not in ENTRY_KINDS:
        raise ledger.LedgerError(
            f"kind must be {KIND_REVALUATION} (a change in value) or {KIND_CAPITAL} (money put in)"
        )
    reference = _validated_reference(reference)
    today = today or clock.ledger_today()
    if as_of > today:
        raise ledger.LedgerError(
            f"as_of {as_of} is in the future; record a change on or before {today}"
        )

    found = ledger.resolve_account(session, account_ref)
    # Serialize with reconciles and other changes of the account (the
    # reconciliation lock order: account row before the balance watermark).
    account = locks.lock_account(session, found.id) if lock else found
    if account is None:
        raise ledger.NotFoundError(f"account {account_ref} not found")
    require_asset(account)

    if reference is not None:
        existing = session.execute(
            select(AssetValueChange).where(
                AssetValueChange.account_id == account.id,
                AssetValueChange.reference == reference,
            )
        ).scalar_one_or_none()
        if existing is not None:
            tx = ledger.get_transaction(session, existing.transaction_id)
            return ValueChange(
                transaction=tx,
                account=account,
                amount=_asset_leg(tx, account.id),
                created=False,
                reversed=_is_reversed(session, tx),
            )

    other = (
        ledger.capital_account(session)
        if kind == KIND_CAPITAL
        else ledger.revaluation_account(session)
    )
    tx = ledger.create_flat_transaction(
        session,
        account_ref=str(account.id),
        category_ref=str(other.id),
        amount=amount,
        date=as_of,
        payee_name=CAPITAL_PAYEE if kind == KIND_CAPITAL else PAYEE,
        description=note.strip() if note and note.strip() else None,
        requested_status=core.Status.POSTED,
        origin=origin,
        token=token,
        system_leg=True,
    )
    session.add(AssetValueChange(transaction_id=tx.id, account_id=account.id, reference=reference))
    session.flush()
    facts = {
        "account_id": str(account.id),
        "as_of": as_of.isoformat(),
        "amount": str(amount),
        "transaction_id": str(tx.id),
        "reference": reference,
        "kind": kind,
    }
    audit.record(
        session,
        actor_token_id=token.id if token else None,
        action="asset.value_change",
        entity_type="account",
        entity_id=account.id,
        after={**facts, "note": note},
    )
    events.emit(
        session,
        type_="asset.value_changed",
        entity_type="account",
        entity_id=account.id,
        payload={"path": account.path, "currency": account.currency.strip(), **facts},
    )
    return ValueChange(transaction=tx, account=account, amount=amount, created=True, reversed=False)


def reverse_value_change(
    session: Session, *, transaction_id: uuid.UUID, token: ApiToken | None
) -> ValueChange:
    """Undo a change in an asset's value through the ledger's reversal pair
    (a posted row is corrected, never offset): the replacement keeps both legs
    at zero, so the chain nets to nothing and its live head records that the
    change was undone. Covers changes posted by a target (reconcile) as well;
    the reconciliation's own claim then reports the difference on its next
    read, as for any backdated change."""
    tx = ledger.lock_transaction(session, transaction_id)
    if not ledger.is_value_change(tx.postings):
        raise ledger.LedgerError(
            f"transaction {tx.id} is not a change in an asset's value (no leg on "
            f"{ledger.REVALUATION_PATH})"
        )
    real = [p for p in tx.postings if not p.account.is_virtual]
    system = [p for p in tx.postings if p.account.is_virtual]
    if len(real) != 1 or len(system) != 1:
        raise ledger.LedgerError(f"transaction {tx.id} is not a single value change")
    account = locks.lock_account(session, real[0].account_id)
    if account is None:  # pragma: no cover - FK
        raise ledger.NotFoundError(f"account {real[0].account_id} not found")
    if account.closed_at is not None:
        raise ledger.LedgerError(f"{account.path} is closed (read-only); reopen it first")
    amount = core.quantize(Decimal(real[0].amount))
    if amount == 0:
        raise ledger.LedgerError(f"transaction {tx.id} is already a reversed (zero) change")
    currency = real[0].currency.strip()
    nothing = core.quantize(Decimal(0))
    replacement = ledger.reverse_and_replace_with_specs(
        session,
        tx=tx,
        token=token,
        replacement_specs=[
            core.PostingSpec(account_id=account.id, amount=nothing, currency=currency),
            core.PostingSpec(account_id=system[0].account_id, amount=nothing, currency=currency),
        ],
        description=f"{tx.description or 'Value change'} (reversed)",
        origin=core.Origin.SYSTEM,
        action="asset.value_change_reverse",
        audit_extra={"account_id": str(account.id), "amount": str(amount)},
    )
    events.emit(
        session,
        type_="asset.value_changed",
        entity_type="account",
        entity_id=account.id,
        payload={
            "path": account.path,
            "currency": currency,
            "account_id": str(account.id),
            "as_of": tx.date.isoformat(),
            "amount": str(-amount),
            "transaction_id": str(replacement.id),
            "reverses": str(tx.id),
        },
    )
    return ValueChange(
        transaction=replacement, account=account, amount=-amount, created=True, reversed=True
    )


# -- what an asset is, and what secures it (#567) ---------------------------


def details(session: Session, account: Account) -> AssetDetails:
    """The asset's details row, created when missing (an account created
    before 0055 ran gets one there; this covers a hand-inserted row)."""
    row = session.get(AssetDetails, account.id)
    if row is None:
        row = AssetDetails(account_id=account.id)
        session.add(row)
        session.flush()
    return row


def details_for(session: Session, account_ids: list[uuid.UUID]) -> dict[uuid.UUID, AssetDetails]:
    if not account_ids:
        return {}
    return {
        row.account_id: row
        for row in session.execute(
            select(AssetDetails).where(AssetDetails.account_id.in_(account_ids))
        ).scalars()
    }


def update_details(
    session: Session,
    *,
    account: Account,
    kind: str | None = None,
    acquired_on: date | None = None,
    acquired_on_set: bool = False,
    description: str | None = None,
    description_set: bool = False,
    revalue_after_days: int | None = None,
    revalue_after_days_set: bool = False,
    actor_token_id: uuid.UUID | None,
) -> AssetDetails:
    """Set an asset's kind, acquisition date or description. ``*_set`` says
    the field was sent (None then clears it). Audited ``asset.update``."""
    if account.is_virtual or account.type not in ASSET_TYPES:
        raise ledger.LedgerError(
            f"{account.path} is a {account.type} account; asset details apply to hand-valued "
            "assets (type property)"
        )
    if kind is not None and kind not in ASSET_KINDS:
        raise ledger.LedgerError(f"asset_kind must be one of: {', '.join(ASSET_KINDS)}")
    if acquired_on_set and acquired_on is not None and acquired_on > clock.ledger_today():
        raise ledger.LedgerError("acquired_on is in the future")
    if description_set and description is not None:
        description = description.strip() or None
        if description is not None and len(description) > DESCRIPTION_MAX:
            raise ledger.LedgerError(f"asset_description is longer than {DESCRIPTION_MAX}")
    if (
        revalue_after_days_set
        and revalue_after_days is not None
        and not 1 <= revalue_after_days <= REVALUE_AFTER_MAX
    ):
        raise ledger.LedgerError(
            f"revalue_after_days must be 1-{REVALUE_AFTER_MAX} days, or null for never"
        )
    row = details(session, account)
    before = {
        "kind": row.kind,
        "acquired_on": row.acquired_on.isoformat() if row.acquired_on else None,
        "description": row.description,
        "revalue_after_days": row.revalue_after_days,
    }
    if kind is not None:
        row.kind = kind
    if acquired_on_set:
        row.acquired_on = acquired_on
    if description_set:
        row.description = description
    if revalue_after_days_set:
        row.revalue_after_days = revalue_after_days
    after = {
        "kind": row.kind,
        "acquired_on": row.acquired_on.isoformat() if row.acquired_on else None,
        "description": row.description,
        "revalue_after_days": row.revalue_after_days,
    }
    if after != before:
        row.updated_at = func.now()
        session.flush()
        audit.record(
            session,
            actor_token_id=actor_token_id,
            action="asset.update",
            entity_type="account",
            entity_id=account.id,
            before=before,
            after=after,
        )
    return row


def secure_loan(
    session: Session,
    *,
    loan: Account,
    asset_id: uuid.UUID | None,
    actor_token_id: uuid.UUID | None,
) -> Account:
    """Say which hand-valued asset secures ``loan`` (None: none). Audited
    ``account.secure``. Only a loan can be secured, only by an open or closed
    property account other than itself."""
    if loan.is_virtual or loan.type != "loan":
        raise ledger.LedgerError(
            f"{loan.path} is a {loan.type} account; only a loan is secured by an asset"
        )
    if asset_id is not None:
        asset = ledger.get_account(session, asset_id)
        if asset.is_virtual or asset.type not in ASSET_TYPES:
            raise ledger.LedgerError(
                f"{asset.path} is a {asset.type} account; a loan is secured by a hand-valued "
                "asset (type property)"
            )
    before = str(loan.secured_by_id) if loan.secured_by_id else None
    loan.secured_by_id = asset_id
    session.flush()
    after = str(asset_id) if asset_id else None
    if before != after:
        audit.record(
            session,
            actor_token_id=actor_token_id,
            action="account.secure",
            entity_type="account",
            entity_id=loan.id,
            before={"secured_by_id": before},
            after={"secured_by_id": after},
        )
    return loan


def secured_loans(session: Session, asset_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[Account]]:
    """The open loans each asset secures."""
    if not asset_ids:
        return {}
    out: dict[uuid.UUID, list[Account]] = {}
    for loan in session.execute(
        select(Account)
        .where(Account.secured_by_id.in_(asset_ids), Account.closed_at.is_(None))
        .order_by(Account.path)
    ).scalars():
        assert loan.secured_by_id is not None
        out.setdefault(loan.secured_by_id, []).append(loan)
    return out


def equities(
    session: Session, assets: list[Account], balances: dict[uuid.UUID, Decimal]
) -> dict[uuid.UUID, tuple[Decimal, list[uuid.UUID]]]:
    """``{asset id: (equity, secured loan ids)}``: the asset's value plus the
    reported balances of the open loans it secures (ledger sign, so a loan
    owed is negative). ``balances`` must hold every asset's and loan's
    reported balance; a loan missing from it is read here."""
    from purser import networth

    ids = [a.id for a in assets if not a.is_virtual and a.type in ASSET_TYPES]
    loans = secured_loans(session, ids)
    missing = [loan.id for group in loans.values() for loan in group if loan.id not in balances]
    if missing:
        balances = {
            **balances,
            **{k: v.balance for k, v in networth.reported_balances(session, missing).items()},
        }
    return {
        asset_id: (
            core.quantize(
                balances.get(asset_id, Decimal(0))
                + sum(
                    (balances.get(loan.id, Decimal(0)) for loan in loans.get(asset_id, [])),
                    Decimal(0),
                )
            ),
            [loan.id for loan in loans.get(asset_id, [])],
        )
        for asset_id in ids
    }


# -- stale values (#568) ------------------------------------------------------

# The newest date each asset's value was stated: a live, nonzero value entry
# (a change, money put in, or a reconcile's difference) or a recorded value on
# a date (a reconciliation, which posts nothing when the value already
# matched — the monthly estimate that did not move still refreshes it).
_LAST_VALUED = text(
    """
SELECT a.id,
       GREATEST(
         (SELECT max(t.date)
            FROM postings p
            JOIN transactions t ON t.id = p.transaction_id
            JOIN postings sp ON sp.transaction_id = t.id
            JOIN accounts s ON s.id = sp.account_id
           WHERE p.account_id = a.id AND p.amount <> 0
             AND s.is_virtual AND s.path IN ('equity.revaluation', 'equity.capital')
             AND t.status = 'posted'
             AND NOT EXISTS (SELECT 1 FROM transactions x WHERE x.reverses_id = t.id)),
         (SELECT max(r.as_of) FROM account_reconciliations r WHERE r.account_id = a.id)
       ) AS last_valued
FROM accounts a
WHERE a.id = ANY(CAST(:ids AS uuid[]))
"""
)


def last_valued(session: Session, account_ids: list[uuid.UUID]) -> dict[uuid.UUID, date | None]:
    """When each asset's value was last stated (see ``_LAST_VALUED``)."""
    if not account_ids:
        return {}
    return {
        account_id: on
        for account_id, on in session.execute(_LAST_VALUED, {"ids": [str(i) for i in account_ids]})
    }


def is_stale(last: date | None, limit: int | None, today: date) -> bool:
    """An asset with a limit is stale when its value is older than the limit,
    or was never stated."""
    if limit is None:
        return False
    return last is None or (today - last).days > limit


@dataclass(frozen=True)
class StaleAsset:
    account: Account
    last_valued: date | None
    revalue_after_days: int


def stale_assets(session: Session, today: date | None = None) -> list[StaleAsset]:
    """Open assets whose value is older than their ``revalue_after_days``."""
    today = today or clock.ledger_today()
    rows = list(
        session.execute(
            select(Account, AssetDetails.revalue_after_days)
            .join(AssetDetails, AssetDetails.account_id == Account.id)
            .where(
                AssetDetails.revalue_after_days.is_not(None),
                Account.closed_at.is_(None),
                Account.type.in_(ASSET_TYPES),
            )
            .order_by(Account.path)
        ).all()
    )
    last = last_valued(session, [account.id for account, _ in rows])
    return [
        StaleAsset(account=account, last_valued=last.get(account.id), revalue_after_days=limit)
        for account, limit in rows
        if limit is not None and is_stale(last.get(account.id), limit, today)
    ]


def emit_stale(session: Session, today: date | None = None) -> int:
    """``asset.stale`` once per asset per stale value: the dedup key is the
    account and the date its value was last stated, so a refreshed value that
    goes stale again alerts again, and a stale one never repeats."""
    emitted = 0
    for stale in stale_assets(session, today):
        on = stale.last_valued.isoformat() if stale.last_valued else "never"
        if events.emit_once(
            session,
            type_=STALE_EVENT,
            dedup_key=f"{stale.account.id}:{on}",
            entity_type="account",
            entity_id=stale.account.id,
            payload={
                "account": stale.account.path,
                "last_valued": stale.last_valued.isoformat() if stale.last_valued else None,
                "revalue_after_days": stale.revalue_after_days,
            },
        ):
            emitted += 1
    return emitted


def stale_check_job() -> None:
    """The daily ``assets.stale_check`` job (07:20 UTC)."""
    from purser.db import get_sessionmaker

    with get_sessionmaker()() as session:
        emit_stale(session)
        session.commit()


# -- sale or disposal; money put in vs gain (#569) ---------------------------

# Per asset, over live rows (chain heads, pending and posted): its balance and
# the part that came from changes in value (rows with a leg on
# equity.revaluation). Money put in is the rest: the purchase (capital) and
# transfers in and out, sale proceeds included.
_GAINS = text(
    """
SELECT p.account_id,
       COALESCE(SUM(p.amount), 0) AS balance,
       COALESCE(SUM(p.amount) FILTER (WHERE EXISTS (
           SELECT 1 FROM postings sp JOIN accounts s ON s.id = sp.account_id
           WHERE sp.transaction_id = t.id AND s.is_virtual
             AND s.path = 'equity.revaluation')), 0) AS gain
FROM postings p
JOIN transactions t ON t.id = p.transaction_id
WHERE p.account_id = ANY(CAST(:ids AS uuid[]))
  AND t.status IN ('pending', 'posted')
  AND NOT EXISTS (SELECT 1 FROM transactions x WHERE x.reverses_id = t.id)
GROUP BY p.account_id
"""
)


def gains(
    session: Session, account_ids: list[uuid.UUID]
) -> dict[uuid.UUID, tuple[Decimal, Decimal]]:
    """``{asset id: (money put in, gain)}``: gain is the sum of its changes in
    value; money put in is its value less that (the purchase and every
    transfer in or out). No tax computation: these are ledger sums."""
    if not account_ids:
        return {}
    out = {
        account_id: (core.quantize(Decimal(balance) - Decimal(gain)), core.quantize(Decimal(gain)))
        for account_id, balance, gain in session.execute(
            _GAINS, {"ids": [str(i) for i in account_ids]}
        )
    }
    zero = core.quantize(Decimal(0))
    return {i: out.get(i, (zero, zero)) for i in account_ids}


@dataclass(frozen=True)
class Disposal:
    account: Account
    as_of: date
    value_before: Decimal  # the asset's value on as_of before the closing change
    closing_change: Decimal  # posted against equity.revaluation (0: nothing posted)
    gain: Decimal  # all its changes in value, the closing one included
    money_put_in: Decimal  # before the closing change: purchase + transfers (proceeds out)
    dry_run: bool


def dispose(
    session: Session,
    *,
    account_ref: str,
    as_of: date,
    token: ApiToken,
    note: str | None = None,
    dry_run: bool = False,
) -> Disposal:
    """Sell, trade in or write off an asset on ``as_of`` (decision 116).

    Record the sale's money first, as transfers out of the asset (the bank's
    deposit of the proceeds, a loan payoff), dated on or before ``as_of``.
    Whatever is left on the asset then is the rest of its change in value — a
    gain or a loss at sale, or the whole value for a write-off — and is posted
    against equity.revaluation on ``as_of``; the account then closes at zero.
    Refused when an entry on the asset is dated after ``as_of``."""
    found = ledger.resolve_account(session, account_ref)
    account = locks.lock_account(session, found.id)
    if account is None:  # pragma: no cover - resolve_account found it
        raise ledger.NotFoundError(f"account {account_ref} not found")
    require_asset(account)
    if as_of > clock.ledger_today():
        raise ledger.LedgerError(f"as_of {as_of} is in the future")
    later = session.execute(
        select(func.max(Transaction.date))
        .join(Posting, Posting.transaction_id == Transaction.id)
        .where(
            Posting.account_id == account.id,
            Transaction.status.in_(["pending", "posted"]),
            Transaction.date > as_of,
        )
    ).scalar_one_or_none()
    if later is not None:
        raise ledger.LedgerError(
            f"{account.path} has an entry dated {later.isoformat()}, after {as_of.isoformat()}; "
            "record the sale on or after its latest entry"
        )
    balance = core.quantize(
        ledger.account_balances(session, [account.id]).get(account.id, Decimal(0))
    )
    put_in, gain = gains(session, [account.id])[account.id]
    if not dry_run:
        ledger.close_account(
            session,
            account_id=account.id,
            adjustment_category=None,
            token=token,
            on=as_of,
            note=note.strip() if note and note.strip() else "Sold or disposed of",
        )
        audit.record(
            session,
            actor_token_id=token.id,
            action="asset.dispose",
            entity_type="account",
            entity_id=account.id,
            after={
                "as_of": as_of.isoformat(),
                "value_before": str(balance),
                "closing_change": str(-balance),
                "gain": str(gain - balance),
                "note": note,
            },
        )
    return Disposal(
        account=account,
        as_of=as_of,
        value_before=balance,
        closing_change=core.quantize(-balance),
        gain=core.quantize(gain - balance),
        money_put_in=put_in,
        dry_run=dry_run,
    )
