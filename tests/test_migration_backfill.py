"""Migration 0007 backfill: existing plaintext raw_fetches rows become
ciphertext, the plaintext column disappears, and append-only survives.

Runs against a scratch database in the session's Postgres container so the
suite-wide migrated-to-head database is never touched.
"""

import json
import os
import uuid
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import DBAPIError

import purser.config
from tests.conftest import TEST_ENCRYPTION_KEY

MARKER_PAYLOADS = [
    {"accounts": [{"id": "SF-1", "transactions": [{"description": "PLAINTEXT MARKER ONE"}]}]},
    {"accounts": [{"id": "SF-2", "transactions": [{"description": "PLAINTEXT MARKER TWO"}]}]},
    {"errors": ["MARKER ERROR"], "accounts": []},
]


@pytest.fixture
def scratch_db(database_url: str) -> Iterator[tuple[Config, sa.Engine]]:
    os.environ["PURSER_ENCRYPTION_KEY"] = TEST_ENCRYPTION_KEY
    purser.config.get_settings.cache_clear()

    admin = sa.create_engine(database_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        # WITH (FORCE) terminates any leaked backends (e.g. alembic's engine is
        # not disposed when a migration raises — as 0016's downgrade does).
        conn.execute(sa.text("DROP DATABASE IF EXISTS purser_backfill WITH (FORCE)"))
        conn.execute(sa.text("CREATE DATABASE purser_backfill"))
    admin.dispose()

    scratch_url = database_url.rsplit("/", 1)[0] + "/purser_backfill"
    root = Path(__file__).resolve().parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    cfg.set_main_option("sqlalchemy.url", scratch_url)

    engine = sa.create_engine(scratch_url)
    yield cfg, engine
    engine.dispose()


def _raw_fetch_columns(engine: sa.Engine) -> set[str]:
    with engine.connect() as conn:
        return set(
            conn.execute(
                sa.text(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_name = 'raw_fetches'"
                )
            ).scalars()
        )


def test_backfill_round_trips_and_keeps_append_only(
    scratch_db: tuple[Config, sa.Engine],
) -> None:
    from purser import crypto

    cfg, engine = scratch_db

    command.upgrade(cfg, "0006")
    with engine.begin() as conn:
        cid = conn.execute(
            sa.text("INSERT INTO connections (access_url_enc) VALUES (:e) RETURNING id"),
            {"e": b"pre-0007-plaintext-era"},
        ).scalar_one()
        for i, payload in enumerate(MARKER_PAYLOADS):
            conn.execute(
                sa.text(
                    "INSERT INTO raw_fetches (connection_id, content_hash, payload)"
                    " VALUES (:cid, :hash, CAST(:payload AS JSONB))"
                ),
                {"cid": cid, "hash": f"hash-{i}", "payload": json.dumps(payload)},
            )

    command.upgrade(cfg, "0015")

    cols = _raw_fetch_columns(engine)
    assert "payload" not in cols
    assert "payload_enc" in cols
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text("SELECT content_hash, payload_enc FROM raw_fetches ORDER BY content_hash")
        ).all()
        assert len(rows) == len(MARKER_PAYLOADS)
        for row, payload in zip(rows, MARKER_PAYLOADS, strict=True):
            assert b"MARKER" not in bytes(row.payload_enc)
            assert crypto.decrypt_json(row.payload_enc) == payload
        canary = conn.execute(sa.text("SELECT sentinel_enc FROM encryption_canary")).scalar_one()
        assert crypto.decrypt(canary) == crypto.CANARY_SENTINEL

    # The append-only trigger was re-enabled after the backfill.
    with pytest.raises(DBAPIError, match="append-only"), engine.begin() as conn:
        conn.execute(sa.text("UPDATE raw_fetches SET content_hash = 'tampered'"))

    # Downgrade mirrors: plaintext restored, encrypted column + canary gone.
    command.downgrade(cfg, "0006")
    cols = _raw_fetch_columns(engine)
    assert "payload" in cols
    assert "payload_enc" not in cols
    with engine.connect() as conn:
        restored = conn.execute(
            sa.text("SELECT payload FROM raw_fetches ORDER BY content_hash")
        ).scalars()
        assert list(restored) == MARKER_PAYLOADS
        tables = set(
            conn.execute(sa.text("SELECT table_name FROM information_schema.tables")).scalars()
        )
        assert "encryption_canary" not in tables


def _api_token_columns(engine: sa.Engine) -> set[str]:
    with engine.connect() as conn:
        return set(
            conn.execute(
                sa.text(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_name = 'api_tokens'"
                )
            ).scalars()
        )


def test_0010_purges_direct_connections(scratch_db: tuple[Config, sa.Engine]) -> None:
    """The purge removes provider='direct' connections and their captures,
    unlinks surviving ledger rows (posted transactions keep everything but
    their source_fetch_id; accounts lose only their connection binding),
    leaves simplefin rows untouched, and re-enables both guard triggers."""
    from purser import crypto

    cfg, engine = scratch_db

    command.upgrade(cfg, "0009")
    with engine.begin() as conn:
        keep_cid = conn.execute(
            sa.text("INSERT INTO connections (access_url_enc) VALUES (:e) RETURNING id"),
            {"e": crypto.encrypt("https://bridge.example.com/access")},
        ).scalar_one()
        keep_fid = conn.execute(
            sa.text(
                "INSERT INTO raw_fetches (connection_id, content_hash, payload_enc)"
                " VALUES (:cid, 'keep-hash', :p) RETURNING id"
            ),
            {"cid": keep_cid, "p": crypto.encrypt_json({"accounts": []})},
        ).scalar_one()

        direct_cid = conn.execute(
            sa.text(
                "INSERT INTO connections (provider, status)"
                " VALUES ('direct', 'disabled') RETURNING id"
            )
        ).scalar_one()
        direct_fid = conn.execute(
            sa.text(
                "INSERT INTO raw_fetches (connection_id, content_hash, payload_enc)"
                " VALUES (:cid, 'direct-hash', :p) RETURNING id"
            ),
            {"cid": direct_cid, "p": crypto.encrypt_json({"accounts": []})},
        ).scalar_one()

        acct = conn.execute(
            sa.text(
                "INSERT INTO accounts (name, type, path, connection_id)"
                " VALUES ('Direct Checking', 'checking', 'assets.direct_checking', :cid)"
                " RETURNING id"
            ),
            {"cid": direct_cid},
        ).scalar_one()
        counter = conn.execute(
            sa.text(
                "INSERT INTO accounts (name, type, path, is_virtual)"
                " VALUES ('Dining', 'category_expense', 'expenses.dining', true)"
                " RETURNING id"
            )
        ).scalar_one()
        tx = conn.execute(
            sa.text(
                "INSERT INTO transactions (date, description, status, origin, source_fetch_id)"
                " VALUES ('2026-07-07', 'direct-era import', 'posted', 'import', :fid)"
                " RETURNING id"
            ),
            {"fid": direct_fid},
        ).scalar_one()
        conn.execute(
            sa.text(
                "INSERT INTO postings (transaction_id, account_id, amount) VALUES"
                " (:tx, :a, '-12.3400'), (:tx, :b, '12.3400')"
            ),
            {"tx": tx, "a": acct, "b": counter},
        )
        conn.execute(
            sa.text(
                "INSERT INTO holdings_snapshots (account_id, as_of, symbol, raw_fetch_id)"
                " VALUES (:a, '2026-07-07', 'TEST', :fid)"
            ),
            {"a": acct, "fid": direct_fid},
        )

    command.upgrade(cfg, "0015")

    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT provider FROM connections")).scalars().all() == [
            "simplefin"
        ]
        assert conn.execute(sa.text("SELECT id FROM raw_fetches")).scalars().all() == [keep_fid]
        tx_row = conn.execute(
            sa.text("SELECT status, source_fetch_id FROM transactions WHERE id = :tx"),
            {"tx": tx},
        ).one()
        assert tx_row.status == "posted"
        assert tx_row.source_fetch_id is None
        assert conn.execute(sa.text("SELECT count(*) FROM postings")).scalar_one() == 2
        assert (
            conn.execute(
                sa.text("SELECT connection_id FROM accounts WHERE id = :a"), {"a": acct}
            ).scalar_one()
            is None
        )
        assert conn.execute(sa.text("SELECT count(*) FROM holdings_snapshots")).scalar_one() == 0

    # Both guards were re-enabled inside the purge transaction.
    with pytest.raises(DBAPIError, match="append-only"), engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM raw_fetches"))
    with pytest.raises(DBAPIError, match="immutable"), engine.begin() as conn:
        conn.execute(
            sa.text("UPDATE transactions SET source_fetch_id = :fid WHERE id = :tx"),
            {"fid": keep_fid, "tx": tx},
        )

    # Idempotent: 0010's downgrade is a no-op and re-running the purge on a
    # database with no direct rows does nothing.
    command.downgrade(cfg, "0009")
    command.upgrade(cfg, "0015")


def test_0009_token_kind_backfill(scratch_db: tuple[Config, sa.Engine]) -> None:
    """Pre-0009 rows: 'session:'-named tokens become kind='session' (with an
    expiry if they never had one); everything else becomes kind='api'."""
    cfg, engine = scratch_db

    command.upgrade(cfg, "0008")
    with engine.begin() as conn:
        uid = conn.execute(
            sa.text("INSERT INTO users (email) VALUES ('backfill@example.com') RETURNING id")
        ).scalar_one()
        conn.execute(
            sa.text(
                "INSERT INTO api_tokens (user_id, name, token_hash, scopes) VALUES"
                " (:uid, 'session:password:backfill@example.com', 'hash-session', '{read}'),"
                " (:uid, 'my-agent', 'hash-agent', '{read}')"
            ),
            {"uid": uid},
        )

    command.upgrade(cfg, "0015")
    with engine.connect() as conn:
        rows = {
            row.name: row
            for row in conn.execute(
                sa.text("SELECT name, kind, expires_at, created_at FROM api_tokens")
            )
        }
    legacy_session = rows["session:password:backfill@example.com"]
    assert legacy_session.kind == "session"
    # Legacy sessions minted with no expiry get one: the 7-day TTL they were
    # minted under, anchored to creation.
    assert legacy_session.expires_at == legacy_session.created_at + timedelta(days=7)
    assert rows["my-agent"].kind == "api"
    assert rows["my-agent"].expires_at is None

    command.downgrade(cfg, "0008")
    assert "kind" not in _api_token_columns(engine)


def _columns(engine: sa.Engine, table: str) -> set[str]:
    with engine.connect() as conn:
        return set(
            conn.execute(
                sa.text("SELECT column_name FROM information_schema.columns WHERE table_name = :t"),
                {"t": table},
            ).scalars()
        )


def test_0016_narrative_encryption_backfill(scratch_db: tuple[Config, sa.Engine]) -> None:
    """0016 on seeded plaintext: narrative columns encrypt and round-trip, the
    blind index / dedup_key / replacement_id skeletons populate, content_hash
    rehashes to keyed HMAC (injective, dedup-preserving), the flat view swaps to
    v2, the posted-immutability guards are restored, and downgrade refuses."""
    from datetime import date
    from decimal import Decimal

    from purser import crypto
    from purser.core import ledger as core

    cfg, engine = scratch_db
    command.upgrade(cfg, "0015")

    v1_hash = core.content_hash(
        external_account_id="SF-x",
        date=date(2024, 3, 31),
        amount=Decimal("-10.00"),
        description="WHOLE FOODS 123",
    )
    replacement = uuid.uuid4()
    with engine.begin() as conn:
        payee_id = conn.execute(
            sa.text(
                "INSERT INTO payees (name, normalized_name) "
                "VALUES ('Whole Foods #12', 'whole foods 12') RETURNING id"
            )
        ).scalar_one()
        acct = conn.execute(
            sa.text(
                "INSERT INTO accounts (name, type, path) "
                "VALUES ('Checking', 'checking', 'assets.checking') RETURNING id"
            )
        ).scalar_one()
        cat = conn.execute(
            sa.text(
                "INSERT INTO accounts (name, type, is_virtual, path) "
                "VALUES ('Dining', 'category_expense', true, 'expenses.dining') RETURNING id"
            )
        ).scalar_one()
        tx = conn.execute(
            sa.text(
                "INSERT INTO transactions"
                " (date, payee_id, description, status, origin, content_hash)"
                " VALUES ('2024-03-31', :p, 'WHOLE FOODS 123', 'posted', 'import', :h) RETURNING id"
            ),
            {"p": payee_id, "h": v1_hash},
        ).scalar_one()
        conn.execute(
            sa.text(
                "INSERT INTO postings (transaction_id, account_id, amount, memo) VALUES "
                "(:tx, :a, '-10.0000', 'card ending 1111'), (:tx, :c, '10.0000', NULL)"
            ),
            {"tx": tx, "a": acct, "c": cat},
        )
        sched = conn.execute(
            sa.text(
                "INSERT INTO schedules (name, account_id, rrule, amount_type, source) "
                "VALUES ('Netflix', :a, 'FREQ=MONTHLY', 'fixed', 'manual') RETURNING id"
            ),
            {"a": acct},
        ).scalar_one()
        audit_id = conn.execute(
            sa.text(
                "INSERT INTO audit_log (action, entity_type, entity_id, before_after, kind) "
                "VALUES ('transaction.categorize', 'transaction', :tx, CAST(:ba AS JSONB),"
                " 'change')"
                " RETURNING id"
            ),
            {
                "tx": tx,
                "ba": json.dumps(
                    {"before": None, "after": {"replacement_id": str(replacement), "source": "ai"}}
                ),
            },
        ).scalar_one()
        event_id = conn.execute(
            sa.text(
                "INSERT INTO events (type, payload) "
                "VALUES ('transfer.confirmed', CAST(:pl AS JSONB)) RETURNING id"
            ),
            {"pl": json.dumps({"pair_key": "aaa:bbb", "outgoing_id": str(tx)})},
        ).scalar_one()

    command.upgrade(cfg, "head")

    # Plaintext narrative columns are gone; encrypted + skeleton columns exist.
    payee_cols = _columns(engine, "payees")
    assert {"name", "normalized_name"}.isdisjoint(payee_cols)
    assert {"name_enc", "name_bidx"} <= payee_cols

    with engine.connect() as conn:
        # DEKs are per-database random material — unwrap THIS database's, don't
        # use crypto.blind_index/keyed_hash (those load via the app engine).
        bidx_dek = crypto.unwrap_key(
            conn.execute(
                sa.text("SELECT key_enc FROM encryption_keys WHERE purpose = 'blind_index'")
            ).scalar_one()
        )
        ch_dek = crypto.unwrap_key(
            conn.execute(
                sa.text("SELECT key_enc FROM encryption_keys WHERE purpose = 'content_hash'")
            ).scalar_one()
        )

        name_enc, name_bidx = conn.execute(
            sa.text("SELECT name_enc, name_bidx FROM payees WHERE id = :id"), {"id": payee_id}
        ).one()
        assert crypto.decrypt(name_enc) == "Whole Foods #12"
        assert name_bidx == crypto._hmac_hex(bidx_dek, "whole foods 12")

        desc_enc, stored_hash = conn.execute(
            sa.text("SELECT description_enc, content_hash FROM transactions WHERE id = :id"),
            {"id": tx},
        ).one()
        assert crypto.decrypt(desc_enc) == "WHOLE FOODS 123"
        # content_hash rehashed to keyed HMAC — a re-import computing keyed_hash
        # over the SAME identity dedups (idempotency preserved across migration).
        assert stored_hash == crypto._hmac_hex(ch_dek, v1_hash)
        assert stored_hash != v1_hash

        memo_enc = conn.execute(
            sa.text("SELECT memo_enc FROM postings WHERE memo_enc IS NOT NULL")
        ).scalar_one()
        assert crypto.decrypt(memo_enc) == "card ending 1111"  # posted-row backfill worked

        assert (
            crypto.decrypt(
                conn.execute(
                    sa.text("SELECT name_enc FROM schedules WHERE id = :id"), {"id": sched}
                ).scalar_one()
            )
            == "Netflix"
        )

        ba_enc, repl = conn.execute(
            sa.text("SELECT before_after_enc, replacement_id FROM audit_log WHERE id = :id"),
            {"id": audit_id},
        ).one()
        assert crypto.decrypt_json(ba_enc)["after"]["source"] == "ai"
        assert repl == replacement  # lifted out of before_after into its column

        pl_enc, dedup = conn.execute(
            sa.text("SELECT payload_enc, dedup_key FROM events WHERE id = :id"), {"id": event_id}
        ).one()
        assert crypto.decrypt_json(pl_enc)["pair_key"] == "aaa:bbb"
        assert dedup == "aaa:bbb"  # lifted out of payload into its column

        views = set(
            conn.execute(
                sa.text(
                    "SELECT table_name FROM information_schema.views"
                    " WHERE table_schema = 'readmodel'"
                )
            ).scalars()
        )
        assert "v1_transactions_flat" not in views
        assert "v2_transactions_flat" in views

    # The posted-immutability guard was re-enabled after the backfill window.
    with pytest.raises(DBAPIError, match="immutable"), engine.begin() as conn:
        conn.execute(sa.text("UPDATE transactions SET origin = 'agent' WHERE id = :id"), {"id": tx})

    # Downgrade is refused: content_hash is a one-way keyed HMAC.
    with pytest.raises(Exception, match="irreversible"):
        command.downgrade(cfg, "0015")


def test_0016_key_rotation_round_trip(scratch_db: tuple[Config, sa.Engine]) -> None:
    """`rotate()` re-wraps every ciphertext under a new primary key while leaving
    content_hash and name_bidx byte-identical (the wrapped-DEK payoff)."""
    import os

    from cryptography.fernet import Fernet
    from sqlalchemy.orm import Session

    from purser import crypto
    from purser.admin.rotate_key import rotate

    cfg, engine = scratch_db
    command.upgrade(cfg, "head")

    with engine.begin() as conn:
        payee_id = conn.execute(
            sa.text(
                "INSERT INTO payees (name_enc, name_bidx) VALUES (:e, 'bidx-const') RETURNING id"
            ),
            {"e": crypto.encrypt("Rotatable Payee")},
        ).scalar_one()
        # A bare posted row (no postings) is vacuously balanced; enough to carry
        # a content_hash through rotation.
        tx = conn.execute(
            sa.text(
                "INSERT INTO transactions (date, payee_id, status, origin, content_hash) "
                "VALUES ('2024-01-01', :p, 'posted', 'import', 'hash-const') RETURNING id"
            ),
            {"p": payee_id},
        ).scalar_one()

    with engine.connect() as conn:
        before_bidx = conn.execute(
            sa.text("SELECT name_bidx FROM payees WHERE id = :id"), {"id": payee_id}
        ).scalar_one()
        before_hash = conn.execute(
            sa.text("SELECT content_hash FROM transactions WHERE id = :id"), {"id": tx}
        ).scalar_one()
        wrapped_dek_before = conn.execute(
            sa.text("SELECT key_enc FROM encryption_keys WHERE purpose = 'blind_index'")
        ).scalar_one()
        dek_before = crypto.unwrap_key(wrapped_dek_before)

    new_key = Fernet.generate_key().decode()
    old_key = os.environ["PURSER_ENCRYPTION_KEY"]
    try:
        os.environ["PURSER_ENCRYPTION_KEY"] = f"{new_key},{old_key}"
        purser.config.get_settings.cache_clear()
        with Session(engine) as session:
            rotate(session)
            session.commit()

        # Under the NEW key alone, everything still decrypts.
        os.environ["PURSER_ENCRYPTION_KEY"] = new_key
        purser.config.get_settings.cache_clear()
        with engine.connect() as conn:
            name_enc, bidx = conn.execute(
                sa.text("SELECT name_enc, name_bidx FROM payees WHERE id = :id"), {"id": payee_id}
            ).one()
            assert crypto.decrypt(name_enc) == "Rotatable Payee"
            # HMAC skeletons are untouched by rotation.
            assert bidx == before_bidx
            after_hash = conn.execute(
                sa.text("SELECT content_hash FROM transactions WHERE id = :id"), {"id": tx}
            ).scalar_one()
            assert after_hash == before_hash
            # DEK plaintext unchanged (re-wrapped, not re-derived).
            wrapped_after = conn.execute(
                sa.text("SELECT key_enc FROM encryption_keys WHERE purpose = 'blind_index'")
            ).scalar_one()
            assert crypto.unwrap_key(wrapped_after) == dek_before
    finally:
        os.environ["PURSER_ENCRYPTION_KEY"] = TEST_ENCRYPTION_KEY
        purser.config.get_settings.cache_clear()


def test_rotation_rewraps_position_event_notes(scratch_db: tuple[Config, sa.Engine]) -> None:
    """`rotate()` covers the #390 ciphertext: a position event's note and
    source note read under the NEW key alone after a rotation, and the
    append-only guard is back on when it is done."""
    import os

    from cryptography.fernet import Fernet
    from sqlalchemy.orm import Session

    from purser import crypto
    from purser.admin.rotate_key import rotate

    cfg, engine = scratch_db
    command.upgrade(cfg, "head")
    with engine.begin() as conn:
        account = conn.execute(
            sa.text(
                "INSERT INTO accounts (name, type, path, holdings_source)"
                " VALUES ('Plan', 'brokerage', 'assets.plan', 'manual') RETURNING id"
            )
        ).scalar_one()
        position = conn.execute(
            sa.text(
                "INSERT INTO positions (account_id, symbol, opened_on)"
                " VALUES (:a, 'ACME', '2026-01-02') RETURNING id"
            ),
            {"a": account},
        ).scalar_one()
        event = conn.execute(
            sa.text(
                "INSERT INTO position_events"
                " (position_id, occurred_on, kind, quantity, origin, note_enc, source_note_enc)"
                " VALUES (:p, '2026-01-02', 'set_quantity', 3, 'manual', :n, :s) RETURNING id"
            ),
            {
                "p": position,
                "n": crypto.encrypt("opening count"),
                "s": crypto.encrypt("broker holdings page"),
            },
        ).scalar_one()

    new_key = Fernet.generate_key().decode()
    old_key = os.environ["PURSER_ENCRYPTION_KEY"]
    try:
        os.environ["PURSER_ENCRYPTION_KEY"] = f"{new_key},{old_key}"
        purser.config.get_settings.cache_clear()
        with Session(engine) as session:
            counts = rotate(session)
            session.commit()
        assert counts["position_events.note_enc"] == 1
        assert counts["position_events.source_note_enc"] == 1

        os.environ["PURSER_ENCRYPTION_KEY"] = new_key
        purser.config.get_settings.cache_clear()
        with engine.connect() as conn:
            note, source = conn.execute(
                sa.text("SELECT note_enc, source_note_enc FROM position_events WHERE id = :id"),
                {"id": event},
            ).one()
        assert crypto.decrypt(note) == "opening count"
        assert crypto.decrypt(source) == "broker holdings page"
        with pytest.raises(DBAPIError, match="append-only"), engine.begin() as conn:
            conn.execute(sa.text("UPDATE position_events SET origin = 'agent'"))
    finally:
        os.environ["PURSER_ENCRYPTION_KEY"] = old_key
        purser.config.get_settings.cache_clear()


def test_rotate_key_covers_every_encrypted_orm_column() -> None:
    """#414: the rotated columns are read from the ORM, so a new
    EncryptedText / EncryptedJSON column can never be left on the old key."""
    from purser import crypto
    from purser.admin.rotate_key import _FERNET_COLUMNS
    from purser.models import Base

    encrypted = {
        (table.name, column.name)
        for table in Base.metadata.sorted_tables
        for column in table.columns
        if isinstance(column.type, (crypto.EncryptedText, crypto.EncryptedJSON))
    }
    rotated = {(table, column) for table, column, _pk in _FERNET_COLUMNS}
    assert encrypted - rotated == set()
    # The DEK rows go last, after everything wrapped... nothing is wrapped by
    # them, but the order is the one the docs describe.
    assert _FERNET_COLUMNS[-1][:2] == ("encryption_keys", "key_enc")


def test_rotate_key_covers_every_ciphertext_column_in_the_schema(
    migrated_engine: sa.Engine,
) -> None:
    """Every *_enc BYTEA column the migrations create is rotated, except the
    canary, which is re-encrypted fresh (its fingerprint moves too)."""
    from purser.admin.rotate_key import _FERNET_COLUMNS

    with migrated_engine.connect() as conn:
        schema = {
            (row.table_name, row.column_name)
            for row in conn.execute(
                sa.text(
                    "SELECT table_name, column_name FROM information_schema.columns"
                    " WHERE table_schema = 'public' AND data_type = 'bytea'"
                    " AND column_name LIKE '%\\_enc' ESCAPE '\\'"
                )
            )
        }
    rotated = {(table, column) for table, column, _pk in _FERNET_COLUMNS}
    assert schema - rotated == {("encryption_canary", "sentinel_enc")}
    assert rotated - schema == set()


def test_rotation_rewraps_a_memory(scratch_db: tuple[Config, sa.Engine]) -> None:
    """One of the columns rotate-key used to miss (#414): a memory reads
    under the NEW key alone after a rotation."""
    import os

    from cryptography.fernet import Fernet
    from sqlalchemy.orm import Session

    from purser import crypto
    from purser.admin.rotate_key import rotate

    cfg, engine = scratch_db
    command.upgrade(cfg, "head")
    with engine.begin() as conn:
        memory = conn.execute(
            sa.text("INSERT INTO memories (text_enc) VALUES (:t) RETURNING id"),
            {"t": crypto.encrypt("rent is due on the 3rd")},
        ).scalar_one()
    new_key = Fernet.generate_key().decode()
    old_key = os.environ["PURSER_ENCRYPTION_KEY"]
    try:
        os.environ["PURSER_ENCRYPTION_KEY"] = f"{new_key},{old_key}"
        purser.config.get_settings.cache_clear()
        with Session(engine) as session:
            counts = rotate(session)
            session.commit()
        assert counts["memories.text_enc"] == 1
        os.environ["PURSER_ENCRYPTION_KEY"] = new_key
        purser.config.get_settings.cache_clear()
        with engine.connect() as conn:
            text_enc = conn.execute(
                sa.text("SELECT text_enc FROM memories WHERE id = :id"), {"id": memory}
            ).scalar_one()
        assert crypto.decrypt(bytes(text_enc)) == "rent is due on the 3rd"
    finally:
        os.environ["PURSER_ENCRYPTION_KEY"] = old_key
        purser.config.get_settings.cache_clear()
