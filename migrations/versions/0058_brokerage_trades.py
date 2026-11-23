"""Brokerage trades and the balance true-up (issue #591, DESIGN decision 119).

- ``accounts.balance_true_up``: after each sync, Purser posts a visible system
  adjustment against ``equity.securities`` so a feed-holdings brokerage
  account's postings plus holdings equal the balance its broker reported in
  the same fetch. On by default for feed-holdings brokerage accounts (every
  existing one is switched on here; ``ledger.create_account`` turns it on for
  a new one), and only there (CHECK ``accounts_balance_true_up_feed_brokerage``).
- ``brokerage_true_ups``: one row per adjustment, naming the fetch it came
  from. UNIQUE ``(account_id, raw_fetch_id)``: one fetch never adjusts an
  account twice, and the register labels the row from it.
- ``suggestions.kind`` gains ``trade_rule`` and ``suggestions.source`` gains
  ``mark``: marking a row as a securities trade by hand proposes a categorize
  rule that marks rows like it.
- Two income categories join the shipped set: ``income.gifts`` and
  ``income.employer_contributions``. Only these two paths are seeded (never
  the whole default tree again, so categories a person deleted stay deleted),
  each once and only under an existing ``income`` parent.

Downgrade drops the table (the adjustments stay, ordinary posted rows
against ``equity.securities``, no longer labelled), the column and CHECK,
deletes every ``trade_rule`` suggestion (none fits the restored CHECK; audit
rows are not FKs) and removes the two categories where nothing uses them.

Revision ID: 0058
Revises: 0057
Create Date: 2026-11-21

"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0058"
down_revision: str | None = "0057"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_CATEGORY_PATHS = ("income.gifts", "income.employer_contributions")

UPGRADE_DDL = """
ALTER TABLE accounts ADD COLUMN balance_true_up BOOLEAN NOT NULL DEFAULT false;
UPDATE accounts SET balance_true_up = true
 WHERE type = 'brokerage' AND holdings_source = 'feed' AND NOT is_virtual;
ALTER TABLE accounts ADD CONSTRAINT accounts_balance_true_up_feed_brokerage
  CHECK (NOT balance_true_up OR (type = 'brokerage' AND holdings_source = 'feed'));

CREATE TABLE brokerage_true_ups (
  transaction_id UUID PRIMARY KEY REFERENCES transactions(id) ON DELETE CASCADE,
  account_id     UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  raw_fetch_id   UUID NOT NULL REFERENCES raw_fetches(id),
  balance_date   DATE NOT NULL,
  reported       NUMERIC(20,4) NOT NULL,
  holdings_value NUMERIC(20,4) NOT NULL,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT brokerage_true_ups_once_per_fetch UNIQUE (account_id, raw_fetch_id)
);

ALTER TABLE suggestions DROP CONSTRAINT IF EXISTS suggestions_kind_check;
ALTER TABLE suggestions ADD CONSTRAINT suggestions_kind_check
  CHECK (kind IN ('schedule', 'transfer_link', 'match', 'transfer_pair', 'split', 'trade_rule'));
ALTER TABLE suggestions DROP CONSTRAINT IF EXISTS suggestions_source_check;
ALTER TABLE suggestions ADD CONSTRAINT suggestions_source_check
  CHECK (source IN ('detected', 'agent', 'account_added', 'confirm', 'mark'));
"""

DOWNGRADE_DDL = """
DELETE FROM suggestions WHERE kind = 'trade_rule' OR source = 'mark';
ALTER TABLE suggestions DROP CONSTRAINT IF EXISTS suggestions_source_check;
ALTER TABLE suggestions ADD CONSTRAINT suggestions_source_check
  CHECK (source IN ('detected', 'agent', 'account_added', 'confirm'));
ALTER TABLE suggestions DROP CONSTRAINT IF EXISTS suggestions_kind_check;
ALTER TABLE suggestions ADD CONSTRAINT suggestions_kind_check
  CHECK (kind IN ('schedule', 'transfer_link', 'match', 'transfer_pair', 'split'));

DROP TABLE brokerage_true_ups;

ALTER TABLE accounts DROP CONSTRAINT accounts_balance_true_up_feed_brokerage;
ALTER TABLE accounts DROP COLUMN balance_true_up;
"""

# A shipped category nothing refers to: no child, posting, schedule template,
# rule target or memory link. Only such a row is removed on downgrade.
_DROP_UNUSED_CATEGORY = text(
    """
    DELETE FROM accounts a
     WHERE a.path = CAST(:path AS ltree)
       AND a.is_virtual
       AND NOT EXISTS (SELECT 1 FROM accounts c WHERE c.parent_id = a.id)
       AND NOT EXISTS (SELECT 1 FROM postings p WHERE p.account_id = a.id)
       AND NOT EXISTS (SELECT 1 FROM schedules s WHERE s.category_path = a.path)
       AND NOT EXISTS (SELECT 1 FROM rules r WHERE r.category_id = a.id)
       AND NOT EXISTS (SELECT 1 FROM memory_links m WHERE m.account_id = a.id)
    """
)


def upgrade() -> None:
    op.execute(UPGRADE_DDL)
    from purser.categoryseed import seed_categories

    seed_categories(op.get_bind(), NEW_CATEGORY_PATHS)


def downgrade() -> None:
    op.execute(DOWNGRADE_DDL)
    bind = op.get_bind()
    for path in NEW_CATEGORY_PATHS:
        bind.execute(_DROP_UNUSED_CATEGORY, {"path": path})
