"""Initial schema — full DESIGN.md section 3 DDL plus guard triggers.

Creates all v1 tables in one migration because the DDL is authoritative in
DESIGN.md and has cross-workstream FK cycles (transactions <-> schedules).
Guard triggers enforce the non-negotiables at the database layer:

- raw_fetches is append-only (no UPDATE / DELETE / TRUNCATE)
- postings per (transaction, currency) sum to zero for pending/posted
  transactions (constraint trigger, deferred to commit)
- posted transactions are immutable except void-with-links (transfer merges);
  corrections happen as linked reversal pairs, never in-place edits

Revision ID: 0001
Revises:
Create Date: 2026-07-03

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DDL = """
CREATE EXTENSION IF NOT EXISTS ltree;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE users (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  email         TEXT UNIQUE NOT NULL,
  display_name  TEXT,
  role          TEXT NOT NULL DEFAULT 'member'
                CHECK (role IN ('member', 'admin')),
  oidc_subject  TEXT UNIQUE,
  password_hash TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE api_tokens (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id      UUID NOT NULL REFERENCES users(id),
  name         TEXT NOT NULL,
  token_hash   TEXT NOT NULL,
  scopes       TEXT[] NOT NULL,
  expires_at   TIMESTAMPTZ,
  last_used_at TIMESTAMPTZ,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX api_tokens_hash ON api_tokens (token_hash);

CREATE TABLE connections (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  provider       TEXT NOT NULL DEFAULT 'simplefin',
  access_url_enc BYTEA NOT NULL,
  status         TEXT NOT NULL DEFAULT 'active',
  last_fetch_at  TIMESTAMPTZ,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE raw_fetches (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  connection_id UUID NOT NULL REFERENCES connections(id),
  fetched_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  content_hash  TEXT NOT NULL,
  payload       JSONB NOT NULL
);
CREATE INDEX raw_fetches_connection ON raw_fetches (connection_id, fetched_at);

CREATE TABLE accounts (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  parent_id     UUID REFERENCES accounts(id),
  name          TEXT NOT NULL,
  type          TEXT NOT NULL
                CHECK (type IN ('checking', 'savings', 'cash', 'credit_card', 'brokerage',
                                'property', 'loan', 'category_expense', 'category_income')),
  is_virtual    BOOLEAN NOT NULL DEFAULT false,
  path          LTREE NOT NULL,
  currency      CHAR(3) NOT NULL DEFAULT 'USD',
  external_id   TEXT,
  connection_id UUID REFERENCES connections(id),
  min_balance   NUMERIC(20,4),
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX accounts_path_gist ON accounts USING GIST (path);
CREATE UNIQUE INDEX accounts_path_uniq ON accounts (path);

CREATE TABLE payees (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name            TEXT NOT NULL,
  normalized_name TEXT NOT NULL
);
CREATE INDEX payees_norm ON payees (normalized_name);

CREATE TABLE schedules (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name            TEXT NOT NULL,
  account_id      UUID NOT NULL REFERENCES accounts(id),
  payee_id        UUID REFERENCES payees(id),
  category_path   LTREE,
  rrule           TEXT NOT NULL,
  amount_type     TEXT NOT NULL
                  CHECK (amount_type IN ('fixed', 'estimated')),
  amount          NUMERIC(20,4),
  estimate_basis  TEXT
                  CHECK (estimate_basis IN ('last', 'rolling_avg_3', 'rolling_avg_12')),
  currency        CHAR(3) NOT NULL DEFAULT 'USD',
  next_due        DATE,
  autopost_policy TEXT NOT NULL DEFAULT 'wait_for_match'
                  CHECK (autopost_policy IN ('auto_post', 'wait_for_match', 'reminder_only')),
  source          TEXT NOT NULL
                  CHECK (source IN ('manual', 'detected', 'agent')),
  status          TEXT NOT NULL DEFAULT 'active',
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE transactions (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  date            DATE NOT NULL,
  payee_id        UUID REFERENCES payees(id),
  description     TEXT,
  status          TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('expected', 'pending', 'posted', 'void')),
  origin          TEXT NOT NULL
                  CHECK (origin IN ('import', 'agent', 'human', 'rule', 'system')),
  content_hash    TEXT,
  schedule_id     UUID REFERENCES schedules(id),
  source_fetch_id UUID REFERENCES raw_fetches(id),
  superseded_by   UUID REFERENCES transactions(id),
  reverses_id     UUID REFERENCES transactions(id),
  created_by      UUID REFERENCES api_tokens(id),
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- content_hash embeds the external account, so a global partial unique index
-- gives per-source-account dedup.
CREATE UNIQUE INDEX transactions_content_hash_uniq
  ON transactions (content_hash) WHERE content_hash IS NOT NULL;
CREATE INDEX transactions_date ON transactions (date);
CREATE INDEX transactions_status ON transactions (status);
CREATE INDEX transactions_schedule ON transactions (schedule_id)
  WHERE schedule_id IS NOT NULL;
CREATE INDEX transactions_source_fetch ON transactions (source_fetch_id)
  WHERE source_fetch_id IS NOT NULL;

CREATE TABLE postings (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  transaction_id UUID NOT NULL REFERENCES transactions(id),
  account_id     UUID NOT NULL REFERENCES accounts(id),
  amount         NUMERIC(20,4) NOT NULL,
  currency       CHAR(3) NOT NULL DEFAULT 'USD',
  memo           TEXT
);
CREATE INDEX postings_transaction ON postings (transaction_id);
CREATE INDEX postings_account ON postings (account_id);

CREATE TABLE matches (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  expected_id UUID NOT NULL REFERENCES transactions(id),
  posted_id   UUID NOT NULL REFERENCES transactions(id),
  score       REAL NOT NULL,
  variance    NUMERIC(20,4),
  state       TEXT NOT NULL
              CHECK (state IN ('proposed', 'confirmed', 'rejected')),
  decided_by  UUID REFERENCES api_tokens(id),
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX matches_expected ON matches (expected_id);
CREATE INDEX matches_posted ON matches (posted_id);

CREATE TABLE holdings_snapshots (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id   UUID NOT NULL REFERENCES accounts(id),
  as_of        DATE NOT NULL,
  symbol       TEXT NOT NULL,
  quantity     NUMERIC(20,6),
  cost_basis   NUMERIC(20,4),
  market_value NUMERIC(20,4),
  lot_id       UUID,
  raw_fetch_id UUID REFERENCES raw_fetches(id)
);
CREATE INDEX holdings_snapshots_account ON holdings_snapshots (account_id, as_of);

CREATE TABLE valuations (
  id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id UUID NOT NULL REFERENCES accounts(id),
  as_of      DATE NOT NULL,
  value      NUMERIC(20,4) NOT NULL,
  source     TEXT NOT NULL
             CHECK (source IN ('manual', 'statement', 'api'))
);
CREATE INDEX valuations_account ON valuations (account_id, as_of);

CREATE TABLE events (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  type        TEXT NOT NULL,
  entity_type TEXT,
  entity_id   UUID,
  payload     JSONB NOT NULL
);
CREATE INDEX events_at ON events (at);
CREATE INDEX events_type ON events (type, at);

CREATE TABLE webhook_subscriptions (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  url         TEXT NOT NULL,
  event_types TEXT[] NOT NULL,
  secret_enc  BYTEA,
  active      BOOLEAN NOT NULL DEFAULT true
);

CREATE TABLE audit_log (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  at             TIMESTAMPTZ NOT NULL DEFAULT now(),
  actor_token_id UUID REFERENCES api_tokens(id),
  action         TEXT NOT NULL,
  entity_type    TEXT NOT NULL,
  entity_id      UUID,
  before_after   JSONB
);
CREATE INDEX audit_log_at ON audit_log (at);
CREATE INDEX audit_log_entity ON audit_log (entity_type, entity_id);

-- ---------------------------------------------------------------------------
-- Guard 1: raw_fetches is append-only.
-- ---------------------------------------------------------------------------
CREATE FUNCTION raw_fetches_append_only() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'raw_fetches is append-only';
END $$ LANGUAGE plpgsql;

CREATE TRIGGER raw_fetches_no_update_delete
  BEFORE UPDATE OR DELETE ON raw_fetches
  FOR EACH ROW EXECUTE FUNCTION raw_fetches_append_only();

CREATE TRIGGER raw_fetches_no_truncate
  BEFORE TRUNCATE ON raw_fetches
  FOR EACH STATEMENT EXECUTE FUNCTION raw_fetches_append_only();

-- ---------------------------------------------------------------------------
-- Guard 2: postings per (transaction, currency) sum to zero for
-- pending/posted transactions. Deferred to commit so multi-row inserts can
-- balance within a database transaction.
-- ---------------------------------------------------------------------------
CREATE FUNCTION check_postings_balance() RETURNS trigger AS $$
DECLARE
  tx_ids UUID[];
  bad RECORD;
BEGIN
  -- An UPDATE can move a posting between transactions; both sides must
  -- balance, so collect every affected transaction id.
  IF TG_TABLE_NAME = 'postings' THEN
    IF TG_OP = 'DELETE' THEN
      tx_ids := ARRAY[OLD.transaction_id];
    ELSIF TG_OP = 'UPDATE' THEN
      tx_ids := ARRAY[OLD.transaction_id, NEW.transaction_id];
    ELSE
      tx_ids := ARRAY[NEW.transaction_id];
    END IF;
  ELSE
    tx_ids := ARRAY[NEW.id];
  END IF;

  SELECT p.transaction_id, p.currency, sum(p.amount) AS total INTO bad
  FROM postings p
  JOIN transactions t ON t.id = p.transaction_id
  WHERE p.transaction_id = ANY(tx_ids)
    AND t.status IN ('pending', 'posted')
  GROUP BY p.transaction_id, p.currency
  HAVING sum(p.amount) <> 0
  LIMIT 1;

  IF FOUND THEN
    RAISE EXCEPTION
      'postings for transaction % do not sum to zero (currency %, total %)',
      bad.transaction_id, bad.currency, bad.total;
  END IF;
  RETURN NULL;
END $$ LANGUAGE plpgsql;

CREATE CONSTRAINT TRIGGER postings_zero_sum
  AFTER INSERT OR UPDATE OR DELETE ON postings
  DEFERRABLE INITIALLY DEFERRED
  FOR EACH ROW EXECUTE FUNCTION check_postings_balance();

CREATE CONSTRAINT TRIGGER transactions_zero_sum
  AFTER INSERT OR UPDATE OF status ON transactions
  DEFERRABLE INITIALLY DEFERRED
  FOR EACH ROW EXECUTE FUNCTION check_postings_balance();

-- ---------------------------------------------------------------------------
-- Guard 3: posted transactions are immutable. The only permitted changes to a
-- posted row are void-with-links (transfer merges set status='void' and
-- superseded_by). Corrections are new linked reversal transactions.
-- ---------------------------------------------------------------------------
CREATE FUNCTION transactions_posted_guard() RETURNS trigger AS $$
BEGIN
  IF OLD.status = 'posted' THEN
    IF NEW.date IS DISTINCT FROM OLD.date
       OR NEW.payee_id IS DISTINCT FROM OLD.payee_id
       OR NEW.description IS DISTINCT FROM OLD.description
       OR NEW.origin IS DISTINCT FROM OLD.origin
       OR NEW.content_hash IS DISTINCT FROM OLD.content_hash
       OR NEW.schedule_id IS DISTINCT FROM OLD.schedule_id
       OR NEW.source_fetch_id IS DISTINCT FROM OLD.source_fetch_id
       OR NEW.reverses_id IS DISTINCT FROM OLD.reverses_id
       OR NEW.created_by IS DISTINCT FROM OLD.created_by
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
       OR (NEW.status IS DISTINCT FROM OLD.status AND NEW.status <> 'void')
    THEN
      RAISE EXCEPTION
        'posted transactions are immutable; corrections are linked reversals';
    END IF;
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER transactions_posted_immutable
  BEFORE UPDATE ON transactions
  FOR EACH ROW EXECUTE FUNCTION transactions_posted_guard();

CREATE FUNCTION transactions_posted_no_delete() RETURNS trigger AS $$
BEGIN
  IF OLD.status = 'posted' THEN
    RAISE EXCEPTION 'posted transactions cannot be deleted';
  END IF;
  RETURN OLD;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER transactions_posted_undeletable
  BEFORE DELETE ON transactions
  FOR EACH ROW EXECUTE FUNCTION transactions_posted_no_delete();

CREATE FUNCTION postings_posted_guard() RETURNS trigger AS $$
DECLARE
  tx_status TEXT;
BEGIN
  SELECT status INTO tx_status FROM transactions WHERE id = OLD.transaction_id;
  IF tx_status = 'posted' THEN
    RAISE EXCEPTION 'postings of posted transactions are immutable';
  END IF;
  IF TG_OP = 'DELETE' THEN
    RETURN OLD;
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER postings_posted_immutable
  BEFORE UPDATE OR DELETE ON postings
  FOR EACH ROW EXECUTE FUNCTION postings_posted_guard();
"""

DROP = """
DROP TABLE IF EXISTS audit_log, webhook_subscriptions, events, valuations,
  holdings_snapshots, matches, postings, transactions, schedules, payees,
  accounts, raw_fetches, connections, api_tokens, users CASCADE;
DROP FUNCTION IF EXISTS raw_fetches_append_only, check_postings_balance,
  transactions_posted_guard, transactions_posted_no_delete,
  postings_posted_guard CASCADE;
"""


def upgrade() -> None:
    op.execute(DDL)


def downgrade() -> None:
    op.execute(DROP)
