"""Schedules v2, part 1 (issue #231, DESIGN decision 55).

``schedules.dtstart`` persists the RRULE anchor that ``create_schedule`` used
to validate and then drop (#221) — materialization re-derived it from
``created_at``, so anchor-sensitive rules silently anchored to row creation.
Backfilled from ``created_at`` (UTC date), which is exactly what the old
derivation computed, so existing schedules keep their occurrences.

``schedules.ended_at`` records the terminal ``ended`` status.

``transactions.occurrence_date`` is the rule slot a schedule instance fills:
materialization becomes idempotent on (schedule_id, occurrence_date) instead
of (schedule_id, date), so a date edit on an instance no longer frees the
slot for a duplicate. Backfilled to ``date`` for every instance row. The
index is deliberately non-unique — ``unmatch`` legitimately clones a slot.

``transactions.schedule_pinned_at`` marks instances a human/agent edited;
those are never amount-refreshed or voided by rule edits/pause.

The backfill touches instance rows that may have been posted directly
(expected -> posted is a legal transition); the posted-immutability trigger
is disabled for the metadata backfill inside this transaction, the same
precedent as migration 0007.

Revision ID: 0025
Revises: 0024
Create Date: 2026-09-13

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE_DDL = """
ALTER TABLE schedules ADD COLUMN dtstart DATE;
UPDATE schedules SET dtstart = (created_at AT TIME ZONE 'UTC')::date;
ALTER TABLE schedules ALTER COLUMN dtstart SET NOT NULL;
ALTER TABLE schedules ADD COLUMN ended_at TIMESTAMPTZ;

ALTER TABLE transactions ADD COLUMN occurrence_date DATE;
ALTER TABLE transactions ADD COLUMN schedule_pinned_at TIMESTAMPTZ;

ALTER TABLE transactions DISABLE TRIGGER transactions_posted_immutable;
UPDATE transactions SET occurrence_date = date WHERE schedule_id IS NOT NULL;
ALTER TABLE transactions ENABLE TRIGGER transactions_posted_immutable;

CREATE INDEX transactions_schedule_occurrence
  ON transactions (schedule_id, occurrence_date) WHERE schedule_id IS NOT NULL;
"""

DOWNGRADE_DDL = """
DROP INDEX IF EXISTS transactions_schedule_occurrence;
ALTER TABLE transactions DISABLE TRIGGER transactions_posted_immutable;
ALTER TABLE transactions DROP COLUMN IF EXISTS schedule_pinned_at;
ALTER TABLE transactions DROP COLUMN IF EXISTS occurrence_date;
ALTER TABLE transactions ENABLE TRIGGER transactions_posted_immutable;
ALTER TABLE schedules DROP COLUMN IF EXISTS ended_at;
ALTER TABLE schedules DROP COLUMN IF EXISTS dtstart;
"""


def upgrade() -> None:
    op.execute(UPGRADE_DDL)


def downgrade() -> None:
    op.execute(DOWNGRADE_DDL)
