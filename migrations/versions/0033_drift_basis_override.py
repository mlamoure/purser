"""Per-account drift-basis pin (issue #307, DESIGN §7.3, decision 83).

Adds ``accounts.drift_basis_override`` (nullable text): when set, the
post-sync balance-drift check reports the delta on this basis instead of the
one the account's sync history selects (decision 64). NULL = learned (prior
behavior). The vocabulary is the drift check's own — ``posted_only`` |
``pending_inclusive`` — so the pinned value equals the reported one.

Real-account-only, like ``feed_start`` and ``feed_invert``: the
``accounts_virtual_drift_basis_override_null`` CHECK keeps categories out.

Revision ID: 0033
Revises: 0032
Create Date: 2026-10-13

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE_DDL = """
ALTER TABLE accounts ADD COLUMN drift_basis_override TEXT;

ALTER TABLE accounts
  ADD CONSTRAINT accounts_drift_basis_override_values
  CHECK (drift_basis_override IN ('posted_only', 'pending_inclusive'));

-- The drift check runs on real feed accounts only; categories have no feed.
ALTER TABLE accounts
  ADD CONSTRAINT accounts_virtual_drift_basis_override_null
  CHECK (NOT is_virtual OR drift_basis_override IS NULL);
"""

DOWNGRADE_DDL = """
ALTER TABLE accounts DROP CONSTRAINT IF EXISTS accounts_virtual_drift_basis_override_null;
ALTER TABLE accounts DROP CONSTRAINT IF EXISTS accounts_drift_basis_override_values;
ALTER TABLE accounts DROP COLUMN IF EXISTS drift_basis_override;
"""


def upgrade() -> None:
    op.execute(UPGRADE_DDL)


def downgrade() -> None:
    op.execute(DOWNGRADE_DDL)
