"""Migration 0041 (issue #429, D1/D4): existing SSO links carry over with the
configured issuer, emails become unique regardless of case, case-only
duplicates stop the migration, and the downgrade restores the old schema.

Runs against a scratch database in the session's Postgres container so the
suite-wide migrated-to-head database is never touched.
"""

import json
import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

import purser.config
from purser.admin import oidc
from tests.conftest import TEST_ENCRYPTION_KEY

ENV_ISSUER = "https://sso.example.com/application/o/purser/"
DB_ISSUER = "https://login.example.org/realms/home"


@pytest.fixture
def scratch_db(database_url: str) -> Iterator[tuple[Config, sa.Engine]]:
    os.environ["PURSER_ENCRYPTION_KEY"] = TEST_ENCRYPTION_KEY
    saved_issuer = os.environ.pop("PURSER_OIDC_ISSUER", None)
    purser.config.get_settings.cache_clear()

    admin = sa.create_engine(database_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(sa.text("DROP DATABASE IF EXISTS purser_oidc_issuer WITH (FORCE)"))
        conn.execute(sa.text("CREATE DATABASE purser_oidc_issuer"))
    admin.dispose()

    scratch_url = database_url.rsplit("/", 1)[0] + "/purser_oidc_issuer"
    root = Path(__file__).resolve().parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    cfg.set_main_option("sqlalchemy.url", scratch_url)

    engine = sa.create_engine(scratch_url)
    command.upgrade(cfg, "0040")
    yield cfg, engine
    engine.dispose()
    os.environ.pop("PURSER_OIDC_ISSUER", None)
    if saved_issuer is not None:
        os.environ["PURSER_OIDC_ISSUER"] = saved_issuer
    purser.config.get_settings.cache_clear()


def _env_issuer(value: str | None) -> None:
    if value is None:
        os.environ.pop("PURSER_OIDC_ISSUER", None)
    else:
        os.environ["PURSER_OIDC_ISSUER"] = value
    purser.config.get_settings.cache_clear()


def _add_user(engine: sa.Engine, email: str, subject: str | None = None) -> uuid.UUID:
    with engine.begin() as conn:
        user_id: uuid.UUID = conn.execute(
            sa.text("INSERT INTO users (email, oidc_subject) VALUES (:e, :s) RETURNING id"),
            {"e": email, "s": subject},
        ).scalar_one()
    return user_id


def _override_issuer(engine: sa.Engine, value: str | None) -> None:
    """An app_settings row for oidc_issuer; None is the JSON-null kill switch."""
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO app_settings (key, value) VALUES ('oidc_issuer', CAST(:v AS jsonb))"
            ),
            {"v": json.dumps(value)},
        )


def _issuers(engine: sa.Engine) -> dict[str, str | None]:
    with engine.connect() as conn:
        return {
            email: issuer
            for email, issuer in conn.execute(sa.text("SELECT email, oidc_issuer FROM users"))
        }


def _backfill_rows(engine: sa.Engine) -> dict[uuid.UUID, object]:
    from purser import crypto

    with engine.connect() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT entity_id, actor_token_id, before_after_enc FROM audit_log "
                "WHERE action = 'user.oidc_issuer_backfill'"
            )
        ).all()
    assert all(row.actor_token_id is None for row in rows)  # the system actor
    return {row.entity_id: crypto.decrypt_json(row.before_after_enc) for row in rows}


def _constraints(engine: sa.Engine) -> set[str]:
    with engine.connect() as conn:
        names = conn.execute(
            sa.text(
                "SELECT conname FROM pg_constraint WHERE conrelid = 'users'::regclass "
                "UNION SELECT indexname FROM pg_indexes WHERE tablename = 'users'"
            )
        ).scalars()
        return set(names)


def _version(engine: sa.Engine) -> str:
    with engine.connect() as conn:
        return str(conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one())


def test_links_carry_over_with_the_env_issuer_and_round_trip(
    scratch_db: tuple[Config, sa.Engine],
) -> None:
    cfg, engine = scratch_db
    linked = _add_user(engine, "linked@example.com", "sub-1")
    _add_user(engine, "local@example.com")
    _env_issuer(ENV_ISSUER)

    command.upgrade(cfg, "0041")

    # Stored without the trailing slash: the form sign-in looks links up in.
    assert oidc.issuer_key(ENV_ISSUER) == ENV_ISSUER.rstrip("/")
    assert _issuers(engine) == {
        "linked@example.com": ENV_ISSUER.rstrip("/"),
        "local@example.com": None,
    }
    assert _backfill_rows(engine) == {
        linked: {"before": {"oidc_issuer": None}, "after": {"oidc_issuer": ENV_ISSUER.rstrip("/")}}
    }
    assert {"users_email_lower_key", "users_oidc_identity_key"} <= _constraints(engine)
    assert not {"users_email_key", "users_oidc_subject_key"} & _constraints(engine)

    command.downgrade(cfg, "0040")
    with engine.connect() as conn:
        columns = set(
            conn.execute(
                sa.text(
                    "SELECT column_name FROM information_schema.columns WHERE table_name = 'users'"
                )
            ).scalars()
        )
        subject = conn.execute(
            sa.text("SELECT oidc_subject FROM users WHERE id = :id"), {"id": linked}
        ).scalar_one()
    assert "oidc_issuer" not in columns
    assert subject == "sub-1"
    assert {"users_email_key", "users_oidc_subject_key"} <= _constraints(engine)

    command.upgrade(cfg, "0041")  # and up again
    assert _issuers(engine)["linked@example.com"] == ENV_ISSUER.rstrip("/")


def test_the_database_override_wins_over_the_env(scratch_db: tuple[Config, sa.Engine]) -> None:
    cfg, engine = scratch_db
    _add_user(engine, "linked@example.com", "sub-1")
    _override_issuer(engine, DB_ISSUER)
    _env_issuer(ENV_ISSUER)
    command.upgrade(cfg, "0041")
    assert _issuers(engine)["linked@example.com"] == DB_ISSUER


@pytest.mark.parametrize("override", [True, False], ids=["sso-switched-off", "not-configured"])
def test_links_stay_legacy_when_no_issuer_is_configured(
    override: bool, scratch_db: tuple[Config, sa.Engine]
) -> None:
    """No issuer to record: the link keeps a NULL issuer, which the next
    sign-in from the configured provider claims (tests/test_oidc.py)."""
    cfg, engine = scratch_db
    _add_user(engine, "linked@example.com", "sub-1")
    if override:
        _override_issuer(engine, None)  # the kill switch beats the env
        _env_issuer(ENV_ISSUER)
    command.upgrade(cfg, "0041")
    assert _issuers(engine) == {"linked@example.com": None}
    assert _backfill_rows(engine) == {}


def test_case_only_duplicates_stop_the_migration(scratch_db: tuple[Config, sa.Engine]) -> None:
    cfg, engine = scratch_db
    _add_user(engine, "Pat@example.com", "sub-1")
    _add_user(engine, "pat@example.com")
    _add_user(engine, "other@example.com")
    _env_issuer(ENV_ISSUER)
    with pytest.raises(RuntimeError, match="differ only by letter case") as exc:
        command.upgrade(cfg, "0041")
    assert "Pat@example.com, pat@example.com" in str(exc.value)
    assert "other@example.com" not in str(exc.value)
    # Nothing merged, nothing changed: still at 0040 with the old constraints.
    assert _version(engine) == "0040"
    assert "users_email_key" in _constraints(engine)
    with engine.connect() as conn:
        emails = set(conn.execute(sa.text("SELECT email FROM users")).scalars())
    assert emails == {"Pat@example.com", "pat@example.com", "other@example.com"}


def test_downgrade_refuses_a_subject_shared_by_two_providers(
    scratch_db: tuple[Config, sa.Engine],
) -> None:
    cfg, engine = scratch_db
    command.upgrade(cfg, "0041")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO users (email, oidc_issuer, oidc_subject) VALUES "
                "('a@example.com', 'https://one.example.com', 'sub-1'), "
                "('b@example.com', 'https://two.example.com', 'sub-1')"
            )
        )
    with pytest.raises(RuntimeError, match="more than one account"):
        command.downgrade(cfg, "0040")
    assert _version(engine) == "0041"
