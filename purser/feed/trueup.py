"""Brokerage balance true-up after a sync (issue #591, DESIGN §7.4, decision 119).

A brokerage account whose holdings come from the feed is reported as its
postings (its cash) plus its holdings' market value (decision 50). The broker
reports the account's whole balance beside those holdings, so its cash is
``reported balance - holdings value``. When the feed sends a deposit without
the matching buy row (or a buy without its deposit), the postings keep cash
that already became shares, and the account reads above (or below) the
broker's balance by exactly that amount.

After each sync, for each open brokerage account with ``holdings_source =
'feed'`` and ``balance_true_up`` on, whose balance and holdings came in the
SAME fetch, Purser posts ONE system transaction for the difference between
that cash and the account's postings (pending included, dated on or before
the balance date), against ``equity.securities``: cash that moved into or out
of the account's holdings, neither spending nor income. ``origin =
'system'``, payee "Balance adjustment", dated the balance date; a
``brokerage_true_ups`` row names the fetch (UNIQUE per account and fetch), the
register labels it, it is audited (``account.balance_true_up``) and evented
(``account.balance_adjusted``).

It never fires on data it cannot trust (each skip is recorded with its
reason on the sync's audit row): no balance or no balance date; a balance
date after the holdings' date or older than ``feed_stale_days`` before it;
holdings missing from the payload, partial or unpriced, or a stored set for
the fetch's date that does not say what the fetch reports (decision 88 keeps
the day's first set; a later fetch adjusts while its holdings still match
it, decision 130); a closed account, hand-tracked holdings or
the switch off; another currency than ``equity.securities``'; or a fetch
that already adjusted the account. Like the drift check, a failure never
fails the sync: each account runs in its own savepoint.
"""

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from purser import audit, events, ledger, locks, reconciliation
from purser.core import ledger as core
from purser.feed import drift, envelope
from purser.holdings import overlay
from purser.models import Account, BrokerageTrueUp, Connection, RawFetch

log = structlog.get_logger()

PAYEE = "Balance adjustment"
DESCRIPTION = "Invested cash: the broker's balance less its holdings"
# The smallest difference worth an entry: a cent. Sub-cent gaps are rounding
# between a fund's unit price and the broker's balance, not missing cash.
THRESHOLD = Decimal("0.01")

ADJUSTED = "adjusted"
IN_STEP = "in_step"
# The set stored for the fetch's date is not what this fetch reports: a
# symbol, a quantity or a value differs (an earlier fetch that day stored
# it), or it holds a hand-tracked row.
HOLDINGS_MISMATCH = "holdings_mismatch"
# The scales of holdings_snapshots.quantity (NUMERIC(20,6)) and
# market_value (NUMERIC(20,4)).
QUANTITY_PLACES = Decimal("0.000001")
VALUE_PLACES = Decimal("0.0001")


@dataclass(frozen=True)
class Outcome:
    """What the true-up did for one account: ``status`` is ``adjusted``,
    ``in_step`` (nothing to do) or the reason it was skipped."""

    account: Account
    status: str
    amount: Decimal | None = None
    transaction_id: uuid.UUID | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"account_id": str(self.account.id), "status": self.status}
        if self.amount is not None:
            out["amount"] = str(self.amount)
        if self.transaction_id is not None:
            out["transaction_id"] = str(self.transaction_id)
        return out


def run_after_sync(
    session: Session,
    *,
    connection: Connection,
    payload: dict[str, Any],
    raw_fetch: RawFetch,
    stale_days: int,
) -> list[dict[str, Any]]:
    """True up every mapped feed-holdings brokerage account in ``payload``.
    Returns one entry per brokerage account considered, for the sync's
    audit row. Never raises."""
    results: list[dict[str, Any]] = []
    try:
        ambiguous = envelope.duplicate_account_ids(payload)
        for sf_account in payload.get("accounts", []):
            external_id = sf_account.get("id")
            if external_id is None or external_id in ambiguous:
                continue
            account = session.execute(
                select(Account).where(
                    Account.connection_id == connection.id,
                    Account.external_id == external_id,
                )
            ).scalar_one_or_none()
            if account is None or account.type != "brokerage":
                continue
            try:
                with session.begin_nested():
                    outcome = true_up(
                        session,
                        account=account,
                        sf_account=sf_account,
                        raw_fetch=raw_fetch,
                        stale_days=stale_days,
                    )
            except Exception:
                log.exception("balance true-up failed; sync unaffected", account_id=str(account.id))
                outcome = Outcome(account, "failed")
            results.append(outcome.as_dict())
    except Exception:
        log.exception("balance true-up pass failed; sync unaffected")
    return results


def _same(stored: Decimal | None, reported: Decimal | None, places: Decimal) -> bool:
    """A stored figure equals the fetch's at the column's scale: Postgres
    rounded the reported one (half away from zero) when it was stored."""
    if stored is None or reported is None:
        return stored is None and reported is None
    return Decimal(stored).quantize(places, ROUND_HALF_UP) == reported.quantize(
        places, ROUND_HALF_UP
    )


def _held_value(
    session: Session, account: Account, sf_account: dict[str, Any], raw_fetch: RawFetch
) -> Decimal | str:
    """The holdings value this fetch reported for ``account``, read from the
    snapshot set stored for the fetch's date — or why it cannot be trusted.

    The importer keeps one set per date (decision 88): a later fetch the same
    day adds only the symbols the day's set lacks, so the set may hold an
    earlier fetch's rows (a deploy, a newly mapped account or the switch
    turned on mid-day). It is trusted by what it says, not where it came
    from (decision 130): every row a feed row, the same symbols as this
    fetch, each with this fetch's quantity and value. Then valuing it is
    valuing this fetch's holdings, as every reader values them."""
    from purser.feed.importer import FeedHolding, feed_holding
    from purser.networth import holdings_set_on

    reported = sf_account.get("holdings")
    if not isinstance(reported, list) or not reported:
        return "no_holdings"
    fetched: dict[str, FeedHolding] = {}
    for holding in reported:
        parsed = feed_holding(holding)
        if parsed.symbol in fetched:
            return "partial_holdings"  # a symbol the payload repeats is stored once
        fetched[parsed.symbol] = parsed
    as_of = raw_fetch.fetched_at.date()  # the importer's snapshot date
    rows = holdings_set_on(session, account.id, as_of)
    if (
        len(rows) != len(fetched)
        or any(r.as_of != as_of or r.source != "feed" for r in rows)
        or {r.symbol for r in rows} != fetched.keys()
        or not all(
            _same(r.quantity, fetched[r.symbol].quantity, QUANTITY_PLACES)
            and _same(r.market_value, fetched[r.symbol].market_value, VALUE_PLACES)
            for r in rows
        )
    ):
        return HOLDINGS_MISMATCH
    total = Decimal(0)
    for held in overlay.as_held(session, rows):
        if held.market_value is None:
            if held.quantity is not None and Decimal(held.quantity) == 0:
                continue  # a position the feed lists at zero shares holds nothing
            return "unpriced_holdings"
        total += Decimal(held.market_value)
    return core.quantize(total)


def true_up(
    session: Session,
    *,
    account: Account,
    sf_account: dict[str, Any],
    raw_fetch: RawFetch,
    stale_days: int,
) -> Outcome:
    """True up one account from one fetch (the caller's savepoint)."""
    if account.closed_at is not None:
        return Outcome(account, "closed")
    if account.holdings_source != "feed":
        return Outcome(account, "manual_holdings")
    if not account.balance_true_up:
        return Outcome(account, "off")
    locked = locks.lock_account(session, account.id)
    if locked is None:
        return Outcome(account, "gone")
    account = locked
    securities = ledger.securities_account(session)
    if account.currency.strip() != securities.currency.strip():
        return Outcome(account, "currency")
    already = session.execute(
        select(BrokerageTrueUp.transaction_id).where(
            BrokerageTrueUp.account_id == account.id,
            BrokerageTrueUp.raw_fetch_id == raw_fetch.id,
        )
    ).first()
    if already is not None:
        return Outcome(account, "already_adjusted")

    reported = drift._parse_reported(sf_account)
    if reported is None:
        return Outcome(account, "no_balance")
    balance_date = drift.parse_balance_date(sf_account.get("balance-date"))
    if balance_date is None:
        return Outcome(account, "no_balance_date")
    holdings_date: date = raw_fetch.fetched_at.date()
    if balance_date > holdings_date:
        return Outcome(account, "balance_date_mismatch")
    if (holdings_date - balance_date).days > stale_days:
        return Outcome(account, "stale_balance")
    held = _held_value(session, account, sf_account, raw_fetch)
    if isinstance(held, str):
        return Outcome(account, held)

    cash = core.quantize(reported - held)
    postings = reconciliation.ledger_balance(
        session, account.id, balance_date, reconciliation.BASIS_PENDING_INCLUSIVE
    )
    difference = core.quantize(cash - postings)
    if abs(difference) < THRESHOLD:
        return Outcome(account, IN_STEP)

    tx = ledger.create_flat_transaction(
        session,
        account_ref=str(account.id),
        category_ref=str(securities.id),
        amount=difference,
        date=balance_date,
        payee_name=PAYEE,
        description=DESCRIPTION,
        requested_status=core.Status.POSTED,
        origin=core.Origin.SYSTEM,
        token=None,
        system_leg=True,
    )
    session.add(
        BrokerageTrueUp(
            transaction_id=tx.id,
            account_id=account.id,
            raw_fetch_id=raw_fetch.id,
            balance_date=balance_date,
            reported=core.quantize(reported),
            holdings_value=held,
        )
    )
    session.flush()
    facts = {
        "account_id": str(account.id),
        "as_of": balance_date.isoformat(),
        "amount": str(difference),
        "transaction_id": str(tx.id),
        "reported": str(core.quantize(reported)),
        "holdings_value": str(held),
        "postings_before": str(postings),
        "raw_fetch_id": str(raw_fetch.id),
    }
    audit.record(
        session,
        actor_token_id=None,
        action="account.balance_true_up",
        entity_type="account",
        entity_id=account.id,
        after=facts,
    )
    events.emit(
        session,
        type_="account.balance_adjusted",
        entity_type="account",
        entity_id=account.id,
        payload={"path": account.path, "currency": account.currency.strip(), **facts},
    )
    return Outcome(account, ADJUSTED, amount=difference, transaction_id=tx.id)


def true_up_ids(session: Session, tx_ids: list[uuid.UUID]) -> set[uuid.UUID]:
    """Which of ``tx_ids`` are balance true-ups (one query, for the
    register's label)."""
    if not tx_ids:
        return set()
    return set(
        session.execute(
            select(BrokerageTrueUp.transaction_id).where(BrokerageTrueUp.transaction_id.in_(tx_ids))
        ).scalars()
    )
