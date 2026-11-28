"""Accounts and categories: lookup, create, update, move, delete, and the
closed-account write guard (DESIGN §3.4)."""

import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from datetime import date as date_type
from decimal import Decimal
from typing import Any

from sqlalchemy import CompoundSelect, cast, exists, literal, not_, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, aliased

from purser import audit, events, locks
from purser.core import ledger as core
from purser.feed import drift
from purser.ledger.errors import LedgerError, NotFoundError, PathConflictError, RowBusy, check_path
from purser.models import Account, LtreeType, Posting, Schedule, Suggestion, Transaction

# ---------------------------------------------------------------------------
# Accounts & categories
# ---------------------------------------------------------------------------

REAL_TYPES = {"checking", "savings", "cash", "credit_card", "brokerage", "property", "loan"}
CATEGORY_TYPES = {"category_expense", "category_income"}
# The account types a floor (`min_balance`, the forecast.shortfall threshold)
# applies to: cash-flow accounts whose forecast is their postings projected
# forward (§6). Brokerage, property and loan forecasts open from a market
# value or a stated valuation that moves on its own, and categories have no
# forecast, so a floor there is refused rather than stored and never useful
# (#466).
FLOOR_TYPES = frozenset({"checking", "savings", "cash", "credit_card"})
_MONEY_DIGITS = 20  # NUMERIC(20,4)
_MONEY_PLACES = 4


def check_statement_close_day(account: Account, value: int | None) -> None:
    """A credit card's statement closing day (#674): 1..31, cards only. A day
    past a month's end means that month's last day."""
    if value is None:
        return
    if account.type != "credit_card":
        raise LedgerError(
            f"{account.path} is a {account.type} account; statement_close_day applies only to "
            "credit cards"
        )
    if isinstance(value, bool) or not 1 <= value <= 31:
        raise LedgerError("statement_close_day must be a day of the month, 1..31")


def check_min_balance(account_type: str, value: Decimal | None) -> None:
    """Validate a floor for an account of ``account_type`` (create and update
    share it, #466). None (no floor, or clearing one) is always valid. A
    value must be a finite decimal that fits NUMERIC(20,4); any sign is
    allowed — 0 warns before overdraft, a negative floor suits a credit card
    (ledger sign: negative when owed) or an overdraft line."""
    if value is None:
        return
    if not value.is_finite():
        raise LedgerError("min_balance must be a finite decimal amount")
    exponent = value.as_tuple().exponent
    places = -exponent if isinstance(exponent, int) and exponent < 0 else 0
    if places > _MONEY_PLACES:
        raise LedgerError(f"min_balance allows at most {_MONEY_PLACES} decimal places")
    integer_digits = len(value.as_tuple().digits) - places
    if integer_digits > _MONEY_DIGITS - _MONEY_PLACES:
        raise LedgerError("min_balance is too large")
    if account_type not in FLOOR_TYPES:
        raise LedgerError(
            f"min_balance applies only to {', '.join(sorted(FLOOR_TYPES))} accounts, "
            f"not {account_type}"
        )


# System virtual accounts (#390 phase 1b, decision 103): virtual like a
# category — a balancing leg outside net worth and the running balances — but
# NOT a category. Nothing a person or an agent chooses books to one: no
# category picker, rule, schedule, classifier or flat-form write may name it.
# Only Purser's own securities-trade cash does, through the holdings service.
SYSTEM_TYPES = core.SYSTEM_ACCOUNT_TYPES
SECURITIES_PATH = "equity.securities"
# The other side of a change in a hand-valued asset's value (#566, decision
# 113): a property account is a ledger, and a rise or fall in its value is an
# entry between it and this account. Only the asset service books here.
REVALUATION_PATH = "equity.revaluation"
# The other side of money put into an asset that no tracked account paid (the
# purchase before Purser's records, an improvement paid in cash; #567): kept
# apart from equity.revaluation so it never reads as a gain in value.
CAPITAL_PATH = "equity.capital"
ASSET_ENTRY_PATHS = frozenset({REVALUATION_PATH, CAPITAL_PATH})
# The other side of an account's starting balance (#585, decision 123): what
# the account held before the first transaction Purser knows, taken from the
# bank's reported balance. Only the starting-balance service books here.
OPENING_PATH = "equity.opening_balances"
# The subtree the system accounts live in. Nothing else may: a category under
# equity.securities, or an ``equity`` category, would share the subtree that
# path-based reports and filters read, and mix with trade cash there.
SYSTEM_ROOT = "equity"


def in_system_subtree(path: str) -> bool:
    return path == SYSTEM_ROOT or path.startswith(SYSTEM_ROOT + ".")


def refuse_system_path(path: str) -> None:
    """Refuse a category or account at a path in the system subtree."""
    if in_system_subtree(path):
        raise LedgerError(
            f"{path} is under {SYSTEM_ROOT}, which holds Purser's system accounts "
            f"({SECURITIES_PATH}, the other side of a securities trade's cash); put the "
            "category or account elsewhere"
        )


def is_system_account(account: Account) -> bool:
    return account.is_virtual and account.type in SYSTEM_TYPES


def is_category(account: Account) -> bool:
    return account.is_virtual and account.type in CATEGORY_TYPES


def has_system_leg(postings: "Iterable[Posting]") -> bool:
    """A row with a leg on a system account: a securities trade's cash
    (decision 103) or a change in an asset's value (decision 113). Such a row
    is Purser's own bookkeeping, so the matcher, the transfer pool, rules and
    schedule detection all leave it alone."""
    return any(p.account is not None and is_system_account(p.account) for p in postings)


def is_value_change(postings: "Iterable[Posting]") -> bool:
    """An entry in a hand-valued asset's value: a leg on equity.revaluation (a
    change in value) or equity.capital (money put in)."""
    return any(p.account is not None and p.account.path in ASSET_ENTRY_PATHS for p in postings)


def is_starting_balance(postings: "Iterable[Posting]") -> bool:
    """An account's starting balance: a leg on equity.opening_balances."""
    return any(p.account is not None and p.account.path == OPENING_PATH for p in postings)


def is_capital(postings: "Iterable[Posting]") -> bool:
    """Money put into a hand-valued asset: a leg on equity.capital."""
    return any(p.account is not None and p.account.path == CAPITAL_PATH for p in postings)


def is_securities_trade(postings: "Iterable[Posting]") -> bool:
    """A securities trade's cash: a leg on equity.securities."""
    return any(p.account is not None and p.account.path == SECURITIES_PATH for p in postings)


def securities_trade_refusal(account: Account) -> str | None:
    """Why a row on ``account`` cannot be marked as a securities trade by a
    person, an agent or a rule (#591, decision 119), or None when it can: a
    brokerage account whose holdings come from the feed. A feed-holdings
    account's buy and sell rows are cash the shares already counted in its
    holdings came from; on an account tracked by hand the trade is recorded
    as a position event whose cash links that row (decision 103), so the
    shares and the cash move together and the double-count probe sees it."""
    if account.is_virtual or account.type != "brokerage":
        return (
            f"{account.path} is a {account.type} account; only a row on a brokerage account "
            "can be a securities trade (a buy or sell moves cash between the account and its "
            "shares)"
        )
    if account.holdings_source != "feed":
        return (
            f"{account.path} has its holdings entered in Purser: record the trade "
            "(record_sale, or a buy or sell position event) and pass this row as its "
            "transaction_id, so the shares and the cash move together"
        )
    return None


def require_securities_trade_account(account: Account) -> None:
    refusal = securities_trade_refusal(account)
    if refusal is not None:
        raise LedgerError(refusal)


def trade_chain_ids(session: Session, tx: Transaction) -> set[uuid.UUID]:
    """``tx``'s whole correction chain (back to the bank's row, forward to the
    live head) plus any row voided INTO it (Purser's own trade cash that a
    broker's copy took over at import, #408): every id a position event may
    name for this bank line."""
    ids = {tx.id}
    current: Transaction | None = tx
    while current is not None and current.reverses_id is not None:
        ids.add(current.reverses_id)
        current = session.get(Transaction, current.reverses_id)
    current = tx
    while current is not None:
        current = session.execute(
            select(Transaction).where(Transaction.reverses_id == current.id)
        ).scalar_one_or_none()
        if current is not None:
            ids.add(current.id)
    ids |= set(
        session.execute(
            select(Transaction.id).where(
                Transaction.superseded_by.in_(ids), Transaction.status == "void"
            )
        ).scalars()
    )
    return ids


def held_by_trade(session: Session, tx: Transaction) -> bool:
    """Whether a recorded trade (a position event, standing or reversed)
    names a row of ``tx``'s bank line as its cash (decision 103). Such a row
    moves only with its trade. A row on ``equity.securities`` that no trade
    holds was marked a securities trade by a person, an agent or a rule, or
    is a balance true-up (decision 119): it stays editable."""
    from purser.models import PositionEvent  # function-local: one model, no cycle

    return (
        session.execute(
            select(PositionEvent.id)
            .where(PositionEvent.transaction_id.in_(trade_chain_ids(session, tx)))
            .limit(1)
        ).first()
        is not None
    )


def _system_leg_rows() -> Any:
    """The ids of the rows with a leg on a system account: the cash of a
    securities trade, Purser's own or a linked one, and the reversals in
    their correction chains."""
    return (
        select(Posting.transaction_id.label("id"))
        .join(Account, Account.id == Posting.account_id)
        .where(Account.is_virtual.is_(True), Account.type.in_(SYSTEM_TYPES))
    )


def trade_cash_lineage_ids() -> CompoundSelect[uuid.UUID]:
    """The ids of every row a securities trade's cash claims (decision 103): a
    row with a leg on a system account, and every row of a correction chain
    whose LIVE HEAD has one — the bank row a trade linked as its cash and the
    reversal between them. Such rows move only with their trade, so no
    merge, absorb, match, schedule detection or feed purge may take one.

    A chain whose head no longer carries the system leg (the trade was
    reversed and the bank row got its category back) is free again: only
    the rows that themselves carry the leg stay claimed.

    One recursive walk from the system-leg heads back through
    ``reverses_id``; the set is as small as the trades are few. The transfer
    pool that pools live chain heads (#386) needs only the head clause:
    a head is claimed exactly when ``has_system_leg`` holds for it."""
    system_rows = _system_leg_rows()
    later = aliased(Transaction)
    chain = (
        select(Transaction.id.label("id"), Transaction.reverses_id.label("reverses_id"))
        .where(
            Transaction.id.in_(system_rows),
            ~exists().where(later.reverses_id == Transaction.id),
        )
        .cte("trade_cash_chain", recursive=True)
    )
    earlier = aliased(Transaction)
    chain = chain.union_all(
        select(earlier.id, earlier.reverses_id).where(earlier.id == chain.c.reverses_id)
    )
    return select(chain.c.id).union(system_rows)


def trade_cash_lineage_set(session: Session) -> set[uuid.UUID]:
    """``trade_cash_lineage_ids`` read once, for a caller that filters many
    rows in Python (the transfer pool)."""
    return set(session.execute(trade_cash_lineage_ids()).scalars())


def is_trade_cash_lineage(session: Session, tx: Transaction) -> bool:
    """Whether a securities trade's cash claims ``tx`` (see
    ``trade_cash_lineage_ids``): it carries a system leg, or the live head
    of its correction chain does. Walks forward through ``reverses_id``; an
    uncorrected row costs one lookup."""
    if has_system_leg(tx.postings):
        return True
    head = tx
    while True:
        later = session.execute(
            select(Transaction).where(Transaction.reverses_id == head.id)
        ).scalar_one_or_none()
        if later is None:
            break
        head = later
    return head is not tx and has_system_leg(head.postings)


def lock_transaction(
    session: Session, tx_id: uuid.UUID, *, skip_locked: bool = False
) -> Transaction:
    """Lock a transaction row (FOR NO KEY UPDATE) and read it afresh, so the
    trade-cash guards and the write that follows them act on one state: a
    sale's link, a match, a transfer merge and a conversion of the same bank
    row exclude each other instead of each passing its check before the
    other commits. A posting's FK insert (KEY SHARE) is never blocked.

    Explicit paths wait, then run their guard on the fresh row. An automatic
    pass (``skip_locked``) never waits — it may already hold the balance
    watermark, which a waiting writer needs next — and raises ``RowBusy`` for
    a row another writer holds, or one a trade claimed after the pass read
    its lineage set (it carries a system leg now, or a correction whose live
    head does); it skips the row. The chain walk runs only for a corrected
    row, so an uncorrected one costs nothing more."""
    row = session.execute(
        select(Transaction)
        .where(Transaction.id == tx_id)
        .with_for_update(key_share=True, skip_locked=skip_locked)
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if row is None:
        present = session.execute(select(exists().where(Transaction.id == tx_id))).scalar_one()
        if skip_locked and present:
            raise RowBusy(f"transaction {tx_id} is held by another writer")
        raise NotFoundError(f"transaction {tx_id} not found")
    if skip_locked:
        corrected = session.execute(
            select(exists().where(Transaction.reverses_id == tx_id))
        ).scalar_one()
        if has_system_leg(row.postings) or (corrected and is_trade_cash_lineage(session, row)):
            raise RowBusy(f"transaction {tx_id} is a securities trade's cash")
    return row


def refuse_trade_cash_lineage(
    session: Session, tx: Transaction, *, what: str, claimed: set[uuid.UUID] | None = None
) -> None:
    """Refuse a write that would part a trade's cash from its trade: merging
    it into a transfer, converting it, settling a bill with it. ``what``
    completes "so it cannot ...".

    ``claimed`` is an automatic pass's ``trade_cash_lineage_set``, read once
    for the whole pass: membership replaces the chain walk. Explicit paths
    (match, confirm, convert) pass nothing and walk."""
    found = tx.id in claimed if claimed is not None else is_trade_cash_lineage(session, tx)
    if found and is_value_change(tx.postings):
        raise LedgerError(
            f"transaction {tx.id} is a change in an asset's value: it is Purser's own "
            f"entry against {REVALUATION_PATH}, so it cannot {what}. Reverse it "
            "(reverse_value_change) and record the right figure if it is wrong"
        )
    if found and is_starting_balance(tx.postings):
        raise LedgerError(
            f"transaction {tx.id} is an account's starting balance: it is Purser's own "
            f"entry against {OPENING_PATH}, so it cannot {what}. Remove it "
            "(set_starting_balance with remove) if the account should not have one"
        )
    if found and is_securities_trade(tx.postings) and not held_by_trade(session, tx):
        # Marked a securities trade by a person, an agent or a rule (decision
        # 119), or a balance true-up: no position event holds it.
        raise LedgerError(
            f"transaction {tx.id} is marked as a securities trade, so it cannot {what}. "
            "Change it back to a category first (update_transaction with a category)"
        )
    if found:
        raise LedgerError(
            f"transaction {tx.id} is the cash of a securities trade (or the row a trade "
            f"linked as its cash): it moves only with its trade, so it cannot {what}. "
            "Reverse the trade's position event first if the trade is wrong"
        )


def refuse_system_account(account: Account) -> None:
    """A user-supplied category must be a category: a system account (the
    other side of a securities trade's cash, or of a change in an asset's
    value) is refused, naming why."""
    if is_system_account(account):
        if account.path in ASSET_ENTRY_PATHS:
            what = "an asset's value entries book to it (record_value_change)"
        elif account.path == OPENING_PATH:
            what = "an account's starting balance books to it (set_starting_balance)"
        else:
            what = "securities trades book their cash to it"
        raise LedgerError(
            f"{account.path} is a system account: {what}, never a category you choose"
        )


def securities_account(session: Session) -> Account:
    """``equity.securities``, the account trade cash balances against.
    Migration 0043 seeds it; if it is ever missing (a wiped test database, a
    hand-deleted row) it is created again here, audited."""
    return _system_account(
        session,
        path=SECURITIES_PATH,
        name="Securities",
        description="System account: the other side of a securities trade's cash. Not a category.",
        needed_for="trade cash needs that path for the system securities account",
    )


def revaluation_account(session: Session) -> Account:
    """``equity.revaluation``, the account a change in an asset's value
    balances against (decision 113). Migration 0054 seeds it; recreated here,
    audited, if it is ever missing."""
    return _system_account(
        session,
        path=REVALUATION_PATH,
        name="Revaluation",
        description=(
            "System account: the other side of a change in a hand-valued asset's value. "
            "Not a category."
        ),
        needed_for="asset value changes need that path for the system revaluation account",
    )


def capital_account(session: Session) -> Account:
    """``equity.capital``, the other side of money put into an asset (#567).
    Migration 0055 seeds it; recreated here, audited, if it is ever missing."""
    return _system_account(
        session,
        path=CAPITAL_PATH,
        name="Capital",
        description=(
            "System account: the other side of money put into a hand-valued asset that no "
            "tracked account paid. Not a category."
        ),
        needed_for="money put into an asset needs that path for the system capital account",
    )


def opening_balances_account(session: Session) -> Account:
    """``equity.opening_balances``, the account a starting balance balances
    against (decision 123). Migration 0059 seeds it; recreated here, audited,
    if it is ever missing."""
    return _system_account(
        session,
        path=OPENING_PATH,
        name="Starting balances",
        description=(
            "System account: the other side of an account's starting balance, which stands "
            "for everything before the first transaction Purser holds. Not a category."
        ),
        needed_for="a starting balance needs that path for the system starting-balances account",
    )


def _system_account(
    session: Session, *, path: str, name: str, description: str, needed_for: str
) -> Account:
    """A system account by path, created if missing. The insert is
    ``ON CONFLICT (path) DO NOTHING``: when two writers recreate it at once,
    the one that loses reads the winner's row instead of failing on the
    unique path."""
    account = session.execute(select(Account).where(Account.path == path)).scalar_one_or_none()
    created: uuid.UUID | None = None
    if account is None:
        created = session.execute(
            pg_insert(Account)
            .values(
                name=name,
                type="equity",
                is_virtual=True,
                path=path,
                description=description,
            )
            .on_conflict_do_nothing(index_elements=[Account.path])
            .returning(Account.id)
        ).scalar_one_or_none()
        account = session.execute(select(Account).where(Account.path == path)).scalar_one()
    if not is_system_account(account):
        raise LedgerError(f"{path} is taken by a {account.type} account; {needed_for}")
    if created is not None:
        audit.record(
            session,
            actor_token_id=None,
            action="account.create",
            entity_type="account",
            entity_id=account.id,
            after={"name": account.name, "type": account.type, "path": account.path},
        )
    return account


def get_account(session: Session, account_id: uuid.UUID) -> Account:
    account = session.get(Account, account_id)
    if account is None:
        raise NotFoundError(f"account {account_id} not found")
    return account


def account_by_path(session: Session, path: str) -> Account:
    check_path(path)  # a malformed path is a 422, never an ltree cast error
    account = session.execute(select(Account).where(Account.path == path)).scalar_one_or_none()
    if account is None:
        raise NotFoundError(f"account path {path!r} not found")
    return account


def resolve_account(session: Session, ref: str) -> Account:
    """Accept an account reference as UUID or ltree path. Only the path form
    is validated (account_by_path): a ref that parses as a UUID never is."""
    try:
        return get_account(session, uuid.UUID(ref))
    except ValueError:
        return account_by_path(session, ref)


def create_account(
    session: Session,
    *,
    name: str,
    type_: str,
    parent_id: uuid.UUID | None = None,
    path: str | None = None,
    currency: str = "USD",
    is_virtual: bool | None = None,
    external_id: str | None = None,
    connection_id: uuid.UUID | None = None,
    min_balance: Decimal | None = None,
    description: str | None = None,
    actor_token_id: uuid.UUID | None,
) -> Account:
    if type_ == "credit":
        # The Direct-to-Bank AccountType contract spells the card type "credit";
        # the ledger's canonical spelling is "credit_card". Reconcile the one
        # difference between the two enum sets here, at the single creation point.
        type_ = "credit_card"
    if type_ not in REAL_TYPES | CATEGORY_TYPES:
        raise LedgerError(f"unknown account type {type_!r}")
    virtual = type_ in CATEGORY_TYPES if is_virtual is None else is_virtual
    if virtual != (type_ in CATEGORY_TYPES):
        raise LedgerError("is_virtual must match the account type (categories are virtual)")
    check_min_balance(type_, min_balance)

    if path is None:
        try:
            label = core.path_label(name)
        except ValueError as exc:  # nothing slug-able in the name: a 422, not a 500
            raise LedgerError(f"{exc}; pass an explicit path") from exc
        if parent_id is not None:
            parent = get_account(session, parent_id)
            path = f"{parent.path}.{label}"
        else:
            path = label
    else:
        check_path(path)
    refuse_system_path(path)
    existing = session.execute(select(Account).where(Account.path == path)).scalar_one_or_none()
    if existing is not None:
        raise LedgerError(f"account path {path!r} already exists")

    account = Account(
        name=name,
        type=type_,
        parent_id=parent_id,
        path=path,
        currency=currency,
        is_virtual=virtual,
        external_id=external_id,
        connection_id=connection_id,
        min_balance=min_balance,
        description=description,
        # A new brokerage account takes its holdings from the feed, and its
        # balance true-up is on by default there (decision 119).
        balance_true_up=type_ == "brokerage",
    )
    session.add(account)
    session.flush()
    if type_ == "property":
        # Every hand-valued asset has its details row (#567); the asset
        # service fills it in.
        from purser.models import AssetDetails

        session.add(AssetDetails(account_id=account.id))
        session.flush()
    audit.record(
        session,
        actor_token_id=actor_token_id,
        action="account.create",
        entity_type="account",
        entity_id=account.id,
        after={
            "name": name,
            "type": type_,
            "path": path,
            "min_balance": _money(min_balance),
        },
    )
    return account


def _money(value: Decimal | None) -> str | None:
    """An audited amount as a NUMERIC(20,4) decimal string (guardrail 1), so
    before and after read alike; null when unset."""
    return str(value.quantize(Decimal("0.0001"))) if value is not None else None


def update_category(
    session: Session,
    *,
    category_id: uuid.UUID,
    name: str | None = None,
    description: str | None = None,
    actor_token_id: uuid.UUID | None,
) -> Account:
    """Rename/re-describe a category. Paths never change here — re-parenting
    is deliberately unsupported (ltree cascade + schedule templates)."""
    account = get_account(session, category_id)
    if not account.is_virtual:
        raise LedgerError(f"{account.path} is not a category")
    refuse_system_account(account)
    before = {"name": account.name, "description": account.description}
    if name is not None:
        account.name = name
    if description is not None:
        account.description = description or None
    session.flush()
    audit.record(
        session,
        actor_token_id=actor_token_id,
        action="category.update",
        entity_type="account",
        entity_id=account.id,
        before=before,
        after={"name": account.name, "description": account.description},
    )
    return account


def update_account(
    session: Session,
    *,
    account_id: uuid.UUID,
    name: str | None = None,
    description: str | None = None,
    hidden: bool | None = None,
    feed_start: date_type | None = None,
    feed_start_set: bool = False,
    feed_invert: bool | None = None,
    drift_basis_override: str | None = None,
    drift_basis_override_set: bool = False,
    holdings_source: str | None = None,
    min_balance: Decimal | None = None,
    min_balance_set: bool = False,
    feed_balance_is_cash: bool | None = None,
    balance_true_up: bool | None = None,
    path: str | None = None,
    statement_close_day: int | None = None,
    statement_close_day_set: bool = False,
    actor_token_id: uuid.UUID | None,
) -> Account:
    """Rename / re-describe / hide / move a REAL account (DESIGN §3.4);
    categories rename through PATCH /categories and keep their paths.
    ``hidden`` maps to ``hidden_at`` (set now / clear). ``feed_start`` (§7.1,
    decision 40) applies only when ``feed_start_set`` — None can't be the
    sentinel for a clearable date, so the API layer passes the explicit-null
    signal separately (model_fields_set). ``feed_invert`` (§7.1, decision 43)
    needs no such sentinel: None = unchanged. ``drift_basis_override``
    (§7.3, decision 83) pins the drift check's basis; like ``feed_start`` it
    applies only when ``drift_basis_override_set`` (None clears the pin), and
    only on the account types the drift check runs on.
    ``holdings_source`` (§8, decision 88; None = unchanged) switches a
    brokerage account between feed and manual holdings, through
    ``holdings.service.switch_source``; what the switch did joins the audit
    row. ``feed_balance_is_cash`` (§7.3, decision 103; None = unchanged) says
    a manually tracked brokerage account's feed balance is its cash, which
    puts it in the drift check (and lets it take a drift-basis pin); turning
    it off clears such a pin. ``path`` moves the account and its subtree
    (``move_account``, decision 72); None = unchanged. ``min_balance`` (§3.4,
    #466), the floor the nightly forecast.shortfall scan checks, applies only
    when ``min_balance_set`` (None clears it, like ``feed_start``) and passes
    ``check_min_balance``. ``balance_true_up`` (decision 119; None =
    unchanged) switches the post-sync balance true-up of a brokerage account
    whose holdings come from the feed; past adjustments stay.
    ``statement_close_day`` (§3.4, #674, decision 136; applies only when
    ``statement_close_day_set``, None clears it) is the day of the month a
    credit card's statement closes, 1..31; payments to the card whose
    schedule sizes them from the statement cycle are resized at once."""
    account = get_account(session, account_id)
    if is_system_account(account):
        raise LedgerError(f"{account.path} is a system account; Purser keeps it as it is")
    if account.is_virtual:
        raise LedgerError(f"{account.path} is a category; use PATCH /categories")
    if min_balance_set:
        check_min_balance(account.type, min_balance)
    if statement_close_day_set:
        check_statement_close_day(account, statement_close_day)
    if holdings_source is not None or feed_balance_is_cash is not None:
        # Function-local: purser.holdings imports the ledger package.
        from purser.holdings import projection as holdings_projection

        if holdings_source is not None and account.type != "brokerage":
            raise LedgerError(
                f"{account.path} is a {account.type} account; holdings_source applies only "
                "to brokerage accounts"
            )
        # Lock before any change: the lock reads the row fresh, which would
        # overwrite edits made to the object first. The cash opt-in is
        # validated against holdings_source, so it reads that under the
        # lock too: a switch to the feed committed meanwhile refuses it (422)
        # instead of letting it hit the CHECK.
        account = holdings_projection.lock_account(session, account.id)
    resulting_source = holdings_source or account.holdings_source
    resulting_cash = (
        feed_balance_is_cash if feed_balance_is_cash is not None else account.feed_balance_is_cash
    )
    if resulting_source != "manual":
        # Switching back to the feed ends the opt-in (switch_source clears it).
        resulting_cash = False
    if feed_balance_is_cash and (account.type != "brokerage" or resulting_source != "manual"):
        raise LedgerError(
            f"{account.path}: feed_balance_is_cash applies only to a brokerage account "
            "whose holdings are entered in Purser (holdings_source 'manual'); other "
            "accounts' feed balances are already compared as cash, or are market value"
        )
    if balance_true_up and (account.type != "brokerage" or resulting_source != "feed"):
        raise LedgerError(
            f"{account.path}: balance_true_up applies only to a brokerage account whose "
            "holdings come from the feed (holdings_source 'feed'): it keeps the account's cash "
            "in step with the balance the broker reports beside those holdings"
        )
    if drift_basis_override_set:
        if drift_basis_override is not None and drift_basis_override not in drift.BASES:
            raise LedgerError(
                f"drift_basis_override must be one of {', '.join(drift.BASES)} or null"
            )
        if drift_basis_override is not None and not drift.checked_type(
            account.type, feed_balance_is_cash=resulting_cash
        ):
            raise LedgerError(
                f"{account.path} is a {account.type} account; the drift check (and its basis "
                f"pin) runs only on {', '.join(sorted(drift.ELIGIBLE_TYPES))} accounts, and on "
                "a brokerage account whose holdings are entered in Purser and whose "
                "feed_balance_is_cash is on"
            )
    if path is not None:
        move_account(session, account_id=account.id, path=path, actor_token_id=actor_token_id)
    before = {
        "name": account.name,
        "description": account.description,
        "hidden_at": account.hidden_at.isoformat() if account.hidden_at else None,
        "feed_start": account.feed_start.isoformat() if account.feed_start else None,
        "feed_invert": account.feed_invert,
        "drift_basis_override": account.drift_basis_override,
        "holdings_source": account.holdings_source,
        "min_balance": _money(account.min_balance),
        "feed_balance_is_cash": account.feed_balance_is_cash,
        "balance_true_up": account.balance_true_up,
        "statement_close_day": account.statement_close_day,
    }
    if name is not None:
        account.name = name
    if description is not None:
        account.description = description or None
    if hidden is not None:
        if hidden:
            if account.hidden_at is None:
                account.hidden_at = datetime.now(UTC)
        else:
            account.hidden_at = None
    if feed_start_set:
        account.feed_start = feed_start
    if feed_invert is not None:
        account.feed_invert = feed_invert
    if drift_basis_override_set:
        account.drift_basis_override = drift_basis_override
    if min_balance_set:
        account.min_balance = min_balance
    close_day_changed = (
        statement_close_day_set and account.statement_close_day != statement_close_day
    )
    if statement_close_day_set:
        account.statement_close_day = statement_close_day
    if balance_true_up is not None and holdings_source is None:
        account.balance_true_up = balance_true_up
    session.flush()
    switched: dict[str, Any] = {}
    if holdings_source is not None:
        from purser.holdings import service as holdings_service

        switched = holdings_service.switch_source(session, account, holdings_source)
        if balance_true_up is not None and account.holdings_source == "feed":
            account.balance_true_up = balance_true_up
            session.flush()
    if feed_balance_is_cash is not None and account.holdings_source == "manual":
        account.feed_balance_is_cash = feed_balance_is_cash
        if (
            not feed_balance_is_cash
            and not drift_basis_override_set
            and not drift.checked_type(account.type, feed_balance_is_cash=False)
        ):
            # Off the drift check, a basis pin has nothing to pin (decision 83).
            account.drift_basis_override = None
        session.flush()
    audit.record(
        session,
        actor_token_id=actor_token_id,
        action="account.update",
        entity_type="account",
        entity_id=account.id,
        before=before,
        after={
            "name": account.name,
            "description": account.description,
            "hidden_at": account.hidden_at.isoformat() if account.hidden_at else None,
            "feed_start": account.feed_start.isoformat() if account.feed_start else None,
            "feed_invert": account.feed_invert,
            "drift_basis_override": account.drift_basis_override,
            "holdings_source": account.holdings_source,
            "min_balance": _money(account.min_balance),
            "feed_balance_is_cash": account.feed_balance_is_cash,
            "balance_true_up": account.balance_true_up,
            "statement_close_day": account.statement_close_day,
            **switched,
        },
    )
    if close_day_changed:
        # Function-local: purser.schedules imports the ledger package.
        from purser import schedules as schedules_service

        schedules_service.resize_card_payments(session, account, actor_token_id=actor_token_id)
    return account


def _in_subtree(root: str) -> Any:
    """``accounts.path <@ root`` for an already-validated ``root``."""
    return Account.path.op("<@")(cast(literal(root), LtreeType()))


def move_account(
    session: Session, *, account_id: uuid.UUID, path: str, actor_token_id: uuid.UUID | None
) -> Account:
    """Move a REAL account, and every account beneath it, to ``path`` (DESIGN
    §3.4, decision 72). Descendants keep their path relative to it; the
    account's ``parent_id`` becomes the account at the new parent path, if
    there is one. A move to the current path changes nothing.

    Refused with a LedgerError (422): a malformed path (#214); a category, or
    a subtree holding one — categories keep their paths, because schedule
    templates store them (§3.2); a move under the account itself; and a
    destination under a CATEGORY ROOT, the first label of any category's path
    — subtree rollups and filters there would count the account's own
    postings as category totals. Refused with PathConflictError (409): an
    occupied destination, i.e. an account outside the moved subtree already
    at the new path or beneath it.

    Moves no money, so it marks no balances (§3.5). Open suggestion drafts
    name accounts by path, so they are re-pointed in the same transaction."""
    account = get_account(session, account_id)
    if account.is_virtual:
        raise LedgerError(f"{account.path} is a category; categories keep their paths")
    new = check_path(path)
    old = account.path
    if new == old:
        return account
    refuse_system_path(new)
    if new.startswith(old + "."):
        raise LedgerError(f"cannot move {old} under itself ({new})")

    # The subtree's rows in id order before any path changes (#422): a sync
    # locks its accounts in id order too, so the two wait instead of
    # deadlocking; the path UPDATEs below then lock nothing new.
    locks.lock_accounts(
        session, session.execute(select(Account.id).where(_in_subtree(old))).scalars()
    )
    moved = list(
        session.execute(select(Account).where(_in_subtree(old)).order_by(Account.path)).scalars()
    )
    category = next((a for a in moved if a.is_virtual), None)
    if category is not None:
        raise LedgerError(
            f"{old} has a category beneath it ({category.path}); categories keep their paths"
        )
    root = new.split(".", 1)[0]
    category_under_root = session.execute(
        select(Account.path)
        .where(Account.is_virtual.is_(True), _in_subtree(root))
        .order_by(Account.path)
        .limit(1)
    ).scalar_one_or_none()
    if category_under_root is not None:
        raise LedgerError(
            f"{root} is a category root ({category_under_root} lives under it); a real "
            "account cannot move there, because category rollups and subtree filters "
            "would count its postings"
        )
    occupant = session.execute(
        select(Account.path)
        .where(_in_subtree(new), not_(_in_subtree(old)))
        .order_by(Account.path)
        .limit(1)
    ).scalar_one_or_none()
    if occupant is not None:
        if occupant == new:
            raise PathConflictError(f"account path {new!r} already exists")
        raise PathConflictError(
            f"account path {new!r} is not free: {occupant} already sits beneath it"
        )

    renames = [(a.id, a.path, new + a.path[len(old) :]) for a in moved]
    parent_path = new.rsplit(".", 1)[0] if "." in new else None
    parent = (
        session.execute(select(Account).where(Account.path == parent_path)).scalar_one_or_none()
        if parent_path is not None
        else None
    )
    before = {"path": old, "parent_id": str(account.parent_id) if account.parent_id else None}
    account.parent_id = parent.id if parent is not None else None
    session.flush()
    # Through a staging root, in two statements: accounts_path_uniq is checked
    # row by row, so one UPDATE could collide with a row it has not moved yet
    # (a.b -> a while a.b.b exists). subpath() refuses an offset equal to the
    # path's length, hence the CASE for the subtree root itself.
    staging = f"purser_move_{uuid.uuid4().hex}"
    session.execute(
        text(
            "UPDATE accounts SET path = CASE WHEN path = CAST(:old AS ltree) "
            "THEN CAST(:staging AS ltree) ELSE CAST(:staging AS ltree) "
            "|| subpath(path, nlevel(CAST(:old AS ltree))) END "
            "WHERE path <@ CAST(:old AS ltree)"
        ),
        {"old": old, "staging": staging},
    )
    session.execute(
        text(
            "UPDATE accounts SET path = CASE WHEN nlevel(path) = 1 "
            "THEN CAST(:new AS ltree) ELSE CAST(:new AS ltree) || subpath(path, 1) END "
            "WHERE path <@ CAST(:staging AS ltree)"
        ),
        {"new": new, "staging": staging},
    )
    for moved_account in moved:
        session.expire(moved_account, ["path"])

    repointed = _repoint_suggestion_drafts(
        session,
        {old_path: new_path for _id, old_path, new_path in renames},
        [moved_id for moved_id, _old, _new in renames],
        actor_token_id=actor_token_id,
    )
    audit.record(
        session,
        actor_token_id=actor_token_id,
        action="account.move",
        entity_type="account",
        entity_id=account.id,
        before=before,
        after={
            "path": new,
            "parent_id": str(account.parent_id) if account.parent_id else None,
            "moved": [
                {"id": str(moved_id), "from": old_path, "to": new_path}
                for moved_id, old_path, new_path in renames
            ],
            "suggestions_repointed": repointed,
        },
    )
    events.emit(
        session,
        type_="account.moved",
        entity_type="account",
        entity_id=account.id,
        payload={"path": new, "from_path": old, "accounts_moved": len(renames)},
    )
    return account


def _repoint_suggestion_drafts(
    session: Session,
    renames: dict[str, str],
    account_ids: list[uuid.UUID],
    *,
    actor_token_id: uuid.UUID | None,
) -> int:
    """Open suggestions carry their accounts as PATHS in the typed draft
    (decision 57): a schedule's ``account``/``counterparty_account``, a
    transfer link's and a transfer pair's ``from_account``/``to_account``
    (#386 review). A move re-points them; otherwise accepting a schedule or
    a transfer link would miss the account — or find whatever later took
    its old path — and a card would show a path that no longer exists.
    Decided suggestions are history and stay as they were."""
    repointed = 0
    rows = session.execute(
        select(Suggestion).where(
            Suggestion.status == "open",
            or_(
                Suggestion.subject_account_id.in_(account_ids),
                Suggestion.counterparty_account_id.in_(account_ids),
            ),
        )
    ).scalars()
    for row in rows:
        draft = dict(row.draft)
        before: dict[str, Any] = {
            key: draft.get(key)
            for key in ("account", "counterparty_account", "from_account", "to_account")
            if key in draft
        }
        for key, value in before.items():
            if value in renames:
                draft[key] = renames[value]
        after = {key: draft.get(key) for key in before}
        if after == before:
            continue
        row.draft = draft
        row.updated_at = datetime.now(UTC)
        repointed += 1
        audit.record(
            session,
            actor_token_id=actor_token_id,
            action="suggestion.repoint",
            entity_type="suggestion",
            entity_id=row.id,
            before=before,
            after=after,
        )
    session.flush()
    return repointed


def ensure_category_path(
    session: Session, path: str, *, actor_token_id: uuid.UUID | None
) -> Account:
    """Resolve a category ltree path, auto-creating any missing ancestors
    along the way (``income.*`` -> category_income, else category_expense).
    Mirrors the QIF importer's category ensure (purser/qif_import.py). A
    system account is never a category, so the system subtree (``equity``
    and everything under it) is refused, found or not."""
    check_path(path)
    refuse_system_path(path)
    try:
        found = account_by_path(session, path)
    except NotFoundError:
        found = None
    if found is not None:
        refuse_system_account(found)
        return found
    parts = path.split(".")
    parent_id: uuid.UUID | None = None
    account: Account | None = None
    for depth in range(1, len(parts) + 1):
        subpath = ".".join(parts[:depth])
        try:
            account = account_by_path(session, subpath)
        except NotFoundError:
            account = create_account(
                session,
                name=parts[depth - 1].replace("_", " ").title(),
                type_="category_income" if parts[0] == "income" else "category_expense",
                parent_id=parent_id,
                path=subpath,
                actor_token_id=actor_token_id,
            )
        parent_id = account.id
    assert account is not None
    return account


def _assert_accounts_open(session: Session, account_ids: set[uuid.UUID]) -> None:
    """The single read-only chokepoint: no write may post to a CLOSED real
    account (DESIGN §3.4). Categories (virtual) never close, so they're
    skipped in one joined query."""
    if not account_ids:
        return
    closed = (
        session.execute(
            select(Account.path).where(
                Account.id.in_(account_ids),
                Account.is_virtual.is_(False),
                Account.closed_at.is_not(None),
            )
        )
        .scalars()
        .first()
    )
    if closed is not None:
        raise LedgerError(f"account {closed} is closed (read-only)")


def delete_category(
    session: Session, *, category_id: uuid.UUID, actor_token_id: uuid.UUID | None
) -> None:
    """Delete an UNUSED category: no children, no postings, no schedule
    templates. Used categories are history — rename them instead."""
    account = get_account(session, category_id)
    if not account.is_virtual:
        raise LedgerError(f"{account.path} is not a category")
    refuse_system_account(account)
    child = session.execute(
        select(Account.id).where(Account.parent_id == account.id).limit(1)
    ).scalar_one_or_none()
    if child is not None:
        raise LedgerError(f"{account.path} has child categories")
    posting = session.execute(
        select(Posting.id).where(Posting.account_id == account.id).limit(1)
    ).scalar_one_or_none()
    if posting is not None:
        raise LedgerError(f"{account.path} has transactions")
    template = session.execute(
        select(Schedule.id).where(Schedule.category_path == account.path).limit(1)
    ).scalar_one_or_none()
    if template is not None:
        raise LedgerError(f"{account.path} is a schedule's category template")
    audit.record(
        session,
        actor_token_id=actor_token_id,
        action="category.delete",
        entity_type="account",
        entity_id=account.id,
        before={"name": account.name, "path": account.path},
    )
    session.delete(account)
    session.flush()
