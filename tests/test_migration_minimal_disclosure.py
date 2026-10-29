"""Migration 0038 (issue #437): the payee-reference DEK is seeded, the
audit_log.disclosed column added, and every read:redacted token converted to
read with an audit row.

Runs against a scratch database in the session's Postgres container so the
suite-wide migrated-to-head database is never touched.
"""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

import purser.config
from tests.conftest import TEST_ENCRYPTION_KEY


@pytest.fixture
def scratch_db(database_url: str) -> Iterator[tuple[Config, sa.Engine]]:
    os.environ["PURSER_ENCRYPTION_KEY"] = TEST_ENCRYPTION_KEY
    purser.config.get_settings.cache_clear()

    admin = sa.create_engine(database_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(sa.text("DROP DATABASE IF EXISTS purser_disclosure WITH (FORCE)"))
        conn.execute(sa.text("CREATE DATABASE purser_disclosure"))
    admin.dispose()

    scratch_url = database_url.rsplit("/", 1)[0] + "/purser_disclosure"
    root = Path(__file__).resolve().parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    cfg.set_main_option("sqlalchemy.url", scratch_url)

    engine = sa.create_engine(scratch_url)
    yield cfg, engine
    engine.dispose()


def test_redacted_tokens_become_read_and_the_key_is_seeded(
    scratch_db: tuple[Config, sa.Engine],
) -> None:
    from purser import crypto

    cfg, engine = scratch_db
    command.upgrade(cfg, "0037")
    with engine.begin() as conn:
        user_id = conn.execute(
            sa.text("INSERT INTO users (email) VALUES ('b@example.com') RETURNING id")
        ).scalar_one()
        for name, scopes in (
            ("redacted", ["read:redacted"]),
            ("both", ["read:redacted", "read", "write"]),
            ("plain", ["read", "write"]),
        ):
            conn.execute(
                sa.text(
                    "INSERT INTO api_tokens (user_id, name, token_hash, scopes) "
                    "VALUES (:u, :n, :h, :s)"
                ),
                {"u": user_id, "n": name, "h": f"hash-{name}", "s": scopes},
            )

    command.upgrade(cfg, "0038")
    with engine.connect() as conn:
        scopes = {
            n: list(s) for n, s in conn.execute(sa.text("SELECT name, scopes FROM api_tokens"))
        }
        audited = conn.execute(
            sa.text("SELECT before_after_enc FROM audit_log WHERE action = 'token.scopes_migrate'")
        ).scalars()
        payloads = [crypto.decrypt_json(row) for row in audited]
        key = conn.execute(
            sa.text("SELECT key_enc FROM encryption_keys WHERE purpose = 'payee_ref'")
        ).scalar_one()
        columns = set(
            conn.execute(
                sa.text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'audit_log'"
                )
            ).scalars()
        )
    assert scopes == {"redacted": ["read"], "both": ["read", "write"], "plain": ["read", "write"]}
    assert sorted(p["before"]["scopes"] for p in payloads) == [
        ["read:redacted"],
        ["read:redacted", "read", "write"],
    ]
    assert len(crypto.unwrap_key(key)) == 32
    assert "disclosed" in columns

    command.downgrade(cfg, "0037")
    with engine.connect() as conn:
        left = conn.execute(
            sa.text("SELECT count(*) FROM encryption_keys WHERE purpose = 'payee_ref'")
        ).scalar_one()
    assert left == 1  # kept: references must survive a re-upgrade
