"""What Purser proposes for a bank account the feed delivers and no ledger
account takes yet, and the mapping an agent applies from it (issue #583,
DESIGN §7.1, decision 126).

Mapping a delivered account means choosing a name, a type and a ledger path,
or ignoring it. SimpleFIN states the name, the bank login, the currency, the
balances and (as a Bridge extension) the holdings, but no account type, and
an account's type can never be changed afterwards. So Purser derives a
proposal from what the feed does state and says what it rests on:

- **name**: the bank's own words ("Savings", "Visa", "Mortgage"), marked
  unsure when the feed also lists holdings for an account not named as an
  investment one;
- **holdings**: the name says nothing and the feed lists holdings, so the
  account is a brokerage one;
- **balance**: a debt with credit left is a card, a debt with no transactions
  is a loan;
- **default**: nothing says, so it is offered as checking and marked unsure.

An agent never sees the bank's account id or its balance (decision 100). It
names a delivered account by ``ref`` (``crypto.feed_account_ref``) and either
accepts the proposal or overrides parts of it; :func:`apply` validates the
whole batch before it writes anything, and its dry run is the same code path
stopping before the first write.
"""

import re
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from purser import crypto
from purser import ledger as ledger_service
from purser.core import ledger as core
from purser.feed import drift, envelope
from purser.feed.inventory import redact_name
from purser.models import Account, Connection

LIABILITY_TYPES = frozenset({"credit_card", "loan"})
MAPPABLE_TYPES = ("checking", "savings", "credit_card", "cash", "brokerage", "loan", "property")

# The bank's own words for the product, in the order they are tried: the
# product wins over a brand ("Example Card Company Online Savings" is savings).
_NAME_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("checking", re.compile(r"\b(checking|chequing)\b")),
    ("savings", re.compile(r"\b(savings?|money market)\b")),
    ("loan", re.compile(r"\b(mortgage|loan|heloc|line of credit)\b")),
    (
        "brokerage",
        re.compile(
            r"\b(brokerage|investments?|investing|ira|roth|401k|401\(k\)|403b|retirement"
            r"|securities)"
        ),
    ),
    ("credit_card", re.compile(r"\b(credit|card|visa|mastercard|amex|american express)\b")),
)


class MappingError(ledger_service.LedgerError):
    """A mapping batch that cannot be applied as given (422)."""


@dataclass(frozen=True)
class Proposal:
    """Purser's proposal for one delivered, unmapped bank account."""

    ref: str
    connection_id: uuid.UUID
    external_id: str
    institution: str | None
    bank_name: str | None  # the bank's name for it, masked digits removed
    currency: str
    action: str  # map | ignore | blocked
    name: str
    type: str
    type_basis: str  # holdings | name | balance | default
    confident: bool  # False when nothing in the feed says what it is
    path: str
    starting_balance: bool
    duplicate_of: str | None = None  # a ledger account path or another ref
    blocked: str | None = None  # why it cannot be mapped now
    lists_holdings: bool = False  # the feed lists holdings for it

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "ref": self.ref,
            "institution": self.institution,
            "bank_name": self.bank_name,
            "action": self.action,
        }
        if self.action == "blocked":
            out["blocked"] = self.blocked
            return out
        if self.duplicate_of is not None:
            out["duplicate_of"] = self.duplicate_of
        if self.lists_holdings and self.type != "brokerage":
            # Why the type is unsure: the name names a cash product and the
            # feed also lists investments in it.
            out["note"] = "the feed lists holdings for this account"
        out.update(
            {
                "name": self.name,
                "type": self.type,
                "type_basis": self.type_basis,
                "confident": self.confident,
                "path": self.path,
                "starting_balance": self.starting_balance,
            }
        )
        return out


def _decimal(raw: object) -> Decimal | None:
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = Decimal(str(raw))
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def propose_type(sf_account: dict[str, Any]) -> tuple[str, str, bool]:
    """``(type, basis, confident)`` for a delivered account, from what the
    feed states about it.

    The bank's own word for the product comes first: it is what the person
    calls the account too. Holdings decide only when the name says nothing.
    When the two disagree (a "Savings" account that lists positions, which
    some banks and the Bridge's demo data do deliver), the name's type is
    proposed and marked unsure: only the person knows whether the balance the
    bank reports is cash or cash plus investments, and the type can never be
    changed afterwards."""
    holdings = bool(sf_account.get("holdings"))
    name = str(sf_account.get("name") or "").lower()
    for type_, pattern in _NAME_RULES:
        if pattern.search(name):
            return type_, "name", type_ == "brokerage" or not holdings
    if holdings:
        return "brokerage", "holdings", True
    balance = _decimal(sf_account.get("balance"))
    if balance is not None and balance < 0:
        available = _decimal(sf_account.get("available-balance"))
        if available is not None and available > 0:
            return "credit_card", "balance", True  # owes money, with credit left
        if not sf_account.get("transactions"):
            return "loan", "balance", False  # a debt nothing moves on
        return "credit_card", "balance", False
    return "checking", "default", False


def _label(name: str) -> str:
    label = re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_")
    return label or "account"


def _login_key(institution: envelope.Institution | None) -> str | None:
    return institution.conn_id if institution else None


def _bank_key(institution: envelope.Institution | None) -> str | None:
    if institution is None:
        return None
    return institution.org_id or institution.org_url or institution.org_name or institution.name


def _twin_key(payload: dict[str, Any], sf_account: dict[str, Any]) -> tuple[Any, ...] | None:
    """What two deliveries of one account share: the bank, the name, the
    currency and the balance. None when the account cannot be compared (no
    login identity, as in a v1 payload; no balance; no name)."""
    institution = envelope.institution_of(payload, sf_account)
    balance = _decimal(sf_account.get("balance"))
    name = redact_name(sf_account.get("name"))
    if _login_key(institution) is None or balance is None or not name:
        return None
    return (_bank_key(institution), name.lower(), sf_account.get("currency"), balance)


def plan(session: Session) -> list[Proposal]:
    """A proposal for every delivered bank account that no ledger account
    takes and nobody chose to ignore, across every connection that syncs.
    Names and paths are unique within the plan and against the ledger."""
    from purser.feed.service import ignored_external_ids, latest_sync_fetch

    taken_paths = {str(p) for p in session.execute(select(Account.path)).scalars()}
    taken_names = {
        str(n).strip().lower()
        for n in session.execute(
            select(Account.name).where(Account.is_virtual.is_(False))
        ).scalars()
    }
    proposals: list[Proposal] = []
    for connection in session.execute(
        select(Connection).where(Connection.status != "disabled").order_by(Connection.created_at)
    ).scalars():
        raw = latest_sync_fetch(session, connection.id)
        if raw is None:
            continue
        payload = crypto.decrypt_json(raw.payload_enc)
        delivered = [
            a for a in payload.get("accounts") or [] if isinstance(a, dict) and a.get("id")
        ]
        mapped = {
            str(account.external_id): account
            for account in session.execute(
                select(Account).where(
                    Account.connection_id == connection.id, Account.external_id.is_not(None)
                )
            ).scalars()
        }
        ignored = ignored_external_ids(session, connection.id)
        ambiguous = envelope.duplicate_account_ids(payload)

        # The same account delivered through a second login at the same bank
        # (a joint account, once per person): same bank, another login, same
        # name, currency and balance. v1 payloads carry no login identity, so
        # nothing is called a duplicate there.
        seen: dict[tuple[Any, ...], tuple[str | None, str]] = {}

        for sf_account in delivered:
            account = mapped.get(str(sf_account["id"]))
            key = _twin_key(payload, sf_account)
            if account is not None and account.closed_at is None and key is not None:
                login = _login_key(envelope.institution_of(payload, sf_account))
                seen.setdefault(key, (login, account.path))

        for sf_account in delivered:
            external_id = str(sf_account["id"])
            if external_id in mapped or external_id in ignored:
                continue
            institution = envelope.institution_of(payload, sf_account)
            bank_name = redact_name(sf_account.get("name"))
            ref = crypto.feed_account_ref(connection.id, external_id)
            currency = str(sf_account.get("currency") or "USD").strip().upper()
            base: dict[str, Any] = {
                "ref": ref,
                "connection_id": connection.id,
                "external_id": external_id,
                "institution": institution.name if institution else None,
                "bank_name": bank_name,
                "currency": currency,
            }
            if external_id in ambiguous:
                proposals.append(
                    Proposal(
                        **base,
                        action="blocked",
                        name="",
                        type="",
                        type_basis="",
                        confident=False,
                        path="",
                        starting_balance=False,
                        blocked="ambiguous_id",
                    )
                )
                continue
            type_, basis, confident = propose_type(sf_account)
            name = bank_name or (institution.name if institution else None) or "Account"
            if name.strip().lower() in taken_names and institution and institution.name:
                name = f"{name} ({institution.name})"
            ordinal = 2
            unique = name
            while unique.strip().lower() in taken_names:
                unique = f"{name} {ordinal}"
                ordinal += 1
            name = unique
            root = "liabilities" if type_ in LIABILITY_TYPES else "assets"
            path = f"{root}.{_label(name)}"
            ordinal = 2
            while path in taken_paths:
                path = f"{root}.{_label(name)}_{ordinal}"
                ordinal += 1

            key = _twin_key(payload, sf_account)
            login = _login_key(institution)
            duplicate_of: str | None = None
            if key is not None:
                first = seen.get(key)
                if first is not None and first[0] != login:
                    duplicate_of = first[1]
                elif first is None:
                    seen[key] = (login, ref)
            action = "ignore" if duplicate_of is not None else "map"
            if action == "map":
                taken_names.add(name.strip().lower())
                taken_paths.add(path)
            proposals.append(
                Proposal(
                    **base,
                    action=action,
                    name=name,
                    type=type_,
                    type_basis=basis,
                    confident=confident,
                    path=path,
                    starting_balance=type_ in drift.ELIGIBLE_TYPES,
                    duplicate_of=duplicate_of,
                    lists_holdings=bool(sf_account.get("holdings")),
                )
            )
    return proposals


def proposal_for(session: Session, connection_id: uuid.UUID, external_id: str) -> Proposal | None:
    """The proposal for one delivered account (the admin discovery list)."""
    return next(
        (
            p
            for p in plan(session)
            if p.connection_id == connection_id and p.external_id == external_id
        ),
        None,
    )


@dataclass(frozen=True)
class Step:
    """One resolved line of a mapping batch."""

    proposal: Proposal
    action: str  # map | ignore
    name: str | None = None
    type: str | None = None
    path: str | None = None
    account: Account | None = None  # an existing account to map onto
    starting_balance: bool = False
    # The feed the existing account takes now, which this line replaces
    # (replace_feed, admin only; #648 M1).
    replaces: dict[str, Any] | None = None


def current_feed(session: Session, account: Account) -> dict[str, Any] | None:
    """The bank account an account's feed comes from, as the plan names a
    delivered one (login and the bank's name for it, masked digits removed,
    never its id), or None when it takes no feed. ``delivered`` is False when
    the latest sync no longer lists it."""
    from purser.feed.service import latest_sync_fetch

    if account.connection_id is None or account.external_id is None:
        return None
    out: dict[str, Any] = {"institution": None, "bank_name": None, "delivered": False}
    raw = latest_sync_fetch(session, account.connection_id)
    payload = crypto.decrypt_json(raw.payload_enc) if raw is not None else {}
    for sf_account in payload.get("accounts") or []:
        if isinstance(sf_account, dict) and str(sf_account.get("id")) == account.external_id:
            institution = envelope.institution_of(payload, sf_account)
            out = {
                "institution": institution.name if institution else None,
                "bank_name": redact_name(sf_account.get("name")),
                "delivered": True,
            }
            break
    return out


def _feed_words(feed: dict[str, Any]) -> str:
    named = " at ".join(str(v) for v in (feed["bank_name"], feed["institution"]) if v)
    words = f"the feed of {named}" if named else "a bank feed"
    return words if feed["delivered"] else f"{words} (not in the latest sync)"


def _resolve(
    session: Session, items: list[dict[str, Any]], *, may_replace: bool = False
) -> list[Step]:
    """Check a whole batch against the current plan and the ledger. Raises
    :class:`MappingError` naming every problem at once.

    A line may map onto an existing account only while that account takes no
    feed, unless it says ``replace_feed`` and the caller is an admin
    (``may_replace``; decision 134): replacing a feed moves the account's own
    bank account back to waiting and brings another bank's rows into it, and
    nothing undoes that through the API."""
    by_ref = {p.ref: p for p in plan(session)}
    problems: list[str] = []
    steps: list[Step] = []
    seen_refs: set[str] = set()
    paths = {str(p) for p in session.execute(select(Account.path)).scalars()}
    targets: set[uuid.UUID] = set()
    for index, item in enumerate(items):
        where = f"accounts[{index}]"
        if not isinstance(item, dict) or not isinstance(item.get("ref"), str):
            problems.append(f"{where}: needs a ref from the plan")
            continue
        unknown = set(item) - {
            "ref",
            "action",
            "name",
            "type",
            "path",
            "account",
            "starting_balance",
            "replace_feed",
        }
        if unknown:
            problems.append(f"{where}: unknown field(s) {sorted(unknown)}")
            continue
        ref = item["ref"]
        proposal = by_ref.get(ref)
        if proposal is None:
            problems.append(
                f"{where}: {ref} is not a bank account waiting to be mapped (it may be "
                "mapped or ignored already; ask for the plan again)"
            )
            continue
        if ref in seen_refs:
            problems.append(f"{where}: {ref} appears twice")
            continue
        seen_refs.add(ref)
        if proposal.action == "blocked":
            problems.append(
                f"{where}: {ref} cannot be mapped yet ({proposal.blocked}): two bank logins "
                "deliver an account with the same id"
            )
            continue
        action = item.get("action") or ("ignore" if proposal.action == "ignore" else "map")
        if action not in ("map", "ignore"):
            problems.append(f"{where}: action must be map or ignore")
            continue
        if action == "ignore":
            extra = set(item) - {"ref", "action"}
            if extra:
                problems.append(f"{where}: an ignored account takes no {sorted(extra)}")
                continue
            steps.append(Step(proposal=proposal, action="ignore"))
            continue
        if item.get("account") is not None:
            if any(item.get(k) is not None for k in ("name", "type", "path")):
                problems.append(f"{where}: give an existing account OR a name, type and path")
                continue
            try:
                existing = ledger_service.resolve_account(session, str(item["account"]))
            except ledger_service.LedgerError as exc:
                problems.append(f"{where}: {exc}")
                continue
            if existing.is_virtual or existing.closed_at is not None:
                problems.append(f"{where}: {existing.path} is not an open account")
                continue
            if existing.currency.strip() != proposal.currency:
                problems.append(
                    f"{where}: {existing.path} is in {existing.currency.strip()}, the bank "
                    f"account in {proposal.currency}"
                )
                continue
            if existing.id in targets:
                problems.append(f"{where}: {existing.path} is the target of another line")
                continue
            feed = current_feed(session, existing)
            if feed is not None and not item.get("replace_feed"):
                problems.append(
                    f"{where}: {existing.path} already takes {_feed_words(feed)}; mapping onto "
                    "it would replace that feed, which needs replace_feed=true from an admin"
                )
                continue
            if feed is not None and not may_replace:
                problems.append(
                    f"{where}: replacing {existing.path}'s feed ({_feed_words(feed)}) needs "
                    "admin scope: nothing undoes it through the API, so a person with admin "
                    "rights does it (the bank account's mapping form in Purser)"
                )
                continue
            targets.add(existing.id)
            wanted = bool(item.get("starting_balance", False))
            steps.append(
                Step(
                    proposal=proposal,
                    action="map",
                    account=existing,
                    starting_balance=wanted and existing.type in drift.ELIGIBLE_TYPES,
                    replaces=feed,
                )
            )
            continue
        if item.get("replace_feed"):
            problems.append(f"{where}: replace_feed applies only with account")
            continue
        type_ = str(item.get("type") or proposal.type)
        if type_ not in MAPPABLE_TYPES:
            problems.append(f"{where}: type must be one of {', '.join(MAPPABLE_TYPES)}")
            continue
        name = str(item.get("name") or proposal.name).strip()
        if not name:
            problems.append(f"{where}: name is empty")
            continue
        if item.get("path") is not None:
            path = str(item["path"]).strip()
        elif type_ == proposal.type and name == proposal.name:
            path = proposal.path
        else:
            root = "liabilities" if type_ in LIABILITY_TYPES else "assets"
            path = f"{root}.{_label(name)}"
            ordinal = 2
            while path in paths:
                path = f"{root}.{_label(name)}_{ordinal}"
                ordinal += 1
        try:
            path = ledger_service.check_path(path)
            ledger_service.refuse_system_path(path)
        except ledger_service.LedgerError as exc:
            problems.append(f"{where}: {exc}")
            continue
        if path in paths:
            problems.append(f"{where}: an account already exists at {path}")
            continue
        paths.add(path)
        asked = item.get("starting_balance")
        starting = (
            bool(asked) if asked is not None else proposal.starting_balance
        ) and type_ in drift.ELIGIBLE_TYPES
        steps.append(
            Step(
                proposal=proposal,
                action="map",
                name=name,
                type=type_,
                path=path,
                starting_balance=starting,
            )
        )
    if problems:
        raise MappingError("; ".join(problems))
    return steps


def _line(step: Step, account: Account | None = None, started: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "ref": step.proposal.ref,
        "institution": step.proposal.institution,
        "bank_name": step.proposal.bank_name,
        "action": step.action,
    }
    if step.action == "ignore":
        return out
    target = account or step.account
    out.update(
        {
            "account": target.path if target is not None else step.path,
            "name": target.name if target is not None else step.name,
            "type": target.type if target is not None else step.type,
            "new_account": step.account is None,
            "starting_balance": started if started is not None else step.starting_balance,
        }
    )
    if step.replaces is not None:
        out["replaces"] = step.replaces
    return out


def apply(
    session: Session,
    *,
    items: list[dict[str, Any]],
    actor_token_id: uuid.UUID | None,
    dry_run: bool = False,
    may_replace: bool = False,
) -> list[dict[str, Any]]:
    """Map or ignore the delivered accounts ``items`` name. The whole batch is
    checked first and written in the caller's transaction, so it lands whole
    or not at all. Each mapping is the admin form's own write
    (``service.map_account``): the account, the import of what was already
    fetched, the starting balance and the audit rows. ``may_replace``: the
    caller is an admin, so a ``replace_feed`` line may take an account's feed
    over (each such line names the feed it replaces, dry run included)."""
    from purser import starting_balance
    from purser.feed import service

    steps = _resolve(session, items, may_replace=may_replace)
    if dry_run:
        return [_line(step) for step in steps]
    results: list[dict[str, Any]] = []
    for step in steps:
        proposal = step.proposal
        if step.action == "ignore":
            service.set_ignored(
                session,
                connection_id=proposal.connection_id,
                external_id=proposal.external_id,
                ignored=True,
                actor_token_id=actor_token_id,
            )
            results.append(_line(step))
            continue
        account = service.map_account(
            session,
            connection_id=proposal.connection_id,
            external_id=proposal.external_id,
            account_id=step.account.id if step.account is not None else None,
            create=(
                None
                if step.account is not None
                else {
                    "name": str(step.name),
                    "type": str(step.type),
                    "path": str(step.path),
                    "currency": proposal.currency,
                }
            ),
            starting_balance=step.starting_balance,
            actor_token_id=actor_token_id,
        )
        marker = starting_balance.get(session, account.id) if step.starting_balance else None
        started = str(core.quantize(Decimal(marker.amount))) if marker is not None else None
        results.append(_line(step, account, started if step.starting_balance else None))
    return results
