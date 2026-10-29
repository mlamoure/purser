"""Memories — user-owned financial context for the user's AI (issue #176,
DESIGN §3.6, decision 48).

Short notes of financial nuance ("the monthly $750 to Fidelity College is her
529 contribution") that Purser serves to connected agents as context.

**Non-executable by construction.** No code path parses memory text: there is
no evaluator, no import hook, no scheduler entry that reads one. If a nuance
should *act*, it graduates into a #174 rule via ``rules.memory_id``, added
here — memories inform, rules decide.

Links go in a table rather than nullable FKs because the motivating example
("the card is paid from checking") spans TWO accounts, so a single
account_id column would be wrong on the first real memory. Categories are
virtual accounts (§3.2), so one nullable pair covers all three link kinds.

Revision ID: 0022
Revises: 0021
Create Date: 2026-08-08

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE_DDL = """
CREATE TABLE memories (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  -- Fernet over the note (ORM attr: text). Capped at 500 plaintext chars by
  -- the service layer. No blind index: nothing looks a memory up by text
  -- equality — scoping is by FK, and Config-UI search is decrypt-and-filter
  -- over at most 100 rows.
  text_enc    BYTEA NOT NULL,
  state       TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('draft','active')),
  pinned      BOOLEAN NOT NULL DEFAULT false,
  archived_at TIMESTAMPTZ,
  -- NULL identifies a Purser-authored pass (the categorizer's convention);
  -- a token's `kind` resolves user-vs-agent for the rest (decision 24).
  created_by  UUID REFERENCES api_tokens(id),
  updated_by  UUID REFERENCES api_tokens(id),
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT memories_draft_not_pinned   CHECK (state = 'active' OR NOT pinned),
  CONSTRAINT memories_draft_not_archived CHECK (state = 'active' OR archived_at IS NULL)
);

CREATE INDEX memories_serving ON memories (pinned DESC, updated_at DESC)
  WHERE state = 'active' AND archived_at IS NULL;

CREATE TABLE memory_links (
  id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  memory_id  UUID NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  -- Exactly one target. `accounts` covers BOTH real accounts and categories.
  account_id UUID REFERENCES accounts(id),
  payee_id   UUID REFERENCES payees(id),
  CONSTRAINT memory_links_one_target CHECK ((account_id IS NULL) <> (payee_id IS NULL))
);

CREATE UNIQUE INDEX memory_links_uniq_account ON memory_links (memory_id, account_id)
  WHERE account_id IS NOT NULL;
CREATE UNIQUE INDEX memory_links_uniq_payee ON memory_links (memory_id, payee_id)
  WHERE payee_id IS NOT NULL;
CREATE INDEX memory_links_account ON memory_links (account_id) WHERE account_id IS NOT NULL;
CREATE INDEX memory_links_payee ON memory_links (payee_id) WHERE payee_id IS NOT NULL;

-- The #174 interlock: a rule may carry the memory that motivated it. Nullable
-- and one-directional — memories know nothing about rules except through this
-- reverse lookup, so archiving a memory never disables its rules.
ALTER TABLE rules ADD COLUMN memory_id UUID REFERENCES memories(id);
CREATE INDEX rules_memory_id ON rules (memory_id) WHERE memory_id IS NOT NULL;
"""

DOWNGRADE_DDL = """
DROP INDEX IF EXISTS rules_memory_id;
ALTER TABLE rules DROP COLUMN IF EXISTS memory_id;
DROP TABLE IF EXISTS memory_links;
DROP TABLE IF EXISTS memories;
"""


def upgrade() -> None:
    op.execute(UPGRADE_DDL)


def downgrade() -> None:
    op.execute(DOWNGRADE_DDL)
