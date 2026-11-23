"""A securities trade's cash (issue #390 phase 1b, decision 103): sale proceeds
and funded buys post against equity.securities, the double-count guard, the
link to a row the account already shows, reversals that undo the cash, and the
opt-in drift check of a cash-sweep feed balance (the cash-sweep shape).

Synthetic fixtures only: ACME / TEST-style symbols, made-up prices and
balances, @example.com users.
"""

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select

from purser import db, ledger
from purser.core import ledger as core
from purser.holdings import cash as trade_cash
from purser.holdings import service
from purser.ledger import LedgerError
from purser.models import Account, AuditLog, Posting, Transaction
from tests.conftest import now, today


def _weekday_before(days: int) -> date:
    """A Monday-to-Thursday date about ``days`` ago, so T+1 is the next day."""
    day = today() - timedelta(days=days)
    while day.weekday() >= 4:
        day -= timedelta(days=1)
    return day


def _symbol() -> str:
    return f"C{uuid.uuid4().hex[:7].upper()}"


def _broker(session, path: str | None = None) -> Account:
    account = ledger.create_account(
        session,
        name="Plan",
        type_="brokerage",
        path=path or f"assets.plan_{uuid.uuid4().hex[:8]}",
        actor_token_id=None,
    )
    ledger.update_account(
        session, account_id=account.id, holdings_source="manual", actor_token_id=None
    )
    return account


def _hold(session, account: Account, symbol: str, quantity: str, on: date):
    return service.record_position(
        session,
        account_ref=str(account.id),
        symbol=symbol,
        quantity=Decimal(quantity),
        as_of=on,
        token=None,
    ).position


def _postings(session, account: Account) -> Decimal:
    return ledger.account_balances(session, [account.id]).get(account.id, Decimal(0))


def _feed_row(session, account: Account, amount: str, on: date, description: str):
    """A row the bank sent: origin import, uncategorized."""
    from purser.feed import importer

    category = importer._uncategorized(session, income=Decimal(amount) > 0)
    return ledger.create_flat_transaction(
        session,
        account_ref=str(account.id),
        category_ref=str(category.id),
        amount=Decimal(amount),
        date=on,
        description=description,
        requested_status=core.Status.POSTED,
        origin=core.Origin.IMPORT,
        token=None,
        content_hash=f"test-{uuid.uuid4().hex}",
    )


class TestSaleProceeds:
    def test_a_sale_posts_its_net_proceeds_on_the_trade_date(self, db_session) -> None:
        """The shares leave the holdings on the trade date, so the cash
        arrives that day too (a later settle_on is opt-in)."""
        account = _broker(db_session)
        symbol = _symbol()
        position = _hold(db_session, account, symbol, "100", today() - timedelta(days=30))
        trade = _weekday_before(10)
        event = service.record_event(
            db_session,
            position_id=position.id,
            kind="sell",
            occurred_on=trade,
            quantity=Decimal("20"),
            price=Decimal("50"),
            fees=Decimal("5"),
            token=None,
        )
        assert event.proceeds_to == "cash"
        assert event.settle_on == trade
        cash = ledger.get_transaction(db_session, event.transaction_id)
        assert cash.status == "posted" and cash.origin == "system" and cash.date == event.settle_on
        legs = {p.account.path: Decimal(p.amount) for p in cash.postings}
        assert legs == {
            account.path: Decimal("995.0000"),
            "equity.securities": Decimal("-995.0000"),
        }
        assert cash.description == f"Sale of 20 {symbol} at 50"
        assert _postings(db_session, account) == Decimal("995.0000")

    def test_settle_on_posts_the_cash_later_and_widens_the_probe(self, db_session) -> None:
        """settle_on posts the cash on a later date, and the double-count
        probe looks to five days after it; by default it looks to five days
        after T+1, whatever the posting date."""
        account = _broker(db_session)
        position = _hold(db_session, account, _symbol(), "100", today() - timedelta(days=40))
        trade = _weekday_before(20)
        later = trade + timedelta(days=3)
        event = service.record_event(
            db_session,
            position_id=position.id,
            kind="sell",
            occurred_on=trade,
            quantity=Decimal("2"),
            price=Decimal("50"),
            settle_on=later,
            token=None,
        )
        assert event.settle_on == later
        assert ledger.get_transaction(db_session, event.transaction_id).date == later
        # A row 8 days after a 3-day settlement is the feed's copy of the next
        # sale's cash; the default window (T+1 + 5) does not reach it.
        _feed_row(db_session, account, "150.00", trade + timedelta(days=8), "SOLD")
        with pytest.raises(trade_cash.CashAlreadyRecorded):
            service.record_event(
                db_session,
                position_id=position.id,
                kind="sell",
                occurred_on=trade,
                quantity=Decimal("3"),
                price=Decimal("50"),
                settle_on=later,
                token=None,
            )
        default = service.record_event(
            db_session,
            position_id=position.id,
            kind="sell",
            occurred_on=trade,
            quantity=Decimal("3"),
            price=Decimal("50"),
            token=None,
        )
        assert default.settle_on == trade

    def test_fractional_shares_settle_to_the_cent(self, db_session) -> None:
        """quantity x price is often sub-cent with fractional shares; the cash
        is what a broker pays, the net rounded once, half up, to the cent."""
        account = _broker(db_session)
        position = _hold(db_session, account, _symbol(), "10", today() - timedelta(days=30))
        service.record_event(
            db_session,
            position_id=position.id,
            kind="sell",
            occurred_on=_weekday_before(10),
            quantity=Decimal("0.5"),
            price=Decimal("10.01"),
            token=None,
        )
        assert _postings(db_session, account) == Decimal("5.0100")  # 5.005, half up
        service.record_event(
            db_session,
            position_id=position.id,
            kind="buy",
            occurred_on=_weekday_before(10),
            quantity=Decimal("0.333333"),
            price=Decimal("3"),
            fees=Decimal("0.0001"),
            funded_from="cash",
            token=None,
        )
        # 0.999999 + 0.0001 = 1.000099 -> 1.00 off the cash.
        assert _postings(db_session, account) == Decimal("4.0100")

    def test_cash_beyond_what_the_ledger_holds_is_refused(self, db_session) -> None:
        account = _broker(db_session)
        position = _hold(db_session, account, _symbol(), "1", today() - timedelta(days=30))
        with pytest.raises(LedgerError, match="out of range"):
            service.record_event(
                db_session,
                position_id=position.id,
                kind="buy",
                occurred_on=_weekday_before(10),
                quantity=Decimal("99999999999999"),
                price=Decimal("1000"),
                funded_from="cash",
                token=None,
            )

    def test_proceeds_none_moves_no_cash(self, db_session) -> None:
        """Shares withheld for tax: the count drops, the cash does not move."""
        account = _broker(db_session)
        position = _hold(db_session, account, _symbol(), "10", today() - timedelta(days=5))
        event = service.record_event(
            db_session,
            position_id=position.id,
            kind="sell",
            occurred_on=today(),
            quantity=Decimal("3"),
            proceeds_to="none",
            token=None,
        )
        assert event.transaction_id is None and event.settle_on is None
        assert _postings(db_session, account) == 0

    def test_a_funded_buy_takes_its_cost_and_a_plain_buy_takes_none(self, db_session) -> None:
        account = _broker(db_session)
        position = _hold(db_session, account, _symbol(), "1", today() - timedelta(days=9))
        trade = _weekday_before(4)
        funded = service.record_event(
            db_session,
            position_id=position.id,
            kind="buy",
            occurred_on=trade,
            quantity=Decimal("10"),
            price=Decimal("20"),
            fees=Decimal("1"),
            funded_from="cash",
            token=None,
        )
        assert funded.funded_from == "cash"
        assert _postings(db_session, account) == Decimal("-201.0000")
        plain = service.record_event(
            db_session,
            position_id=position.id,
            kind="buy",
            occurred_on=trade,
            quantity=Decimal("2"),
            price=Decimal("20"),
            token=None,
        )
        assert plain.funded_from == "none" and plain.transaction_id is None
        assert _postings(db_session, account) == Decimal("-201.0000")

    @pytest.mark.parametrize(
        ("kind", "fields", "message"),
        [
            ("sell", {}, "needs a price"),
            ("sell", {"funded_from": "cash", "price": Decimal("1")}, "funded_from applies to buys"),
            ("buy", {"proceeds_to": "cash"}, "proceeds_to applies to sells"),
            ("sell", {"proceeds_to": "none", "settle_on": 0}, "settle_on and transaction_id"),
            (
                "sell",
                {"price": Decimal("1"), "settle_on": -30},
                "before the trade date",
            ),
            ("sell", {"price": Decimal("1"), "fees": Decimal("9")}, "at least one cent"),
            ("buy", {"funded_from": "cash", "price": Decimal("0.000001")}, "at least one cent"),
        ],
    )
    def test_cash_fields_are_validated(
        self, db_session, kind: str, fields: dict, message: str
    ) -> None:
        if "settle_on" in fields:  # days from the test's today, resolved in the test (#533)
            fields = {**fields, "settle_on": today() + timedelta(days=fields["settle_on"])}
        account = _broker(db_session)
        position = _hold(db_session, account, _symbol(), "10", today() - timedelta(days=20))
        with pytest.raises(LedgerError, match=message):
            service.record_event(
                db_session,
                position_id=position.id,
                kind=kind,
                occurred_on=today() - timedelta(days=2),
                quantity=Decimal("1"),
                token=None,
                **fields,
            )

    def test_a_non_usd_account_trades_without_cash(self, db_session) -> None:
        """equity.securities is USD and v1 is single-currency: a trade's cash
        on a CAD account is refused with a reason, posted or linked, while a
        trade that moves no cash still records its shares."""
        account = ledger.create_account(
            db_session,
            name="Plan CAD",
            type_="brokerage",
            path=f"assets.plan_cad_{uuid.uuid4().hex[:8]}",
            currency="CAD",
            actor_token_id=None,
        )
        ledger.update_account(
            db_session, account_id=account.id, holdings_source="manual", actor_token_id=None
        )
        position = _hold(db_session, account, _symbol(), "10", today() - timedelta(days=30))
        trade = _weekday_before(10)
        income = ledger.create_account(
            db_session,
            name="Uncategorized CAD",
            type_="category_income",
            path=f"income.cad_{uuid.uuid4().hex[:8]}",
            currency="CAD",
            actor_token_id=None,
        )
        row = ledger.create_flat_transaction(
            db_session,
            account_ref=str(account.id),
            category_ref=str(income.id),
            amount=Decimal("100.00"),
            date=trade,
            description="SOLD",
            requested_status=core.Status.POSTED,
            origin=core.Origin.IMPORT,
            token=None,
            content_hash=f"test-{uuid.uuid4().hex}",
        )
        sell = {
            "position_id": position.id,
            "kind": "sell",
            "occurred_on": trade,
            "quantity": Decimal("1"),
            "price": Decimal("100"),
            "token": None,
        }
        for fields in ({}, {"transaction_id": row.id}):
            with pytest.raises(LedgerError, match=r"equity\.securities, which is USD"):
                service.record_event(db_session, **sell, **fields)
        with pytest.raises(LedgerError, match=r"equity\.securities, which is USD"):
            service.record_event(db_session, **{**sell, "kind": "buy"}, funded_from="cash")
        shares_only = service.record_event(db_session, **sell, proceeds_to="none")
        assert shares_only.transaction_id is None
        db_session.refresh(row)
        assert {p.account_id for p in row.postings} == {account.id, income.id}  # untouched

    def test_set_quantity_carries_no_cash_fields(self, db_session) -> None:
        account = _broker(db_session)
        position = _hold(db_session, account, _symbol(), "1", today())
        with pytest.raises(LedgerError, match="set_quantity moves no cash"):
            service.record_event(
                db_session,
                position_id=position.id,
                kind="set_quantity",
                occurred_on=today(),
                quantity=Decimal("2"),
                proceeds_to="cash",
                token=None,
            )


def _sync(session, account: Account, rows: list[dict], window_start: date | None = None) -> dict:
    """One SimpleFIN fetch of ``rows`` for ``account``, imported by the real
    importer (archived first, as a sync does)."""
    from purser.feed import importer
    from purser.feed import service as feed_service
    from purser.models import Connection
    from tests.test_feed import payload_of

    if account.connection_id is None:
        connection = Connection()
        session.add(connection)
        session.flush()
        account.connection_id = connection.id
        account.external_id = f"broker-{uuid.uuid4().hex[:8]}"
        session.flush()
    raw = feed_service._archive_payload(
        session, account.connection_id, payload_of(rows, account_id=account.external_id)
    )
    return importer.import_fetch(session, raw, window_start=window_start)


def _epoch(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, 12, tzinfo=UTC).timestamp())


class TestDoubleCountGuardAndLink:
    def test_a_pending_broker_copy_blocks_the_sale_until_it_posts(self, db_session) -> None:
        """The broker lists the sale's cash as pending first. Posting Purser's
        own cash beside it counted the sale twice once the bank posted it; the
        probe takes the pending copy and says to wait for it."""
        account = _broker(db_session)
        position = _hold(db_session, account, _symbol(), "50", today() - timedelta(days=40))
        trade = _weekday_before(12)
        pending = {
            "id": "a",
            "posted": _epoch(trade + timedelta(days=1)),
            "amount": "100.00",
            "description": "SOLD 1 ACME",
            "pending": True,
        }
        assert _sync(db_session, account, [pending])["imported"] == 1
        sell = {
            "position_id": position.id,
            "kind": "sell",
            "occurred_on": trade,
            "quantity": Decimal("1"),
            "price": Decimal("100"),
            "token": None,
        }
        with pytest.raises(trade_cash.CashAlreadyRecorded) as caught:
            service.record_event(db_session, **sell)
        assert "the broker's copy is pending" in str(caught.value)
        assert "after it posts" in str(caught.value) and "force_cash" in str(caught.value)
        # The bank posts it; the sale then links the posted row: one sale.
        stats = _sync(
            db_session, account, [{**pending, "pending": False}], window_start=trade - timedelta(7)
        )
        assert stats["imported"] == 1 and stats["voided_pendings"] == 1
        with pytest.raises(trade_cash.CashAlreadyRecorded) as again:
            service.record_event(db_session, **sell)
        posted = ledger.get_transaction(db_session, again.value.transaction_id)
        assert posted.status == "posted" and "pending" not in str(again.value)
        service.record_event(db_session, **sell, transaction_id=posted.id)
        assert _postings(db_session, account) == Decimal("100.0000")

    def test_the_probe_compares_cents(self, db_session) -> None:
        """A fractional-share sale's sub-cent product still finds the feed's
        cent row: 3.5 x 10.1234 = 35.4319 settles as 35.43."""
        account = _broker(db_session)
        position = _hold(db_session, account, _symbol(), "10", today() - timedelta(days=30))
        trade = _weekday_before(10)
        feed = _feed_row(db_session, account, "35.43", trade + timedelta(days=1), "SOLD")
        with pytest.raises(trade_cash.CashAlreadyRecorded) as caught:
            service.record_event(
                db_session,
                position_id=position.id,
                kind="sell",
                occurred_on=trade,
                quantity=Decimal("3.5"),
                price=Decimal("10.1234"),
                token=None,
            )
        assert caught.value.transaction_id == feed.id
        assert caught.value.amount == Decimal("35.4300")

    def test_the_feeds_copy_of_the_cash_blocks_a_second_posting(self, db_session) -> None:
        account = _broker(db_session)
        position = _hold(db_session, account, _symbol(), "50", today() - timedelta(days=40))
        trade = _weekday_before(12)
        feed = _feed_row(db_session, account, "995.00", trade + timedelta(days=2), "SOLD SHARES")
        with pytest.raises(trade_cash.CashAlreadyRecorded) as raised:
            service.record_event(
                db_session,
                position_id=position.id,
                kind="sell",
                occurred_on=trade,
                quantity=Decimal("20"),
                price=Decimal("50"),
                fees=Decimal("5"),
                token=None,
            )
        assert raised.value.transaction_id == feed.id
        assert str(feed.id) in str(raised.value)

    @pytest.mark.parametrize("shape", ["transfer", "split", "corrected"])
    def test_the_probe_takes_only_a_one_sided_row(self, db_session, shape: str) -> None:
        """The feed's copy of a sale's cash is a row with one real leg and
        one category leg (pending or posted). A transfer into the account, a
        split, or a corrected original is something else: the
        probe passes it by, so the sale posts its own cash instead of a 409
        whose only way out was recording no cash at all."""
        from purser.feed import importer

        account = _broker(db_session)
        position = _hold(db_session, account, _symbol(), "50", today() - timedelta(days=40))
        trade = _weekday_before(12)
        on = trade + timedelta(days=1)
        income = importer._uncategorized(db_session, income=True)
        if shape == "transfer":
            checking = ledger.create_account(
                db_session,
                name="Chk",
                type_="checking",
                path=f"assets.chk_{uuid.uuid4().hex[:8]}",
                actor_token_id=None,
            )
            ledger.create_transfer(  # a transfer a person entered
                db_session,
                from_account_ref=str(checking.id),
                to_account_ref=str(account.id),
                amount=Decimal("100.00"),
                date=on,
                requested_status=core.Status.POSTED,
                token=_commit_token(db_session),
            )
        elif shape == "split":
            other = ledger.ensure_category_path(db_session, "income.dividends", actor_token_id=None)
            ledger.create_split_transaction(
                db_session,
                account_ref=str(account.id),
                amount=Decimal("100.00"),
                lines=[
                    core.SplitLine(category_id=income.id, amount=Decimal("60.00")),
                    core.SplitLine(category_id=other.id, amount=Decimal("40.00")),
                ],
                date=on,
                requested_status=core.Status.POSTED,
                origin=core.Origin.IMPORT,
                token=None,
            )
        else:
            row = _feed_row(db_session, account, "100.00", on, "DEPOSIT")
            ledger.ensure_category_path(db_session, "income.dividends", actor_token_id=None)
            ledger.update_transaction(
                db_session,
                tx_id=row.id,
                category_ref="income.dividends",
                token=_commit_token(db_session),
            )
            # The replacement is a live head of that amount: the probe finds
            # it, never the corrected original behind it.
        sell = {
            "position_id": position.id,
            "kind": "sell",
            "occurred_on": trade,
            "quantity": Decimal("1"),
            "price": Decimal("100"),
            "token": None,
        }
        if shape == "corrected":
            with pytest.raises(trade_cash.CashAlreadyRecorded) as caught:
                service.record_event(db_session, **sell)
            found = ledger.get_transaction(db_session, caught.value.transaction_id)
            assert found.reverses_id is not None  # the replacement, the chain's head
            return
        event = service.record_event(db_session, **sell)
        cash = ledger.get_transaction(db_session, event.transaction_id)
        assert ledger.has_system_leg(cash.postings)

    def test_a_link_refuses_a_bank_line_matched_anywhere_in_its_chain(self, db_session) -> None:
        """The nightly matcher paired a bill with an IMPORT row that a rule
        then recategorized; the sale then links the rule's replacement. The
        bill's match sits on the chain, so the link is refused, naming the
        match and how to undo a confirmed one."""
        from purser import matching, schedules
        from purser.feed import importer
        from purser.models import Match

        account = _broker(db_session)
        position = _hold(db_session, account, _symbol(), "50", today() - timedelta(days=40))
        uncategorized = importer._uncategorized(db_session, income=True)
        schedule = schedules.create_schedule(
            db_session,
            name="Plan Sweep",
            account_ref=str(account.id),
            rrule="FREQ=MONTHLY",
            amount_type="fixed",
            amount=Decimal("100.00"),
            payee_name="Plan Sweep",
            category_path=uncategorized.path,
            dtstart=today(),
            token=None,
        )
        original = ledger.create_flat_transaction(
            db_session,
            account_ref=str(account.id),
            category_ref=str(uncategorized.id),
            amount=Decimal("100.00"),
            date=today(),
            payee_name="Plan Sweep",
            description="PLAN SWEEP SOLD",
            requested_status=core.Status.POSTED,
            origin=core.Origin.IMPORT,
            token=None,
            content_hash=f"test-{uuid.uuid4().hex}",
        )
        # The bill is matched first; a rule then recategorizes the row, so the
        # match stays on the import original behind the rule's replacement.
        matching.run_matching(db_session)
        match = db_session.execute(
            select(Match).where(Match.posted_id == original.id, Match.state == "confirmed")
        ).scalar_one()
        category = ledger.ensure_category_path(db_session, "income.sweep", actor_token_id=None)
        head = ledger.recategorize_posted(
            db_session, tx=original, category_id=category.id, token=None, provenance={}
        )
        assert match.expected_id is not None and schedule.id is not None
        sell = {
            "position_id": position.id,
            "kind": "sell",
            "occurred_on": today(),
            "quantity": Decimal("1"),
            "price": Decimal("100"),
            "token": None,
        }
        with pytest.raises(trade_cash.CashAlreadyRecorded) as caught:
            service.record_event(db_session, **sell)
        assert str(match.id) in str(caught.value) and "unmatch" in str(caught.value)
        with pytest.raises(LedgerError, match="unmatch") as refused:
            service.record_event(db_session, **sell, transaction_id=head.id)
        assert str(match.id) in str(refused.value)
        db_session.refresh(match)
        assert match.state == "confirmed" and match.posted_id == original.id

    def test_a_proposed_match_is_rejected_not_unmatched(self, db_session) -> None:
        """A proposal the real matcher made: the 409 and the link refusal say
        to reject it (unmatch refuses a proposal); after the reject the link
        goes through."""
        from purser import matching
        from purser.feed import importer
        from purser.models import Match

        account = _broker(db_session)
        position = _hold(db_session, account, _symbol(), "50", today() - timedelta(days=40))
        trade = _weekday_before(12)
        row = _feed_row(db_session, account, "100.00", trade, "SOLD")
        ledger.create_flat_transaction(
            db_session,
            account_ref=str(account.id),
            category_ref=str(importer._uncategorized(db_session, income=True).id),
            amount=Decimal("100.00"),
            date=trade,
            description="Monthly contribution",
            requested_status=core.Status.EXPECTED,
            token=None,
        )
        matching.run_matching(db_session)
        proposal = db_session.execute(select(Match).where(Match.posted_id == row.id)).scalar_one()
        assert proposal.state == "proposed"
        sell = {
            "position_id": position.id,
            "kind": "sell",
            "occurred_on": trade,
            "quantity": Decimal("1"),
            "price": Decimal("100"),
            "token": None,
        }
        with pytest.raises(trade_cash.CashAlreadyRecorded) as caught:
            service.record_event(db_session, **sell)
        assert str(proposal.id) in str(caught.value) and "reject" in str(caught.value)
        with pytest.raises(LedgerError, match="reject") as refused:
            service.record_event(db_session, **sell, transaction_id=row.id)
        assert "unmatch" not in str(refused.value)
        matching.reject_match(db_session, match_id=proposal.id, token=_commit_token(db_session))
        event = service.record_event(db_session, **sell, transaction_id=row.id)
        assert event.transaction_id is not None

    def test_force_cash_posts_over_a_coincidence_and_is_audited(self, db_session) -> None:
        """A row of exactly the net proceeds that is NOT the sale's cash (a
        coincidence): force_cash posts the cash anyway, and the trade's audit
        row names the row it overrode."""
        account = _broker(db_session)
        position = _hold(db_session, account, _symbol(), "50", today() - timedelta(days=40))
        trade = _weekday_before(12)
        other = _feed_row(db_session, account, "100.00", trade + timedelta(days=1), "REFUND")
        with pytest.raises(trade_cash.CashAlreadyRecorded) as caught:
            service.record_event(
                db_session,
                position_id=position.id,
                kind="sell",
                occurred_on=trade,
                quantity=Decimal("1"),
                price=Decimal("100"),
                token=None,
            )
        message = str(caught.value)
        assert str(other.id) in message
        assert f"transaction_id={other.id}" in message and "force_cash=true" in message
        event = service.record_event(
            db_session,
            position_id=position.id,
            kind="sell",
            occurred_on=trade,
            quantity=Decimal("1"),
            price=Decimal("100"),
            force_cash=True,
            token=None,
        )
        assert _postings(db_session, account) == Decimal("200.0000")  # both rows count
        audit = db_session.execute(
            select(AuditLog).where(
                AuditLog.action == "position.record", AuditLog.entity_id == position.id
            )
        ).scalars()
        (row,) = [a for a in audit if a.before_after["after"].get("event_id") == str(event.id)]
        assert row.before_after["after"]["force_cash"] is True
        assert row.before_after["after"]["cash_probe_overridden"] == str(other.id)

    @pytest.mark.parametrize(
        ("fields", "message"),
        [
            ({"proceeds_to": "none"}, "force_cash posts"),
            ({"transaction_id": "link"}, "force_cash posts"),
        ],
    )
    def test_force_cash_applies_only_to_cash_purser_posts(
        self, db_session, fields: dict, message: str
    ) -> None:
        account = _broker(db_session)
        position = _hold(db_session, account, _symbol(), "50", today() - timedelta(days=40))
        trade = _weekday_before(12)
        if fields.get("transaction_id") == "link":
            fields = {"transaction_id": _feed_row(db_session, account, "100.00", trade, "SOLD").id}
        with pytest.raises(LedgerError, match=message):
            service.record_event(
                db_session,
                position_id=position.id,
                kind="sell",
                occurred_on=trade,
                quantity=Decimal("1"),
                price=Decimal("100"),
                force_cash=True,
                token=None,
                **fields,
            )

    def test_a_row_past_the_window_does_not_block(self, db_session) -> None:
        account = _broker(db_session)
        position = _hold(db_session, account, _symbol(), "50", today() - timedelta(days=60))
        trade = _weekday_before(30)
        _feed_row(db_session, account, "100.00", trade + timedelta(days=20), "UNRELATED")
        event = service.record_event(
            db_session,
            position_id=position.id,
            kind="sell",
            occurred_on=trade,
            quantity=Decimal("1"),
            price=Decimal("100"),
            token=None,
        )
        assert event.transaction_id is not None

    def test_linking_rebooks_the_feed_row_to_equity_securities(self, db_session) -> None:
        account = _broker(db_session)
        position = _hold(db_session, account, _symbol(), "50", today() - timedelta(days=40))
        trade = _weekday_before(12)
        feed = _feed_row(db_session, account, "995.00", trade + timedelta(days=1), "SOLD SHARES")
        event = service.record_event(
            db_session,
            position_id=position.id,
            kind="sell",
            occurred_on=trade,
            quantity=Decimal("20"),
            price=Decimal("50"),
            fees=Decimal("5"),
            transaction_id=feed.id,
            token=None,
        )
        assert event.settle_on == feed.date  # settled when the bank booked it
        linked = ledger.get_transaction(db_session, event.transaction_id)
        assert linked.id != feed.id and linked.origin == "system"
        assert {p.account.path for p in linked.postings} == {account.path, "equity.securities"}
        # The bank's row is still the only cash: linking moves no money.
        assert _postings(db_session, account) == Decimal("995.0000")
        audit = db_session.execute(
            select(AuditLog).where(
                AuditLog.action == "position.link_cash", AuditLog.entity_id == feed.id
            )
        ).scalar_one()
        assert audit.before_after["after"]["linked_from_category"] == "income.uncategorized"

        # Reversing the sale gives the bank row its category back.
        reversal = service.reverse_event(db_session, event_id=event.id, token=None)
        restored = ledger.get_transaction(db_session, reversal.transaction_id)
        assert {p.account.path for p in restored.postings} == {account.path, "income.uncategorized"}
        assert _postings(db_session, account) == Decimal("995.0000")

    @pytest.mark.parametrize(
        "problem",
        [
            "wrong account",
            "wrong sign",
            "already trade cash",
            "before the trade",
            "wrong amount",
            "after the window",
            "no price",
            "pending",
            "settles a bill",
        ],
    )
    def test_link_refusals(self, db_session, problem: str) -> None:
        """A link names the row that holds exactly this trade's cash: on the
        account, posted, of the trade's sign and net (to the cent), dated
        from the trade to five days after settlement, settling no bill."""
        from purser import matching

        account = _broker(db_session)
        position = _hold(db_session, account, _symbol(), "50", today() - timedelta(days=40))
        other = _broker(db_session)
        trade = _weekday_before(12)  # T+1 is the next day; the window ends 6 days on
        price: Decimal | None = Decimal("10")
        if problem == "wrong account":
            row = _feed_row(db_session, other, "10.00", trade, "ELSEWHERE")
        elif problem == "wrong sign":
            row = _feed_row(db_session, account, "-10.00", trade, "OUT")
        elif problem == "before the trade":
            row = _feed_row(db_session, account, "10.00", trade - timedelta(days=20), "EARLY")
        elif problem == "wrong amount":
            row = _feed_row(db_session, account, "9.99", trade, "SOLD")
        elif problem == "after the window":
            row = _feed_row(db_session, account, "10.00", trade + timedelta(days=7), "LATE")
        elif problem == "no price":
            row = _feed_row(db_session, account, "10.00", trade, "SOLD")
            price = None
        elif problem == "pending":
            from purser.feed import importer

            row = ledger.create_flat_transaction(
                db_session,
                account_ref=str(account.id),
                category_ref=str(importer._uncategorized(db_session, income=True).id),
                amount=Decimal("10.00"),
                date=trade,
                requested_status=core.Status.PENDING,
                origin=core.Origin.IMPORT,
                token=None,
                content_hash=f"test-{uuid.uuid4().hex}",
            )
        elif problem == "settles a bill":
            from purser.feed import importer

            row = _feed_row(db_session, account, "10.00", trade, "SOLD")
            expected = ledger.create_flat_transaction(
                db_session,
                account_ref=str(account.id),
                category_ref=str(importer._uncategorized(db_session, income=True).id),
                amount=Decimal("10.00"),
                date=trade,
                description="Expected deposit",
                requested_status=core.Status.EXPECTED,
                token=None,
            )
            matching.manual_match(
                db_session,
                expected_id=expected.id,
                posted_id=row.id,
                token=_commit_token(db_session),
            )
        else:
            first = service.record_event(
                db_session,
                position_id=position.id,
                kind="sell",
                occurred_on=trade,
                quantity=Decimal("1"),
                price=Decimal("10"),
                token=None,
            )
            row = ledger.get_transaction(db_session, first.transaction_id)
        reason = {
            "wrong account": "is not on",
            "wrong sign": "a sale's cash comes into the account",
            "already trade cash": "already the cash of a securities trade",
            "before the trade": "before the trade",
            "wrong amount": "come to 10.00",
            "after the window": "more than five days after",
            "no price": "needs a price",
            "pending": "is pending",
            "settles a bill": "settles expected",
        }[problem]
        with pytest.raises(LedgerError, match=reason):
            service.record_event(
                db_session,
                position_id=position.id,
                kind="sell",
                occurred_on=trade,
                quantity=Decimal("1"),
                price=price,
                transaction_id=row.id,
                token=None,
            )


class TestReversalAndLocking:
    def test_reversing_a_sale_undoes_its_cash_and_restores_the_shares(self, db_session) -> None:
        account = _broker(db_session)
        position = _hold(db_session, account, _symbol(), "100", today() - timedelta(days=30))
        event = service.record_event(
            db_session,
            position_id=position.id,
            kind="sell",
            occurred_on=_weekday_before(10),
            quantity=Decimal("20"),
            price=Decimal("50"),
            token=None,
        )
        assert _postings(db_session, account) == Decimal("1000.0000")
        reversal = service.reverse_event(db_session, event_id=event.id, token=None)
        undo = ledger.get_transaction(db_session, reversal.transaction_id)
        original = ledger.get_transaction(db_session, event.transaction_id)
        assert undo.date == original.date and undo.origin == "system"
        assert _postings(db_session, account) == 0
        states = service.position_states(db_session, account, stale_days=7)
        assert states[0].quantity == Decimal("100")

    def test_reversing_a_sale_corrects_its_cash_through_a_reversal_pair(self, db_session) -> None:
        """Guardrail 3: Purser's own cash row is posted, so undoing it is a
        correction — a reversal linked by reverses_id and its replacement —
        never an unlinked offsetting row. The replacement carries nothing, so
        the chain nets to zero and the heads-only running balance agrees."""
        from purser import balances

        account = _broker(db_session)
        position = _hold(db_session, account, _symbol(), "100", today() - timedelta(days=30))
        event = service.record_event(
            db_session,
            position_id=position.id,
            kind="sell",
            occurred_on=_weekday_before(10),
            quantity=Decimal("20"),
            price=Decimal("50"),
            token=None,
        )
        cash = ledger.get_transaction(db_session, event.transaction_id)
        reversal = service.reverse_event(db_session, event_id=event.id, token=None)
        undone_by = ledger.get_transaction(db_session, reversal.transaction_id)
        reverses_cash = db_session.execute(
            select(Transaction).where(Transaction.reverses_id == cash.id)
        ).scalar_one()
        assert undone_by.reverses_id == reverses_cash.id  # a pair, linked
        assert {p.account.path: Decimal(p.amount) for p in reverses_cash.postings} == {
            account.path: Decimal("-1000.0000"),
            "equity.securities": Decimal("1000.0000"),
        }
        assert [Decimal(p.amount) for p in undone_by.postings] == [Decimal(0), Decimal(0)]
        assert undone_by.date == cash.date and undone_by.origin == "system"
        assert cash.status == "posted"  # immutable: still there, corrected
        balances.recompute_running(db_session)
        assert balances.balance_asof(db_session, account.id, today()) == _postings(
            db_session, account
        )
        assert _postings(db_session, account) == 0

    def test_a_trade_cash_row_cannot_be_edited(self, db_session) -> None:
        account = _broker(db_session)
        position = _hold(db_session, account, _symbol(), "10", today() - timedelta(days=30))
        event = service.record_event(
            db_session,
            position_id=position.id,
            kind="sell",
            occurred_on=_weekday_before(10),
            quantity=Decimal("2"),
            price=Decimal("5"),
            token=None,
        )
        token = _commit_token(db_session)
        with pytest.raises(LedgerError, match="cash of a securities trade"):
            ledger.update_transaction(
                db_session, tx_id=event.transaction_id, description="mine now", token=token
            )

    @pytest.mark.parametrize("weekday", [0, 4])  # a Monday, and a Friday (T+1 is Monday)
    def test_net_worth_is_unmoved_by_a_sale_at_the_market_on_every_day(
        self, db_session, weekday: int
    ) -> None:
        """By default Purser posts the cash on the trade date, the day the
        shares leave the holdings: no day dips by the sale's value. Cash that
        lands later (a linked bank row, a later settle_on) is in transit
        until then: tests/test_holdings_in_transit.py (#421)."""
        from purser import networth
        from purser.holdings import prices

        account = _broker(db_session)
        symbol = _symbol()
        _hold(db_session, account, symbol, "100", today() - timedelta(days=40))
        prices.record_price(
            db_session,
            symbol=symbol,
            price_date=today() - timedelta(days=40),
            close=Decimal("50"),
            token=None,
        )
        trade = today() - timedelta(days=14)
        while trade.weekday() != weekday:
            trade -= timedelta(days=1)
        days = [trade + timedelta(days=n) for n in range(-2, (today() - trade).days + 1)]
        before = {day: networth.net_worth_at(db_session, day)["net_worth"] for day in days}
        position = service.open_position(db_session, account.id, symbol)
        service.record_event(
            db_session,
            position_id=position.id,
            kind="sell",
            occurred_on=trade,
            quantity=Decimal("20"),
            price=Decimal("50"),
            token=None,
        )
        after = {day: networth.net_worth_at(db_session, day)["net_worth"] for day in days}
        assert after == before
        entry = {b["path"]: b for b in networth.net_worth_at(db_session, trade)["breakdown"]}
        assert entry[account.path]["value"] == Decimal("5000.0000")  # 1000 cash + 80 x 50


def _commit_token(session):
    from purser.models import ApiToken, User

    user = session.execute(select(User).limit(1)).scalar_one_or_none()
    if user is None:
        user = User(email=f"cash-{uuid.uuid4().hex[:6]}@example.com", role="admin")
        session.add(user)
        session.flush()
    token = ApiToken(
        user_id=user.id, name="test", kind="api", token_hash=uuid.uuid4().hex, scopes=["write"]
    )
    session.add(token)
    session.flush()
    return token


class TestRest:
    @pytest.fixture
    def broker(self, admin_client: TestClient) -> dict[str, str]:
        account = admin_client.post(
            "/accounts", json={"name": "Plan", "type": "brokerage", "path": "assets.plan"}
        ).json()
        admin_client.patch(f"/accounts/{account['id']}", json={"holdings_source": "manual"})
        position = admin_client.post(
            f"/accounts/{account['id']}/positions",
            json={
                "symbol": _symbol(),
                "quantity": "100",
                "as_of": (today() - timedelta(days=40)).isoformat(),
            },
        ).json()
        return {"account": account["id"], "position": position["id"]}

    def test_the_conflict_names_the_row_and_the_register_shows_a_trade(
        self, broker: dict[str, str], admin_client: TestClient
    ) -> None:
        trade = _weekday_before(12)
        with db.get_sessionmaker()() as session:
            account = session.get(Account, uuid.UUID(broker["account"]))
            feed = _feed_row(session, account, "250.00", trade + timedelta(days=1), "SOLD")
            session.commit()
        body = {
            "kind": "sell",
            "occurred_on": trade.isoformat(),
            "quantity": "5",
            "price": "50",
        }
        resp = admin_client.post(f"/positions/{broker['position']}/events", json=body)
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert detail["code"] == "cash_already_recorded"
        assert detail["transaction_id"] == str(feed.id) and detail["amount"] == "250.0000"

        resp = admin_client.post(
            f"/positions/{broker['position']}/events",
            json={**body, "transaction_id": str(feed.id)},
        )
        assert resp.status_code == 201, resp.text
        event = resp.json()["event"]
        assert event["proceeds_to"] == "cash" and event["transaction_id"]

        rows = admin_client.get(
            "/transactions", params={"account": broker["account"], "limit": 10}
        ).json()["items"]
        (trade_row,) = [r for r in rows if r["id"] == event["transaction_id"]]
        assert trade_row["securities_trade"] is True
        assert trade_row["category_path"] == "equity.securities"
        resp = admin_client.patch(
            f"/transactions/{trade_row['id']}", json={"category": "expenses.groceries"}
        )
        assert resp.status_code == 422 and "securities trade" in resp.text

    def test_settle_on_never_stretches_a_links_window(
        self, broker: dict[str, str], admin_client: TestClient
    ) -> None:
        """A link's window comes from the trade alone (T+1 + 5 days): passing
        settle_on equal to a row's date 25 days later does not make that row
        the sale's cash."""
        trade = _weekday_before(30)
        late = trade + timedelta(days=25)
        with db.get_sessionmaker()() as session:
            account = session.get(Account, uuid.UUID(broker["account"]))
            row = _feed_row(session, account, "250.00", late, "DIVIDEND CASH")
            session.commit()
        body = {
            "kind": "sell",
            "occurred_on": trade.isoformat(),
            "quantity": "5",
            "price": "50",
            "transaction_id": str(row.id),
        }
        for extra in ({}, {"settle_on": late.isoformat()}):
            resp = admin_client.post(
                f"/positions/{broker['position']}/events", json={**body, **extra}
            )
            assert resp.status_code == 422, resp.text
            assert "more than five days after" in resp.text
            assert "settle_on moves" not in resp.text

    def test_an_unknown_cash_row_is_a_422_not_a_404(
        self, broker: dict[str, str], admin_client: TestClient
    ) -> None:
        """A 404 here would read as "no such position"; the position exists,
        the transaction_id in the body does not."""
        missing = uuid.uuid4()
        resp = admin_client.post(
            f"/positions/{broker['position']}/events",
            json={
                "kind": "sell",
                "occurred_on": _weekday_before(5).isoformat(),
                "quantity": "1",
                "price": "10",
                "transaction_id": str(missing),
            },
        )
        assert resp.status_code == 422, resp.text
        assert str(missing) in resp.json()["detail"] and "not found" in resp.json()["detail"]

    def test_an_oversell_is_refused_before_any_cash_check(
        self, broker: dict[str, str], admin_client: TestClient
    ) -> None:
        """The share history is checked first: a sale of more than the
        position holds is the 422 naming the date, whatever its cash would
        have run into (the double-count probe, a missing price)."""
        trade = _weekday_before(12)
        with db.get_sessionmaker()() as session:
            account = session.get(Account, uuid.UUID(broker["account"]))
            _feed_row(session, account, "7500.00", trade + timedelta(days=1), "SOLD")
            session.commit()
        for body in (
            {"quantity": "150", "price": "50"},  # its cash is the row above (the probe's 409)
            {"quantity": "150"},  # no price ("needs a price")
        ):
            resp = admin_client.post(
                f"/positions/{broker['position']}/events",
                json={"kind": "sell", "occurred_on": trade.isoformat(), **body},
            )
            assert resp.status_code == 422, resp.text
            assert trade.isoformat() in resp.json()["detail"]
            assert "-50" in resp.json()["detail"]


class TestTradeCashStaysWithItsTrade:
    """No explicit path may part a trade's cash from its trade: merging it
    into a transfer (confirm-transfer, a conversion's absorb or merge) or
    settling a bill with it (match, Mark paid). Both Purser's own cash row
    and the bank row a trade linked (with its correction chain) are refused."""

    @pytest.fixture
    def trade(self, admin_client: TestClient) -> dict[str, str]:
        account = admin_client.post(
            "/accounts", json={"name": "Plan", "type": "brokerage", "path": "assets.plan"}
        ).json()["id"]
        admin_client.patch(f"/accounts/{account}", json={"holdings_source": "manual"})
        admin_client.post(
            "/accounts", json={"name": "Chk", "type": "checking", "path": "assets.chk"}
        )
        position = admin_client.post(
            f"/accounts/{account}/positions",
            json={
                "symbol": _symbol(),
                "quantity": "100",
                "as_of": (today() - timedelta(days=60)).isoformat(),
            },
        ).json()["id"]
        own_day, linked_day = _weekday_before(20), _weekday_before(10)
        own = admin_client.post(
            f"/positions/{position}/events",
            json={
                "kind": "sell",
                "occurred_on": own_day.isoformat(),
                "quantity": "4",
                "price": "50",
            },
        )
        assert own.status_code == 201, own.text
        with db.get_sessionmaker()() as session:
            plan = session.get(Account, uuid.UUID(account))
            feed = _feed_row(session, plan, "300.00", linked_day, "SOLD SHARES")
            session.commit()
        linked = admin_client.post(
            f"/positions/{position}/events",
            json={
                "kind": "sell",
                "occurred_on": linked_day.isoformat(),
                "quantity": "6",
                "price": "50",
                "transaction_id": str(feed.id),
            },
        )
        assert linked.status_code == 201, linked.text
        return {
            "account": account,
            "own": own.json()["event"]["transaction_id"],
            "own_date": own_day.isoformat(),
            "original": str(feed.id),
            "linked": linked.json()["event"]["transaction_id"],
            "linked_date": linked_day.isoformat(),
            "own_event": own.json()["event"]["id"],
        }

    @staticmethod
    def _checking_row(client: TestClient, amount: str, on: str, description: str) -> str:
        """A bank row on the checking account, as the feed imports it."""
        with db.get_sessionmaker()() as session:
            checking = ledger.account_by_path(session, "assets.chk")
            row = _feed_row(session, checking, amount, date.fromisoformat(on), description)
            session.commit()
            return str(row.id)

    @staticmethod
    def _live(tx_id: str) -> bool:
        with db.get_sessionmaker()() as session:
            tx = ledger.get_transaction(session, uuid.UUID(tx_id))
            return tx.status == "posted" and tx.superseded_by is None

    def test_confirm_transfer_refuses_trade_cash(
        self, trade: dict[str, str], admin_client: TestClient
    ) -> None:
        cases = [
            (trade["own"], "200.00", trade["own_date"]),  # Purser's own cash row
            (trade["original"], "300.00", trade["linked_date"]),  # the bank row it linked
            (trade["linked"], "300.00", trade["linked_date"]),  # the linked replacement
        ]
        for incoming, amount, on in cases:
            outgoing = self._checking_row(admin_client, f"-{amount}", on, "TRANSFER TO PLAN")
            resp = admin_client.post(
                "/matching/confirm-transfer",
                json={"outgoing_id": outgoing, "incoming_id": incoming},
            )
            assert resp.status_code == 422, resp.text
            assert "cash of a securities trade" in resp.json()["detail"]
            assert self._live(outgoing)
        assert self._live(trade["own"]) and self._live(trade["linked"])
        with db.get_sessionmaker()() as session:
            original = ledger.get_transaction(session, uuid.UUID(trade["original"]))
            assert original.status == "posted" and original.superseded_by is None

    def test_a_conversion_refuses_the_bank_row_a_trade_linked(
        self, trade: dict[str, str], admin_client: TestClient
    ) -> None:
        """The corrected bank original is still posted and still in the
        flat form: a [account] edit would absorb or merge it."""
        counterpart = self._checking_row(
            admin_client, "-300.00", trade["linked_date"], "TRANSFER TO PLAN"
        )
        resp = admin_client.patch(
            f"/transactions/{trade['original']}", json={"category": "[assets.chk]"}
        )
        assert resp.status_code == 422, resp.text
        assert self._live(counterpart)
        with db.get_sessionmaker()() as session:
            original = ledger.get_transaction(session, uuid.UUID(trade["original"]))
            assert original.status == "posted" and original.superseded_by is None

    def test_sale_cash_never_settles_a_bill(
        self, trade: dict[str, str], admin_client: TestClient
    ) -> None:
        for posted, amount, on in (
            (trade["own"], "200.00", trade["own_date"]),
            (trade["original"], "300.00", trade["linked_date"]),
            (trade["linked"], "300.00", trade["linked_date"]),
        ):
            expected = admin_client.post(
                "/transactions",
                json={
                    "account": "assets.plan",
                    "category": "income.uncategorized",
                    "amount": amount,
                    "date": on,
                    "description": "Expected deposit",
                    "status": "expected",
                },
            )
            assert expected.status_code in (200, 201), expected.text
            resp = admin_client.post(
                "/matching/match",
                json={"expected_id": expected.json()["id"], "posted_id": posted},
            )
            assert resp.status_code == 422, resp.text
            assert "cash of a securities trade" in resp.json()["detail"]
        with db.get_sessionmaker()() as session:
            from purser import matching

            candidates = matching._candidates_for(
                session, uuid.UUID(trade["account"]), date.fromisoformat(trade["linked_date"])
            )
            ids = {str(c.id) for c in candidates}
            assert not ids & {trade["own"], trade["original"], trade["linked"]}

    def test_undo_refuses_a_cash_row_that_moved_without_its_trade(
        self, trade: dict[str, str], admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Belt and braces: if any path ever merged a trade's cash row away,
        reversing the trade must not undo money that is no longer there."""
        from purser import matching

        monkeypatch.setattr(
            matching.ledger_service,
            "refuse_trade_cash_lineage",
            lambda *a, **k: None,
            raising=False,
        )
        outgoing = self._checking_row(admin_client, "-200.00", trade["own_date"], "TRANSFER")
        merged = admin_client.post(
            "/matching/confirm-transfer",
            json={"outgoing_id": outgoing, "incoming_id": trade["own"]},
        )
        assert merged.status_code == 200, merged.text
        resp = admin_client.post(f"/position-events/{trade['own_event']}/reverse")
        assert resp.status_code == 422, resp.text
        assert "moved without its trade" in resp.json()["detail"]
        with db.get_sessionmaker()() as session:
            securities = ledger.securities_account(session)
            rows = session.execute(
                select(Transaction.id)
                .join(Posting, Posting.transaction_id == Transaction.id)
                .where(Posting.account_id == securities.id)
            ).all()
            assert len(rows) == 2  # the two trades' cash rows; no undo was booked


class TestCorrectingAClosedPosition:
    """A position closes once it holds nothing, so a wrong final sale used to
    be uncorrectable: reversing an event needs an open position. Reopen,
    reverse, record again, close."""

    @pytest.fixture
    def plan(self, admin_client: TestClient) -> str:
        account = admin_client.post(
            "/accounts", json={"name": "Plan", "type": "brokerage", "path": "assets.plan"}
        ).json()["id"]
        admin_client.patch(f"/accounts/{account}", json={"holdings_source": "manual"})
        return account

    @staticmethod
    def _sold_out(client: TestClient, account: str, symbol: str, days: int) -> tuple[str, str]:
        """A position held, sold to nothing and closed: (position, sale event)."""
        position = client.post(
            f"/accounts/{account}/positions",
            json={
                "symbol": symbol,
                "quantity": "10",
                "as_of": (today() - timedelta(days=days + 10)).isoformat(),
            },
        ).json()["id"]
        sale = client.post(
            f"/positions/{position}/events",
            json={
                "kind": "sell",
                "occurred_on": _weekday_before(days).isoformat(),
                "quantity": "10",
                "price": "50",
            },
        )
        assert sale.status_code == 201, sale.text
        closed = client.patch(f"/positions/{position}", json={"closed": True})
        assert closed.status_code == 200, closed.text
        return position, sale.json()["event"]["id"]

    def test_reopen_reverse_record_again_close(self, plan: str, admin_client: TestClient) -> None:
        from purser.models import Event

        position, sale = self._sold_out(admin_client, plan, "ACME", 20)
        resp = admin_client.post(f"/position-events/{sale}/reverse")
        assert resp.status_code == 422 and "closed" in resp.text

        resp = admin_client.patch(f"/positions/{position}", json={"closed": False})
        assert resp.status_code == 200, resp.text
        assert resp.json()["closed_at"] is None
        with db.get_sessionmaker()() as session:
            entity = uuid.UUID(position)
            audited = session.execute(
                select(AuditLog).where(
                    AuditLog.action == "position.reopen", AuditLog.entity_id == entity
                )
            ).scalar_one()
            assert audited.before_after["before"]["closed_at"] is not None
            changes = [
                e.payload["change"]
                for e in session.execute(
                    select(Event).where(Event.type == "position.updated", Event.entity_id == entity)
                ).scalars()
            ]
            assert "reopened" in changes

        resp = admin_client.post(f"/position-events/{sale}/reverse")
        assert resp.status_code == 200, resp.text
        with db.get_sessionmaker()() as session:
            account = session.get(Account, uuid.UUID(plan))
            assert _postings(session, account) == 0  # the sale's cash went with it
        resp = admin_client.post(
            f"/positions/{position}/events",
            json={
                "kind": "sell",
                "occurred_on": _weekday_before(20).isoformat(),
                "quantity": "10",
                "price": "51",
            },
        )
        assert resp.status_code == 201, resp.text
        assert (
            admin_client.patch(f"/positions/{position}", json={"closed": True}).status_code == 200
        )
        with db.get_sessionmaker()() as session:
            account = session.get(Account, uuid.UUID(plan))
            assert _postings(session, account) == Decimal("510.0000")

    def test_reopen_refuses_overlapping_positions(
        self, plan: str, admin_client: TestClient
    ) -> None:
        """One open position per symbol and account (the unique index), and
        a symbol's positions never overlap in time: an earlier position is
        not reopened while a later one exists."""
        first, _sale = self._sold_out(admin_client, plan, "ACME", 30)
        second = admin_client.post(
            f"/accounts/{plan}/positions",
            json={
                "symbol": "ACME",
                "quantity": "4",
                "as_of": (today() - timedelta(days=5)).isoformat(),
            },
        ).json()["id"]
        resp = admin_client.patch(f"/positions/{first}", json={"closed": False})
        assert resp.status_code == 409 and "open ACME position" in resp.text
        admin_client.post(
            f"/positions/{second}/events",
            json={
                "kind": "sell",
                "occurred_on": today().isoformat(),
                "quantity": "4",
                "proceeds_to": "none",
            },
        )
        assert admin_client.patch(f"/positions/{second}", json={"closed": True}).status_code == 200
        resp = admin_client.patch(f"/positions/{first}", json={"closed": False})
        assert resp.status_code == 409 and "later" in resp.text
        # The newest one reopens; an open one cannot be reopened again.
        assert admin_client.patch(f"/positions/{second}", json={"closed": False}).status_code == 200
        resp = admin_client.patch(f"/positions/{second}", json={"closed": False})
        assert resp.status_code == 422 and "is open" in resp.text


def _blocked_on_a_lock(engine: Engine, seconds: float = 15) -> bool:
    """Whether a backend of this database waits on a lock within ``seconds``:
    the contender of a race test has reached the point where it must wait."""
    import time

    from sqlalchemy import text

    deadline = time.monotonic() + seconds
    with engine.connect() as conn:
        while time.monotonic() < deadline:
            waiting = conn.execute(
                text(
                    "SELECT 1 FROM pg_stat_activity WHERE datname = current_database()"
                    " AND wait_event_type = 'Lock'"
                )
            ).first()
            conn.rollback()
            if waiting is not None:
                return True
            time.sleep(0.05)
    return False


def test_two_writers_recreating_the_system_account_agree(
    clean_db: None, migrated_engine: Engine
) -> None:
    """securities_account() recreates equity.securities when it is missing
    (clean_db truncates the seeded row). Two writers doing it at once both
    get the one account: the insert that loses the race on the unique path
    reads the winner's row instead of failing with an IntegrityError (500)."""
    import threading

    maker = db.get_sessionmaker()
    first = maker()
    outcome: dict[str, object] = {}
    try:
        created = ledger.securities_account(first)  # inserted, not committed

        def contender() -> None:
            with maker() as session:
                try:
                    outcome["id"] = ledger.securities_account(session).id
                    session.commit()
                except Exception as exc:  # the race's failure is the finding
                    session.rollback()
                    outcome["error"] = exc

        thread = threading.Thread(target=contender, daemon=True)
        thread.start()
        assert _blocked_on_a_lock(migrated_engine)  # its insert waits on ours
        first.commit()
        thread.join(15)
    finally:
        first.close()
    assert "error" not in outcome, outcome.get("error")
    assert outcome["id"] == created.id


def test_the_cash_opt_in_is_validated_under_the_account_lock(
    clean_db: None, migrated_engine: Engine
) -> None:
    """An admin turns feed_balance_is_cash on while another switches the
    account back to the feed. The opt-in waits for the switch and is refused
    with its reason (422): validated on the unlocked read, it passed and then
    hit the CHECK (a 500)."""
    import threading

    from purser.models import Connection

    maker = db.get_sessionmaker()
    with maker() as session:
        account = _broker(session)
        # Only a mapped account can go back to the feed (decision 88).
        connection = Connection()
        session.add(connection)
        session.flush()
        account.connection_id = connection.id
        account.external_id = f"broker-{uuid.uuid4().hex[:8]}"
        account_id = account.id
        session.commit()
    first = maker()
    outcome: dict[str, object] = {}
    try:
        ledger.update_account(
            first, account_id=account_id, holdings_source="feed", actor_token_id=None
        )  # holds the account lock, not committed

        def contender() -> None:
            with maker() as session:
                try:
                    ledger.update_account(
                        session,
                        account_id=account_id,
                        feed_balance_is_cash=True,
                        actor_token_id=None,
                    )
                    session.commit()
                    outcome["ok"] = True
                except LedgerError as exc:
                    session.rollback()
                    outcome["refused"] = str(exc)
                except Exception as exc:  # the CHECK violation is the finding
                    session.rollback()
                    outcome["error"] = exc

        thread = threading.Thread(target=contender, daemon=True)
        thread.start()
        assert _blocked_on_a_lock(migrated_engine)
        first.commit()
        thread.join(15)
    finally:
        first.close()
    assert "error" not in outcome, outcome.get("error")
    assert "holdings_source 'manual'" in str(outcome.get("refused"))


def test_a_dry_run_leaves_the_balance_watermark_alone(db_session) -> None:
    """A record_sale dry run is rolled back, so it must not mark balances
    dirty: the UPDATE takes the global watermark's row lock, and the session
    flag makes the commit run the running-balance recompute under it."""
    from sqlalchemy import event as sa_event

    account = _broker(db_session)
    symbol = _symbol()
    _hold(db_session, account, symbol, "10", today() - timedelta(days=30))
    db_session.info.pop("balances_dirty", None)  # the setup's own marks
    statements: list[str] = []

    def capture(conn, cursor, statement, parameters, context, executemany) -> None:  # type: ignore[no-untyped-def]
        statements.append(statement)

    engine = db_session.get_bind()
    sa_event.listen(engine, "before_cursor_execute", capture)
    try:
        sale = service.record_sale(
            db_session,
            account_ref=str(account.id),
            symbol=symbol,
            quantity=Decimal("2"),
            price=Decimal("10"),
            trade_date=_weekday_before(5),
            token=None,
            dry_run=True,
        )
    finally:
        sa_event.remove(engine, "before_cursor_execute", capture)
    assert sale.dry_run and sale.cash_amount == Decimal("20.00")
    assert statements  # the dry run did run the real path
    assert not [s for s in statements if "balance_watermark" in s]
    assert "balances_dirty" not in db_session.info


def test_review_batch_marks_trade_cash_locked(db_session) -> None:
    """Agents reviewing rows are steered to update_transaction and
    confirm_transfer, both wrong for a trade's cash: the row says it is
    locked and names the trade's event; the guidance says what to do."""
    from purser import review

    account = _broker(db_session)
    position = _hold(db_session, account, _symbol(), "10", today() - timedelta(days=30))
    event = service.record_event(
        db_session,
        position_id=position.id,
        kind="sell",
        occurred_on=_weekday_before(5),
        quantity=Decimal("2"),
        price=Decimal("10"),
        token=None,
    )
    batch = review.batch(db_session, limit=50, account_ref=str(account.id))
    (row,) = [r for r in batch["rows"] if r["id"] == str(event.transaction_id)]
    assert "securities_trade" in row["flags"]
    assert row["locked"] == {"event_id": str(event.id), "position_id": str(position.id)}
    assert any("reverse_position_event" in line for line in batch["guidance"])
    quiet = review.batch(db_session, limit=50, account_ref=str(account.id), include_guidance=False)
    (row,) = [r for r in quiet["rows"] if r["id"] == str(event.transaction_id)]
    assert "reverse_position_event" not in repr(row)


def test_an_escalated_bank_row_a_trade_linked_is_locked(db_session) -> None:
    """The backend reviewer escalates a feed SOLD row; a sale then links it.
    The escalated list still shows the corrected original — it is the trade's
    cash all the same, so it is locked and names the trade."""
    from purser import crypto, review
    from purser.feed import importer
    from purser.models import Connection, RawFetch

    account = _broker(db_session)
    position = _hold(db_session, account, _symbol(), "50", today() - timedelta(days=40))
    trade = _weekday_before(12)
    connection = Connection(provider="simplefin")
    db_session.add(connection)
    db_session.flush()
    fetch = RawFetch(
        connection_id=connection.id,
        content_hash=uuid.uuid4().hex,
        payload_enc=crypto.encrypt("{}"),
    )
    db_session.add(fetch)
    db_session.flush()
    bank = ledger.create_flat_transaction(
        db_session,
        account_ref=str(account.id),
        category_ref=str(importer._uncategorized(db_session, income=True).id),
        amount=Decimal("100.00"),
        date=trade,
        description="SOLD",
        requested_status=core.Status.POSTED,
        origin=core.Origin.IMPORT,
        token=None,
        source_fetch_id=fetch.id,
        content_hash=f"test-{uuid.uuid4().hex}",
    )
    review.mark(db_session, tx_id=bank.id, token=None, via=review.VIA_AI_REVIEW, escalate=True)
    event = service.record_event(
        db_session,
        position_id=position.id,
        kind="sell",
        occurred_on=trade,
        quantity=Decimal("1"),
        price=Decimal("100"),
        transaction_id=bank.id,
        token=_commit_token(db_session),
    )
    page = review.batch(db_session, limit=25, account_ref=str(account.id))
    (row,) = [r for r in page["escalated"] if r["id"] == str(bank.id)]
    assert "securities_trade" in row["flags"]
    assert row["locked"] == {"event_id": str(event.id), "position_id": str(position.id)}


# ---------------------------------------------------------------------------
# The opt-in cash drift check (the cash-sweep shape)
# ---------------------------------------------------------------------------

ACCESS_URL = "https://user:secret@bridge.example.com/simplefin"


def _sweep_payload(balance: str) -> dict:
    """A cash sweep: the feed reports only the sweep's small cash balance and
    the disbursements out; the sale proceeds that funded them never appear."""
    epoch = int(now().timestamp())
    return {
        "errors": [],
        "accounts": [
            {
                "id": "SF-SWEEP",
                "name": "Synthetic Plan Sweep",
                "currency": "USD",
                "balance": balance,
                "transactions": [
                    {
                        "id": "d1",
                        "posted": epoch - 20 * 86400,
                        "amount": "-1000.00",
                        "description": "ACH DISBURSEMENT",
                    },
                    {
                        "id": "d2",
                        "posted": epoch - 10 * 86400,
                        "amount": "-500.00",
                        "description": "ACH DISBURSEMENT",
                    },
                ],
            }
        ],
    }


@pytest.fixture
def sweep(admin_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    import base64

    payload = _sweep_payload("12.34")
    real_client = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/claim/TEST123"):
            return httpx.Response(200, text=ACCESS_URL)
        return httpx.Response(200, json=payload)

    def patched(*args: object, **kwargs: object) -> httpx.Client:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx, "Client", patched)
    token = base64.b64encode(b"https://bridge.example.com/simplefin/claim/TEST123").decode()
    connection = admin_client.post("/connections", json={"setup_token": token}).json()["id"]
    account = admin_client.post(
        "/accounts", json={"name": "Plan", "type": "brokerage", "path": "assets.plan"}
    ).json()["id"]
    resp = admin_client.post(
        f"/connections/{connection}/map", json={"external_id": "SF-SWEEP", "account_id": account}
    )
    assert resp.status_code == 200, resp.text
    resp = admin_client.patch(f"/accounts/{account}", json={"holdings_source": "manual"})
    assert resp.status_code == 200, resp.text
    return {"connection": connection, "account": account}


def _discovered(client: TestClient, connection: str) -> dict:
    (row,) = client.get(f"/connections/{connection}/discovered").json()
    return row


class TestFeedBalanceIsCash:
    def test_opt_in_is_admin_only_and_needs_manual_holdings(
        self, sweep: dict[str, str], admin_client: TestClient
    ) -> None:
        operator = admin_client.post(
            "/tokens", json={"name": "op", "scopes": ["read", "write"]}
        ).json()["token"]
        resp = admin_client.patch(
            f"/accounts/{sweep['account']}",
            json={"feed_balance_is_cash": True},
            headers={"Authorization": f"Bearer {operator}"},
        )
        assert resp.status_code == 403
        checking = admin_client.post(
            "/accounts", json={"name": "Chk", "type": "checking", "path": "assets.chk"}
        ).json()["id"]
        resp = admin_client.patch(f"/accounts/{checking}", json={"feed_balance_is_cash": True})
        assert resp.status_code == 422
        # A pin needs the opt-in on a brokerage account.
        resp = admin_client.patch(
            f"/accounts/{sweep['account']}", json={"drift_basis_override": "posted_only"}
        )
        assert resp.status_code == 422
        resp = admin_client.patch(
            f"/accounts/{sweep['account']}",
            json={"feed_balance_is_cash": True, "drift_basis_override": "posted_only"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["feed_balance_is_cash"] is True
        # Back to the feed: the opt-in and its pin end with manual holdings.
        resp = admin_client.patch(f"/accounts/{sweep['account']}", json={"holdings_source": "feed"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["feed_balance_is_cash"] is False and body["drift_basis_override"] is None

    def test_a_cash_sweep_drifts_until_the_sale_proceeds_are_recorded(
        self, sweep: dict[str, str], admin_client: TestClient, migrated_engine: Engine
    ) -> None:
        connection, account = sweep["connection"], sweep["account"]
        first = admin_client.post(f"/connections/{connection}/sync")
        assert first.status_code == 200, first.text
        # Not opted in: a brokerage feed balance is market value, so no drift.
        row = _discovered(admin_client, connection)
        assert row["ledger_balance"] is None and row["drift"] is None

        assert (
            admin_client.patch(
                f"/accounts/{account}", json={"feed_balance_is_cash": True}
            ).status_code
            == 200
        )
        row = _discovered(admin_client, connection)
        # The ledger saw only the disbursements: -1500.00 against a 12.34 sweep.
        assert row["ledger_balance"] == "-1500.0000" and row["drift"] == "1512.3400"

        # Two syncs at the same delta confirm the episode: one event, and the
        # digest names the account.
        admin_client.post(f"/connections/{connection}/sync")
        admin_client.post(f"/connections/{connection}/sync")
        with db.get_sessionmaker()() as session:
            from purser.digest import whats_new
            from purser.models import Event

            drifts = (
                session.execute(
                    select(Event).where(
                        Event.type == "balance.drift", Event.entity_id == uuid.UUID(account)
                    )
                )
                .scalars()
                .all()
            )
            assert [e.payload["delta"] for e in drifts] == ["1512.3400"]
            lines = whats_new(session)["balance_drifts"]
            assert any(line["account"] == "assets.plan" for line in lines)

        # Record the sales whose proceeds funded the disbursements.
        symbol = _symbol()
        position = admin_client.post(
            f"/accounts/{account}/positions",
            json={
                "symbol": symbol,
                "quantity": "40",
                "as_of": (today() - timedelta(days=60)).isoformat(),
            },
        ).json()
        for days, quantity, price in ((25, "10", "100"), (14, "5", "102.468")):
            trade = _weekday_before(days)
            resp = admin_client.post(
                f"/positions/{position['id']}/events",
                json={
                    "kind": "sell",
                    "occurred_on": trade.isoformat(),
                    "quantity": quantity,
                    "price": price,
                },
            )
            assert resp.status_code == 201, resp.text
        row = _discovered(admin_client, connection)
        assert row["ledger_balance"] == "12.3400" and row["drift"] is None

        # And the next sync observes a clean account.
        admin_client.post(f"/connections/{connection}/sync")
        with db.get_sessionmaker()() as session:
            sync = session.execute(
                select(AuditLog)
                .where(AuditLog.action == "connection.sync")
                .order_by(AuditLog.at.desc(), AuditLog.id.desc())
                .limit(1)
            ).scalar_one()
            observation = sync.before_after["after"]["balance_drift"][account]
            assert observation["delta"] is None and observation["pending"] == "12.3400"
            balance = ledger.account_balances(session, [uuid.UUID(account)])[uuid.UUID(account)]
            assert balance == Decimal("12.3400")
            # The proceeds sit against equity.securities, never in spending.
            securities = ledger.securities_account(session)
            booked = (
                session.execute(
                    select(Posting.amount)
                    .join(Transaction, Transaction.id == Posting.transaction_id)
                    .where(Posting.account_id == securities.id)
                )
                .scalars()
                .all()
            )
            assert sum(booked, Decimal(0)) == Decimal("-1512.3400")


def test_account_purge_removes_trade_cash_with_its_events(clean_db: None) -> None:
    """A trade's event names its cash row by foreign key, so the guarded
    delete-account CLI removes positions and events before the transactions."""
    from purser.admin import account_delete

    with db.get_sessionmaker()() as session:
        account = _broker(session)
        position = _hold(session, account, _symbol(), "10", today() - timedelta(days=30))
        service.record_event(
            session,
            position_id=position.id,
            kind="sell",
            occurred_on=_weekday_before(10),
            quantity=Decimal("2"),
            price=Decimal("10"),
            token=None,
        )
        session.commit()
        summary = account_delete.execute_delete(session, account, actor_token_id=None)
        session.commit()
        assert summary["position_events_deleted"] == 2
        assert summary["transactions_deleted"] == 1
        # The system account outlives it, empty of this account's cash.
        securities = ledger.securities_account(session)
        left = session.execute(select(Posting.id).where(Posting.account_id == securities.id)).all()
        assert left == []


def test_a_bills_match_is_never_carried_onto_sale_cash(
    migrated_engine: Engine, clean_db: None, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through real syncs: a pending deposit P is marked paid against a
    weekly contribution; the broker's posted SOLD row B of the same amount is
    linked to a sale; the bank then drops P. The reconciler's pending->posted
    carry would move the bill's match onto B, the sale's cash. The bill is
    released instead, its slot due again."""
    from sqlalchemy import text

    from tests.test_feed import FeedHarness, _recent_epoch, payload_of

    h = FeedHarness(client, monkeypatch)
    ext, path = "Demo Brokerage", "assets.brokerage"
    h.use_payload(payload_of([], account_id=ext))
    cid = h.connect()
    resp = h.client.post(
        f"/connections/{cid}/map",
        json={"external_id": ext, "create": {"name": ext, "type": "brokerage", "path": path}},
    )
    assert resp.status_code == 200, resp.text
    (acct,) = [a for a in h.client.get("/accounts").json() if a["path"] == path]
    assert (
        h.client.patch(f"/accounts/{acct['id']}", json={"holdings_source": "manual"}).status_code
        == 200
    )
    position = h.client.post(
        f"/accounts/{acct['id']}/positions",
        json={
            "symbol": "XYZ",
            "quantity": "50",
            "as_of": (today() - timedelta(days=40)).isoformat(),
        },
    ).json()["id"]
    schedule = h.client.post(
        "/schedules",
        json={
            "name": "Contribution",
            "account": path,
            "rrule": "FREQ=WEEKLY",
            "amount_type": "fixed",
            "amount": "100.00",
            "payee": "Acme Payroll",
        },
    )
    assert schedule.status_code == 201, schedule.text
    expected_id = h.client.get(f"/schedules/{schedule.json()['id']}").json()["instances"][0][
        "transaction_id"
    ]
    pend = {
        "id": "dep-p",
        "posted": _recent_epoch(1),
        "amount": "100.00",
        "description": "ACH DEPOSIT 0042",
        "payee": "Acme Payroll",
        "pending": True,
    }
    sold = {
        "id": "sold-b",
        "posted": _recent_epoch(0),
        "amount": "100.00",
        "description": "SOLD 1 XYZ",
        "payee": "XYZ",
    }
    h.use_payload(payload_of([pend, sold], account_id=ext))
    h.sync(cid)

    def rows(status: str) -> list[dict]:
        return h.client.get("/transactions", params={"account": path, "status": status}).json()[
            "items"
        ]

    (p_row,) = rows("pending")
    (b_row,) = [r for r in rows("posted") if r["description"] == "SOLD 1 XYZ"]
    paid = h.client.post(
        "/matching/match", json={"expected_id": expected_id, "posted_id": p_row["id"]}
    )
    assert paid.status_code == 200, paid.text
    sale = h.client.post(
        f"/positions/{position}/events",
        json={
            "kind": "sell",
            "occurred_on": today().isoformat(),
            "quantity": "1",
            "price": "100",
            "transaction_id": b_row["id"],
        },
    )
    assert sale.status_code == 201, sale.text

    h.use_payload(payload_of([sold], account_id=ext))
    assert h.sync(cid)["voided_pendings"] == 1
    with migrated_engine.connect() as conn:
        on_b = conn.execute(
            text(
                "SELECT count(*) FROM matches WHERE posted_id = :b"
                " AND state IN ('proposed', 'confirmed')"
            ),
            {"b": b_row["id"]},
        ).scalar_one()
        released = (
            conn.execute(
                text("SELECT state FROM matches WHERE expected_id = :e"), {"e": expected_id}
            )
            .scalars()
            .all()
        )
    assert on_b == 0
    assert released == ["rejected"]  # released: the bill is due again in its slot


def test_unmatched_posted_rows_leave_trade_cash_out(db_session) -> None:
    """list_unmatched(direction='posted') is new-spending triage: neither a
    trade's own cash nor the bank row a trade linked belongs there."""
    from purser import matching

    account = _broker(db_session)
    position = _hold(db_session, account, _symbol(), "50", today() - timedelta(days=40))
    trade = _weekday_before(12)
    feed = _feed_row(db_session, account, "100.00", trade, "SOLD")
    linked = service.record_event(
        db_session,
        position_id=position.id,
        kind="sell",
        occurred_on=trade,
        quantity=Decimal("1"),
        price=Decimal("100"),
        transaction_id=feed.id,
        token=None,
    )
    other = _feed_row(db_session, account, "-12.00", trade, "FEE")
    listed = {r["transaction_id"] for r in matching.list_unmatched(db_session, direction="posted")}
    assert other.id in listed
    assert not listed & {feed.id, linked.transaction_id}


def test_automatic_passes_read_the_trade_cash_lineage_once(db_session, monkeypatch) -> None:
    """The nightly matcher and the auto-link pass read the trade-cash lineage
    once per pass and test membership; neither re-runs the recursive walk per
    expected row nor walks a chain per auto-match. The explicit paths (match,
    confirm, convert) keep the walking guard."""
    from sqlalchemy import event

    from purser import matching, transfers
    from purser.feed import importer
    from purser.ledger import accounts as ledger_accounts

    account = _broker(db_session)
    position = _hold(db_session, account, _symbol(), "50", today() - timedelta(days=40))
    service.record_event(
        db_session,
        position_id=position.id,
        kind="sell",
        occurred_on=_weekday_before(20),
        quantity=Decimal("1"),
        price=Decimal("50"),
        token=None,
    )
    income = importer._uncategorized(db_session, income=True)
    for i in range(4):
        day = _weekday_before(3 + 5 * i)
        amount = Decimal(f"{311 + i}.17")
        _feed_row(db_session, account, str(amount), day, f"DEPOSIT {i}")
        ledger.create_flat_transaction(
            db_session,
            account_ref=str(account.id),
            category_ref=str(income.id),
            amount=amount,
            date=day,
            description=f"DEPOSIT {i}",
            requested_status=core.Status.EXPECTED,
            token=None,
        )
    walks: list[str] = []
    real_walk = ledger_accounts.is_trade_cash_lineage

    def counting_walk(session, tx):
        walks.append(str(tx.id))
        return real_walk(session, tx)

    monkeypatch.setattr(ledger_accounts, "is_trade_cash_lineage", counting_walk)
    statements: list[str] = []

    def record(conn, cursor, statement, *args) -> None:
        statements.append(statement)

    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", record)
    try:
        stats = matching.run_matching(db_session)
        lineage_reads = sum("trade_cash_chain" in s for s in statements)
        statements.clear()
        transfers.auto_link_pass(db_session)
        link_reads = sum("trade_cash_chain" in s for s in statements)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert stats["auto_matched"] + stats["proposed"] == 4
    assert lineage_reads == 1
    assert link_reads == 1
    assert walks == []


class TestTheGuardsHoldUnderConcurrency:
    """Two sessions, production orderings (round-3 item 6): a sale linking a
    bank row and the machinery that would merge, match or convert it exclude
    each other through the row lock; the automatic passes skip a held row
    rather than wait, so one holding the balance watermark never deadlocks."""

    @staticmethod
    def _setup(maker, *, expected: bool = False, counterpart: bool = False, fetched: bool = False):
        from purser import crypto
        from purser.feed import importer
        from purser.models import Connection, RawFetch

        day = _weekday_before(8)
        with maker() as session:
            account = _broker(session)
            position = _hold(session, account, _symbol(), "10", today() - timedelta(days=60))
            if fetched:  # archived, as purge-feed-rows requires of what it deletes
                connection = Connection(provider="simplefin")
                session.add(connection)
                session.flush()
                fetch = RawFetch(
                    connection_id=connection.id,
                    content_hash=uuid.uuid4().hex,
                    payload_enc=crypto.encrypt("{}"),
                )
                session.add(fetch)
                session.flush()
                sold = ledger.create_flat_transaction(
                    session,
                    account_ref=str(account.id),
                    category_ref=str(importer._uncategorized(session, income=True).id),
                    amount=Decimal("250.00"),
                    date=day,
                    description="SOLD SHARES",
                    requested_status=core.Status.POSTED,
                    origin=core.Origin.IMPORT,
                    token=None,
                    source_fetch_id=fetch.id,
                    content_hash=f"test-{uuid.uuid4().hex}",
                )
            else:
                sold = _feed_row(session, account, "250.00", day, "SOLD SHARES")
            ids = {"account": account.id, "position": position.id, "sold": sold.id, "day": day}
            if expected:
                ids["expected"] = ledger.create_flat_transaction(
                    session,
                    account_ref=str(account.id),
                    category_ref=str(importer._uncategorized(session, income=True).id),
                    amount=Decimal("250.00"),
                    date=day,
                    description="SOLD SHARES",
                    requested_status=core.Status.EXPECTED,
                    token=None,
                ).id
            if counterpart:
                ids["checking"] = ledger.create_account(
                    session,
                    name="Chk",
                    type_="checking",
                    path=f"assets.chk_{uuid.uuid4().hex[:8]}",
                    actor_token_id=None,
                ).id
            session.commit()
        return ids

    @staticmethod
    def _sell(session, ids):
        return service.record_event(
            session,
            position_id=ids["position"],
            kind="sell",
            occurred_on=ids["day"],
            quantity=Decimal("5"),
            price=Decimal("50"),
            transaction_id=ids["sold"],
            token=None,
        )

    @staticmethod
    def _heads(session, account_id) -> Decimal:
        later = select(Transaction.reverses_id).where(Transaction.reverses_id.is_not(None))
        amounts = session.execute(
            select(Posting.amount)
            .join(Transaction, Transaction.id == Posting.transaction_id)
            .where(
                Posting.account_id == account_id,
                Transaction.status.in_(["pending", "posted"]),
                Transaction.id.not_in(later),
            )
        ).scalars()
        return sum((Decimal(a) for a in amounts), Decimal(0))

    def test_an_import_holding_the_watermark_skips_a_row_a_sale_is_linking(
        self, clean_db: None, migrated_engine: Engine
    ) -> None:
        """The import has written (it holds the watermark); a sale linking
        its SOLD row waits on it; the import's own auto-link pass, which
        would merge that row with the counterpart it brought, skips it and
        commits; the sale then commits. The cash counts once."""
        import threading

        from sqlalchemy import text

        from purser import db, transfers

        maker = db.get_sessionmaker()
        ids = self._setup(maker, counterpart=True)
        job = maker()
        outcome: dict[str, object] = {}
        try:
            job.execute(text("SET LOCAL lock_timeout = '10s'"))
            checking = job.get(Account, ids["checking"])
            _feed_row(job, checking, "-250.00", ids["day"], "TRANSFER TO PLAN")

            def agent() -> None:
                with maker() as session:
                    try:
                        outcome["event"] = self._sell(session, ids).id
                        session.commit()
                    except Exception as exc:
                        session.rollback()
                        outcome["error"] = repr(exc)

            thread = threading.Thread(target=agent, daemon=True)
            thread.start()
            assert _blocked_on_a_lock(migrated_engine)
            stats = transfers.auto_link_pass(job)
            job.commit()
            thread.join(30)
        finally:
            job.close()
        assert "error" not in outcome, outcome
        assert stats["transfers_auto_linked"] == 0
        with maker() as session:
            sold = session.get(Transaction, ids["sold"])
            assert sold is not None and sold.status == "posted" and sold.superseded_by is None
            assert self._heads(session, ids["account"]) == Decimal("250.0000")

    def test_the_matcher_skips_a_row_a_sale_is_linking(
        self, clean_db: None, migrated_engine: Engine
    ) -> None:
        """A sale's link of R is in flight while the nightly matcher would
        auto-match R to an open expected of the same amount: the matcher
        skips R without waiting, so sale cash never settles the bill."""
        import threading

        from purser import db, matching
        from purser.models import Match

        maker = db.get_sessionmaker()
        ids = self._setup(maker, expected=True)
        first = maker()
        outcome: dict[str, object] = {}
        try:
            self._sell(first, ids)

            def job() -> None:
                with maker() as session:
                    try:
                        outcome["stats"] = matching.run_matching(session)
                        session.commit()
                    except Exception as exc:
                        session.rollback()
                        outcome["error"] = repr(exc)

            thread = threading.Thread(target=job, daemon=True)
            thread.start()
            thread.join(30)
            outcome["finished_before_the_sale_committed"] = not thread.is_alive()
            first.commit()
            thread.join(30)
        finally:
            first.close()
        assert "error" not in outcome, outcome
        assert outcome["finished_before_the_sale_committed"] is True
        assert outcome["stats"]["auto_matched"] == 0  # type: ignore[index]
        with maker() as session:
            assert (
                session.execute(
                    select(Match).where(
                        Match.posted_id == ids["sold"], Match.state.in_(["proposed", "confirmed"])
                    )
                ).first()
                is None
            )
            expected = session.get(Transaction, ids["expected"])
            assert expected is not None and expected.status == "expected"

    def test_an_auto_match_rechecks_a_claim_made_after_its_pass_began(
        self, db_session, monkeypatch
    ) -> None:
        """The matcher reads the lineage once per pass; a sale that links a
        row after that read (simulated: the pass's set is empty) is caught
        when the auto-match locks the row, which it then skips."""
        from purser import matching
        from purser.feed import importer
        from purser.ledger import accounts as ledger_accounts
        from purser.models import Match

        account = _broker(db_session)
        position = _hold(db_session, account, _symbol(), "50", today() - timedelta(days=40))
        day = _weekday_before(8)
        sold = _feed_row(db_session, account, "250.00", day, "SOLD SHARES")
        event = service.record_event(
            db_session,
            position_id=position.id,
            kind="sell",
            occurred_on=day,
            quantity=Decimal("5"),
            price=Decimal("50"),
            transaction_id=sold.id,
            token=None,
        )
        ledger.create_flat_transaction(
            db_session,
            account_ref=str(account.id),
            category_ref=str(importer._uncategorized(db_session, income=True).id),
            amount=Decimal("250.00"),
            date=day,
            description="SOLD SHARES",
            requested_status=core.Status.EXPECTED,
            token=None,
        )
        monkeypatch.setattr(ledger_accounts, "trade_cash_lineage_set", lambda session: set())
        stats = matching.run_matching(db_session)
        assert stats["auto_matched"] == 0
        live = db_session.execute(
            select(Match).where(
                Match.posted_id.in_([sold.id, event.transaction_id]),
                Match.state.in_(["proposed", "confirmed"]),
            )
        ).first()
        assert live is None

    def test_an_explicit_match_waits_for_the_sale_and_is_refused(
        self, clean_db: None, migrated_engine: Engine
    ) -> None:
        """Mark paid against R while a sale links it: the match waits on the
        row, then sees the committed link and refuses."""
        import threading

        from purser import db, matching

        maker = db.get_sessionmaker()
        ids = self._setup(maker, expected=True)
        first = maker()
        outcome: dict[str, object] = {}
        try:
            self._sell(first, ids)

            def person() -> None:
                with maker() as session:
                    try:
                        matching.manual_match(
                            session,
                            expected_id=ids["expected"],
                            posted_id=ids["sold"],
                            token=_commit_token(session),
                        )
                        session.commit()
                        outcome["matched"] = True
                    except LedgerError as exc:
                        session.rollback()
                        outcome["refused"] = str(exc)

            thread = threading.Thread(target=person, daemon=True)
            thread.start()
            assert _blocked_on_a_lock(migrated_engine)
            first.commit()
            thread.join(30)
        finally:
            first.close()
        assert "matched" not in outcome
        assert "securities trade" in str(outcome.get("refused")), outcome

    def test_a_purge_and_a_sale_on_one_account_exclude_each_other(
        self, clean_db: None, migrated_engine: Engine
    ) -> None:
        """A sale linking a feed row is in flight; the purge of that account
        waits on the account lock, then sees the committed link and refuses."""
        import threading

        from purser import db
        from purser.admin import feed_repair

        maker = db.get_sessionmaker()
        ids = self._setup(maker, fetched=True)
        first = maker()
        outcome: dict[str, object] = {}
        try:
            self._sell(first, ids)

            def admin() -> None:
                with maker() as session:
                    try:
                        account = session.get(Account, ids["account"])
                        feed_repair.execute_purge(session, account, since=None, actor_token_id=None)
                        session.commit()
                        outcome["purged"] = True
                    except feed_repair.RepairRefused as exc:
                        session.rollback()
                        outcome["refused"] = str(exc)

            thread = threading.Thread(target=admin, daemon=True)
            thread.start()
            assert _blocked_on_a_lock(migrated_engine)
            first.commit()
            thread.join(30)
        finally:
            first.close()
        assert "purged" not in outcome
        assert str(ids["sold"]) in str(outcome.get("refused")), outcome
