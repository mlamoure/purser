"""Transfer-link suggestions and payee-scoped transfer_link rules (issue #271,
DESIGN decision 67).

A manual ``confirm_transfer`` used to teach nothing — or, in the rejected #253
design, a hidden payee memory that auto-linked from a single confirm. Instead
a confirm now proposes a ``transfer_link`` rule in the suggestions inbox, and
that rule may be scoped to the confirm's payee:

- ``suggestions.kind`` gains ``transfer_link`` (the first kind after
  ``schedule``) and ``suggestions.source`` gains ``confirm`` (the suggestion
  exists because a human confirmed a pair).
- ``rules.payee_scope_id`` narrows a declared pair to rows where either leg's
  payee belongs to that payee's alias group. ``transfer_link`` rules only;
  NULL is the whole pair, which is every rule before this migration, so there
  is no backfill.

Downgrade drops the scope, which would silently widen a scoped rule to its
whole account pair — a drift-blind change the activation confirm exists to
gate (decision 46). So every ACTIVE scoped rule is disabled first: re-activating
it re-runs the confirm against the broader count. ``transfer_link`` suggestions
cannot satisfy the restored ``kind`` CHECK and are deleted; nothing references
them but audit rows, which are not FKs.

Revision ID: 0030
Revises: 0029
Create Date: 2026-10-04

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE_DDL = """
ALTER TABLE suggestions DROP CONSTRAINT IF EXISTS suggestions_kind_check;
ALTER TABLE suggestions ADD CONSTRAINT suggestions_kind_check
  CHECK (kind IN ('schedule', 'transfer_link'));
ALTER TABLE suggestions DROP CONSTRAINT IF EXISTS suggestions_source_check;
ALTER TABLE suggestions ADD CONSTRAINT suggestions_source_check
  CHECK (source IN ('detected', 'agent', 'account_added', 'confirm'));

ALTER TABLE rules ADD COLUMN payee_scope_id UUID REFERENCES payees(id);
ALTER TABLE rules ADD CONSTRAINT rules_payee_scope_shape CHECK (
  payee_scope_id IS NULL OR type = 'transfer_link');
"""

DOWNGRADE_DDL = """
UPDATE rules SET state = 'disabled', updated_at = now()
  WHERE payee_scope_id IS NOT NULL AND state = 'active';
ALTER TABLE rules DROP CONSTRAINT IF EXISTS rules_payee_scope_shape;
ALTER TABLE rules DROP COLUMN IF EXISTS payee_scope_id;

DELETE FROM suggestions WHERE kind = 'transfer_link' OR source = 'confirm';
ALTER TABLE suggestions DROP CONSTRAINT IF EXISTS suggestions_source_check;
ALTER TABLE suggestions ADD CONSTRAINT suggestions_source_check
  CHECK (source IN ('detected', 'agent', 'account_added'));
ALTER TABLE suggestions DROP CONSTRAINT IF EXISTS suggestions_kind_check;
ALTER TABLE suggestions ADD CONSTRAINT suggestions_kind_check
  CHECK (kind IN ('schedule'));
"""


def upgrade() -> None:
    op.execute(UPGRADE_DDL)


def downgrade() -> None:
    op.execute(DOWNGRADE_DDL)
