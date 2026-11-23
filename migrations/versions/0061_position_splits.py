"""Stock split events for hand-tracked positions (issue #624, decision 127).

- ``position_events.kind`` gains ``split``: on its effective date every
  share of the position is multiplied by ``split_numerator /
  split_denominator`` (2:1 forward, 1:10 reverse), at the start of that day.
  A split row carries the ratio and nothing else: ``quantity`` 0, no price,
  fees, basis or cash (``position_events_split_shape``). The ratio is two
  positive integers, at most 1,000,000 each, and never 1:1.
- ``position_events.in_lieu_of``: a ``sell`` that is a split's cash in lieu
  of a fractional share names that split. It is recorded with the split and
  reversed with it; one per split (``position_events_in_lieu_once``).

The downgrade refuses while any split event exists: it would destroy them
(restore a pre-0061 backup instead).

DESIGN.md sections 3.1, 8 and 12 are updated in the same PR.

Revision ID: 0061
Revises: 0060
Create Date: 2026-11-22

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0061"
down_revision: str | None = "0060"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE_DDL = """
ALTER TABLE position_events DROP CONSTRAINT position_events_kind_values;
ALTER TABLE position_events
  ADD CONSTRAINT position_events_kind_values
  CHECK (kind IN ('set_quantity', 'buy', 'sell', 'vest', 'transfer_out', 'split'));
ALTER TABLE position_events DROP CONSTRAINT position_events_quantity_shape;
ALTER TABLE position_events
  ADD CONSTRAINT position_events_quantity_shape
  CHECK (quantity >= 0 AND (kind IN ('set_quantity', 'split') OR quantity > 0));
ALTER TABLE position_events
  ADD COLUMN split_numerator INTEGER,
  ADD COLUMN split_denominator INTEGER,
  ADD COLUMN in_lieu_of UUID REFERENCES position_events(id);
ALTER TABLE position_events
  ADD CONSTRAINT position_events_split_shape
  CHECK (
    (kind = 'split') = (split_numerator IS NOT NULL)
    AND (kind = 'split') = (split_denominator IS NOT NULL)
    AND (kind <> 'split' OR (
      quantity = 0 AND price IS NULL AND fees IS NULL AND cost_basis IS NULL
      AND split_numerator BETWEEN 1 AND 1000000
      AND split_denominator BETWEEN 1 AND 1000000
      AND split_numerator <> split_denominator
    ))
  );
ALTER TABLE position_events
  ADD CONSTRAINT position_events_in_lieu_shape
  CHECK (in_lieu_of IS NULL OR kind = 'sell');
CREATE UNIQUE INDEX position_events_in_lieu_once
  ON position_events (in_lieu_of) WHERE in_lieu_of IS NOT NULL;
"""

DOWNGRADE_DDL = """
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM position_events WHERE kind = 'split') THEN
    RAISE EXCEPTION 'split events exist; a downgrade below 0061 would destroy them '
      '(restore a pre-0061 backup instead)';
  END IF;
END $$;
DROP INDEX IF EXISTS position_events_in_lieu_once;
ALTER TABLE position_events DROP CONSTRAINT IF EXISTS position_events_in_lieu_shape;
ALTER TABLE position_events DROP CONSTRAINT IF EXISTS position_events_split_shape;
ALTER TABLE position_events
  DROP COLUMN in_lieu_of,
  DROP COLUMN split_denominator,
  DROP COLUMN split_numerator;
ALTER TABLE position_events DROP CONSTRAINT position_events_quantity_shape;
ALTER TABLE position_events
  ADD CONSTRAINT position_events_quantity_shape
  CHECK (quantity >= 0 AND (kind = 'set_quantity' OR quantity > 0));
ALTER TABLE position_events DROP CONSTRAINT position_events_kind_values;
ALTER TABLE position_events
  ADD CONSTRAINT position_events_kind_values
  CHECK (kind IN ('set_quantity', 'buy', 'sell', 'vest', 'transfer_out'));
"""


def upgrade() -> None:
    op.execute(UPGRADE_DDL)


def downgrade() -> None:
    op.execute(DOWNGRADE_DDL)
