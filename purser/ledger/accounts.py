"""Accounts and categories: lookup, create, update, move, delete, and the
closed-account write guard (DESIGN §3.4)."""

import uuid
from datetime import UTC, datetime
from datetime import date as date_type
from decimal import Decimal
from typing import Any

from sqlalchemy import cast, literal, not_, or_, select, text
from sqlalchemy.orm import Session

from purser import audit, events, locks
from purser.core import ledger as core
from purser.feed import drift
from purser.ledger.errors import LedgerError, NotFoundError, PathConflictError, check_path
from purser.models import Account, LtreeType, Posting, Schedule, Suggestion

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
    )
    session.add(account)
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
    path: str | None = None,
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
    row. ``path`` moves the account and its subtree (``move_account``,
    decision 72); None = unchanged. ``min_balance`` (§3.4, #466), the floor
    the nightly forecast.shortfall scan checks, applies only when
    ``min_balance_set`` (None clears it, like ``feed_start``) and passes
    ``check_min_balance``."""
    account = get_account(session, account_id)
    if account.is_virtual:
        raise LedgerError(f"{account.path} is a category; use PATCH /categories")
    if min_balance_set:
        check_min_balance(account.type, min_balance)
    if holdings_source is not None:
        # Function-local: purser.holdings imports the ledger package.
        from purser.holdings import projection as holdings_projection

        if account.type != "brokerage":
            raise LedgerError(
                f"{account.path} is a {account.type} account; holdings_source applies only "
                "to brokerage accounts"
            )
        # Lock before any change: the lock reads the row fresh, which would
        # overwrite edits made to the object first.
        account = holdings_projection.lock_account(session, account.id)
    if drift_basis_override_set:
        if drift_basis_override is not None and drift_basis_override not in drift.BASES:
            raise LedgerError(
                f"drift_basis_override must be one of {', '.join(drift.BASES)} or null"
            )
        if drift_basis_override is not None and account.type not in drift.ELIGIBLE_TYPES:
            raise LedgerError(
                f"{account.path} is a {account.type} account; the drift check (and its basis "
                f"pin) runs only on {', '.join(sorted(drift.ELIGIBLE_TYPES))} accounts"
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
    session.flush()
    switched: dict[str, Any] = {}
    if holdings_source is not None:
        from purser.holdings import service as holdings_service

        switched = holdings_service.switch_source(session, account, holdings_source)
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
            **switched,
        },
    )
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
    """Open schedule suggestions carry their accounts as PATHS in the typed
    draft accept applies (decision 57), so a move re-points them; otherwise
    accepting one would miss the account — or find whatever later took its
    old path. Decided suggestions are history and stay as they were."""
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
        before = {key: draft.get(key) for key in ("account", "counterparty_account")}
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
    Mirrors the QIF importer's category ensure (purser/qif_import.py)."""
    try:
        return account_by_path(session, path)
    except NotFoundError:
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
