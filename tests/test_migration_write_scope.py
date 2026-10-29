"""Migration 0037 (issue #436): every token holding propose or commit is
converted to write, in place and audited; other scopes are untouched, and the
downgrade restores propose + commit.

Runs against a scratch database in the session's Postgres container so the
suite-wide migrated-to-head database is never touched.
"""

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

import purser.config
from tests.conftest import TEST_ENCRYPTION_KEY

SCOPES_BEFORE = {
    "agent": ["read", "propose"],
    "committer": ["read", "propose", "commit"],
    "member_session": ["read", "propose", "commit"],
    "admin_session": ["read", "propose", "commit", "admin"],
    "reader": ["read"],
    "redacted": ["read:redacted", "propose"],
}
SCOPES_AFTER = {
    "agent": ["read", "write"],
    "committer": ["read", "write"],
    "member_session": ["read", "write"],
    "admin_session": ["read", "write", "admin"],
    "reader": ["read"],
    "redacted": ["read:redacted", "write"],
}


@pytest.fixture
def scratch_db(database_url: str) -> Iterator[tuple[Config, sa.Engine]]:
    os.environ["PURSER_ENCRYPTION_KEY"] = TEST_ENCRYPTION_KEY
    purser.config.get_settings.cache_clear()

    admin = sa.create_engine(database_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(sa.text("DROP DATABASE IF EXISTS purser_write_scope WITH (FORCE)"))
        conn.execute(sa.text("CREATE DATABASE purser_write_scope"))
    admin.dispose()

    scratch_url = database_url.rsplit("/", 1)[0] + "/purser_write_scope"
    root = Path(__file__).resolve().parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    cfg.set_main_option("sqlalchemy.url", scratch_url)

    engine = sa.create_engine(scratch_url)
    yield cfg, engine
    engine.dispose()


def _scopes(engine: sa.Engine) -> dict[str, list[str]]:
    with engine.connect() as conn:
        return {
            name: list(scopes)
            for name, scopes in conn.execute(sa.text("SELECT name, scopes FROM api_tokens"))
        }


def test_tokens_convert_to_write_audited_and_round_trip(
    scratch_db: tuple[Config, sa.Engine],
) -> None:
    from purser import crypto

    cfg, engine = scratch_db
    command.upgrade(cfg, "0036")
    ids: dict[str, uuid.UUID] = {}
    with engine.begin() as conn:
        user_id = conn.execute(
            sa.text(
                "INSERT INTO users (email, role) VALUES ('a@example.com', 'admin') RETURNING id"
            )
        ).scalar_one()
        for name, scopes in SCOPES_BEFORE.items():
            ids[name] = conn.execute(
                sa.text(
                    "INSERT INTO api_tokens (user_id, name, kind, token_hash, scopes) "
                    "VALUES (:u, :n, :k, :h, :s) RETURNING id"
                ),
                {
                    "u": user_id,
                    "n": name,
                    "k": "session" if name.endswith("_session") else "api",
                    "h": f"hash-{name}",
                    "s": scopes,
                },
            ).scalar_one()

    command.upgrade(cfg, "0037")
    assert _scopes(engine) == SCOPES_AFTER

    with engine.connect() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT entity_id, actor_token_id, entity_type, before_after_enc FROM audit_log "
                "WHERE action = 'token.scopes_migrate'"
            )
        ).all()
    audited = {row.entity_id: row for row in rows}
    converted = {n for n in SCOPES_BEFORE if SCOPES_BEFORE[n] != SCOPES_AFTER[n]}
    assert set(audited) == {ids[n] for n in converted}
    for name in converted:
        row = audited[ids[name]]
        assert row.actor_token_id is None  # the system actor
        assert row.entity_type == "api_token"
        assert crypto.decrypt_json(row.before_after_enc) == {
            "before": {"scopes": SCOPES_BEFORE[name]},
            "after": {"scopes": SCOPES_AFTER[name]},
        }

    command.downgrade(cfg, "0036")
    restored = _scopes(engine)
    assert restored["reader"] == ["read"]
    assert restored["agent"] == ["read", "propose", "commit"]
    assert restored["admin_session"] == ["read", "propose", "commit", "admin"]
