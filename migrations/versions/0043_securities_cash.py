"""Sale proceeds move cash; a cash-sweep feed balance can be checked (issue
#390 phase 1b, decision 103).

- ``equity.securities``: a system virtual account of the new type ``equity``
  (0001's ``accounts_type_check`` is widened to admit it, and
  ``accounts_equity_virtual`` keeps the type virtual).
  It is NOT a category (``category_expense`` / ``category_income`` stay the
  only category types), so spending, income, the classifier, rules and
  schedules never see it. The cash of a securities trade balances against it:
  a sale's net proceeds land on the brokerage account (+) and on
  ``equity.securities`` (-).
- ``position_events`` gains ``proceeds_to`` (sells: ``cash`` | ``none``),
  ``funded_from`` (buys: ``cash`` | ``none``), ``settle_on`` (when the cash
  posts, the trade date or later) and ``transaction_id`` (the trade's cash
  row: posted by Purser, or a feed row linked to it). One trade per cash row;
  every event naming one is indexed.
- ``accounts.feed_balance_is_cash``: an admin opt-in, only on a brokerage
  account tracked by hand, saying its feed balance is the account's CASH. The
  post-sync drift check then compares that balance with the account's
  postings, like a checking account's.
- ``holdings_snapshots.in_transit`` (decision 104): on a manual row, the
  cash of a trade that lands after the trade date, counted in the row's
  market value until it lands, so net worth does not dip between the two.

DESIGN.md §3.1/§7.3/§8 updated in the same PR (schema rule).

Revision ID: 0043
Revises: 0042
Create Date: 2026-10-18

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0043"
down_revision: str | None = "0042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE_DDL = """
-- The new system type: virtual, and never a category (0001's inline CHECK is
-- widened; categories keep their two types).
ALTER TABLE accounts DROP CONSTRAINT accounts_type_check;
ALTER TABLE accounts
  ADD CONSTRAINT accounts_type_check
  CHECK (type IN ('checking', 'savings', 'cash', 'credit_card', 'brokerage',
                  'property', 'loan', 'category_expense', 'category_income', 'equity'));
ALTER TABLE accounts
  ADD CONSTRAINT accounts_equity_virtual CHECK (type <> 'equity' OR is_virtual);

-- The system account trade cash balances against (seeded once; the service
-- recreates it if it is ever missing).
INSERT INTO accounts (name, type, is_virtual, path, description)
SELECT 'Securities', 'equity', true, CAST('equity.securities' AS ltree),
       'System account: the other side of a securities trade''s cash. Not a category.'
WHERE NOT EXISTS (SELECT 1 FROM accounts WHERE path = CAST('equity.securities' AS ltree));

ALTER TABLE position_events
  ADD COLUMN proceeds_to TEXT,
  ADD COLUMN funded_from TEXT,
  ADD COLUMN settle_on DATE,
  ADD COLUMN transaction_id UUID REFERENCES transactions(id);
ALTER TABLE position_events
  ADD CONSTRAINT position_events_proceeds_shape
  CHECK (proceeds_to IS NULL OR (kind = 'sell' AND proceeds_to IN ('cash', 'none')));
ALTER TABLE position_events
  ADD CONSTRAINT position_events_funded_shape
  CHECK (funded_from IS NULL OR (kind = 'buy' AND funded_from IN ('cash', 'none')));
ALTER TABLE position_events
  ADD CONSTRAINT position_events_settle_shape
  CHECK (settle_on IS NULL OR (kind IN ('buy', 'sell') AND settle_on >= occurred_on));
ALTER TABLE position_events
  ADD CONSTRAINT position_events_cash_shape
  CHECK (transaction_id IS NULL OR kind IN ('buy', 'sell'));
-- A cash row is the cash of one trade at most (reversal rows excluded: they
-- name the row that undid it).
CREATE UNIQUE INDEX position_events_transaction_once
  ON position_events (transaction_id)
  WHERE transaction_id IS NOT NULL AND reverses_id IS NULL;
-- Every event naming a cash row, reversal rows included: the foreign key is
-- checked whenever a transaction is deleted, and a row's trade is looked up
-- by it.
CREATE INDEX position_events_transaction
  ON position_events (transaction_id)
  WHERE transaction_id IS NOT NULL;

ALTER TABLE accounts ADD COLUMN feed_balance_is_cash BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE accounts
  ADD CONSTRAINT accounts_feed_balance_is_cash_manual
  CHECK (NOT feed_balance_is_cash OR (type = 'brokerage' AND holdings_source = 'manual'));

-- A manual row's share of a trade's cash not yet on the account (decision
-- 104): included in market_value from the trade date to the day before the
-- cash lands. Derived, like the rest of a manual row; never on a feed row.
ALTER TABLE holdings_snapshots ADD COLUMN in_transit NUMERIC(20,4);
ALTER TABLE holdings_snapshots
  ADD CONSTRAINT holdings_in_transit_manual CHECK (in_transit IS NULL OR source = 'manual');
"""

DOWNGRADE_DDL = """
ALTER TABLE holdings_snapshots DROP CONSTRAINT IF EXISTS holdings_in_transit_manual;
ALTER TABLE holdings_snapshots DROP COLUMN IF EXISTS in_transit;

ALTER TABLE accounts DROP CONSTRAINT IF EXISTS accounts_feed_balance_is_cash_manual;
ALTER TABLE accounts DROP COLUMN IF EXISTS feed_balance_is_cash;

DROP INDEX IF EXISTS position_events_transaction;
DROP INDEX IF EXISTS position_events_transaction_once;
ALTER TABLE position_events DROP CONSTRAINT IF EXISTS position_events_cash_shape;
ALTER TABLE position_events DROP CONSTRAINT IF EXISTS position_events_settle_shape;
ALTER TABLE position_events DROP CONSTRAINT IF EXISTS position_events_funded_shape;
ALTER TABLE position_events DROP CONSTRAINT IF EXISTS position_events_proceeds_shape;
ALTER TABLE position_events
  DROP COLUMN IF EXISTS transaction_id,
  DROP COLUMN IF EXISTS settle_on,
  DROP COLUMN IF EXISTS funded_from,
  DROP COLUMN IF EXISTS proceeds_to;

-- The seeded account goes only while nothing books to it; a database that
-- has trade cash cannot be downgraded past this revision.
DELETE FROM accounts a
WHERE a.path = CAST('equity.securities' AS ltree) AND a.type = 'equity'
  AND NOT EXISTS (SELECT 1 FROM postings p WHERE p.account_id = a.id);
ALTER TABLE accounts DROP CONSTRAINT IF EXISTS accounts_equity_virtual;
ALTER TABLE accounts DROP CONSTRAINT accounts_type_check;
ALTER TABLE accounts
  ADD CONSTRAINT accounts_type_check
  CHECK (type IN ('checking', 'savings', 'cash', 'credit_card', 'brokerage',
                  'property', 'loan', 'category_expense', 'category_income'));
"""


def upgrade() -> None:
    op.execute(UPGRADE_DDL)


def downgrade() -> None:
    op.execute(DOWNGRADE_DDL)
