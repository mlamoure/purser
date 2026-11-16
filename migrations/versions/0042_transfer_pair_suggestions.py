"""Transfer candidates in the suggestions inbox (issue #386, #246 phase 2,
DESIGN decision 102).

A transfer candidate — two one-sided bank rows of exactly opposite amounts on
two accounts, inside the transfer window, with no signal that links them on
its own — gets a card: ``suggestions.kind`` gains ``transfer_pair``. Its
fingerprint is the candidate's pair key in plain text
(``transfer_pair:<low id>:<high id>``), so a rejection is durable memory that
the automatic link paths honour. ``withdrawn`` already exists (0034).

Nothing to backfill: the matching pass after each import and the daily job
create the cards for current candidates whose newer leg is inside the
lookback window.

Downgrade deletes every transfer_pair row (none fits the restored CHECK).
Nothing references them but audit rows, which are not FKs. Rejection memory
goes with them, so a pair a person rejected may be auto-linked again by a
name signal or a ``transfer_link`` rule.

Revision ID: 0042
Revises: 0041
Create Date: 2026-10-18

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0042"
down_revision: str | None = "0041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE_DDL = """
ALTER TABLE suggestions DROP CONSTRAINT IF EXISTS suggestions_kind_check;
ALTER TABLE suggestions ADD CONSTRAINT suggestions_kind_check
  CHECK (kind IN ('schedule', 'transfer_link', 'match', 'transfer_pair'));
"""

DOWNGRADE_DDL = """
DELETE FROM suggestions WHERE kind = 'transfer_pair';
ALTER TABLE suggestions DROP CONSTRAINT IF EXISTS suggestions_kind_check;
ALTER TABLE suggestions ADD CONSTRAINT suggestions_kind_check
  CHECK (kind IN ('schedule', 'transfer_link', 'match'));
"""


def upgrade() -> None:
    op.execute(UPGRADE_DDL)


def downgrade() -> None:
    op.execute(DOWNGRADE_DDL)
