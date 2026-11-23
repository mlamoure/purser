"""An account's starting balance from the bank (issue #585, DESIGN decision 123).

- ``equity.opening_balances``: the system account a starting balance balances
  against. A bank feed delivers about three months of rows and no balance from
  before them; the starting balance stands for everything earlier, so it is
  neither income nor spending.
- ``account_starting_balances``: one row per account that has one. It keeps
  what the entry was derived from (the fetch, the bank's figure, the account's
  two ledger sums at that moment, the basis and the horizon: the earliest row
  the account then held), so the entry can be worked out again when older
  history arrives or the bank's basis is learned.

Downgrade drops the table (the entries stay, ordinary posted rows against
``equity.opening_balances``, no longer labelled or kept in step) and removes
the system account only when nothing is posted to it.

Revision ID: 0059
Revises: 0058
Create Date: 2026-11-22

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0059"
down_revision: str | None = "0058"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE_DDL = """
INSERT INTO accounts (name, type, is_virtual, path, description)
SELECT 'Starting balances', 'equity', true, CAST('equity.opening_balances' AS ltree),
       'System account: the other side of an account''s starting balance, which stands '
       || 'for everything before the first transaction Purser holds. Not a category.'
WHERE NOT EXISTS (
    SELECT 1 FROM accounts WHERE path = CAST('equity.opening_balances' AS ltree));

CREATE TABLE account_starting_balances (
  account_id     UUID PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
  transaction_id UUID REFERENCES transactions(id) ON DELETE SET NULL,
  raw_fetch_id   UUID NOT NULL REFERENCES raw_fetches(id),
  balance_date   DATE,
  reported       NUMERIC(20,4) NOT NULL,
  posted_before  NUMERIC(20,4) NOT NULL,
  pending_before NUMERIC(20,4) NOT NULL,
  basis          TEXT NOT NULL CHECK (basis IN ('posted_only', 'pending_inclusive')),
  horizon        DATE NOT NULL,
  amount         NUMERIC(20,4) NOT NULL,
  created_by     UUID REFERENCES api_tokens(id),
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

DOWNGRADE_DDL = """
DROP TABLE account_starting_balances;
DELETE FROM accounts a
 WHERE a.path = CAST('equity.opening_balances' AS ltree) AND a.type = 'equity'
   AND NOT EXISTS (SELECT 1 FROM postings p WHERE p.account_id = a.id);
"""


def upgrade() -> None:
    op.execute(UPGRADE_DDL)


def downgrade() -> None:
    op.execute(DOWNGRADE_DDL)
