"""Transfer-aware schedules (issue #232, DESIGN decision 56).

``schedules.counterparty_account_id`` names the other REAL account of a
recurring transfer — a card payment from checking, a 529 contribution, a
loan payment. Instances of such a schedule materialize as two-leg expected
transfers (schedule account leg = ``amount``, counterparty leg = the
negation) instead of a flat row against a category, so the matcher can pair
them with the merged transfer the bank rows auto-link into. A schedule is
either flat (category_path) or a transfer (counterparty), never both.

Revision ID: 0026
Revises: 0025
Create Date: 2026-09-13

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE_DDL = """
ALTER TABLE schedules
  ADD COLUMN counterparty_account_id UUID REFERENCES accounts(id);
ALTER TABLE schedules
  ADD CONSTRAINT schedules_flat_or_transfer
  CHECK (counterparty_account_id IS NULL OR category_path IS NULL);
CREATE INDEX schedules_counterparty
  ON schedules (counterparty_account_id) WHERE counterparty_account_id IS NOT NULL;

-- counterparty_balance: pay the counterparty's full reported balance (cards).
ALTER TABLE schedules DROP CONSTRAINT schedules_estimate_basis_check;
ALTER TABLE schedules
  ADD CONSTRAINT schedules_estimate_basis_check
  CHECK (estimate_basis IN ('last', 'rolling_avg_3', 'rolling_avg_12', 'counterparty_balance'));
"""

DOWNGRADE_DDL = """
ALTER TABLE schedules DROP CONSTRAINT schedules_estimate_basis_check;
ALTER TABLE schedules
  ADD CONSTRAINT schedules_estimate_basis_check
  CHECK (estimate_basis IN ('last', 'rolling_avg_3', 'rolling_avg_12'));
DROP INDEX IF EXISTS schedules_counterparty;
ALTER TABLE schedules DROP CONSTRAINT IF EXISTS schedules_flat_or_transfer;
ALTER TABLE schedules DROP COLUMN IF EXISTS counterparty_account_id;
"""


def upgrade() -> None:
    op.execute(UPGRADE_DDL)


def downgrade() -> None:
    op.execute(DOWNGRADE_DDL)
