"""Proposed matches in the suggestions inbox (issue #246 phase 1, DESIGN
decision 86).

A match proposal (``matches.state = 'proposed'``) gets a mirror card in the
inbox: ``suggestions.kind`` gains ``match``. The ``matches`` row stays the
source of truth — the ledger relies on a proposal holding its rows — and the
card only renders it, carries snooze, and remembers a rejection.

A card the system closes (the proposal was rescinded, carried, purged or
auto-confirmed) is ``withdrawn``, kept rather than deleted, so
``suggestions.status`` gains ``withdrawn``. Nothing to backfill: the nightly
matching pass's sweep creates the cards for proposals that already exist.

Downgrade deletes every match card and every withdrawn row (neither fits the
restored CHECKs). Nothing references them but audit rows, which are not FKs.
Rejection memory goes with them, so a rejected pair may be proposed again.

Revision ID: 0034
Revises: 0033
Create Date: 2026-10-13

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE_DDL = """
ALTER TABLE suggestions DROP CONSTRAINT IF EXISTS suggestions_kind_check;
ALTER TABLE suggestions ADD CONSTRAINT suggestions_kind_check
  CHECK (kind IN ('schedule', 'transfer_link', 'match'));
ALTER TABLE suggestions DROP CONSTRAINT IF EXISTS suggestions_status_check;
ALTER TABLE suggestions ADD CONSTRAINT suggestions_status_check
  CHECK (status IN ('open', 'accepted', 'rejected', 'withdrawn'));
"""

DOWNGRADE_DDL = """
DELETE FROM suggestions WHERE kind = 'match' OR status = 'withdrawn';
ALTER TABLE suggestions DROP CONSTRAINT IF EXISTS suggestions_status_check;
ALTER TABLE suggestions ADD CONSTRAINT suggestions_status_check
  CHECK (status IN ('open', 'accepted', 'rejected'));
ALTER TABLE suggestions DROP CONSTRAINT IF EXISTS suggestions_kind_check;
ALTER TABLE suggestions ADD CONSTRAINT suggestions_kind_check
  CHECK (kind IN ('schedule', 'transfer_link'));
"""


def upgrade() -> None:
    op.execute(UPGRADE_DDL)


def downgrade() -> None:
    op.execute(DOWNGRADE_DDL)
