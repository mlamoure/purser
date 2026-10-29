"""Per-rule date window for declared transfer pairs (issue #210, DESIGN decision 66).

A ``transfer_link`` rule declares an account pair for which the auto-linker
drops the name-signal test. Until now the ±3-day window
(``core.matching.TRANSFER_WINDOW_DAYS``) still applied to that pair, so a
declared pair whose legs post four days apart stayed unlinked. ``window_days``
is the largest gap, in days, the declared pair's two legs may have:

- NULL means the global window, which is exactly the behaviour before this
  migration, so existing rules are untouched (no backfill).
- 0..14 inclusive (``core.matching.TRANSFER_WINDOW_MAX_DAYS``). Zero is
  same-day legs only.
- Only ``transfer_link`` rules carry one: no other rule type pairs rows.

Downgrade drops the column: every rule falls back to the global window.

Revision ID: 0029
Revises: 0028
Create Date: 2026-10-04

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE_DDL = """
ALTER TABLE rules ADD COLUMN window_days INTEGER;
ALTER TABLE rules ADD CONSTRAINT rules_window_shape CHECK (
  window_days IS NULL
  OR (type = 'transfer_link' AND window_days BETWEEN 0 AND 14));
"""

DOWNGRADE_DDL = """
ALTER TABLE rules DROP CONSTRAINT IF EXISTS rules_window_shape;
ALTER TABLE rules DROP COLUMN IF EXISTS window_days;
"""


def upgrade() -> None:
    op.execute(UPGRADE_DDL)


def downgrade() -> None:
    op.execute(DOWNGRADE_DDL)
