"""Lock order between a feed import, a holdings-source switch and an account
close (issue #390 round 3, #413, #403): every writer takes the account row
before the balance watermark, so they serialize instead of racing or
deadlocking, and a date never carries both sources.

Two real sessions per test, one of them in a thread. Synthetic data only.
"""

import json
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from purser import balances, crypto, db, ledger
from purser.core import ledger as core_ledger
from purser.feed import importer
from purser.holdings import prices, projection
from purser.holdings import service as holdings
from purser.models import Account, ApiToken, Connection, RawFetch, User

TODAY = datetime.now(UTC).date()


def _feed_account(session: Session) -> tuple[Account, Connection]:
    connection = Connection(provider="simplefin")
    session.add(connection)
    session.flush()
    account = ledger.create_account(
        session,
        name="Plan",
        type_="brokerage",
        path=f"assets.plan_{uuid.uuid4().hex[:8]}",
        actor_token_id=None,
    )
    account.connection_id = connection.id
    account.external_id = "SF-PLAN"
    session.flush()
    return account, connection


def _fetch(session: Session, connection_id: uuid.UUID, holdings: list[dict]) -> RawFetch:
    payload = {
        "errors": [],
        "accounts": [
            {
                "id": "SF-PLAN",
                "name": "Plan",
                "currency": "USD",
                "balance": "0.00",
                "balance-date": int(datetime.now(UTC).timestamp()),
                "transactions": [],
                "holdings": holdings,
            }
        ],
    }
    raw = RawFetch(
        connection_id=connection_id,
        content_hash=f"lock-{uuid.uuid4().hex}",
        payload_enc=crypto.encrypt(json.dumps(payload, sort_keys=True)),
    )
    session.add(raw)
    session.flush()
    return raw


def _in_thread(work: Callable[[], None]) -> tuple[threading.Thread, list[BaseException]]:
    errors: list[BaseException] = []

    def run() -> None:
        try:
            work()
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    return thread, errors


def _sources_by_day(engine: Engine, account_id: uuid.UUID) -> dict[date, set[str]]:
    """Each date's sources (read from the rows, never from a module-level
    TODAY: a run can cross midnight UTC)."""
    out: dict[date, set[str]] = {}
    with engine.connect() as conn:
        for as_of, source in conn.execute(
            text("SELECT as_of, source FROM holdings_snapshots WHERE account_id = :a"),
            {"a": account_id},
        ):
            out.setdefault(as_of, set()).add(source)
    return out


ACME = [{"symbol": "ACME", "shares": "3", "market_value": "300.00"}]


class TestSyncAgainstASwitch:
    def test_a_switch_holding_the_account_makes_the_sync_wait(
        self, clean_db: None, migrated_engine: Engine
    ) -> None:
        """The switch to manual (with a count recorded today) holds the account
        when the sync starts: the import waits, then finds the account manual
        and adds no feed row beside the hand-tracked set."""
        with db.get_sessionmaker()() as session:
            account, connection = _feed_account(session)
            account_id, connection_id = account.id, connection.id
            session.commit()
        switch = db.get_sessionmaker()()
        try:
            ledger.update_account(
                switch, account_id=account_id, holdings_source="manual", actor_token_id=None
            )
            holdings.record_position(
                switch,
                account_ref=str(account_id),
                symbol="ACME",
                quantity=Decimal("3"),
                as_of=TODAY,
                token=None,
            )

            def sync() -> None:
                with db.get_sessionmaker()() as session:
                    stats = importer.import_fetch(session, _fetch(session, connection_id, ACME))
                    assert stats["holdings_skipped_manual"] == 1
                    session.commit()

            thread, errors = _in_thread(sync)
            thread.join(timeout=2)
            assert thread.is_alive()  # waiting for the account lock
            switch.commit()
            thread.join(timeout=60)
            assert errors == []
        finally:
            switch.close()
        by_day = _sources_by_day(migrated_engine, account_id)
        assert by_day and all(sources == {"manual"} for sources in by_day.values())

    def test_a_sync_holding_the_account_makes_the_switch_wait(
        self, clean_db: None, migrated_engine: Engine
    ) -> None:
        """The sync holds the account (and the watermark) when the admin
        switches to manual: the switch waits, then projects from the day after
        the sync's feed rows, so today keeps one source. No deadlock."""
        with db.get_sessionmaker()() as session:
            account, connection = _feed_account(session)
            account_id, connection_id = account.id, connection.id
            session.commit()
        sync = db.get_sessionmaker()()
        try:
            importer.import_fetch(sync, _fetch(sync, connection_id, ACME))

            def switch() -> None:
                with db.get_sessionmaker()() as session:
                    ledger.update_account(
                        session,
                        account_id=account_id,
                        holdings_source="manual",
                        actor_token_id=None,
                    )
                    holdings.record_position(
                        session,
                        account_ref=str(account_id),
                        symbol="ACME",
                        quantity=Decimal("4"),
                        as_of=TODAY - timedelta(days=2),
                        token=None,
                    )
                    session.commit()

            thread, errors = _in_thread(switch)
            thread.join(timeout=2)
            assert thread.is_alive()
            sync.commit()
            thread.join(timeout=60)
            assert errors == []
        finally:
            sync.close()
        by_day = _sources_by_day(migrated_engine, account_id)
        assert {"feed"} in by_day.values()
        assert all(len(sources) == 1 for sources in by_day.values())  # one source per date
        with migrated_engine.connect() as conn:
            assert (
                conn.execute(
                    text(
                        "SELECT count(*) FROM holdings_snapshots WHERE account_id = :a"
                        " GROUP BY as_of, symbol ORDER BY count(*) DESC LIMIT 1"
                    ),
                    {"a": account_id},
                ).scalar_one()
                == 1
            )

    def test_a_switch_committed_before_the_sync_is_seen_by_it(
        self, clean_db: None, migrated_engine: Engine
    ) -> None:
        with db.get_sessionmaker()() as session:
            account, connection = _feed_account(session)
            account_id, connection_id = account.id, connection.id
            session.commit()
        with db.get_sessionmaker()() as session:
            ledger.update_account(
                session, account_id=account_id, holdings_source="manual", actor_token_id=None
            )
            session.commit()
        with db.get_sessionmaker()() as session:
            stats = importer.import_fetch(session, _fetch(session, connection_id, ACME))
            session.commit()
        assert stats["holdings_skipped_manual"] == 1
        assert _sources_by_day(migrated_engine, account_id) == {}


class TestCloseAgainstAnImport:
    def test_close_takes_the_account_before_the_watermark(
        self, clean_db: None, migrated_engine: Engine
    ) -> None:
        """#403: an import holding the account, then marking the watermark,
        while the account is closed with a cash adjustment. The close waits
        for the account first, so neither side deadlocks."""
        suffix = uuid.uuid4().hex[:8]
        with db.get_sessionmaker()() as session:
            account, _connection = _feed_account(session)
            account_id = account.id
            user = User(email=f"close_{suffix}@example.com")
            session.add(user)
            session.flush()
            token = ApiToken(
                user_id=user.id, name=f"t_{suffix}", token_hash=uuid.uuid4().hex, scopes=["write"]
            )
            session.add(token)
            seed = ledger.ensure_category_path(session, "income.seed", actor_token_id=None)
            ledger.create_flat_transaction(
                session,
                account_ref=str(account_id),
                category_ref=str(seed.id),
                amount=Decimal("25.00"),
                date=TODAY,
                requested_status=core_ledger.Status.POSTED,
                token=None,
            )
            session.commit()
            token_id = token.id
        importing = db.get_sessionmaker()()
        try:
            projection.lock_accounts(importing, [account_id])  # the importer's first step

            def close() -> None:
                with db.get_sessionmaker()() as session:
                    closer = session.execute(
                        select(ApiToken).where(ApiToken.id == token_id)
                    ).scalar_one()
                    ledger.close_account(
                        session,
                        account_id=account_id,
                        adjustment_category="expenses.adjustments",
                        token=closer,
                    )
                    session.commit()

            thread, errors = _in_thread(close)
            thread.join(timeout=2)
            balances.mark_dirty(importing, TODAY, networth_only=True)  # then the watermark
            importing.commit()
            thread.join(timeout=60)
            assert errors == []
        finally:
            importing.close()
        with db.get_sessionmaker()() as session:
            assert session.get(Account, account_id).closed_at is not None  # type: ignore[union-attr]


def _manual_account(session: Session) -> uuid.UUID:
    account = ledger.create_account(
        session,
        name="Brokerage",
        type_="brokerage",
        path=f"assets.brokerage_{uuid.uuid4().hex[:8]}",
        actor_token_id=None,
    )
    ledger.update_account(
        session, account_id=account.id, holdings_source="manual", actor_token_id=None
    )
    return account.id


def _stored_values(
    engine: Engine, account_id: uuid.UUID, symbol: str
) -> dict[date, Decimal | None]:
    with engine.connect() as conn:
        return dict(
            conn.execute(
                text(
                    "SELECT as_of, market_value FROM holdings_snapshots"
                    " WHERE account_id = :a AND symbol = :s AND source = 'manual'"
                ),
                {"a": account_id, "s": symbol},
            ).all()
        )


class TestPriceAgainstANewHolder:
    """#415: a close recorded while another write starts holding the symbol
    on a manual account re-values that account too, whichever commits
    first. A per-symbol advisory lock serializes them: record_price takes it
    exclusively before it reads which accounts hold the symbol; a write that
    starts holding it takes it shared before it projects."""

    D = TODAY - timedelta(days=3)

    def test_a_price_recorded_while_a_position_is_uncommitted(
        self, clean_db: None, migrated_engine: Engine
    ) -> None:
        """The issue's repro: the position is recorded first and committed
        after the price. The price waits for it, then re-values it."""
        with db.get_sessionmaker()() as session:
            account_id = _manual_account(session)
            session.commit()
        writer = db.get_sessionmaker()()
        try:
            holdings.record_position(
                writer,
                account_ref=str(account_id),
                symbol="LOCKX",
                quantity=Decimal("4"),
                as_of=self.D,
                token=None,
            )

            def price() -> None:
                with db.get_sessionmaker()() as session:
                    recorded = prices.record_price(
                        session, symbol="LOCKX", price_date=self.D, close=Decimal("25"), token=None
                    )
                    assert recorded.accounts == 1
                    session.commit()

            thread, errors = _in_thread(price)
            thread.join(timeout=2)
            assert thread.is_alive()  # waiting for the symbol
            writer.commit()
            thread.join(timeout=60)
            assert errors == []
        finally:
            writer.close()
        values = _stored_values(migrated_engine, account_id, "LOCKX")
        assert values and all(v == Decimal("100.0000") for d, v in values.items() if d >= self.D)

    def test_a_position_recorded_while_a_price_is_uncommitted(
        self, clean_db: None, migrated_engine: Engine
    ) -> None:
        """The other order: the position waits for the price, then projects
        with it."""
        with db.get_sessionmaker()() as session:
            account_id = _manual_account(session)
            session.commit()
        pricer = db.get_sessionmaker()()
        try:
            prices.record_price(
                pricer, symbol="LOCKY", price_date=self.D, close=Decimal("25"), token=None
            )

            def position() -> None:
                with db.get_sessionmaker()() as session:
                    holdings.record_position(
                        session,
                        account_ref=str(account_id),
                        symbol="LOCKY",
                        quantity=Decimal("4"),
                        as_of=self.D,
                        token=None,
                    )
                    session.commit()

            thread, errors = _in_thread(position)
            thread.join(timeout=2)
            assert thread.is_alive()
            pricer.commit()
            thread.join(timeout=60)
            assert errors == []
        finally:
            pricer.close()
        values = _stored_values(migrated_engine, account_id, "LOCKY")
        assert values and all(v == Decimal("100.0000") for d, v in values.items() if d >= self.D)

    def test_a_price_recorded_while_an_account_switches_back_to_manual(
        self, clean_db: None, migrated_engine: Engine
    ) -> None:
        """A feed account that held the symbol in an earlier manual period
        starts holding it again when it switches back to manual."""
        with db.get_sessionmaker()() as session:
            account, _ = _feed_account(session)
            account_id = account.id
            ledger.update_account(
                session, account_id=account_id, holdings_source="manual", actor_token_id=None
            )
            holdings.record_position(
                session,
                account_ref=str(account_id),
                symbol="LOCKZ",
                quantity=Decimal("4"),
                as_of=self.D,
                token=None,
            )
            ledger.update_account(
                session, account_id=account_id, holdings_source="feed", actor_token_id=None
            )
            session.commit()
        switch = db.get_sessionmaker()()
        try:
            ledger.update_account(
                switch, account_id=account_id, holdings_source="manual", actor_token_id=None
            )

            def price() -> None:
                with db.get_sessionmaker()() as session:
                    prices.record_price(
                        session, symbol="LOCKZ", price_date=self.D, close=Decimal("25"), token=None
                    )
                    session.commit()

            thread, errors = _in_thread(price)
            thread.join(timeout=2)
            assert thread.is_alive()
            switch.commit()
            thread.join(timeout=60)
            assert errors == []
        finally:
            switch.close()
        values = _stored_values(migrated_engine, account_id, "LOCKZ")
        assert values and all(v == Decimal("100.0000") for d, v in values.items() if d >= self.D)


class TestMoveAgainstASync:
    def test_a_move_locks_its_subtree_in_id_order_first(
        self, clean_db: None, migrated_engine: Engine
    ) -> None:
        """#422 item 3: a sync locks its accounts in id order; a subtree move
        updated paths in whatever order the UPDATE met them, so the two could
        deadlock. The move now takes the subtree in id order first: while a
        sync holds the lowest id, the waiting move holds none of the others."""
        with db.get_sessionmaker()() as session:
            parent = ledger.create_account(
                session,
                name="Parent",
                type_="brokerage",
                path=f"assets.mv_{uuid.uuid4().hex[:8]}",
                actor_token_id=None,
            )
            children = [
                ledger.create_account(
                    session,
                    name=f"Child {i}",
                    type_="brokerage",
                    path=f"{parent.path}.c{i}",
                    actor_token_id=None,
                )
                for i in range(4)
            ]
            ids = sorted([parent.id, *(c.id for c in children)])
            parent_id, old_root = parent.id, parent.path.split(".")[0]
            session.commit()
        sync = db.get_sessionmaker()()
        try:
            projection.lock_account(sync, ids[0])

            def move() -> None:
                with db.get_sessionmaker()() as session:
                    ledger.move_account(
                        session,
                        account_id=parent_id,
                        path=f"{old_root}.moved_{uuid.uuid4().hex[:8]}",
                        actor_token_id=None,
                    )
                    session.commit()

            thread, errors = _in_thread(move)
            thread.join(timeout=2)
            assert thread.is_alive()
            with migrated_engine.connect() as probe:
                for other in ids[1:]:
                    probe.execute(
                        text("SELECT 1 FROM accounts WHERE id = :i FOR NO KEY UPDATE NOWAIT"),
                        {"i": other},
                    )
                probe.rollback()
            sync.commit()
            thread.join(timeout=60)
            assert errors == []
        finally:
            sync.close()


class TestMoreLockOrders:
    """#422 item 6: the two-session cases the #398 review found untested."""

    def test_delete_account_and_an_import_share_the_watermark_without_deadlock(
        self, clean_db: None, migrated_engine: Engine
    ) -> None:
        """Delete-account refuses a mapped account, so the contention with a
        sync is the balance watermark: the import holds it (and its own
        accounts) while the delete, holding the account it removes, marks it
        dirty. Account before watermark on both sides: the delete waits, then
        removes the account's rows. No deadlock."""
        from purser.admin import account_delete

        with db.get_sessionmaker()() as session:
            _, connection = _feed_account(session)
            connection_id = connection.id
            doomed_id = _manual_account(session)
            holdings.record_position(
                session,
                account_ref=str(doomed_id),
                symbol="ACME",
                quantity=Decimal("1"),
                as_of=TODAY - timedelta(days=2),
                token=None,
            )
            doomed_path = session.get(Account, doomed_id).path
            session.commit()
        sync = db.get_sessionmaker()()
        try:
            importer.import_fetch(sync, _fetch(sync, connection_id, ACME))

            def delete() -> None:
                with db.get_sessionmaker()() as session:
                    doomed = account_delete.resolve_and_check(session, doomed_path)
                    account_delete.execute_delete(session, doomed, actor_token_id=None)
                    session.commit()

            thread, errors = _in_thread(delete)
            thread.join(timeout=2)
            assert thread.is_alive(), errors  # waiting for the watermark
            sync.commit()
            thread.join(timeout=60)
            assert errors == []
        finally:
            sync.close()
        with migrated_engine.connect() as conn:
            for table, column in (("accounts", "id"), ("holdings_snapshots", "account_id")):
                assert (
                    conn.execute(
                        text(f"SELECT count(*) FROM {table} WHERE {column} = :a"),
                        {"a": doomed_id},
                    ).scalar_one()
                    == 0
                )

    def test_a_switch_back_to_manual_waits_for_the_import_ending_the_period(
        self, clean_db: None, migrated_engine: Engine
    ) -> None:
        """The #413 destructive case: back on the feed with the period open,
        a fetch is ending it (deleting manual rows from its date, writing its
        set) when the admin switches to manual again. The switch waits, then
        projects after the feed's rows: one source per date, and the
        hand-tracked history before the fetch survives."""
        held_on = TODAY - timedelta(days=5)
        with db.get_sessionmaker()() as session:
            account, connection = _feed_account(session)
            account_id, connection_id = account.id, connection.id
            ledger.update_account(
                session, account_id=account_id, holdings_source="manual", actor_token_id=None
            )
            holdings.record_position(
                session,
                account_ref=str(account_id),
                symbol="ACME",
                quantity=Decimal("2"),
                as_of=held_on,
                token=None,
            )
            ledger.update_account(
                session, account_id=account_id, holdings_source="feed", actor_token_id=None
            )
            session.commit()
        sync = db.get_sessionmaker()()
        try:
            importer.import_fetch(sync, _fetch(sync, connection_id, ACME))

            def switch() -> None:
                with db.get_sessionmaker()() as session:
                    ledger.update_account(
                        session,
                        account_id=account_id,
                        holdings_source="manual",
                        actor_token_id=None,
                    )
                    session.commit()

            thread, errors = _in_thread(switch)
            thread.join(timeout=2)
            assert thread.is_alive()
            sync.commit()
            thread.join(timeout=60)
            assert errors == []
        finally:
            sync.close()
        by_day = _sources_by_day(migrated_engine, account_id)
        assert all(len(sources) == 1 for sources in by_day.values())
        assert by_day[held_on] == {"manual"}
        assert {"feed"} in by_day.values()
