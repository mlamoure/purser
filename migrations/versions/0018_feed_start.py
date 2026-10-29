"""Per-account feed sync-from date (issue #154, DESIGN §7.1, decision 40).

Adds ``accounts.feed_start`` (nullable DATE): the feed importer skips feed
rows dated strictly before it, partitioning an account's history between
manual imports and the SimpleFIN feed as ``manual < boundary <= feed`` — the
persistent mirror of QIF v2's per-request cutoff (decision 35). NULL means
unscoped: exactly the pre-0.10 behavior.

Real-account-only, like the lifecycle timestamps: the
``accounts_virtual_feed_start_null`` CHECK keeps categories out.

Revision ID: 0018
Revises: 0017
Create Date: 2026-07-15

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE_DDL = """
ALTER TABLE accounts
  ADD COLUMN feed_start DATE NULL;

-- Feed sync-from is for real accounts only; categories have no feed.
ALTER TABLE accounts
  ADD CONSTRAINT accounts_virtual_feed_start_null
  CHECK (NOT is_virtual OR feed_start IS NULL);
"""

DOWNGRADE_DDL = """
ALTER TABLE accounts DROP CONSTRAINT IF EXISTS accounts_virtual_feed_start_null;
ALTER TABLE accounts DROP COLUMN IF EXISTS feed_start;
"""


def upgrade() -> None:
    op.execute(UPGRADE_DDL)


def downgrade() -> None:
    op.execute(DOWNGRADE_DDL)
