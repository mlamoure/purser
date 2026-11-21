"""Migration 0050 (issue #534): schedules gain an encrypted memo, and each
stored name is split into payee / memo by the rules ``resolve_labels`` applies
to an agent that still sends ``name``.

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
        conn.execute(sa.text("DROP DATABASE IF EXISTS purser_sched_memo WITH (FORCE)"))
        conn.execute(sa.text("CREATE DATABASE purser_sched_memo"))
    admin.dispose()

    scratch_url = database_url.rsplit("/", 1)[0] + "/purser_sched_memo"
    root = Path(__file__).resolve().parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    cfg.set_main_option("sqlalchemy.url", scratch_url)

    engine = sa.create_engine(scratch_url)
    yield cfg, engine
    engine.dispose()


def test_names_split_into_payee_and_memo(scratch_db: tuple[Config, sa.Engine]) -> None:
    from purser import crypto
    from purser.core import ledger as core

    cfg, engine = scratch_db
    command.upgrade(cfg, "0049")
    with engine.begin() as conn:
        key_enc = conn.execute(
            sa.text("SELECT key_enc FROM encryption_keys WHERE purpose = 'blind_index'")
        ).scalar_one()
        bidx_key = crypto.unwrap_key(key_enc)

        def bidx(name: str) -> str:
            return crypto._hmac_hex(bidx_key, core.normalize_payee(name))

        def account(name: str, type_: str, path: str, virtual: bool = False) -> object:
            return conn.execute(
                sa.text(
                    "INSERT INTO accounts (name, type, path, is_virtual)"
                    " VALUES (:n, :t, CAST(:p AS ltree), :v) RETURNING id"
                ),
                {"n": name, "t": type_, "p": path, "v": virtual},
            ).scalar_one()

        def payee(name: str) -> object:
            return conn.execute(
                sa.text("INSERT INTO payees (name_enc, name_bidx) VALUES (:n, :b) RETURNING id"),
                {"n": crypto.encrypt(name), "b": bidx(name)},
            ).scalar_one()

        checking = account("Checking", "checking", "assets.checking")
        card = account("Card", "credit_card", "liabilities.card")
        account("Fitness", "category_expense", "expenses.fitness", virtual=True)
        existing_gym = payee("Gym")
        streaming = payee("Streamflix")

        def schedule(name: str, *, payee_id: object = None, counterparty: object = None) -> object:
            return conn.execute(
                sa.text(
                    "INSERT INTO schedules (name_enc, account_id, payee_id, category_path,"
                    " counterparty_account_id, rrule, dtstart, amount_type, amount, source)"
                    " VALUES (:n, :a, :p, CAST(:c AS ltree), :cp,"
                    " 'FREQ=MONTHLY;BYMONTHDAY=5', '2026-01-05', 'fixed', -10.0000, 'manual')"
                    " RETURNING id"
                ),
                {
                    "n": crypto.encrypt(name),
                    "a": checking,
                    "p": payee_id,
                    "c": None if counterparty else "expenses.fitness",
                    "cp": counterparty,
                },
            ).scalar_one()

        reuses_payee = schedule("GYM")  # no payee: finds "Gym" by blind index
        new_payee = schedule("Corner Bakery")  # no payee: creates one
        same_as_payee = schedule("Streamflix", payee_id=streaming)
        differs = schedule("Family plan", payee_id=streaming)
        transfer = schedule("Card payment", counterparty=card)

    command.upgrade(cfg, "0050")
    with engine.connect() as conn:
        rows = {
            r.id: r
            for r in conn.execute(
                sa.text(
                    "SELECT s.id, s.name_enc, s.memo_enc, s.payee_id, p.name_enc AS payee_enc"
                    " FROM schedules s LEFT JOIN payees p ON p.id = s.payee_id"
                )
            )
        }

    def fields(sid: object) -> tuple[str, str | None, str | None]:
        r = rows[sid]
        return (
            crypto.decrypt(r.name_enc),
            crypto.decrypt(r.payee_enc) if r.payee_enc is not None else None,
            crypto.decrypt(r.memo_enc) if r.memo_enc is not None else None,
        )

    assert rows[reuses_payee].payee_id == existing_gym
    assert fields(reuses_payee) == ("GYM", "Gym", None)
    assert fields(new_payee) == ("Corner Bakery", "Corner Bakery", None)
    assert fields(same_as_payee) == ("Streamflix", "Streamflix", None)
    assert fields(differs) == ("Family plan", "Streamflix", "Family plan")
    assert fields(transfer) == ("Card payment", None, "Card payment")

    command.downgrade(cfg, "0049")
    with engine.connect() as conn:
        columns = set(
            conn.execute(
                sa.text(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_name = 'schedules'"
                )
            ).scalars()
        )
    assert "memo_enc" not in columns
