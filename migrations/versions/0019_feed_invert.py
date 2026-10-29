"""Per-account feed sign inversion (issue #170, DESIGN §7.1, decision 43).

Adds ``accounts.feed_invert`` (bool, default false): the feed importer
negates this account's transaction amounts — strictly AFTER content-hashing,
so a row's identity stays the bytes the bank sent and flipping the flag
never re-imports history. For feeds that deliver sign-inverted streams
(observed: Fidelity brokerage/HSA/401(k) accounts reporting deposits
negative). The QIF importer's per-request ``invert`` (decision 35) is the
manual-import mirror of this persistent feed-side flag.

Real-account-only, like ``feed_start``: the
``accounts_virtual_feed_invert_false`` CHECK keeps categories out.

Revision ID: 0019
Revises: 0018
Create Date: 2026-08-05

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE_DDL = """
ALTER TABLE accounts
  ADD COLUMN feed_invert BOOLEAN NOT NULL DEFAULT false;

-- Feed sign inversion is for real accounts only; categories have no feed.
ALTER TABLE accounts
  ADD CONSTRAINT accounts_virtual_feed_invert_false
  CHECK (NOT is_virtual OR NOT feed_invert);
"""

DOWNGRADE_DDL = """
ALTER TABLE accounts DROP CONSTRAINT IF EXISTS accounts_virtual_feed_invert_false;
ALTER TABLE accounts DROP COLUMN IF EXISTS feed_invert;
"""


def upgrade() -> None:
    op.execute(UPGRADE_DDL)


def downgrade() -> None:
    op.execute(DOWNGRADE_DDL)
