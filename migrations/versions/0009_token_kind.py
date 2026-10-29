"""Add api_tokens.kind: browser sessions vs long-lived API tokens (issue #99).

Sessions and API tokens share one table because both are audit/attribution
anchors (audit_log.actor_token_id, transactions.created_by, matches.decided_by
all FK here) — rows referenced by the ledger are never deleted, only
soft-revoked via expires_at. The 'session:' name prefix was the only
discriminator and it was load-bearing AND client-spoofable: ledger origin and
schedule source classified human-vs-agent by name. `kind` is server-controlled;
only the login paths mint kind='session'.

Backfill by name prefix preserves the current runtime classification exactly
(every real session is named 'session:{method}:{email}'). Legacy session rows
without an expiry get one (created_at + 7 days, the TTL they were minted
under) so "sessions always expire" holds for pre-existing data too.

DESIGN.md §3.1/§9/§12 updated in the same PR (schema rule).

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-11

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DDL = """
ALTER TABLE api_tokens
  ADD COLUMN kind TEXT NOT NULL DEFAULT 'api'
  CONSTRAINT api_tokens_kind_check CHECK (kind IN ('session', 'api'));

UPDATE api_tokens SET kind = 'session' WHERE name LIKE 'session:%';

UPDATE api_tokens SET expires_at = created_at + interval '7 days'
 WHERE kind = 'session' AND expires_at IS NULL;
"""

DOWNGRADE_DDL = """
ALTER TABLE api_tokens DROP COLUMN IF EXISTS kind;
"""


def upgrade() -> None:
    op.execute(DDL)


def downgrade() -> None:
    op.execute(DOWNGRADE_DDL)
