"""Account lifecycle: hide + close (DESIGN §3.4).

Adds two nullable timestamps to accounts:

- ``hidden_at`` — display-only: the account drops out of the default
  ``GET /accounts`` listing but still syncs, still counts in net worth.
- ``closed_at`` — read-only: no new postings may land on the account. The
  service layer is the primary chokepoint (``_assert_accounts_open``); this
  migration adds a BEFORE INSERT trigger as the structural backstop, in the
  house style of migration 0005.

Both are real-account-only: the ``accounts_virtual_lifecycle_null`` CHECK keeps
categories (virtual accounts) out of the lifecycle entirely.

Revision ID: 0015
Revises: 0014
Create Date: 2026-07-12

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014_backfill_cursor"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE_DDL = """
ALTER TABLE accounts
  ADD COLUMN hidden_at TIMESTAMPTZ NULL,
  ADD COLUMN closed_at TIMESTAMPTZ NULL;

-- Lifecycle timestamps are for real accounts only; categories never hide/close.
ALTER TABLE accounts
  ADD CONSTRAINT accounts_virtual_lifecycle_null
  CHECK (NOT is_virtual OR (hidden_at IS NULL AND closed_at IS NULL));

-- ---------------------------------------------------------------------------
-- postings_closed_account_guard: no new posting may land on a closed account.
-- The service layer refuses first (LedgerError -> 422); this BEFORE INSERT
-- trigger is the structural backstop that also catches raw SQL. INSERT-only,
-- so it never interferes with correction/void UPDATEs or with deletes.
-- ---------------------------------------------------------------------------
CREATE FUNCTION postings_closed_account_guard() RETURNS trigger AS $$
DECLARE
  acct_closed TIMESTAMPTZ;
  acct_path TEXT;
BEGIN
  SELECT closed_at, path::text INTO acct_closed, acct_path
    FROM accounts WHERE id = NEW.account_id;
  IF acct_closed IS NOT NULL THEN
    RAISE EXCEPTION 'account % is closed (read-only)', acct_path;
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER postings_closed_account_guard
  BEFORE INSERT ON postings
  FOR EACH ROW EXECUTE FUNCTION postings_closed_account_guard();
"""

DOWNGRADE_DDL = """
DROP TRIGGER IF EXISTS postings_closed_account_guard ON postings;
DROP FUNCTION IF EXISTS postings_closed_account_guard();
ALTER TABLE accounts DROP CONSTRAINT IF EXISTS accounts_virtual_lifecycle_null;
ALTER TABLE accounts DROP COLUMN IF EXISTS closed_at;
ALTER TABLE accounts DROP COLUMN IF EXISTS hidden_at;
"""


def upgrade() -> None:
    op.execute(UPGRADE_DDL)


def downgrade() -> None:
    op.execute(DOWNGRADE_DDL)
