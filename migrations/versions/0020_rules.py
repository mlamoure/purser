"""Deterministic server-side rules (issue #174, DESIGN §3.1/§5/§7.1, decision 46).

Adds ``rules``: user- or AI-authored deterministic instructions that run where
automation already touches rows — the ``_import_account`` chokepoint and the
nightly ``categorize.run`` / ``auto_link_pass`` sweep. Three types:

- ``categorize``  sets the category leg. ``category_id`` may name a virtual
  account (an ordinary category) OR a real one, which is the `[account]`
  transfer sugar and the deterministic twin of the AI classifier (decision 49).
- ``rename``      replaces the PAYEE (never the bank's description — three
  deterministic identities read that string: content_hash, masked_variant,
  and decision 45's replay).
- ``transfer_link`` declares an account pair for which the existing
  exact-amount / window / posted-only / cross-account machinery no longer
  needs ``has_transfer_signal``.

The predicate is structured, never regex (see purser/core/rules.py for why).
Evaluation is first-match by ``(priority, created_at, id)``.

Also adds ``payees.merged_into_id``: retroactive ``rename`` aliases the payee
ENTITY rather than re-pointing ``transactions.payee_id``, which migration
0005's allow-list makes immutable on posted rows — exactly the history a
rename wants to consolidate.

Revision ID: 0020
Revises: 0019
Create Date: 2026-08-07

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE_DDL = """
CREATE TABLE rules (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  type           TEXT NOT NULL CHECK (type IN ('categorize','rename','transfer_link')),
  state          TEXT NOT NULL DEFAULT 'draft'
                 CHECK (state IN ('draft','active','disabled','archived')),
  priority       INTEGER NOT NULL DEFAULT 100,
  account_ids    UUID[],
  match_field    TEXT CHECK (match_field IN ('payee','description','payee_or_description')),
  match_mode     TEXT CHECK (match_mode IN ('equals','prefix','contains')),
  match_text_enc BYTEA,
  match_text_bidx TEXT,
  amount_exact   NUMERIC(20,4),
  amount_min     NUMERIC(20,4),
  amount_max     NUMERIC(20,4),
  direction      TEXT CHECK (direction IN ('debit','credit')),
  effective_from DATE,
  category_id    UUID REFERENCES accounts(id),
  payee_name_enc BYTEA,
  pair_account_a UUID REFERENCES accounts(id),
  pair_account_b UUID REFERENCES accounts(id),
  note_enc       BYTEA,
  applied_count  INTEGER NOT NULL DEFAULT 0,
  last_applied_at TIMESTAMPTZ,
  created_by     UUID REFERENCES api_tokens(id),
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- The three text-predicate columns are set or unset together, so a
  -- half-built predicate can never read as "unconstrained" and match
  -- everything.
  CONSTRAINT rules_match_shape CHECK (
    (match_field IS NULL) = (match_mode IS NULL)
    AND (match_mode IS NULL) = (match_text_enc IS NULL)),
  CONSTRAINT rules_action_shape CHECK (
    (type='categorize'    AND category_id IS NOT NULL AND payee_name_enc IS NULL
                          AND pair_account_a IS NULL) OR
    (type='rename'        AND payee_name_enc IS NOT NULL AND category_id IS NULL
                          AND pair_account_a IS NULL) OR
    -- A transfer_link rule carries NO text predicate: its account pair IS its
    -- scope. Only amount_exact (compared on abs()) and effective_from narrow it.
    (type='transfer_link' AND pair_account_a IS NOT NULL AND pair_account_b IS NOT NULL
                          AND pair_account_a <> pair_account_b
                          AND category_id IS NULL AND payee_name_enc IS NULL
                          AND match_field IS NULL AND account_ids IS NULL
                          AND direction IS NULL AND amount_min IS NULL AND amount_max IS NULL)),
  CONSTRAINT rules_amount_shape CHECK (
    amount_exact IS NULL OR (amount_min IS NULL AND amount_max IS NULL))
);

CREATE INDEX rules_eval_order ON rules (state, type, priority, created_at, id);

-- Retroactive rename aliases the payee entity; reads resolve through the
-- chain and payee_memory aggregates over the resolved id. Self-reference is
-- rejected here; deeper cycles are guarded in the service layer, which also
-- bounds chain depth.
ALTER TABLE payees ADD COLUMN merged_into_id UUID REFERENCES payees(id);
ALTER TABLE payees ADD CONSTRAINT payees_merged_into_not_self
  CHECK (merged_into_id IS NULL OR merged_into_id <> id);
CREATE INDEX payees_merged_into_id ON payees (merged_into_id)
  WHERE merged_into_id IS NOT NULL;
"""

DOWNGRADE_DDL = """
DROP INDEX IF EXISTS payees_merged_into_id;
ALTER TABLE payees DROP CONSTRAINT IF EXISTS payees_merged_into_not_self;
ALTER TABLE payees DROP COLUMN IF EXISTS merged_into_id;
DROP INDEX IF EXISTS rules_eval_order;
DROP TABLE IF EXISTS rules;
"""


def upgrade() -> None:
    op.execute(UPGRADE_DDL)


def downgrade() -> None:
    op.execute(DOWNGRADE_DDL)
