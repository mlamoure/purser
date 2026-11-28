"""A securities trade's cash (issue #390 phase 1b, DESIGN §8, decision 103).

A sale with ``proceeds_to='cash'`` puts its net proceeds on the brokerage
account; a buy with ``funded_from='cash'`` takes its cost from it. The other
side of that cash is ``equity.securities``, a system virtual account that is
not a category: spending, income, the classifier, rules and schedules never
see it, and net worth is unmoved by the posting (the market value of the
shares sold leaves the holdings at the same time).

The cash is either posted here — one ordinary flat transaction, posted,
``origin='system'``, dated ``settle_on`` — or
LINKED: a row the feed (or a person) already recorded is booked to
``equity.securities`` through the ledger's reversal-pair correction. Before
posting, the account is probed for such a row (``probe``): when one exists the
write is refused with its id (``CashAlreadyRecorded``), because posting would
count the same cash twice.

A broker's copy that arrives AFTER Purser posted the cash is caught at import
instead (``unclaimed_cash`` / ``hand_over``, #408, decision 104): it is linked
in place of Purser's row, which is voided into it.

The cash and the shares always move together. A trade's cash row can be
neither edited nor recategorized (``ledger._flat_legs`` refuses it), and
reversing the trade undoes its cash through the ledger's reversal pair
(guardrail 3): Purser's own row is corrected to nothing, and a linked row
gets its category back.
"""

import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, localcontext

from sqlalchemy import and_, exists, func, literal, or_, select
from sqlalchemy.orm import Session, aliased

from purser import audit, balances, ledger
from purser.core import holdings as core_holdings
from purser.core import ledger as core
from purser.ledger import LedgerError, NotFoundError
from purser.models import (
    Account,
    AccountReconciliation,
    ApiToken,
    Match,
    Position,
    PositionEvent,
    Posting,
    Transaction,
)

CASH = "cash"
NONE = "none"
MODES = (CASH, NONE)

# How long after settlement a recorded row of the same amount still counts as
# the trade's cash for the double-count probe.
PROBE_DAYS_AFTER_SETTLEMENT = 5

# A trade's cash settles in cents, as a broker's statement shows it and as the
# feed's copy of it reads (the probe compares amounts exactly): quantity x
# price is often sub-cent with fractional shares, so the net is rounded once,
# half up, to the cent.
CASH_QUANT = Decimal("0.01")


def cash_amount(kind: str, quantity: Decimal, price: Decimal, fees: Decimal | None) -> Decimal:
    """What the trade moves, before its sign: a sale's net proceeds
    (quantity x price - fees) or a purchase's cost (quantity x price + fees),
    to the cent."""
    with localcontext() as ctx:
        ctx.prec = 60  # quantity and price hold 20 digits each: the product is exact
        gross = quantity * price
        fee = fees or Decimal(0)
        net = gross - fee if kind == core_holdings.SELL else gross + fee
        return net.quantize(CASH_QUANT, rounding=ROUND_HALF_UP)


class CashAlreadyRecorded(LedgerError):
    """The account already holds a row for this trade's cash (REST 409)."""

    def __init__(self, tx: Transaction, amount: Decimal, match: Match | None = None) -> None:
        what = f" ({tx.description})" if tx.description else ""
        found = (
            f"the account already shows this cash: transaction {tx.id} on "
            f"{tx.date.isoformat()} for {core.quantize(amount)}{what}"
        )
        if tx.status == core.Status.PENDING.value:
            # A link takes a posted row only, and the bank may still post it
            # under another identity: wait for it rather than post a second.
            ways = (
                "the broker's copy is pending: record the sale after it posts and link it "
                "(transaction_id names the posted row), or pass force_cash=true if it is a "
                "different payment, or proceeds_to='none' to record the trade without cash"
            )
        else:
            ways = (
                f"if that row is this trade's cash, pass transaction_id={tx.id} to link it; if "
                "it is a different payment of the same amount, pass force_cash=true to post "
                "the trade's cash as well"
            )
        if match is not None:
            # The link would be refused while the bank line settles a bill.
            ways += (
                f". That row's bank line settles expected {match.expected_id}: "
                f"{match_remedy(match)} before linking it"
            )
        super().__init__(f"{found}; {ways}")
        self.transaction_id = tx.id
        self.date = tx.date
        self.amount = core.quantize(amount)
        self.pending = tx.status == core.Status.PENDING.value


@dataclass(frozen=True)
class CashPlan:
    """What a buy or sell does with cash, validated."""

    mode: str | None  # the stored proceeds_to (sell) / funded_from (buy); None for set_quantity
    moves: bool
    # When the cash posts: the trade date unless settle_on names a later
    # day; for a link, the linked row's date (None here, ``link`` knows it).
    settle_on: date | None
    amount: Decimal | None  # account side: + proceeds, - cost; None when no cash moves
    # The last day a row on the account still counts as this trade's cash:
    # five days after settlement (``probe_window``).
    window_end: date | None = None


def correction_chain(session: Session, tx: Transaction) -> list[uuid.UUID]:
    """``tx`` and every row its correction chain holds behind it (the
    reversal and the original it corrects, back to the bank's row): one bank
    line, whichever row of it a match names."""
    ids = [tx.id]
    current = tx.reverses_id
    while current is not None:
        ids.append(current)
        current = session.execute(
            select(Transaction.reverses_id).where(Transaction.id == current)
        ).scalar_one_or_none()
    return ids


def live_match_on_chain(session: Session, tx: Transaction) -> Match | None:
    """A live (proposed or confirmed) match naming any row of ``tx``'s
    chain: the matcher's pool holds the import original, so a bill can be
    settled through a row that a rule or a person has since corrected."""
    return session.execute(
        select(Match)
        .where(
            Match.posted_id.in_(correction_chain(session, tx)),
            Match.state.in_(["proposed", "confirmed"]),
        )
        .order_by(Match.created_at, Match.id)
        .limit(1)
    ).scalar_one_or_none()


def match_remedy(match: Match) -> str:
    """How to take a match off a bank row, by its state: a proposal is
    rejected (unmatch refuses one), a confirmed match is unmatched."""
    if match.state == "proposed":
        return (
            f"reject proposal {match.id} (decide_suggestion on its match card, or "
            f"POST /matching/{match.id}/reject)"
        )
    return f"unmatch match {match.id} (POST /matching/{match.id}/unmatch)"


def next_business_day(day: date) -> date:
    """T+1: the next weekday (no holiday calendar)."""
    nxt = day + timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    return nxt


def probe_window(occurred_on: date, settle_on: date | None) -> tuple[date, date]:
    """Where the account's copy of a trade's cash may sit: from the trade to
    five days after settlement — ``settle_on`` when given, else the next
    business day (T+1), the date a broker books it — whatever date Purser
    posts the cash on."""
    settled = settle_on or next_business_day(occurred_on)
    return occurred_on, settled + timedelta(days=PROBE_DAYS_AFTER_SETTLEMENT)


def copy_window_end(occurred_on: date, settle_on: date | None) -> date:
    """The last day a broker's copy of a trade's cash Purser posted may be
    dated (#408): five days after its settlement, the later of the date the
    cash posted on (``settle_on``) and the next business day, the date a
    broker books it. The window the double-count probe looked in when the
    trade was recorded, whichever way its settlement was given."""
    settled = max(settle_on or occurred_on, next_business_day(occurred_on))
    return settled + timedelta(days=PROBE_DAYS_AFTER_SETTLEMENT)


def plan(
    kind: str,
    *,
    occurred_on: date,
    quantity: Decimal,
    price: Decimal | None,
    fees: Decimal | None,
    proceeds_to: str | None,
    funded_from: str | None,
    settle_on: date | None,
    transaction_id: uuid.UUID | None,
    force_cash: bool = False,
) -> CashPlan:
    """Validate a trade's cash fields. A sale moves cash unless
    ``proceeds_to='none'`` (shares withheld for tax, say); a buy moves none
    unless ``funded_from='cash'`` (or a ``transaction_id`` names its cash).
    ``force_cash`` overrides the double-count probe, so it applies only to
    cash Purser posts: never to a link, nor to a trade that moves none."""
    if kind == core_holdings.SET_QUANTITY:
        if any(v is not None for v in (proceeds_to, funded_from, settle_on, transaction_id)):
            raise LedgerError(
                "set_quantity moves no cash; proceeds_to, funded_from, settle_on and "
                "transaction_id apply to buys and sells"
            )
        if force_cash:
            raise LedgerError("set_quantity moves no cash; force_cash applies to buys and sells")
        return CashPlan(None, False, None, None)
    if kind == core_holdings.SELL:
        if funded_from is not None:
            raise LedgerError("funded_from applies to buys; a sale's cash is proceeds_to")
        mode = proceeds_to or CASH
        field = "proceeds_to"
    else:
        if proceeds_to is not None:
            raise LedgerError("proceeds_to applies to sells; a buy's cash is funded_from")
        mode = funded_from or (CASH if transaction_id is not None else NONE)
        field = "funded_from"
    if mode not in MODES:
        raise LedgerError(f"{field} must be one of: {', '.join(MODES)}")
    if force_cash and (mode == NONE or transaction_id is not None):
        raise LedgerError(
            "force_cash posts the trade's cash over a row of the same amount the account "
            f"already shows; it applies to a trade whose cash Purser posts ({field} 'cash'), "
            "not to one that links its row (transaction_id) or moves none"
        )
    if mode == NONE:
        if settle_on is not None or transaction_id is not None:
            raise LedgerError(
                f"settle_on and transaction_id apply when the trade moves cash ({field} 'cash')"
            )
        return CashPlan(mode, False, None, None)
    if settle_on is not None and settle_on < occurred_on:
        raise LedgerError("settle_on cannot be before the trade date")
    # A linked row settles when the bank booked it (``link`` checks it).
    # Otherwise the trade date by default: the projection moves the shares on
    # the trade date, so cash posted at settlement would leave net worth
    # short by the sale's value until then.
    settle = settle_on if transaction_id is not None else settle_on or occurred_on
    # A link's window comes from the trade alone: the settle_on of a link is
    # its row's own date, so letting it widen the window would let any later
    # row of that amount stand as the trade's cash.
    _start, window_end = probe_window(
        occurred_on, None if transaction_id is not None else settle_on
    )
    if price is None:
        # A link needs one too: the row it names must hold exactly this net.
        raise LedgerError(
            f"a {kind} that moves cash needs a price, which sets its cash (a linked row must "
            f"hold exactly that amount); {field} 'none' records it without cash"
        )
    amount = cash_amount(kind, quantity, price, fees)
    if amount >= core_holdings.MAX_MONEY:
        raise LedgerError(f"the {kind}'s cash ({field} 'cash') is out of range")
    if amount <= 0:
        what = (
            "the sale's net proceeds (quantity x price - fees)"
            if kind == core_holdings.SELL
            else "the purchase's cost (quantity x price + fees)"
        )
        raise LedgerError(f"{what} must be at least one cent")
    return CashPlan(
        mode, True, settle, amount if kind == core_holdings.SELL else -amount, window_end
    )


def probe(
    session: Session, account: Account, *, amount: Decimal, start: date, end: date
) -> Transaction | None:
    """The account's own copy of a trade's cash, if it has one: the shape a
    broker's "SOLD" row (or a person's entry of it) has — a live (pending or
    posted) chain head with exactly two legs, ``amount`` (same sign, to the
    cent) on ``account`` and a category on the other side, dated in [start,
    end]. A pending copy counts: the bank posts it later, and cash posted
    beside it would count the sale twice. Nothing else is a copy: not a
    transfer, a split, a corrected original (its replacement is the live
    row), a trade's cash (its other leg is a system account) or a
    reconciliation adjustment. The oldest one wins.

    Correlated NOT EXISTS for the chain-head test: the probe touches only the
    account's rows of that amount, never every correction ever made."""
    later = aliased(Transaction)
    leg = aliased(Posting)
    category_leg = aliased(Posting)
    category = aliased(Account)
    return session.execute(
        select(Transaction)
        .join(Posting, Posting.transaction_id == Transaction.id)
        .where(
            Posting.account_id == account.id,
            Posting.amount == amount,
            Transaction.status.in_([core.Status.PENDING.value, core.Status.POSTED.value]),
            Transaction.superseded_by.is_(None),
            Transaction.date >= start,
            Transaction.date <= end,
            ~exists().where(later.reverses_id == Transaction.id),
            select(func.count(leg.id)).where(leg.transaction_id == Transaction.id).scalar_subquery()
            == 2,
            exists().where(
                category_leg.transaction_id == Transaction.id,
                category_leg.account_id == category.id,
                category.is_virtual.is_(True),
                category.type.in_(ledger.CATEGORY_TYPES),
            ),
            ~exists().where(AccountReconciliation.adjustment_tx_id == Transaction.id),
        )
        .order_by(Transaction.date, Transaction.created_at, Transaction.id)
        .limit(1)
    ).scalar_one_or_none()


@dataclass(frozen=True)
class Settled:
    """What ``settle`` did: the trade's cash row (posted or linked; None
    when it moves no cash), and the row ``force_cash`` posted over."""

    transaction: Transaction | None
    overridden: Transaction | None


def _describe(kind: str, position: Position, quantity: Decimal, price: Decimal | None) -> str:
    what = "Sale" if kind == core_holdings.SELL else "Purchase"
    shares = format(quantity.normalize(), "f")
    at = f" at {format(price.normalize(), 'f')}" if price is not None else ""
    return f"{what} of {shares} {position.symbol}{at}"


def settle(
    session: Session,
    account: Account,
    position: Position,
    cash: CashPlan,
    *,
    kind: str,
    occurred_on: date,
    quantity: Decimal,
    price: Decimal | None,
    transaction_id: uuid.UUID | None,
    force_cash: bool = False,
    token: ApiToken | None,
) -> Settled:
    """Post the trade's cash, or link the row that already holds it. The
    caller holds the account lock and records the event afterwards (events
    are append-only, so its ``transaction_id`` is known up front).

    ``force_cash`` posts the cash even when the probe finds a row of that
    amount (a true coincidence: a different payment, not the trade's cash);
    the row it overrode comes back for the trade's audit row."""
    if not cash.moves:
        return Settled(None, None)
    assert cash.amount is not None and cash.window_end is not None
    securities = ledger.securities_account(session)
    if account.currency.strip() != securities.currency.strip():
        # v1 is single-currency per transaction (decision 4): the cash would
        # put a foreign-currency posting on the system account.
        raise LedgerError(
            f"{account.path} is a {account.currency.strip()} account, and a trade's cash books "
            f"against {securities.path}, which is {securities.currency.strip()} only "
            "(Purser v1 is single-currency): its trades move no cash, so record them with "
            "proceeds_to / funded_from 'none'"
        )
    if transaction_id is not None:
        linked = link(
            session,
            account,
            position,
            kind=kind,
            transaction_id=transaction_id,
            occurred_on=occurred_on,
            settle_on=cash.settle_on,
            amount=cash.amount,
            window_end=cash.window_end,
            token=token,
        )
        return Settled(linked, None)
    assert cash.settle_on is not None
    found = probe(session, account, amount=cash.amount, start=occurred_on, end=cash.window_end)
    if found is not None and not force_cash:
        raise CashAlreadyRecorded(found, cash.amount, live_match_on_chain(session, found))
    posted = ledger.create_flat_transaction(
        session,
        account_ref=str(account.id),
        category_ref=str(securities.id),
        amount=cash.amount,
        date=cash.settle_on,
        description=_describe(kind, position, quantity, price),
        requested_status=core.Status.POSTED,
        origin=core.Origin.SYSTEM,
        token=token,
        system_leg=True,
    )
    return Settled(posted, found)


@dataclass(frozen=True)
class UnclaimedCash:
    """Purser's own posted cash of a standing trade, which no broker row has
    taken over yet (``unclaimed_cash``)."""

    transaction: Transaction
    event: PositionEvent


def unclaimed_cash(
    session: Session, account: Account, *, amount: Decimal, on: date, taken_too: bool = False
) -> UnclaimedCash | None:
    """The trade whose cash a NEW feed row of ``amount`` dated ``on`` on
    ``account`` repeats (#408): a standing (unreversed) buy or sell whose cash
    Purser posted itself — a live posted row, origin ``system``, no content
    hash, not a correction (a linked row already is the broker's) — of exactly
    ``amount`` on the account (same sign, to the cent), with ``on`` in [the
    trade date, ``copy_window_end``]. The oldest trade wins, so the same
    facts always pick the same row. None when there is no such trade.

    ``taken_too`` also accepts a trade whose cash a broker row already took
    over (Purser's row voided into it): the importer asks that for a PENDING
    copy, which is never stored, so a re-import of the same payload skips it
    again whatever was linked meanwhile."""
    later = aliased(Transaction)
    undone = aliased(PositionEvent)
    rows = session.execute(
        select(Transaction, PositionEvent)
        .join(PositionEvent, PositionEvent.transaction_id == Transaction.id)
        .join(Posting, Posting.transaction_id == Transaction.id)
        .where(
            Posting.account_id == account.id,
            Posting.amount == amount,
            or_(
                and_(
                    Transaction.status == core.Status.POSTED.value,
                    Transaction.superseded_by.is_(None),
                ),
                and_(
                    literal(taken_too),
                    Transaction.status == core.Status.VOID.value,
                    Transaction.superseded_by.is_not(None),
                ),
            ),
            Transaction.reverses_id.is_(None),
            Transaction.content_hash.is_(None),
            Transaction.origin == core.Origin.SYSTEM.value,
            ~exists().where(later.reverses_id == Transaction.id),
            PositionEvent.reverses_id.is_(None),
            ~exists().where(undone.reverses_id == PositionEvent.id),
            PositionEvent.occurred_on <= on,
        )
        .order_by(PositionEvent.occurred_on, Transaction.created_at, Transaction.id)
    ).all()
    for tx, event in rows:
        if on <= copy_window_end(event.occurred_on, event.settle_on):
            return UnclaimedCash(tx, event)
    return None


def hand_over(
    session: Session, account: Account, found: UnclaimedCash, feed_row: Transaction
) -> Transaction:
    """The broker's own copy of a trade's cash arrived after Purser posted it
    (#408): ``feed_row``, just imported as an ordinary row, takes over as the
    trade's cash. It is LINKED the way ``transaction_id`` links a row (the
    reversal-pair correction to ``equity.securities``, ``position.link_cash``),
    and Purser's own row is voided with ``superseded_by`` pointing at the
    link's replacement — the one mutation a posted row allows (a transfer
    merge's). The bank's row keeps its date, description and identity; the
    trade's event still names Purser's row, and every reader of a trade's
    cash follows ``superseded_by`` to the live one (``live_cash``).
    Reversing the trade then undoes it like any linked row: the bank's row
    gets back the category it was imported with. The caller (the importer)
    holds the account lock. Returns the replacement now holding the cash."""
    position = session.get(Position, found.event.position_id)
    assert position is not None
    purser_row = ledger.lock_transaction(session, found.transaction.id)
    real = next(p for p in purser_row.postings if p.account_id == account.id)
    amount = core.quantize(Decimal(real.amount))
    holder = link(
        session,
        account,
        position,
        kind=found.event.kind,
        transaction_id=feed_row.id,
        occurred_on=found.event.occurred_on,
        settle_on=None,
        amount=amount,
        window_end=copy_window_end(found.event.occurred_on, found.event.settle_on),
        token=None,
    )
    purser_row.status = core.Status.VOID.value
    purser_row.superseded_by = holder.id
    session.flush()
    balances.mark_dirty(session, min(purser_row.date, holder.date))
    audit.record(
        session,
        actor_token_id=None,
        action="position.feed_cash",
        entity_type="position",
        entity_id=position.id,
        after={
            "event_id": str(found.event.id),
            "symbol": position.symbol,
            "kind": found.event.kind,
            "occurred_on": found.event.occurred_on.isoformat(),
            "amount": str(amount),
            "superseded_id": str(purser_row.id),
            "superseded_date": purser_row.date.isoformat(),
            "feed_transaction_id": str(feed_row.id),
            "cash_transaction_id": str(holder.id),
            "cash_date": holder.date.isoformat(),
            "source_fetch_id": str(feed_row.source_fetch_id) if feed_row.source_fetch_id else None,
        },
    )
    # The cash now lands on the bank's date: re-project, so the proceeds
    # between the trade and that date count as in transit (decision 104).
    from purser.holdings import projection

    projection.project_account(session, account, from_date=found.event.occurred_on)
    return holder


def live_cash(session: Session, tx: Transaction) -> Transaction:
    """A trade's cash row as it stands now: ``tx``, or the broker row that
    took it over (``superseded_by``, #408). Only a successor that is itself
    trade cash (a system leg) took it over; any other one (a merge no path
    may make) is the row moving without its trade, which the undo refuses."""
    seen = {tx.id}
    while tx.status == core.Status.VOID.value and tx.superseded_by is not None:
        nxt = session.get(Transaction, tx.superseded_by)
        if nxt is None or nxt.id in seen or not ledger.has_system_leg(nxt.postings):
            break
        seen.add(nxt.id)
        tx = nxt
    return tx


def _flat_parts(tx: Transaction) -> tuple[Posting, Posting]:
    real = [p for p in tx.postings if not p.account.is_virtual]
    virtual = [p for p in tx.postings if p.account.is_virtual]
    if len(tx.postings) != 2 or len(real) != 1 or len(virtual) != 1:
        raise LedgerError(
            f"transaction {tx.id} is not a row with one real leg and one category leg; only "
            "such a row can be a trade's cash"
        )
    return real[0], virtual[0]


def link(
    session: Session,
    account: Account,
    position: Position,
    *,
    kind: str,
    transaction_id: uuid.UUID,
    occurred_on: date,
    settle_on: date | None,
    amount: Decimal,
    window_end: date,
    token: ApiToken | None,
) -> Transaction:
    """Book an existing posted row on the account as the trade's cash: the
    reversal-pair correction (posted rows are immutable) whose replacement's
    category leg is ``equity.securities``. Returns the replacement; its date
    (the bank's) is when the trade settled.

    The row must hold exactly this trade's cash: ``amount`` (the account
    side, to the cent), dated in [trade, ``window_end``], settling no bill.
    Any other row is refused (422) rather than re-booked as the trade's."""
    try:
        # Locked before any check: a merge, match or conversion of this row
        # waits (or, on an automatic pass, skips it) until the link commits,
        # and this link sees what one committed first.
        ledger.lock_transaction(session, transaction_id)
        tx = ledger.get_transaction(session, transaction_id)
    except NotFoundError as exc:
        # The position exists; a 404 would say it does not.
        raise LedgerError(
            f"transaction {transaction_id} not found; transaction_id names the row on "
            f"{account.path} that holds this trade's cash"
        ) from exc
    if tx.status != core.Status.POSTED.value:
        raise LedgerError(
            f"transaction {tx.id} is {tx.status}; a trade links a posted row (a pending one "
            "once it posts)"
        )
    corrected = session.execute(
        select(Transaction.id).where(Transaction.reverses_id == tx.id)
    ).first()
    if tx.superseded_by is not None or corrected is not None:
        raise LedgerError(f"transaction {tx.id} was superseded or corrected; link its live row")
    if ledger.has_system_leg(tx.postings):
        raise LedgerError(f"transaction {tx.id} is already the cash of a securities trade")
    real, category = _flat_parts(tx)
    if real.account_id != account.id:
        raise LedgerError(f"transaction {tx.id} is not on {account.path}")
    # No currency check here: settle() refused a non-USD account before any
    # link, and every write path posts a leg in its account's currency.
    securities = ledger.securities_account(session)
    if tx.date < occurred_on:
        raise LedgerError(
            f"transaction {tx.id} is dated {tx.date.isoformat()}, before the trade on "
            f"{occurred_on.isoformat()}; a trade's cash settles on or after the trade"
        )
    if settle_on is not None and settle_on != tx.date:
        raise LedgerError(
            f"a linked trade settles on its row's date ({tx.date.isoformat()}); leave "
            "settle_on out or pass that date"
        )
    booked = core.quantize(Decimal(real.amount))
    if (kind == core_holdings.SELL) != (booked > 0):
        raise LedgerError(
            "a sale's cash comes into the account (a positive amount) and a purchase's leaves "
            f"it (a negative one); transaction {tx.id} is {booked}"
        )
    if booked != amount:
        what = (
            "the sale's net proceeds (quantity x price - fees)"
            if kind == core_holdings.SELL
            else "the purchase's cost (quantity x price + fees)"
        )
        raise LedgerError(
            f"transaction {tx.id} is for {abs(booked).quantize(CASH_QUANT)}, but {what} come to "
            f"{abs(amount).quantize(CASH_QUANT)}: link the row that holds exactly this trade's "
            "cash, or give the price and fees that produced it"
        )
    if tx.date > window_end:
        raise LedgerError(
            f"transaction {tx.id} is dated {tx.date.isoformat()}, more than five days after the "
            f"trade's settlement on the next business day (the window ends "
            f"{window_end.isoformat()}); a broker's cash for this trade arrives within it"
        )
    match = live_match_on_chain(session, tx)
    if match is not None:
        raise LedgerError(
            f"transaction {tx.id} settles expected {match.expected_id} (a bill or a deposit "
            "the matcher paired its bank line with), and sale cash never settles a bill: "
            f"{match_remedy(match)} first if it is this trade's cash"
        )
    return ledger.reverse_and_replace_with_specs(
        session,
        tx=tx,
        token=token,
        replacement_specs=core.expand_flat(
            account_id=account.id,
            category_id=securities.id,
            amount=booked,
            currency=real.currency.strip(),
        ),
        origin=core.Origin.SYSTEM,
        action="position.link_cash",
        audit_extra={
            "position_id": str(position.id),
            "symbol": position.symbol,
            "linked_from_category": category.account.path,
        },
    )


def _linked_from(session: Session, tx: Transaction) -> Account:
    """The category a linked row had before it was booked as trade cash: the
    replacement reverses a reversal, which reverses the original."""
    reversal = session.get(Transaction, tx.reverses_id) if tx.reverses_id else None
    original = (
        session.get(Transaction, reversal.reverses_id)
        if reversal is not None and reversal.reverses_id
        else None
    )
    if original is None:
        raise LedgerError(f"transaction {tx.id} has no original row to restore")
    _real, category = _flat_parts(original)
    return category.account


def _require_live(session: Session, tx: Transaction) -> None:
    """The cash row an undo corrects must still be what the trade left: a
    posted chain head. Anything else means the row moved without its trade,
    and correcting it now would undo money that is no longer there."""
    corrected = session.execute(
        select(Transaction.id).where(Transaction.reverses_id == tx.id)
    ).scalar_one_or_none()
    if tx.status != core.Status.POSTED.value:
        state = f"is {tx.status}"
        if tx.superseded_by is not None:
            state += f", merged into {tx.superseded_by}"
    elif tx.superseded_by is not None:
        state = f"was superseded by {tx.superseded_by}"
    elif corrected is not None:
        state = f"was already corrected by {corrected}"
    else:
        return
    raise LedgerError(
        f"the trade's cash row {tx.id} {state}: it moved without its trade, so reversing "
        "the trade cannot undo it. Restore that row first"
    )


def undo(
    session: Session,
    account: Account,
    position: Position,
    event: PositionEvent,
    *,
    token: ApiToken | None,
) -> Transaction | None:
    """Undo the cash of ``event`` as its trade is reversed, through the
    ledger's reversal pair (guardrail 3: a posted row is corrected, never
    offset). Purser's own row is corrected to nothing: its replacement keeps
    the two legs at zero, so the chain nets to zero and its live head is the
    record that the trade was undone. A linked row is corrected back to the
    category it had — a broker row that took the cash over at import (#408)
    included: it was linked, and ``live_cash`` finds it from Purser's voided
    row. Returns the replacement, the row that undid it."""
    if event.transaction_id is None:
        return None
    tx = live_cash(session, ledger.get_transaction(session, event.transaction_id))
    _require_live(session, tx)
    real, category = _flat_parts(tx)
    amount = core.quantize(Decimal(real.amount))
    currency = real.currency.strip()
    if tx.reverses_id is None:
        nothing = Decimal(0).quantize(core.QUANT)
        return ledger.reverse_and_replace_with_specs(
            session,
            tx=tx,
            token=token,
            replacement_specs=[
                core.PostingSpec(account_id=account.id, amount=nothing, currency=currency),
                core.PostingSpec(account_id=category.account_id, amount=nothing, currency=currency),
            ],
            description=f"{tx.description or 'Securities trade'} (reversed)",
            origin=core.Origin.SYSTEM,
            action="position.undo_cash",
            audit_extra={"position_id": str(position.id), "symbol": position.symbol},
        )
    restored = _linked_from(session, tx)
    return ledger.reverse_and_replace_with_specs(
        session,
        tx=tx,
        token=token,
        replacement_specs=core.expand_flat(
            account_id=account.id,
            category_id=restored.id,
            amount=amount,
            currency=real.currency.strip(),
        ),
        action="position.unlink_cash",
        audit_extra={"position_id": str(position.id), "symbol": position.symbol},
    )
