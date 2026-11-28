"""Needs review only for a reason (issue #694, DESIGN decision 141).

A transaction needs review when it is a bank-sync row with no category, or a
reconciliation adjustment nobody has explained or accepted. Both are derived
from the row itself, so review marks go: ``transaction_reviews`` and its
review-scope index are dropped.

- ``account_reconciliations.adjustment_accepted_at`` / ``_by``: a person's (or
  an agent's) acceptance of the adjustment a reconcile posted. A mark that
  already sat on an adjustment is carried into it first: the mark on the live
  head of the adjustment's correction chain, else on the nearest earlier row
  of that chain (the adjustment row itself last), its ``reviewed_at`` and
  ``reviewed_by``.
- ``transactions_superseded_by``: the lineage walk the predicate makes
  (``review.flagged_rows``) steps from a row to the void row it carries on
  (a pending's posted successor, an unlinked original's clone) by
  ``superseded_by``; nothing indexed it.
- The removed settings' persisted values go: ``ai_review``,
  ``review_max_age_days`` and ``review_guidance``.

The downgrade recreates 0021/0032's table and indexes and starts it silent,
as 0021 did (every existing row marked ``backfill``); an accepted adjustment
gets its acceptance back as an ``explicit`` mark. The removed settings rows
are not restored.

DESIGN.md section 3.1 is updated in the same PR.

Revision ID: 0065
Revises: 0064
Create Date: 2026-11-25

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0065"
down_revision: str | None = "0064"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE_DDL = """
ALTER TABLE account_reconciliations
  ADD COLUMN adjustment_accepted_at TIMESTAMPTZ,
  ADD COLUMN adjustment_accepted_by UUID REFERENCES api_tokens(id);

-- Carry a review mark on an adjustment into its acceptance. The chain walks
-- forward from the adjustment row through its corrections (original <-
-- reversal <- replacement); the deepest marked row wins, so the live head's
-- mark is preferred and the adjustment row's own mark comes last.
WITH RECURSIVE chain(reconciliation_id, transaction_id, depth) AS (
  SELECT r.id, r.adjustment_tx_id, 0
  FROM account_reconciliations r
  WHERE r.adjustment_tx_id IS NOT NULL
  UNION ALL
  SELECT c.reconciliation_id, replacement.id, c.depth + 1
  FROM chain c
  JOIN transactions reversal ON reversal.reverses_id = c.transaction_id
  JOIN transactions replacement ON replacement.reverses_id = reversal.id
  WHERE c.depth < 64
), marked AS (
  SELECT DISTINCT ON (c.reconciliation_id)
         c.reconciliation_id, tr.reviewed_at, tr.reviewed_by
  FROM chain c
  JOIN transaction_reviews tr ON tr.transaction_id = c.transaction_id
  ORDER BY c.reconciliation_id, c.depth DESC
)
UPDATE account_reconciliations r
SET adjustment_accepted_at = m.reviewed_at,
    adjustment_accepted_by = m.reviewed_by
FROM marked m
WHERE m.reconciliation_id = r.id;

DROP INDEX IF EXISTS transactions_review_scope;
DROP INDEX IF EXISTS transaction_reviews_escalated;
DROP TABLE transaction_reviews;

CREATE INDEX transactions_superseded_by ON transactions (superseded_by)
  WHERE superseded_by IS NOT NULL;

DELETE FROM app_settings
WHERE key IN ('ai_review', 'review_max_age_days', 'review_guidance');
"""

DOWNGRADE_DDL = """
DROP INDEX IF EXISTS transactions_superseded_by;

CREATE TABLE transaction_reviews (
  transaction_id UUID PRIMARY KEY REFERENCES transactions(id),
  reviewed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  reviewed_by    UUID REFERENCES api_tokens(id),
  via            TEXT NOT NULL,
  note_enc       BYTEA,
  escalated_at   TIMESTAMPTZ,
  carried_from   UUID REFERENCES transactions(id) ON DELETE SET NULL
);
CREATE INDEX transactions_review_scope ON transactions (date DESC, id DESC)
  WHERE superseded_by IS NULL
    AND status IN ('pending','posted')
    AND (origin IN ('system','rule')
         OR (origin = 'import' AND source_fetch_id IS NOT NULL));
CREATE INDEX transaction_reviews_escalated ON transaction_reviews (escalated_at)
  WHERE escalated_at IS NOT NULL;

-- Start silent, as 0021 did; an accepted adjustment keeps who accepted it.
INSERT INTO transaction_reviews (transaction_id, via, reviewed_at, reviewed_by)
SELECT t.id,
       CASE WHEN r.adjustment_accepted_at IS NOT NULL THEN 'explicit' ELSE 'backfill' END,
       COALESCE(r.adjustment_accepted_at, now()),
       r.adjustment_accepted_by
FROM transactions t
LEFT JOIN account_reconciliations r ON r.adjustment_tx_id = t.id;

ALTER TABLE account_reconciliations
  DROP COLUMN adjustment_accepted_by,
  DROP COLUMN adjustment_accepted_at;
"""


def upgrade() -> None:
    op.execute(UPGRADE_DDL)


def downgrade() -> None:
    op.execute(DOWNGRADE_DDL)
