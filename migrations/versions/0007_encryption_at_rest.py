"""Encryption at rest becomes required (issue #93).

- ``encryption_canary``: single row binding PURSER_ENCRYPTION_KEY to this
  database. Seeded HERE so fresh installs and upgrades get it the same way;
  startup decrypts the sentinel and fails closed on a key mismatch.
- ``raw_fetches``: ``payload JSONB`` → ``payload_enc BYTEA``, Fernet over the
  canonical (sort_keys) JSON — the exact string ``content_hash`` is computed
  over, so idempotency is untouched. The append-only trigger is disabled only
  around the in-migration backfill and re-enabled before commit; append-only
  semantics are unchanged afterwards.

Requires PURSER_ENCRYPTION_KEY (the CLI enforces this before invoking
alembic). DESIGN.md §3.1/§9/§12 updated in the same PR (schema rule).

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-08

"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from purser import crypto

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BATCH = 200  # envelopes can be hundreds of KB; keep working set bounded

DDL = """
CREATE TABLE encryption_canary (
  id                  BOOLEAN PRIMARY KEY DEFAULT true CHECK (id),
  sentinel_enc        BYTEA NOT NULL,
  key_fingerprint     TEXT NOT NULL,
  key_acknowledged_at TIMESTAMPTZ,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def upgrade() -> None:
    crypto.ensure_key()  # fail before any DDL: the backfill needs the key
    bind = op.get_bind()

    op.execute(DDL)
    bind.execute(
        sa.text(
            "INSERT INTO encryption_canary (sentinel_enc, key_fingerprint) VALUES (:sentinel, :fp)"
        ),
        {"sentinel": crypto.encrypt(crypto.CANARY_SENTINEL), "fp": crypto.key_fingerprint()},
    )

    op.execute("ALTER TABLE raw_fetches ADD COLUMN payload_enc BYTEA")
    op.execute("ALTER TABLE raw_fetches DISABLE TRIGGER raw_fetches_no_update_delete")
    last_id = None
    while True:
        rows = bind.execute(
            sa.text(
                "SELECT id, payload::text AS payload FROM raw_fetches"
                + (" WHERE id > :last" if last_id is not None else "")
                + " ORDER BY id LIMIT :batch"
            ),
            ({"last": last_id, "batch": BATCH} if last_id is not None else {"batch": BATCH}),
        ).all()
        if not rows:
            break
        for row in rows:
            canonical = json.dumps(json.loads(row.payload), sort_keys=True)
            bind.execute(
                sa.text("UPDATE raw_fetches SET payload_enc = :enc WHERE id = :id"),
                {"enc": crypto.encrypt(canonical), "id": row.id},
            )
        last_id = rows[-1].id
    op.execute("ALTER TABLE raw_fetches ALTER COLUMN payload_enc SET NOT NULL")
    op.execute("ALTER TABLE raw_fetches DROP COLUMN payload")
    op.execute("ALTER TABLE raw_fetches ENABLE TRIGGER raw_fetches_no_update_delete")


def downgrade() -> None:
    crypto.ensure_key()
    bind = op.get_bind()

    op.execute("ALTER TABLE raw_fetches ADD COLUMN payload JSONB")
    op.execute("ALTER TABLE raw_fetches DISABLE TRIGGER raw_fetches_no_update_delete")
    last_id = None
    while True:
        rows = bind.execute(
            sa.text(
                "SELECT id, payload_enc FROM raw_fetches"
                + (" WHERE id > :last" if last_id is not None else "")
                + " ORDER BY id LIMIT :batch"
            ),
            ({"last": last_id, "batch": BATCH} if last_id is not None else {"batch": BATCH}),
        ).all()
        if not rows:
            break
        for row in rows:
            bind.execute(
                sa.text("UPDATE raw_fetches SET payload = CAST(:plain AS JSONB) WHERE id = :id"),
                {"plain": crypto.decrypt(row.payload_enc), "id": row.id},
            )
        last_id = rows[-1].id
    op.execute("ALTER TABLE raw_fetches ALTER COLUMN payload SET NOT NULL")
    op.execute("ALTER TABLE raw_fetches DROP COLUMN payload_enc")
    op.execute("ALTER TABLE raw_fetches ENABLE TRIGGER raw_fetches_no_update_delete")

    op.execute("DROP TABLE encryption_canary")
