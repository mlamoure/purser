"""Manual imports are raw-first too (issue #127, DESIGN §7, decision 53).

``raw_fetches`` becomes the archive for EVERY payload, not just SimpleFIN
fetches: ``connection_id`` goes nullable and a ``source`` discriminator
(``simplefin`` | ``qif`` | ``csv``) says what kind of payload a row holds.
A manual upload has no connection, so the CHECK ties the two together:
feed rows keep their connection, manual rows have none.

The ledger rows a manual import creates keep ``source_fetch_id`` NULL —
that NULL is doubly load-bearing (PR-7 cutoff semantics AND the #175
review scope both read "non-NULL source_fetch_id" as "this is a feed
row") — so the archive link is the content hash + audit trail, never a
foreign key from transactions.

Append-only triggers are untouched: they fire on UPDATE/DELETE/TRUNCATE,
and this migration only ALTERs the table shape.

Revision ID: 0023
Revises: 0022
Create Date: 2026-08-26

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE_DDL = """
ALTER TABLE raw_fetches
  ADD COLUMN source TEXT NOT NULL DEFAULT 'simplefin'
  CHECK (source IN ('simplefin', 'qif', 'csv'));

ALTER TABLE raw_fetches
  ALTER COLUMN connection_id DROP NOT NULL;

-- Feed rows keep their connection; manual uploads have none.
ALTER TABLE raw_fetches
  ADD CONSTRAINT raw_fetches_source_connection
  CHECK ((source = 'simplefin') = (connection_id IS NOT NULL));
"""

DOWNGRADE_DDL = """
ALTER TABLE raw_fetches DROP CONSTRAINT IF EXISTS raw_fetches_source_connection;
-- Manual-upload rows can't survive a NOT NULL connection_id; the append-only
-- trigger must step aside for exactly this statement (0007 precedent).
ALTER TABLE raw_fetches DISABLE TRIGGER raw_fetches_no_update_delete;
DELETE FROM raw_fetches WHERE connection_id IS NULL;
ALTER TABLE raw_fetches ENABLE TRIGGER raw_fetches_no_update_delete;
ALTER TABLE raw_fetches ALTER COLUMN connection_id SET NOT NULL;
ALTER TABLE raw_fetches DROP COLUMN IF EXISTS source;
"""


def upgrade() -> None:
    op.execute(UPGRADE_DDL)


def downgrade() -> None:
    op.execute(DOWNGRADE_DDL)
