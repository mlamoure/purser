"""Audited account reconciliation (issue #268, DESIGN decision 61).

``account_reconciliations`` records each "the statement said X on D" claim:
the date, the balance basis it was compared on (``posted`` or
``pending_inclusive``, the two bases of DESIGN §7.3), the target in ledger
sign, and the adjustment Purser posted to reach it. A reconcile that found the
ledger already in balance is recorded too: ``adjustment = 0`` and no
adjustment transaction, which is what the shape CHECK pins.

One reconciliation per account and date. Two bases on one date would compete:
the second adjustment moves both bases, so it would silently break the first.

``accounts.reconciled_through`` is the compact marker (the latest reconciled
date), maintained under the account row lock in the same transaction as the
row. It is never trusted on its own: every read that shows it also reports
whether the ledger still matches the target on that date (computed live), so a
backdated write cannot leave it stale silently.

Revision ID: 0028
Revises: 0027
Create Date: 2026-10-04

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE_DDL = """
ALTER TABLE accounts ADD COLUMN reconciled_through DATE;
ALTER TABLE accounts
  ADD CONSTRAINT accounts_virtual_reconciled_null
  CHECK (NOT is_virtual OR reconciled_through IS NULL);

CREATE TABLE account_reconciliations (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id       UUID NOT NULL REFERENCES accounts(id),
  as_of            DATE NOT NULL,
  basis            TEXT NOT NULL DEFAULT 'posted'
                   CHECK (basis IN ('posted', 'pending_inclusive')),
  target_balance   NUMERIC(20,4) NOT NULL,
  adjustment       NUMERIC(20,4) NOT NULL,
  adjustment_tx_id UUID UNIQUE REFERENCES transactions(id),
  created_by       UUID REFERENCES api_tokens(id),
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT account_reconciliations_one_per_date UNIQUE (account_id, as_of),
  CONSTRAINT account_reconciliations_adjustment_shape
    CHECK ((adjustment = 0) = (adjustment_tx_id IS NULL))
);
"""

DOWNGRADE_DDL = """
DROP TABLE IF EXISTS account_reconciliations;
ALTER TABLE accounts DROP CONSTRAINT IF EXISTS accounts_virtual_reconciled_null;
ALTER TABLE accounts DROP COLUMN IF EXISTS reconciled_through;
"""


def upgrade() -> None:
    op.execute(UPGRADE_DDL)


def downgrade() -> None:
    op.execute(DOWNGRADE_DDL)
