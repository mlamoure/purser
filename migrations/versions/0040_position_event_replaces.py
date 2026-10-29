"""A correction keeps its place in the day: position_events.replaces_id (issue #401).

Same-day position events are ordered (occurred_on, created_at, id), so the
documented correction, "reverse it and record the right one", put the new
event after every other event of that day and silently changed the
position. A correction recorded with ``replaces`` now names the reversed
event it corrects and takes its slot in the day's order
(``core.holdings.effective_events``).

- ``replaces_id`` points at another event of the same position (the service
  checks the position, the date and that the event is reversed); a reversal
  row never carries one (CHECK).
- An event is replaced at most once (partial unique index); a replacement
  that is itself reversed can be replaced in turn, and the chain keeps the
  first event's slot.

Adding a column is DDL, so the append-only trigger is untouched; existing
rows get NULL and keep their order. DESIGN.md §3.1 and decision 93 updated
in the same PR.

Revision ID: 0040
Revises: 0039
Create Date: 2026-10-25

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0040"
down_revision: str | None = "0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE position_events
          ADD COLUMN replaces_id UUID REFERENCES position_events(id),
          ADD CONSTRAINT position_events_reversal_replaces_nothing
            CHECK (replaces_id IS NULL OR reverses_id IS NULL);
        CREATE UNIQUE INDEX position_events_replaced_once
          ON position_events (replaces_id) WHERE replaces_id IS NOT NULL;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX position_events_replaced_once;
        ALTER TABLE position_events
          DROP CONSTRAINT position_events_reversal_replaces_nothing,
          DROP COLUMN replaces_id;
        """
    )
