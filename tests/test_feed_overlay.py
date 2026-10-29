"""Zero-valued feed funds valued from stored prices (issue #392, decision 92).

A plan's feed can report a held fund at market value 0 (a plan-only fund no
provider prices). The readers value such a FEED row (quantity > 0, market
value exactly 0) as quantity x the stored close in effect on the row's date,
never rewrite it, and report it unpriced (no value, never a silent 0) until
a close exists. Synthetic tickers and amounts only; Decimal throughout.
"""

import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from purser import db, networth
from purser.holdings import prices
from purser.models import Account, AuditLog, Event, HoldingsSnapshot, SecurityPrice
from tests.test_mcp import LiveServer, _mcp_raw, live_server, seeded_tokens  # noqa: F401

TODAY = datetime.now(UTC).date()


def _symbol(prefix: str = "F") -> str:
    return f"{prefix}{uuid.uuid4().hex[:6].upper()}"


def _feed_account(session: Session) -> Account:
    from purser import ledger

    account = ledger.create_account(
        session,
        name="Plan",
        type_="brokerage",
        path=f"assets.plan_{uuid.uuid4().hex[:10]}",
        actor_token_id=None,
    )
    session.flush()
    return account


def _row(
    session: Session,
    account_id: uuid.UUID,
    on: date,
    symbol: str,
    quantity: str,
    market: str | None,
    basis: str | None = None,
) -> None:
    session.add(
        HoldingsSnapshot(
            account_id=account_id,
            as_of=on,
            symbol=symbol,
            quantity=Decimal(quantity),
            cost_basis=Decimal(basis) if basis is not None else None,
            market_value=Decimal(market) if market is not None else None,
            source="feed",
        )
    )
    session.flush()


def _close(session: Session, symbol: str, on: date, close: str, source: str = "tiingo") -> None:
    session.add(SecurityPrice(symbol=symbol, price_date=on, source=source, close=Decimal(close)))
    session.flush()


def _ago(days: int) -> date:
    return TODAY - timedelta(days=days)


class TestOverlay:
    def test_a_zero_valued_feed_row_is_valued_from_the_close_in_effect(
        self, db_session: Session
    ) -> None:
        """quantity x the close in effect on the row's own date, newest date
        first: every reader of a feed row's value agrees."""
        account = _feed_account(db_session)
        fund = _symbol()
        _close(db_session, fund, _ago(9), "23.10")
        _close(db_session, fund, _ago(2), "23.50")
        for days in (5, 1):
            _row(db_session, account.id, _ago(days), fund, "537.25", "0", basis="12700")
        older = networth.latest_holdings(db_session, account.id, _ago(4))
        newer = networth.latest_holdings(db_session, account.id, _ago(1))
        (old_row,), (new_row,) = older, newer
        assert old_row.market_value == Decimal("12410.4750")  # 537.25 x 23.10
        assert (old_row.price, old_row.price_as_of, old_row.price_source) == (
            Decimal("23.10"),
            _ago(9),
            "tiingo",
        )
        assert new_row.market_value == Decimal("12625.3750")  # 537.25 x 23.50
        assert new_row.price_as_of == _ago(2)
        # The sums every surface reads: the account addend, net worth and the
        # balance series.
        assert networth.holdings_addends(db_session, [account.id], as_of=_ago(1)) == {
            account.id: (Decimal("12625.3750"), _ago(1))
        }
        worth = networth.net_worth_at(db_session, _ago(1))
        (line,) = [b for b in worth["breakdown"] if b["account_id"] == account.id]
        assert line["value"] == Decimal("12625.3750") and line["method"] == "holdings"
        series, method = networth.account_balance_series(
            db_session, account, dates=[_ago(4), _ago(1)]
        )
        assert method == "postings+holdings"
        assert series == {_ago(4): Decimal("12410.4750"), _ago(1): Decimal("12625.3750")}

    def test_a_manual_close_outranks_a_provider_close_of_the_same_date(
        self, db_session: Session
    ) -> None:
        account = _feed_account(db_session)
        fund = _symbol()
        _close(db_session, fund, _ago(3), "20.00", source="tiingo")
        _close(db_session, fund, _ago(3), "21.00", source="manual")
        _row(db_session, account.id, _ago(1), fund, "10", "0")
        (row,) = networth.latest_holdings(db_session, account.id)
        assert row.market_value == Decimal("210.0000") and row.price_source == "manual"

    def test_a_non_zero_feed_value_is_never_overridden(self, db_session: Session) -> None:
        account = _feed_account(db_session)
        fund, cash = _symbol(), _symbol()
        _close(db_session, fund, _ago(3), "99.99")
        _row(db_session, account.id, _ago(1), fund, "10", "100.00")
        _row(db_session, account.id, _ago(1), cash, "0", "0")  # holds nothing: not a fund at 0
        # The feed's own 0-share row stays visible (#390 round 3) and is not
        # overlaid: it holds nothing.
        rows = {h.symbol: h for h in networth.latest_holdings(db_session, account.id)}
        row, zero = rows[fund], rows[cash]
        assert row.market_value == Decimal("100.0000") and row.price is None
        assert row.price_source is None and row.unpriced is False
        assert zero.market_value == 0 and zero.price is None and zero.unpriced is False
        assert networth.holdings_addends(db_session, [account.id])[account.id][0] == Decimal(
            "100.0000"
        )

    def test_a_feed_implied_close_does_not_value_a_zero_row(self, db_session: Session) -> None:
        """Another account's feed row of the symbol implies a close; a zero
        row is valued from STORED closes only, so it stays unpriced."""
        fund = _symbol()
        valued, zero = _feed_account(db_session), _feed_account(db_session)
        _row(db_session, valued.id, _ago(2), fund, "10", "250.00")
        _row(db_session, zero.id, _ago(1), fund, "4", "0")
        (row,) = networth.latest_holdings(db_session, zero.id)
        assert row.market_value is None and row.unpriced is True
        assert networth.holdings_addends(db_session, [zero.id]) == {}

    def test_an_unpriced_zero_row_has_no_value_never_a_silent_zero(
        self, db_session: Session
    ) -> None:
        account = _feed_account(db_session)
        _row(db_session, account.id, _ago(1), _symbol(), "548.1", "0")
        (row,) = networth.latest_holdings(db_session, account.id)
        assert row.market_value is None and row.unpriced is True and row.price is None

    def test_a_value_that_does_not_fit_keeps_the_close_and_is_not_unpriced(
        self, db_session: Session
    ) -> None:
        """No value, but a close (value_out_of_range, #390 round 3): the share
        count is what to check, so it is not reported as unpriced."""
        account = _feed_account(db_session)
        fund = _symbol()
        _close(db_session, fund, _ago(3), "20000000")  # 2e7 a share
        _row(db_session, account.id, _ago(1), fund, "99999999999999", "0")  # 1e14 shares
        (row,) = networth.latest_holdings(db_session, account.id)
        assert row.market_value is None and row.unpriced is False
        assert row.price == Decimal("20000000")

    def test_the_stored_feed_rows_are_untouched(self, db_session: Session) -> None:
        """A read-side overlay: every stored column of the feed rows is the
        same after every reader ran and a close was recorded."""
        account = _feed_account(db_session)
        fund = _symbol()
        for days in (3, 2, 1):
            _row(db_session, account.id, _ago(days), fund, "537.25", "0", basis="12700")

        def stored() -> list[tuple]:
            return [
                tuple(r)
                for r in db_session.execute(
                    text(
                        "SELECT id, account_id, as_of, symbol, quantity, cost_basis,"
                        " market_value, lot_id, raw_fetch_id, source, position_id, price,"
                        " price_as_of FROM holdings_snapshots WHERE account_id = :a ORDER BY id"
                    ),
                    {"a": account.id},
                )
            ]

        before = stored()
        prices.record_price(
            db_session, symbol=fund, price_date=_ago(3), close=Decimal("23.40"), token=None
        )
        (held,) = networth.latest_holdings(db_session, account.id)
        assert held.market_value == Decimal("12571.6500")  # valued on read...
        assert networth.holdings_addends(db_session, [account.id])[account.id][0] == (
            held.market_value
        )
        networth.net_worth_at(db_session, _ago(1))
        networth.account_balance_series(db_session, account, dates=[_ago(2), _ago(1)])
        db_session.execute(text("SELECT * FROM readmodel.v1_holdings_latest")).all()
        db_session.flush()
        assert stored() == before  # ...and never written back


class TestHealing:
    def test_net_worth_daily_heals_when_a_backdated_close_lands(
        self, clean_db: None, migrated_engine: Engine
    ) -> None:
        fund = _symbol()
        with db.get_sessionmaker()() as session:
            account = _feed_account(session)
            account_id = account.id
            for days in range(10, -1, -1):  # the feed syncs daily
                _row(session, account_id, _ago(days), fund, "10", "0")
            for days in range(10, -1, -1):
                networth.materialize_day(session, _ago(days))
            session.commit()

        def stored(on: date) -> Decimal:
            with migrated_engine.connect() as conn:
                return Decimal(
                    conn.execute(
                        text("SELECT net_worth FROM net_worth_daily WHERE as_of = :d"), {"d": on}
                    ).scalar_one()
                )

        assert stored(_ago(5)) == 0  # unpriced: adds nothing
        with db.get_sessionmaker()() as session:
            recorded = prices.record_price(
                session, symbol=fund, price_date=_ago(8), close=Decimal("50"), token=None
            )
            session.commit()
        assert recorded.feed_accounts == 1
        networth.consume_watermark()
        assert stored(_ago(9)) == 0  # before the close: still unpriced
        for days in (8, 5, 0):
            assert stored(_ago(days)) == Decimal("500.0000")
        with db.get_sessionmaker()() as session:
            (audit,) = session.execute(
                select(AuditLog).where(AuditLog.action == "price.record")
            ).scalars()
            assert audit.before_after["after"]["feed_accounts_revalued"] == 1

    def test_a_close_no_zero_valued_row_needs_marks_nothing(
        self, clean_db: None, migrated_engine: Engine
    ) -> None:
        fund = _symbol()
        with db.get_sessionmaker()() as session:
            account = _feed_account(session)
            _row(session, account.id, _ago(3), fund, "10", "100.00")  # valued by the feed
            session.commit()
        with db.get_sessionmaker()() as session:
            recorded = prices.record_price(
                session, symbol=fund, price_date=_ago(5), close=Decimal("9"), token=None
            )
            session.commit()
        assert recorded.feed_accounts == 0
        with migrated_engine.connect() as conn:
            assert (
                conn.execute(text("SELECT networth_dirty_from FROM balance_watermark")).scalar()
                is None
            )


class TestUnpricedEvent:
    @staticmethod
    def _events() -> list[Event]:
        with db.get_sessionmaker()() as session:
            return list(
                session.execute(
                    select(Event).where(Event.type == "holding.unpriced").order_by(Event.at)
                ).scalars()
            )

    def test_one_line_per_account_and_symbol_on_the_transition(self, clean_db: None) -> None:
        from purser.digest import whats_new

        fund = _symbol()
        with db.get_sessionmaker()() as session:
            account = _feed_account(session)
            path = account.path
            _row(session, account.id, _ago(1), fund, "548.1", "0")
            session.commit()
        prices.run_fetch(today=TODAY)
        prices.run_fetch(today=TODAY)  # the same state: no second line
        (event,) = self._events()
        assert event.payload == {
            "account": path,
            "symbol": fund,
            "quantity": "548.100000",
            "as_of": _ago(1).isoformat(),
            "unpriceable": False,
        }
        with db.get_sessionmaker()() as session:
            digest = whats_new(session)
        assert digest["unpriced_holdings"] == [
            {
                "account": path,
                "symbol": fund,
                "quantity": "548.100000",
                "as_of": _ago(1).isoformat(),
            }
        ]

        # Priced: nothing to report. A newer set whose value at that close
        # does not fit an amount is value_out_of_range, not unpriced: the
        # share count is what to check, so no new line.
        with db.get_sessionmaker()() as session:
            prices.record_price(
                session, symbol=fund, price_date=_ago(1), close=Decimal("20000000"), token=None
            )
            session.commit()
        prices.run_fetch(today=TODAY)
        assert len(self._events()) == 1
        with db.get_sessionmaker()() as session:
            account_row = session.execute(select(Account).where(Account.path == path)).scalar_one()
            _row(session, account_row.id, TODAY, fund, "99999999999999", "0")
            session.commit()
        prices.run_fetch(today=TODAY)
        assert len(self._events()) == 1

    def test_overlapping_runs_emit_once(self, clean_db: None) -> None:
        """#405: two runs that overlap cannot both emit the line."""
        from purser import events

        first = db.get_sessionmaker()()
        try:
            assert events.emit_once(
                first, type_="holding.unpriced", dedup_key="race", payload={"symbol": "X"}
            )
            results: list[bool] = []

            def second() -> None:
                with db.get_sessionmaker()() as session:
                    results.append(
                        events.emit_once(
                            session,
                            type_="holding.unpriced",
                            dedup_key="race",
                            payload={"symbol": "X"},
                        )
                    )
                    session.commit()

            thread = threading.Thread(target=second)
            thread.start()
            thread.join(timeout=2)
            assert thread.is_alive()  # it waits for the first run to commit
            first.commit()
            thread.join(timeout=30)
            assert results == [False]
        finally:
            first.close()
        assert len(self._events()) == 1


class TestListHoldings:
    def test_an_overlaid_row_shows_its_close_and_an_unpriced_one_counts_as_no_price(
        self,
        seeded_tokens: dict[str, str],  # noqa: F811  (the fixture imported above)
        live_server: LiveServer,  # noqa: F811
        admin_client: TestClient,
    ) -> None:
        fund, tiny = _symbol(), _symbol()
        with db.get_sessionmaker()() as session:
            account = _feed_account(session)
            path = account.path
            _row(session, account.id, _ago(1), fund, "537.25", "0", basis="12700")
            _row(session, account.id, _ago(1), tiny, "1.5", "0")
            session.commit()
        with db.get_sessionmaker()() as session:
            prices.record_price(
                session, symbol=fund, price_date=_ago(10), close=Decimal("23.40"), token=None
            )
            session.commit()
        result = _mcp_raw(
            live_server.url, seeded_tokens["reader"], "list_holdings", {"account": path}
        )
        assert not result.is_error
        (entry,) = result.structured_content["accounts"]
        assert entry["source"] == "feed" and entry["no_price"] == 1 and entry["stale_price"] == 1
        lines = {line["symbol"]: line for line in entry["top"]}
        assert lines[fund]["market_value"] == "12571.65"  # 537.25 x 23.40
        assert lines[fund]["price"] == "23.4" and lines[fund]["price_source"] == "manual"
        assert lines[fund]["price_as_of"] == _ago(10).isoformat() and lines[fund]["stale"] is True
        assert lines[tiny]["market_value"] is None and "price" not in lines[tiny]
        assert "no price" in result.content[0].text
        # REST says the same.
        rows = {
            h["symbol"]: h for h in admin_client.get("/holdings", params={"account": path}).json()
        }
        assert Decimal(rows[fund]["market_value"]) == Decimal("12571.65")
        assert rows[fund]["price_source"] == "manual" and rows[tiny]["market_value"] is None


class TestView:
    def test_the_view_agrees_with_latest_holdings(self, db_session: Session) -> None:
        """Grafana's readmodel.v1_holdings_latest applies the same overlay:
        the same close, manual first on a same-date tie, the same half-even
        rounding, no value when unpriced or when the value does not fit."""
        account = _feed_account(db_session)
        valued, tie, kept, unpriced, huge = (_symbol(p) for p in "VTKUH")
        _close(db_session, valued, _ago(4), "11.000000", source="tiingo")
        _close(db_session, valued, _ago(4), "12.500000", source="manual")
        _close(db_session, tie, _ago(4), "2.000050")  # 1 x 2.00005 rounds half to even: 2.0000
        _close(db_session, kept, _ago(4), "99")
        _close(db_session, huge, _ago(4), "20000000")
        on = _ago(1)
        _row(db_session, account.id, on, valued, "3.123457", "0")
        _row(db_session, account.id, on, tie, "1", "0")
        _row(db_session, account.id, on, kept, "2", "50.00")
        _row(db_session, account.id, on, unpriced, "7", "0")
        _row(db_session, account.id, on, huge, "99999999999999", "0")
        held = {h.symbol: h.market_value for h in networth.latest_holdings(db_session, account.id)}
        view = dict(
            db_session.execute(
                text(
                    "SELECT symbol, market_value FROM readmodel.v1_holdings_latest"
                    " WHERE account_path = :p"
                ),
                {"p": account.path},
            ).all()
        )
        assert view == held
        assert held[valued] == Decimal("39.0432")  # 3.123457 x 12.5 = 39.0432125
        assert held[tie] == Decimal("2.0000")
        assert held[kept] == Decimal("50.0000")
        assert held[unpriced] is None and held[huge] is None


# ---------------------------------------------------------------------------
# #392 review round: NULL values, symbol spelling, the view's shape, the
# single implementation, staleness, events and round trips
# ---------------------------------------------------------------------------


@contextmanager
def _statements(session: Session) -> Iterator[list[str]]:
    """Every SQL statement the session's engine runs inside the block."""
    from sqlalchemy import event

    engine = session.get_bind()
    seen: list[str] = []

    def record(_conn, _cursor, statement, _params, _context, _many) -> None:  # type: ignore[no-untyped-def]
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", record)


class TestNullValue:
    def test_a_feed_row_with_no_value_at_all_is_valued_too(self, db_session: Session) -> None:
        """The importer stores a numeric 0 or a missing value as NULL: such
        a row is a fund the feed cannot value, like an explicit 0."""
        account = _feed_account(db_session)
        fund, bare = _symbol(), _symbol()
        _close(db_session, fund, _ago(3), "10.000000", source="manual")
        _row(db_session, account.id, _ago(1), fund, "4", None)
        _row(db_session, account.id, _ago(1), bare, "2", None)
        held = {h.symbol: h for h in networth.latest_holdings(db_session, account.id)}
        assert held[fund].market_value == Decimal("40.0000") and held[fund].price_source == "manual"
        assert held[bare].market_value is None and held[bare].unpriced is True
        assert networth.holdings_addends(db_session, [account.id])[account.id][0] == Decimal(
            "40.0000"
        )


class TestSymbolSpelling:
    def test_a_feed_symbol_matches_the_ticker_record_price_stores(
        self, db_session: Session
    ) -> None:
        account = _feed_account(db_session)
        ticker = _symbol()
        _close(db_session, ticker, _ago(3), "5.000000", source="manual")
        _row(db_session, account.id, _ago(1), f" {ticker.lower()} ", "3", "0")
        (held,) = networth.latest_holdings(db_session, account.id)
        assert held.market_value == Decimal("15.0000") and held.unpriceable is False
        assert networth.holdings_addends(db_session, [account.id])[account.id][0] == Decimal(
            "15.0000"
        )
        view = db_session.execute(
            text("SELECT market_value FROM readmodel.v1_holdings_latest WHERE account_path = :p"),
            {"p": account.path},
        ).scalar_one()
        assert view == Decimal("15.0000")

    def test_a_description_is_unpriceable_and_never_sent_to_record_price(
        self,
        seeded_tokens: dict[str, str],  # noqa: F811
        live_server: LiveServer,  # noqa: F811
        admin_client: TestClient,
    ) -> None:
        from purser.digest import whats_new

        description = f"Stable Value Trust {uuid.uuid4().hex[:4]}"
        with db.get_sessionmaker()() as session:
            account = _feed_account(session)
            path = account.path
            _row(session, account.id, _ago(1), description, "12.5", "0")
            session.commit()
        prices.run_fetch(today=TODAY)
        with db.get_sessionmaker()() as session:
            (event,) = [
                e
                for e in session.execute(
                    select(Event).where(Event.type == "holding.unpriced")
                ).scalars()
                if e.payload["symbol"] == description
            ]
            digest = whats_new(session)
        assert event.payload["unpriceable"] is True
        (line,) = [u for u in digest["unpriced_holdings"] if u["symbol"] == description]
        assert line["unpriceable"] is True
        result = _mcp_raw(
            live_server.url, seeded_tokens["reader"], "list_holdings", {"account": path}
        )
        (entry,) = result.structured_content["accounts"]
        assert entry["unpriceable"] == 1 and entry["no_price"] == 0
        (row,) = entry["top"]
        assert row["unpriceable"] is True
        assert "record_price" not in result.content[0].text
        assert "no ticker" in result.content[0].text
        news = _mcp_raw(live_server.url, seeded_tokens["reader"], "whats_new", {})
        assert "no ticker" in news.content[0].text
        (rest,) = admin_client.get("/holdings", params={"account": path}).json()
        assert rest["unpriceable"] is True and rest["market_value"] is None


class TestViewShape:
    def test_the_price_lookup_runs_once_per_returned_row_not_per_history_row(
        self, db_session: Session
    ) -> None:
        """30 more days of history of one symbol add no lookup: the LATERAL
        runs after DISTINCT ON."""
        import json

        account = _feed_account(db_session)
        fund = _symbol()
        _close(db_session, fund, _ago(60), "7.000000")
        _row(db_session, account.id, _ago(1), fund, "2", "0")

        def loops() -> int:
            plan = db_session.execute(
                text("EXPLAIN (ANALYZE, FORMAT JSON) SELECT * FROM readmodel.v1_holdings_latest")
            ).scalar_one()
            plan = plan if isinstance(plan, list) else json.loads(plan)
            total = 0

            def walk(node: dict) -> None:
                nonlocal total
                if node.get("Relation Name") == "security_prices":
                    total += node.get("Actual Loops", 0)
                for child in node.get("Plans", []):
                    walk(child)

            walk(plan[0]["Plan"])
            return total

        before = loops()
        for days in range(2, 32):
            _row(db_session, account.id, _ago(days), fund, "2", "0")
        assert loops() == before


class TestOneImplementation:
    def test_every_reader_agrees_on_the_same_rows(self, db_session: Session) -> None:
        """latest_holdings (Python, as_held), holdings_addends (SQL folded
        into its aggregate), the balance series (overlay_totals) and the view
        give the same values for NULL, 0, lower-case, out-of-range and
        unpriceable rows."""
        account = _feed_account(db_session)
        priced, lower, huge = _symbol("P"), _symbol("L"), _symbol("H")
        _close(db_session, priced, _ago(4), "3.333333", source="manual")
        _close(db_session, lower, _ago(4), "1.000050")
        _close(db_session, huge, _ago(4), "20000000")
        on = _ago(1)
        _row(db_session, account.id, on, priced, "3.000001", None)
        _row(db_session, account.id, on, lower.lower(), "1", "0")
        _row(db_session, account.id, on, huge, "99999999999999", "0")
        _row(db_session, account.id, on, "Some Description Fund", "4", "0")
        _row(db_session, account.id, on, _symbol("K"), "2", "50.00")
        held = {h.symbol: h.market_value for h in networth.latest_holdings(db_session, account.id)}
        total = sum((v for v in held.values() if v is not None), Decimal(0))
        assert networth.holdings_addends(db_session, [account.id])[account.id][0] == total
        view = dict(
            db_session.execute(
                text(
                    "SELECT symbol, market_value FROM readmodel.v1_holdings_latest"
                    " WHERE account_path = :p"
                ),
                {"p": account.path},
            ).all()
        )
        assert view == held
        series, _method = networth.account_balance_series(db_session, account, dates=[on])
        assert series[on] == total  # no postings: the holdings alone


class TestStaleOverlay:
    def test_a_lapsed_price_of_an_overlaid_fund_turns_stale(
        self, clean_db: None, admin_client: TestClient
    ) -> None:
        from purser.digest import whats_new

        fund = _symbol()
        with db.get_sessionmaker()() as session:
            account = _feed_account(session)
            _row(session, account.id, _ago(1), fund, "10", "0")
            session.commit()
        with db.get_sessionmaker()() as session:
            prices.record_price(
                session, symbol=fund, price_date=_ago(30), close=Decimal("23.4"), token=None
            )
            session.commit()
        result = prices.run_fetch(today=TODAY)
        assert {"symbol": fund, "price_as_of": _ago(30).isoformat()} in result.stale
        status = admin_client.get("/prices/status").json()
        (row,) = [r for r in status["symbols"] if r["symbol"] == fund]
        assert row["stale"] is True
        with db.get_sessionmaker()() as session:
            digest = whats_new(session)
        assert any(line["symbol"] == fund for line in digest["stale_prices"])


class TestEventsOnce:
    def test_overlapping_runs_emit_price_stale_and_fetch_failed_once(self, clean_db: None) -> None:
        """#405: price.stale and price.fetch_failed go through emit_once."""
        from purser.holdings.providers import PriceFetchError

        symbol = _symbol()
        first = db.get_sessionmaker()()
        try:
            assert prices.emit_stale(first, [(symbol, None)], stale_days=7) == 1
            prices._fetch_failed(first, "tiingo", PriceFetchError(symbol, 500, "refused"), TODAY)
            results: list[int] = []

            def second() -> None:
                with db.get_sessionmaker()() as session:
                    results.append(prices.emit_stale(session, [(symbol, None)], stale_days=7))
                    prices._fetch_failed(
                        session, "tiingo", PriceFetchError(symbol, 500, "refused"), TODAY
                    )
                    session.commit()

            thread = threading.Thread(target=second)
            thread.start()
            thread.join(timeout=2)
            assert thread.is_alive()  # waits for the first run to commit
            first.commit()
            thread.join(timeout=30)
            assert results == [0]
        finally:
            first.close()
        with db.get_sessionmaker()() as session:
            kinds = [
                e.type
                for e in session.execute(select(Event)).scalars()
                if (e.payload or {}).get("symbol") == symbol
            ]
        assert sorted(kinds) == ["price.fetch_failed", "price.stale"]


class TestRoundTrips:
    def test_holdings_addends_is_one_statement(self, db_session: Session) -> None:
        """The overlay folds into the aggregate: a day of a 365-day backfill
        costs one statement, overlaid rows or not."""
        account = _feed_account(db_session)
        fund = _symbol()
        _close(db_session, fund, _ago(3), "2.000000")
        _row(db_session, account.id, _ago(1), fund, "5", "0")
        with _statements(db_session) as seen:
            addend = networth.holdings_addends(db_session, [account.id])[account.id][0]
        assert addend == Decimal("10.0000")
        assert len(seen) == 1, seen

    def test_unpriced_holdings_reads_security_prices_once(self, db_session: Session) -> None:
        from purser.holdings import overlay

        account = _feed_account(db_session)
        fund, other = _symbol(), _symbol()
        _close(db_session, other, _ago(3), "2.000000")
        _row(db_session, account.id, _ago(1), fund, "5", "0")
        _row(db_session, account.id, _ago(1), other, "5", "0")
        with _statements(db_session) as seen:
            unpriced = overlay.unpriced_holdings(db_session, today=TODAY)
        assert [u.symbol for u in unpriced if u.account_id == account.id] == [fund]
        assert len([q for q in seen if "FROM security_prices" in q]) == 1, seen

    def test_the_fetch_finds_the_revalued_date_in_one_statement(self, db_session: Session) -> None:
        """overlay.first_revalued: one statement for every symbol that got
        closes, matched on the normalized symbol."""
        from purser.holdings import overlay

        account = _feed_account(db_session)
        held, idle = _symbol(), _symbol()
        _row(db_session, account.id, _ago(5), held.lower(), "5", "0")
        with _statements(db_session) as seen:
            first = overlay.first_revalued(db_session, {held: _ago(9), idle: _ago(20)})
        assert first == _ago(9) and len(seen) == 1
        assert overlay.first_revalued(db_session, {held: _ago(2)}) is None


class TestGrafana:
    def test_the_holdings_panel_lists_unvalued_rows_last(self, db_session: Session) -> None:
        import json
        from pathlib import Path

        dashboard = json.loads(Path("grafana/dashboards/net-worth.json").read_text())
        (sql,) = [
            target["rawSql"]
            for panel in dashboard["panels"]
            for target in panel.get("targets", [])
            if "v1_holdings_latest" in target.get("rawSql", "")
        ]
        account = _feed_account(db_session)
        _row(db_session, account.id, _ago(1), _symbol(), "5", "0")  # no close: NULL
        _row(db_session, account.id, _ago(1), _symbol(), "2", "50.00")
        values = [r.market_value for r in db_session.execute(text(sql))]
        nulls = [i for i, v in enumerate(values) if v is None]
        assert nulls and nulls[0] > max(i for i, v in enumerate(values) if v is not None)


class TestOpenPeriodEvent:
    """#422 item 2: an account switched back to the feed whose manual period
    no fetch ever ends (the connection fails, or the bank stopped listing
    the account) kept counting its frozen hand-tracked set with nothing to
    say so. The daily price job now flags it once, in events and whats_new."""

    def test_flagged_once_after_the_staleness_window(self, clean_db: None) -> None:
        from purser.digest import whats_new

        with db.get_sessionmaker()() as session:
            account = _feed_account(session)
            account.manual_period_open = True
            account.holdings_source_changed_at = datetime.now(UTC) - timedelta(days=10)
            path, account_id = account.path, account.id
            fresh = _feed_account(session)  # switched back yesterday: not yet
            fresh.manual_period_open = True
            fresh.holdings_source_changed_at = datetime.now(UTC) - timedelta(days=1)
            session.commit()
        prices.run_fetch(today=TODAY)
        prices.run_fetch(today=TODAY)  # the same state: no second line
        with db.get_sessionmaker()() as session:
            (event,) = session.execute(
                select(Event).where(Event.type == "holding.period_open")
            ).scalars()
            assert event.entity_id == account_id
            assert event.payload["account"] == path
            assert event.payload["since"] == (TODAY - timedelta(days=10)).isoformat()
            digest = whats_new(session, since_cursor=None)
        assert digest["open_manual_periods"] == [
            {"account": path, "since": (TODAY - timedelta(days=10)).isoformat()}
        ]
