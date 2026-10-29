"""A review mark records the row it was carried from (issue #342, DESIGN
decision 82; reverses decision 47's "a carried settlement moves no review").

A pending row's review mark now follows it to its posted successor's live
head, and correction inheritance and unlink copy a mark the same way: whole
(actor, via, time, note, escalation). A copied ``reviewed_at`` is earlier than
the row's own ``created_at``, and only ``carried_from`` explains that — the
table has no JSON field, and audit rows are pruned after
``audit_retention_days``, so the link is a column.

``ON DELETE SET NULL``: a guarded admin delete of the source row (decisions
34, 45, 72) keeps the copy and drops only the link. Nothing to backfill —
every mark before this migration was made on its own row.

Revision ID: 0032
Revises: 0031
Create Date: 2026-10-13

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE_DDL = """
ALTER TABLE transaction_reviews
  ADD COLUMN carried_from UUID REFERENCES transactions(id) ON DELETE SET NULL;
"""

DOWNGRADE_DDL = """
ALTER TABLE transaction_reviews DROP COLUMN IF EXISTS carried_from;
"""


def upgrade() -> None:
    op.execute(UPGRADE_DDL)


def downgrade() -> None:
    op.execute(DOWNGRADE_DDL)
