"""New-transaction review flags (issue #175, DESIGN §4, decision 47).

Rows that land in the ledger WITHOUT human eyes form an unreviewed *set* — a
filter over the registers, not a second queue (0.5.0 deleted the queue
deliberately).

Marks live in a SIDE TABLE, not as columns on ``transactions``. Migration
0005's ``transactions_posted_guard`` allow-lists only ``status`` and
``superseded_by`` via ``to_jsonb``, so ANY new column on that table is
immutable once a row is posted — precisely the state the rows needing marks
are in. A review mark is also an annotation about a row rather than ledger
content: it moves no money and belongs outside the immutability envelope.

The migration BACKFILLS every existing row, so the feature starts silent and
only ever concerns transactions that arrive after it ships.

Revision ID: 0021
Revises: 0020
Create Date: 2026-08-08

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE_DDL = """
CREATE TABLE transaction_reviews (
  transaction_id UUID PRIMARY KEY REFERENCES transactions(id),
  reviewed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  reviewed_by    UUID REFERENCES api_tokens(id),  -- NULL = migration backfill
  via            TEXT NOT NULL,
    -- explicit | update | post | confirm_transfer | match | unlink
    -- | ai_review (the backend reviewer) | aged (staleness sweep)
    -- | backfill (this migration)
  note_enc       BYTEA,
  escalated_at   TIMESTAMPTZ
);

-- The review SET: live chain heads that arrived unattended.
-- The source_fetch_id clause is load-bearing: QIF and CSV imports are ALSO
-- origin='import', and a 6,081-row Quicken import must not become 6,081
-- review items — the user chose those rows. origin='agent' is excluded too,
-- because the propose/commit lifecycle already gates those.
CREATE INDEX transactions_review_scope ON transactions (date DESC, id DESC)
  WHERE superseded_by IS NULL
    AND status IN ('pending','posted')
    AND (origin IN ('system','rule')
         OR (origin = 'import' AND source_fetch_id IS NOT NULL));

CREATE INDEX transaction_reviews_escalated ON transaction_reviews (escalated_at)
  WHERE escalated_at IS NOT NULL;

-- Start silent: everything that already exists counts as reviewed.
INSERT INTO transaction_reviews (transaction_id, via)
  SELECT id, 'backfill' FROM transactions;
"""

DOWNGRADE_DDL = """
DROP INDEX IF EXISTS transactions_review_scope;
DROP INDEX IF EXISTS transaction_reviews_escalated;
DROP TABLE IF EXISTS transaction_reviews;
"""


def upgrade() -> None:
    op.execute(UPGRADE_DDL)


def downgrade() -> None:
    op.execute(DOWNGRADE_DDL)
