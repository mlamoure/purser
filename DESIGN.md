# Purser — Design Document

**The finance backend for the personal agent stack.**

Purser is an AI-native, self-hosted personal finance backend. Its intended primary
user is not a person clicking a UI — it is the person's AI agent (an OpenClaw, a
Claude with MCP, any agent runtime). Purser's job is to hold a correct,
auditable, source-traceable financial ledger and expose it through tools an
agent can operate safely. The features people associate with "a finance app" —
analysis, advice, alert judgment, conversation — live in the agent. Purser
builds the tools.

This document is ground truth for v1.0. Implementation tracking: issue #14.

---

## 1. Design principles

1. **Agent-first interfaces.** MCP + REST are the primary interfaces; the web UI
   is administrative only. Tool ergonomics are the product: every aggregate
   question should be answerable in one tool call, in a compact response.
   **Financial computation server-side, interpretation normally client-side.**
   Optional server-side AI categorization and review follow their configured
   review controls; the core works with AI disabled. An agent must
   never need to sum rows in-context to answer an aggregate question.
2. **The books cannot be silently wrong.** Double-entry core behind a flat API.
   Postings per transaction sum to zero per currency — enforced in the app
   layer with a database trigger as backstop. Posted transactions are
   immutable; corrections are linked reversals.
3. **Own the raw feed.** Every SimpleFIN payload is archived append-only,
   verbatim, before anything touches it. Every ledger row is traceable to the
   bytes the bank sent. The ledger is a projection and is re-derivable.
4. **The agent is the user's own.** It acts with the user's authority, and
   Purser makes every write attributable and reversible and every disclosure
   the agent's explicit choice. A user should be able to hand an agent their
   finances and know precisely what it did and what it saw. Trust is a
   feature list, not a value statement: scoped tokens, a full read and
   mutation audit, immutable posted rows corrected by linked reversals, and
   proposing as a choice the agent makes per call when it acts on its own
   initiative (#436, decision 90).
5. **Matching is the center of gravity.** Expected transactions (manual entries
   and schedule instances) reconcile against imported reality. The posted
   amount always wins; the variance is recorded and useful.
6. **Optional AI, BYO provider.** The core is fully functional with AI disabled.
   Model output carries provenance and is attributed and reversible like any
   agent write; what is the user's to decide (activating a rule, a memory, a
   detected schedule) stays a draft or a suggestion.
7. **Share the reporting data.** The SPA includes a forecast chart and MCP
   clients can render widgets. A stable read schema and shipped Grafana
   templates support broader reporting outside the app.

## 2. System architecture

- **Language/framework:** Python. FastAPI + SQLAlchemy 2.x + Pydantic v2.
- **One image, multiple roles:** `purser serve` (API + MCP + static frontend +
  in-process APScheduler), `purser worker` (sync/matching/forecast jobs only),
  `purser migrate`. Default deployment runs `serve` alone; the worker split is
  optional and uses the same image, with `PURSER_DISABLE_SCHEDULER` on the API
  so exactly one scheduler runs (the Helm chart sets it when its worker is
  enabled, issue #160).
- **Datastore:** PostgreSQL only. Relational ledger + append-only JSONB raw
  feed in the same database. Alembic migrations, run on startup by every role
  under a session-level Postgres advisory lock held on the upgrade's own
  connection, so roles starting together upgrade once (issue #215). Extensions:
  `ltree` (category paths), `pgcrypto` (uuid). Amounts are `NUMERIC(20,4)` —
  never floats.
- **Topology (default):** two containers — `purser` + `postgres` — via
  `docker compose up`. Release images are built natively for linux/amd64 and
  linux/arm64 (#426); Apple Silicon verification of the published image is
  pending. Native macOS installation is not supported. The retained Helm chart
  is experimental.
- **Releases:** calendar versions `YYYY.M.PATCH` with no zero padding (also
  valid semver and PEP 440), starting at 2026.10.0, which follows 0.18.0. One
  version is carried by `pyproject.toml`, `purser.__version__` and the chart's
  `appVersion`; the tag is `v<version>` and so is the image tag
  (`ghcr.io/mlamoure/purser:v<version>`, linux/amd64 + linux/arm64).
- **MCP:** standalone FastMCP 4 (on the official SDK v2), streamable HTTP, served
  from the same process, authenticated with the same scoped tokens as REST
  (decision 58).
- **Frontend:** Vite + React + TypeScript static SPA (shadcn/ui + Tailwind,
  TanStack Query, OpenAPI-generated TS client), built at image build time and
  served by FastAPI. No Node in the production image. The SPA consumes the
  same public REST API agents use — no private admin endpoints.
- **Feed paths:** two, chosen per account — Connect (aggregator → SimpleFIN
  Bridge, background sync) and manual QIF import (the user downloads files
  from their bank). Both terminate at the same raw-first pipeline (§7).
- **Currency:** ISO 4217 code on every amount and account from day one.
  v1 behavior is USD-only (no FX, no conversion); reporting tools take a
  `currency` param defaulting to USD. A transaction is single-currency: a
  category leg carries its real leg's currency, so a category of another
  currency is refused (`ledger.check_category_currency`) at create and on
  every recategorize path — an in-place edit, a posted correction, a
  transfer converted back to a category, and the categorize pass — and the
  automated passes (categorize, rule apply-history) skip such a target
  rather than fail on it (#361).

## 3. Data model

Groups: identity (`users`, `api_tokens`) · feed (`connections`, `raw_fetches`) ·
ledger (`accounts`, `payees`, `transactions`, `postings`, `schedules`,
`matches`) · snapshots (`holdings_snapshots`, `valuations`; manually tracked
holdings: `positions`, `position_events`, `security_prices`) · platform
(`events`, `webhook_subscriptions`, `audit_log`).

### 3.1 DDL sketch (authoritative shapes; Alembic owns the real thing)

```sql
CREATE TABLE users (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  email         TEXT NOT NULL,                         -- stored as entered
  display_name  TEXT,
  role          TEXT NOT NULL DEFAULT 'member',        -- member | admin
  oidc_issuer   TEXT,          -- 0041: the SSO link is (issuer, subject); issuer kept
                               --   without a trailing slash; NULL = unlinked, or a
                               --   legacy link the next sign-in attributes (§9)
  oidc_subject  TEXT,                                  -- NULL for local auth
  password_hash TEXT,                                  -- NULL for OIDC-only
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT users_oidc_issuer_needs_subject
    CHECK (oidc_issuer IS NULL OR oidc_subject IS NOT NULL)
);
-- 0041 (#429): emails are unique regardless of case; every lookup is lower() = lower().
CREATE UNIQUE INDEX users_email_lower_key ON users (lower(email));
CREATE UNIQUE INDEX users_oidc_identity_key ON users (oidc_issuer, oidc_subject)
  WHERE oidc_issuer IS NOT NULL;
CREATE UNIQUE INDEX users_oidc_legacy_subject_key ON users (oidc_subject)
  WHERE oidc_issuer IS NULL AND oidc_subject IS NOT NULL;

CREATE TABLE api_tokens (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id      UUID NOT NULL REFERENCES users(id),
  name         TEXT NOT NULL,
  kind         TEXT NOT NULL DEFAULT 'api'
               CHECK (kind IN ('session', 'api')),  -- server-controlled; only login paths mint 'session'
  token_hash   TEXT NOT NULL,
  scopes       TEXT[] NOT NULL,   -- read | write | admin (0037: propose/commit -> write;
                                  --   0038: read:redacted -> read)
  expires_at   TIMESTAMPTZ,
  last_used_at TIMESTAMPTZ,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE app_settings (        -- admin-editable runtime config (issue #102)
  key        TEXT PRIMARY KEY,     -- curated registry: purser/appsettings.py
  value      JSONB,                -- non-secret values (JSON null = explicit null override)
  value_enc  BYTEA,                -- Fernet over the JSON encoding (secret keys)
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_by UUID REFERENCES api_tokens(id),
  CONSTRAINT app_settings_value_shape CHECK ((value IS NULL) <> (value_enc IS NULL))
);

CREATE TABLE connections (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  provider       TEXT NOT NULL DEFAULT 'simplefin',
  -- Encrypted at rest. Nullable since 0006/0008 for the retired
  -- provider='direct' rows; 0010 purged those rows (decision 25), but the
  -- column stays nullable to avoid schema churn.
  access_url_enc BYTEA,
  status         TEXT NOT NULL DEFAULT 'active',
  last_fetch_at  TIMESTAMPTZ,
  -- Historical backfill (issue #126, migration 0014). raw_fetches archives the
  -- payload but NOT the request params, so "how far back have we asked" is
  -- unrecoverable from the archive — these two carry it. backfill_cursor is the
  -- oldest start-date fetched so far (NULL until the first backfill run derives
  -- it from the first sync's window); backfill_complete latches once history is
  -- exhausted. last_fetch_at is deliberately NOT reused (it drives the forward
  -- incremental-sync window; backfill must never move it).
  backfill_cursor   DATE,
  backfill_complete BOOLEAN NOT NULL DEFAULT false,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE raw_fetches (          -- APPEND-ONLY. Never UPDATE, never DELETE.
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  connection_id UUID REFERENCES connections(id),  -- NULL for manual uploads
  fetched_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  content_hash  TEXT NOT NULL,    -- SHA-256 over the canonical PLAINTEXT
  payload_enc   BYTEA NOT NULL,   -- Fernet over that same plaintext (§9):
                                  -- sort_keys JSON for feed fetches, the
                                  -- verbatim file for QIF/CSV uploads
  -- Manual imports are raw-first too (issue #127, migration 0023, decision 53)
  source        TEXT NOT NULL DEFAULT 'simplefin'
                CHECK (source IN ('simplefin', 'qif', 'csv')),
  CONSTRAINT raw_fetches_source_connection
    CHECK ((source = 'simplefin') = (connection_id IS NOT NULL))
);

CREATE TABLE encryption_canary (  -- single row: binds PURSER_ENCRYPTION_KEY
  id                  BOOLEAN PRIMARY KEY DEFAULT true CHECK (id),
  sentinel_enc        BYTEA NOT NULL,        -- decrypted at every startup
  key_fingerprint     TEXT NOT NULL,         -- for mismatch error messages
  key_acknowledged_at TIMESTAMPTZ,           -- wizard "I recorded my key"
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE encryption_keys (    -- wrapped DEKs for the narrative layer (#137)
  purpose    TEXT PRIMARY KEY,    -- 'blind_index' | 'content_hash' | 'payee_ref' (0038, #437)
  key_enc    BYTEA NOT NULL,      -- random 32-byte HMAC key, Fernet-wrapped
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Loaded lazily + cached (purser.crypto). Rotation re-wraps these rows in
-- place, so their unwrapped plaintext — and every name_bidx / content_hash /
-- payee reference derived from them — stays byte-identical (decision 36).
-- payee_ref keys the references agents receive in place of payee names
-- (`py_` + lowercase base32 of HMAC(payee_ref, payee id), first 10 chars;
-- decision 91), so an agent's memory of a reference survives rotate-key.

CREATE TABLE accounts (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  parent_id     UUID REFERENCES accounts(id),
  name          TEXT NOT NULL,
  type          TEXT NOT NULL,
    -- checking | savings | cash | credit_card | brokerage | property | loan
    -- | category_expense | category_income
  is_virtual    BOOLEAN NOT NULL DEFAULT false,   -- true for categories
  path          LTREE NOT NULL,                   -- e.g. expenses.dining.coffee
  description   TEXT,                             -- categories: classifier-oriented (0012)
  currency      CHAR(3) NOT NULL DEFAULT 'USD',
  external_id   TEXT,                             -- SimpleFIN account id
  connection_id UUID REFERENCES connections(id),
  min_balance   NUMERIC(20,4),                    -- shortfall threshold
  feed_start    DATE,                             -- feed sync-from: importer skips feed
                                                  -- rows dated before it (§7.1, decision 40)
  feed_invert   BOOLEAN NOT NULL DEFAULT false,   -- importer negates feed amounts, post-
                                                  -- hash (§7.1, decision 43)
  drift_basis_override TEXT,                      -- drift-basis pin: 'posted_only' |
                                                  -- 'pending_inclusive'; NULL = learned
                                                  -- (§7.3, decision 83)
  holdings_source TEXT NOT NULL DEFAULT 'feed',   -- 'feed' | 'manual': where a brokerage
                                                  -- account's holdings come from (§8,
                                                  -- decision 88); admin-set
  holdings_source_changed_at TIMESTAMPTZ,         -- when holdings_source last changed;
                                                  -- NULL until it first does (§7.1, §8)
  manual_period_open BOOLEAN NOT NULL DEFAULT false, -- a manual period waits to be
                                                  -- ended by the importer (§7.1, §8)
  hidden_at     TIMESTAMPTZ,                      -- display-only hide (0.6.0, §3.4)
  closed_at     TIMESTAMPTZ,                      -- read-only close (0.6.0, §3.4)
  reconciled_through DATE,                        -- latest reconciled date (§3.7, decision 61);
                                                  -- never served without its live check
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- Lifecycle timestamps are real-accounts-only; categories never hide/close.
  CONSTRAINT accounts_virtual_lifecycle_null
    CHECK (NOT is_virtual OR (hidden_at IS NULL AND closed_at IS NULL)),
  -- Feed sync-from is real-accounts-only too; categories have no feed (0018).
  CONSTRAINT accounts_virtual_feed_start_null
    CHECK (NOT is_virtual OR feed_start IS NULL),
  -- Feed sign inversion likewise (0019).
  CONSTRAINT accounts_virtual_feed_invert_false
    CHECK (NOT is_virtual OR NOT feed_invert),
  -- The drift-basis pin speaks the drift check's vocabulary, real accounts
  -- only (0033); the service also refuses it off the drift-checked types.
  CONSTRAINT accounts_drift_basis_override_values
    CHECK (drift_basis_override IN ('posted_only', 'pending_inclusive')),
  CONSTRAINT accounts_virtual_drift_basis_override_null
    CHECK (NOT is_virtual OR drift_basis_override IS NULL),
  -- Categories never reconcile (0028).
  CONSTRAINT accounts_virtual_reconciled_null
    CHECK (NOT is_virtual OR reconciled_through IS NULL),
  -- Holdings source (0036): brokerage only, so categories are covered too.
  CONSTRAINT accounts_holdings_source_values
    CHECK (holdings_source IN ('feed', 'manual')),
  CONSTRAINT accounts_holdings_source_brokerage
    CHECK (holdings_source = 'feed' OR type = 'brokerage')
);
CREATE INDEX accounts_path_gist ON accounts USING GIST (path);
-- One ledger account per (connection, external id) — feed idempotency backstop.
CREATE UNIQUE INDEX accounts_connection_external_uniq
  ON accounts (connection_id, external_id) WHERE external_id IS NOT NULL;
-- Backstop for the read-only chokepoint: no INSERT of a posting may land on a
-- closed account (BEFORE INSERT trigger postings_closed_account_guard, §3.4).

CREATE TABLE payees (             -- name encrypted (#137)
  id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name_enc   BYTEA NOT NULL,       -- Fernet over the payee name (ORM attr: name)
  name_bidx  TEXT NOT NULL,        -- blind index: HMAC(bidx_key, normalize_payee(name))
  -- Payee ALIASING (0.14.0, #174): a retroactive `rename` rule re-points the
  -- payee ENTITY, never transactions.payee_id — migration 0005's allow-list
  -- makes that column immutable on posted rows, which is exactly the history
  -- a rename wants to consolidate. Reads resolve through the chain (bounded
  -- depth, cycles rejected) and payee_memory aggregates over the resolved id,
  -- so consolidation strengthens memory retroactively. Reversible: clear it.
  merged_into_id UUID REFERENCES payees(id)
);
-- name_bidx REPLACES the old plaintext normalized_name (leaving it plaintext
-- would leak what name_enc hides). resolve_payee, the register payee filter,
-- and schedule detection resolve payee identity by equality on this index.
CREATE INDEX payees_name_bidx ON payees (name_bidx);

CREATE TABLE transactions (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  date            DATE NOT NULL,
  payee_id        UUID REFERENCES payees(id),
  description_enc BYTEA,         -- Fernet over the description (ORM attr: description; #137)
  status          TEXT NOT NULL DEFAULT 'pending',
    -- expected | pending | posted | void
  origin          TEXT NOT NULL,
    -- import | agent | human | rule | system
  content_hash    TEXT,          -- dedup key; keyed HMAC (#137); UNIQUE per source account (partial idx)
  schedule_id     UUID REFERENCES schedules(id),   -- set for expected instances
  occurrence_date DATE,          -- the rule slot an instance fills (#231, decision 55):
                                 --   materialization is idempotent on (schedule_id,
                                 --   occurrence_date) across every status; a user skip
                                 --   keeps it, a resync void releases it (NULL)
  schedule_pinned_at TIMESTAMPTZ,-- set by a token-bearing edit of an instance: the
                                 --   rule never refreshes/voids a pinned instance
  source_fetch_id UUID REFERENCES raw_fetches(id), -- set for imports
  superseded_by   UUID REFERENCES transactions(id),-- match-wins / merge voids
  reverses_id     UUID REFERENCES transactions(id),-- correction chains
  created_by      UUID REFERENCES api_tokens(id),
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- A posted row is corrected at most once: only one reversal may point at it
-- (the correction chain continues from the replacement, so legitimate chains
-- always target distinct ids).
CREATE UNIQUE INDEX transactions_reverses_id_uniq
  ON transactions (reverses_id) WHERE reverses_id IS NOT NULL;

-- Review marks (0.14.0, #175, §4). A SIDE TABLE, not columns on transactions:
-- the posted guard allow-lists only status/superseded_by via to_jsonb, so any
-- new transactions column is immutable once posted — precisely the rows that
-- need marking. A mark is an annotation, not ledger content.
CREATE TABLE transaction_reviews (
  transaction_id UUID PRIMARY KEY REFERENCES transactions(id),
  reviewed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  reviewed_by    UUID REFERENCES api_tokens(id),  -- NULL = migration backfill
  via            TEXT NOT NULL,
    -- explicit | update | post | confirm_transfer | match | unlink
    -- | ai_review (the backend reviewer, §11.2) | aged (staleness sweep)
    -- | backfill (migration)
  note_enc       BYTEA,          -- Fernet over the reviewer's note (ORM attr: note)
  escalated_at   TIMESTAMPTZ,    -- reviewed, but handed back for a human look
  carried_from   UUID REFERENCES transactions(id) ON DELETE SET NULL
    -- the row this mark was copied from, whole (0032, decision 82): a
    -- pending's posted successor, a system correction's replacement, an
    -- unlinked bank line's clone. Explains a reviewed_at older than the row.
);
-- The review SET: live chain heads that arrived unattended. QIF/CSV rows are
-- also origin='import', hence the source_fetch_id clause (decision 45's
-- feed-row definition) — a 6,081-row Quicken import is not 6,081 review items.
CREATE INDEX transactions_review_scope ON transactions (date DESC, id DESC)
  WHERE superseded_by IS NULL
    AND status IN ('pending','posted')
    AND (origin IN ('system','rule')
         OR (origin = 'import' AND source_fetch_id IS NOT NULL));

-- Card statement balances (0.18, #351, §6, decision 80): one per card-payment
-- instance, recorded by a user or an agent (SimpleFIN carries no statement).
-- A side table like transaction_reviews: it annotates the instance, whose
-- amount moves through the ordinary transfer edit. Goes with its transaction
-- (CASCADE) and with reset_instance; release_settlement copies it onto a clone.
CREATE TABLE instance_statements (
  transaction_id    UUID PRIMARY KEY REFERENCES transactions(id) ON DELETE CASCADE,
  statement_balance NUMERIC(20,4) NOT NULL CHECK (statement_balance > 0),
    -- as printed: the amount owed
  statement_date    DATE,         -- the closing date, when known
  source            TEXT NOT NULL CHECK (source IN ('human','agent')),
    -- the recording token's kind (session = human); 'feed' joins only
    -- when a provider supplies statements
  recorded_by       UUID REFERENCES api_tokens(id),
  recorded_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE postings (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  transaction_id UUID NOT NULL REFERENCES transactions(id),
  account_id     UUID NOT NULL REFERENCES accounts(id),
  amount         NUMERIC(20,4) NOT NULL,
  currency       CHAR(3) NOT NULL DEFAULT 'USD',
  memo_enc       BYTEA          -- Fernet over the memo (ORM attr: memo; #137)
);
-- Invariants (app layer + trigger backstop):
--   SUM(amount) per (transaction_id, currency) = 0 for status IN (pending, posted)
--   postings of a posted transaction are immutable — INSERT, UPDATE, DELETE,
--   and re-pointing a posting into/out of one are all rejected; the postings
--   created together with the transaction row (same DB transaction,
--   created_at = transaction_timestamp()) are the carve-out that lets a
--   transaction be born posted

CREATE TABLE rules (              -- 0.14.0, #174, migration 0020, decision 46
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  type           TEXT NOT NULL CHECK (type IN ('categorize','rename','transfer_link')),
  state          TEXT NOT NULL DEFAULT 'draft'
                 CHECK (state IN ('draft','active','disabled','archived')),
                 -- created 'draft' unless activate, and a draft NEVER executes:
                 -- load_active() is the only loader and filters state='active'.
                 -- DELETE maps to 'archived' so audit rows stay resolvable.
  priority       INTEGER NOT NULL DEFAULT 100,   -- lower runs first
  -- PREDICATE. Every field optional; the AND of whatever is set; NULL is
  -- unconstrained. STRUCTURED, never regex: it runs at the import chokepoint
  -- (no backtracking DoS), it has to be explainable in an audit row and in
  -- §4's reviewer guidance, and a regex invites patterns written against raw
  -- bank bytes that normalize_payee / feed_invert / decision 44 move
  -- underneath. Text matching normalizes both sides and is token-boundary
  -- aware, so "chase" cannot match inside "purchase return".
  account_ids    UUID[],                          -- real leg's account must be in this set
  match_field    TEXT CHECK (match_field IN ('payee','description','payee_or_description')),
  match_mode     TEXT CHECK (match_mode IN ('equals','prefix','contains')),
  match_text_enc BYTEA,                           -- Fernet (ORM attr: match_text)
  match_text_bidx TEXT,                           -- blind index over normalize_payee(pattern);
                                                  -- duplicate detection + the equals fast path
  amount_exact   NUMERIC(20,4),
  amount_min     NUMERIC(20,4),
  amount_max     NUMERIC(20,4),
  direction      TEXT CHECK (direction IN ('debit','credit')),
                 -- sign of the real leg AFTER feed_invert (decision 43), so a
                 -- rule is written in the user's sign world, not the bank's
  effective_from DATE,                            -- rows dated strictly before are out of scope
  -- ACTION: exactly one shape per type (rules_action_shape below).
  category_id    UUID REFERENCES accounts(id),
                 -- categorize. MAY reference a virtual account (an ordinary
                 -- category) OR a REAL one — categories are accounts (§3.2),
                 -- so a real target is the deterministic twin of decision 49
                 -- and routes through the same [account] transfer sugar, not
                 -- the leg-swap path. It is the graduation target when the
                 -- classifier keeps identifying the same one-sided transfer.
  payee_name_enc BYTEA,                           -- rename: the friendly payee (ORM attr: payee_name)
  pair_account_a UUID REFERENCES accounts(id),    -- transfer_link
  pair_account_b UUID REFERENCES accounts(id),
  window_days    INTEGER,                         -- transfer_link (migration 0029, #210,
                 -- decision 66): the most days apart the pair's two legs may
                 -- post and still link. NULL = the global ±3-day window
                 -- (core.matching.TRANSFER_WINDOW_DAYS); 0 = same-day only;
                 -- capped at 14 (TRANSFER_WINDOW_MAX_DAYS). It widens or
                 -- narrows THIS pair only — every undeclared pair keeps ±3.
  payee_scope_id UUID REFERENCES payees(id),      -- transfer_link (migration 0030, #271,
                 -- decision 67): the pair links only rows where EITHER leg's
                 -- payee is in this payee's ALIAS GROUP, resolved when the rule
                 -- loads (rules.alias_group) — so a rename whose apply-history
                 -- aliases the payee keeps it in scope. NULL = the whole pair.
                 -- Set by accepting a transfer_link suggestion (§6).
  note_enc       BYTEA,                           -- why this rule exists (ORM attr: note)
  memory_id      UUID REFERENCES memories(id),    -- added by migration 0022 (#176); rationale
  applied_count  INTEGER NOT NULL DEFAULT 0,       -- times the rule FIRED (rules.mark_applied):
                 -- +1 per row a categorize/rename rule shaped at import or in a
                 -- categorize pass, +1 per apply-history run, +1 per link (merge
                 -- or absorb) a transfer_link rule made (#314). Previews and the
                 -- activation projection never count; an unlink does not
                 -- decrement. Shown as `applied` (MCP list_rules) / `applied_count`
                 -- (REST); last_applied_at is stamped alongside, not exposed.
  last_applied_at TIMESTAMPTZ,
  created_by     UUID REFERENCES api_tokens(id),  -- user-vs-AI derives from token kind (decision 24)
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT rules_match_shape CHECK (
    (match_field IS NULL) = (match_mode IS NULL)
    AND (match_mode IS NULL) = (match_text_enc IS NULL)),
  CONSTRAINT rules_action_shape CHECK (
    (type='categorize'    AND category_id IS NOT NULL AND payee_name_enc IS NULL
                          AND pair_account_a IS NULL) OR
    (type='rename'        AND payee_name_enc IS NOT NULL AND category_id IS NULL
                          AND pair_account_a IS NULL) OR
    -- A transfer_link rule carries NO text predicate: its account pair IS its
    -- scope. Only amount_exact (compared on abs(), a pair being inherently ±),
    -- effective_from and payee_scope_id (an alias group, never text) may
    -- narrow it, and window_days set its date window.
    (type='transfer_link' AND pair_account_a IS NOT NULL AND pair_account_b IS NOT NULL
                          AND pair_account_a <> pair_account_b
                          AND category_id IS NULL AND payee_name_enc IS NULL
                          AND match_field IS NULL AND account_ids IS NULL
                          AND direction IS NULL AND amount_min IS NULL AND amount_max IS NULL)),
  CONSTRAINT rules_amount_shape CHECK (
    amount_exact IS NULL OR (amount_min IS NULL AND amount_max IS NULL)),
  CONSTRAINT rules_window_shape CHECK (       -- migration 0029; the service and the
    window_days IS NULL                       -- REST/MCP boundaries refuse first (422)
    OR (type = 'transfer_link' AND window_days BETWEEN 0 AND 14)),
  CONSTRAINT rules_payee_scope_shape CHECK (  -- migration 0030; the service refuses first
    payee_scope_id IS NULL OR type = 'transfer_link')
);
-- Evaluation order within a type: (priority, created_at, id), FIRST MATCH WINS.
-- Priority, not most-specific-wins: specificity is a hidden second ranking
-- function whose meaning shifts when an unrelated predicate field is edited,
-- and "why did this rule win" must be answerable by pointing at one row.
-- transfer_link rules are unordered (no conflicting action — set union).
CREATE INDEX rules_eval_order ON rules (state, type, priority, created_at, id);

CREATE TABLE memories (           -- 0.14.x, #176, migration 0022, §3.6
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  text_enc    BYTEA NOT NULL,     -- Fernet (ORM attr: text); ≤500 plaintext chars.
                                  -- No blind index: nothing looks a memory up by
                                  -- text equality — scoping is by FK.
  state       TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('draft','active')),
  pinned      BOOLEAN NOT NULL DEFAULT false,   -- rides along in describe()
  archived_at TIMESTAMPTZ,                      -- soft archive; never served
  created_by  UUID REFERENCES api_tokens(id),   -- NULL = a Purser-authored pass (§11.3)
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
  -- Exactly one target. accounts covers BOTH real accounts and categories
  -- (categories are virtual accounts, §3.2) — no third column needed.
  account_id UUID REFERENCES accounts(id),
  payee_id   UUID REFERENCES payees(id),
  CONSTRAINT memory_links_one_target CHECK ((account_id IS NULL) <> (payee_id IS NULL))
);
CREATE UNIQUE INDEX memory_links_uniq_account ON memory_links (memory_id, account_id)
  WHERE account_id IS NOT NULL;
CREATE UNIQUE INDEX memory_links_uniq_payee   ON memory_links (memory_id, payee_id)
  WHERE payee_id IS NOT NULL;
CREATE INDEX memory_links_account ON memory_links (account_id) WHERE account_id IS NOT NULL;
CREATE INDEX memory_links_payee   ON memory_links (payee_id)   WHERE payee_id IS NOT NULL;

CREATE TABLE schedules (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name_enc        BYTEA NOT NULL,       -- Fernet over the schedule name (ORM attr: name; #137)
  account_id      UUID NOT NULL REFERENCES accounts(id),
  payee_id        UUID REFERENCES payees(id),
  category_path   LTREE,                 -- posting template (flat schedules)
  counterparty_account_id UUID REFERENCES accounts(id),
                                         -- the other REAL account of a recurring transfer
                                         --   (#232): instances are two-leg expected transfers;
                                         --   CHECK: never both category_path and counterparty
  rrule           TEXT NOT NULL,         -- RFC 5545; FREQ >= DAILY (sub-daily is 422)
  dtstart         DATE NOT NULL,         -- the RRULE anchor (#231): every expansion counts
                                         --   from it; anchor-sensitive rules never drift
  amount_type     TEXT NOT NULL,         -- fixed | estimated
  amount          NUMERIC(20,4),
  estimate_basis  TEXT,                  -- last | rolling_avg_3 | rolling_avg_12
                                         --   | counterparty_balance (transfer schedules, #232)
  currency        CHAR(3) NOT NULL DEFAULT 'USD',
  next_due        DATE,                  -- cache: earliest live instance (overdue counts),
                                         --   else next rrule occurrence; NULL when paused/ended
  autopost_policy TEXT NOT NULL DEFAULT 'wait_for_match',
    -- auto_post | wait_for_match | reminder_only
  source          TEXT NOT NULL,         -- manual | detected | agent — provenance only,
                                         --   never a status (#347): an accepted suggestion
                                         --   is stored 'detected' and is fully active
  status          TEXT NOT NULL DEFAULT 'active',
    -- active | paused | ended (terminal) — CHECK-enforced since 0027;
    -- "proposed" is a SUGGESTION now (#233), never a schedule row
  ended_at        TIMESTAMPTZ,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE suggestions (            -- the inbox (#233, decision 57)
  id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  kind                    TEXT NOT NULL,       -- schedule | transfer_link (CHECK; 0030,
                                               --   decision 67) | match (0034, decision 86:
                                               --   the mirror of an open match proposal)
  status                  TEXT NOT NULL DEFAULT 'open',  -- open | accepted | rejected
                                               --   | withdrawn (0034: closed by the system —
                                               --   its match proposal was rescinded, carried,
                                               --   purged or auto-confirmed; kept, not deleted)
  fingerprint             TEXT NOT NULL UNIQUE,-- one row per fingerprint, EVER:
                                               --   re-detection upserts the open row,
                                               --   a rejected row is durable suppression;
                                               --   match: plain text, match:slot:<schedule>:
                                               --   <occurrence>:<bank row> or match:tx:<expected>:
                                               --   <bank row> (a re-proposal re-opens it)
  source                  TEXT NOT NULL,       -- detected | agent | account_added |
                                               --   confirm (a manual transfer confirm, 0030)
  detector                TEXT,
  detector_version        INTEGER,
  confidence              NUMERIC(4,3),        -- deterministic evidence score, 0..1;
                                               --   NULL for transfer_link (a human acted)
                                               --   and for an agent proposal (unscored);
                                               --   match: the match score
  subject_account_id      UUID REFERENCES accounts(id),  -- transfer_link: the pair, as
  subject_payee_id        UUID REFERENCES payees(id),    --   first confirmed, and the
  counterparty_account_id UUID REFERENCES accounts(id),  --   alias-resolved scope payee
  draft_enc               BYTEA NOT NULL,      -- Fernet over the kind's typed draft:
                                               --   ScheduleDraft | TransferLinkDraft | MatchDraft
  evidence_enc            BYTEA,               -- Fernet over {occurrences, cadence, ...};
                                               --   transfer_link: the confirmed pairs;
                                               --   agent: {note} when it left one
  snooze_until            DATE,                -- hidden from the inbox until then; never auto-accepts
  seen_count              INTEGER NOT NULL DEFAULT 1,
  last_seen_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  decided_at              TIMESTAMPTZ,
  decided_by              UUID REFERENCES api_tokens(id),
  decision_note_enc       BYTEA,
  reject_scope            TEXT,                -- exact | counterparty (never again for this pair)
  result_entity_type      TEXT,                -- what accept produced: schedule | rule | match
  result_entity_id        UUID,
  created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE matches (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  expected_id UUID NOT NULL REFERENCES transactions(id),
  posted_id   UUID NOT NULL REFERENCES transactions(id),
  score       REAL NOT NULL,
  variance    NUMERIC(20,4),   -- posted - expected
  state       TEXT NOT NULL,   -- proposed | confirmed | rejected
  decided_by  UUID REFERENCES api_tokens(id),
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- At most one live (proposed/confirmed) match per side; confirming a match
-- rescinds (rejects) competing proposals on either side first.
CREATE UNIQUE INDEX matches_posted_live_uniq
  ON matches (posted_id) WHERE state IN ('proposed', 'confirmed');
CREATE UNIQUE INDEX matches_expected_live_uniq
  ON matches (expected_id) WHERE state IN ('proposed', 'confirmed');

CREATE TABLE holdings_snapshots (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id   UUID NOT NULL REFERENCES accounts(id),
  as_of        DATE NOT NULL,
  symbol       TEXT NOT NULL,
  quantity     NUMERIC(20,6),
  cost_basis   NUMERIC(20,4),
  market_value NUMERIC(20,4),   -- NULL on a manual row = no price (adds nothing)
  lot_id       UUID,            -- v2 hedge; always NULL in v1
  raw_fetch_id UUID REFERENCES raw_fetches(id),
  -- Manually tracked holdings (0036, decision 88): 'feed' rows come from
  -- SimpleFIN; 'manual' rows are the projection of a position's events and
  -- prices, one per (account, as_of, position), carrying the close used, plus
  -- the zero rows (tombstones) that end a manual period (§8). At most one row
  -- per (account, as_of, symbol).
  source       TEXT NOT NULL DEFAULT 'feed' CHECK (source IN ('feed', 'manual')),
  position_id  UUID REFERENCES positions(id),
  price        NUMERIC(20,6),   -- manual rows: the close the value uses
  price_as_of  DATE,            -- manual rows: that close's date (staleness on read)
  CONSTRAINT holdings_manual_has_position
    CHECK ((source = 'manual') = (position_id IS NOT NULL))
);
CREATE UNIQUE INDEX holdings_manual_uniq
  ON holdings_snapshots (account_id, as_of, position_id) WHERE source = 'manual';
-- + INDEX holdings_feed_symbol (symbol, as_of) WHERE source = 'feed': feed-implied prices

-- One symbol held in one manually tracked brokerage account (0036, decision 88).
CREATE TABLE positions (
  id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id UUID NOT NULL REFERENCES accounts(id),
  symbol     TEXT NOT NULL,     -- upper-case ticker, plaintext like holdings_snapshots.symbol
  name       TEXT,
  opened_on  DATE NOT NULL,     -- the earliest event date (reversals never move it later)
  closed_at  TIMESTAMPTZ,       -- set only while it holds nothing; then read-only
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT positions_symbol_format CHECK (symbol ~ '^[A-Z0-9][A-Z0-9.-]{0,14}$')
);
CREATE UNIQUE INDEX positions_open_symbol_uniq
  ON positions (account_id, symbol) WHERE closed_at IS NULL;

-- APPEND-ONLY (BEFORE UPDATE trigger position_events_immutable, the shape of
-- transactions_posted_guard): a correction is a reversal row, never an edit.
-- Rows go only with their account, through the delete-account CLI.
CREATE TABLE position_events (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  position_id     UUID NOT NULL REFERENCES positions(id),
  occurred_on     DATE NOT NULL,            -- today or earlier (the service refuses a future date)
  kind            TEXT NOT NULL CHECK (kind IN ('set_quantity', 'buy', 'sell')),
  quantity        NUMERIC(20,6) NOT NULL,   -- set_quantity: absolute; buy/sell: the delta (> 0)
  price           NUMERIC(20,6),            -- per share; buy/sell only
  fees            NUMERIC(20,4),            -- buy/sell only
  cost_basis      NUMERIC(20,4),            -- set_quantity only: the total basis after it
  reverses_id     UUID REFERENCES position_events(id),
  replaces_id     UUID REFERENCES position_events(id),
                                            -- 0040, decision 93: a correction of a reversed
                                            -- event of the same position and date; it takes
                                            -- that event's slot in the day's order. NULL on
                                            -- a reversal (CHECK)
  origin          TEXT NOT NULL CHECK (origin IN ('manual', 'agent', 'import')),
                                            -- session token = manual, any other = agent
  note_enc        BYTEA,                    -- Fernet (ORM attr: note)
  source_note_enc BYTEA,                    -- Fernet (ORM attr: source_note): where it came from
  actor_token_id  UUID REFERENCES api_tokens(id),  -- an attribution anchor (§9 session hygiene)
  created_at      TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
                                            -- not now(): events recorded in one transaction
                                            -- keep their order (occurred_on, slot, created_at,
                                            -- id); slot = created_at, or the replaced event's
  -- + shape CHECKs: quantity >= 0 and > 0 off set_quantity; price > 0; fees,
  --   cost_basis >= 0; set_quantity carries no price/fees; buy/sell no cost_basis
);
CREATE UNIQUE INDEX position_events_reversed_once
  ON position_events (reverses_id) WHERE reverses_id IS NOT NULL;
CREATE UNIQUE INDEX position_events_replaced_once
  ON position_events (replaces_id) WHERE replaces_id IS NOT NULL;

-- Daily closes (0036, decision 89). Feed-implied prices are computed on read
-- from feed snapshots and never stored, so there is no 'feed' source.
CREATE TABLE security_prices (
  symbol     TEXT NOT NULL,     -- same format CHECK as positions.symbol
  price_date DATE NOT NULL,
  close      NUMERIC(20,6) NOT NULL CHECK (close > 0),
  source     TEXT NOT NULL CHECK (source IN ('manual', 'tiingo')),
                                -- widened when another provider lands
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (symbol, price_date, source)
);

-- The date ranges a provider has answered for good for one symbol (§8,
-- decision 89): closes, or a definitive "nothing" (an empty answer or a
-- not-found for dates at least 3 days old), so such a range is asked for
-- once, and a gap between two answered ranges still is.
CREATE TABLE price_answered (
  symbol     TEXT NOT NULL,          -- same format CHECK as positions.symbol
  source     TEXT NOT NULL CHECK (source IN ('tiingo')),  -- widen with
                                     -- security_prices.source for a new provider
  answered   DATEMULTIRANGE NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (symbol, source)
);

CREATE TABLE valuations (
  id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id UUID NOT NULL REFERENCES accounts(id),
  as_of      DATE NOT NULL,
  value      NUMERIC(20,4) NOT NULL,  -- ledger sign: a liability is negative while owed (decision 71)
  source     TEXT NOT NULL      -- manual | statement | api
);

CREATE TABLE events (              -- durable; subscribers can replay
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  type        TEXT NOT NULL,
    -- transaction.posted | match.proposed | transfer.candidate | bill.late
    -- | variance.threshold | forecast.shortfall | sync.failed
    -- | recurring.price_changed (confirmed-match variance shifted an
    --   estimated schedule's instance amount; payload carries old/new
    --   estimate + delta — 0.2.0, #74)
    -- | balance.drift (post-sync reported-vs-ledger drift, §7.3;
    --   dedup_key = "{account_id}:{delta}" — 0.9.0, #149)
    -- | suggestion.created (dedup_key = fingerprint) | suggestion.decided
    --   (0.16.0, #233; replaces schedule.detected)
    -- | account.reconciled (a statement-balance claim was recorded, §3.7;
    --   payload carries basis, target, ledger balance, adjustment — #268) | ...
  entity_type TEXT,
  entity_id   UUID,
  payload_enc BYTEA NOT NULL,   -- Fernet over the JSON payload (ORM attr: payload; #137)
  dedup_key   TEXT              -- plaintext structural id lifted out of payload (#137):
);                              --   the once-only emit guards (transfer pair_key,
                                --   forecast shortfall key) filter on it, since a JSONB
                                --   path over ciphertext can't. NULL when there is none.
CREATE INDEX events_dedup_key ON events (dedup_key);

CREATE TABLE webhook_subscriptions (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  url         TEXT NOT NULL,
  event_types TEXT[] NOT NULL,
  secret_enc  BYTEA,
  active      BOOLEAN NOT NULL DEFAULT true,
  -- Fan-out watermark over events.at (issue #219, migration 0024, decision
  -- 54): scans start here; a new subscription (DEFAULT now()) receives no
  -- historical backlog — GET /events replay covers history.
  cursor_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE webhook_deliveries (   -- per-subscription delivery log + retry state
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  subscription_id UUID NOT NULL REFERENCES webhook_subscriptions(id),
  event_id        UUID NOT NULL REFERENCES events(id),
  status          TEXT NOT NULL DEFAULT 'pending',  -- pending | delivered | failed
  attempts        INT NOT NULL DEFAULT 0,
  next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_attempt_at TIMESTAMPTZ,
  response_status INT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (subscription_id, event_id)
);

CREATE TABLE net_worth_daily (      -- materialization of §8's formula (watermark-driven, §3.5)
  as_of       DATE NOT NULL,
  currency    CHAR(3) NOT NULL DEFAULT 'USD',
  assets      NUMERIC(20,4) NOT NULL,
  liabilities NUMERIC(20,4) NOT NULL,
  net_worth   NUMERIC(20,4) NOT NULL,
  computed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (as_of, currency)
);

CREATE TABLE account_running_balances (  -- derived (§3.5, decision 37); rebuildable
  account_id     UUID NOT NULL REFERENCES accounts(id),
  transaction_id UUID NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
  date           DATE NOT NULL,           -- ordering-key copies (join-free lookups)
  created_at     TIMESTAMPTZ NOT NULL,
  currency       CHAR(3) NOT NULL DEFAULT 'USD',
  delta          NUMERIC(20,4) NOT NULL,  -- the account's posting sum in the tx
  balance        NUMERIC(20,4) NOT NULL,  -- running balance after this row
  PRIMARY KEY (account_id, transaction_id)
);
-- + INDEX account_running_balances_order_idx (account_id, date, created_at, transaction_id)

CREATE TABLE balance_watermark (    -- singleton dirty-watermark (§3.5, decision 38)
  id                  BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id),
  running_dirty_from  DATE,        -- consumed at commit (before_commit hook)
  networth_dirty_from DATE,        -- consumed async (networth.consume_watermark)
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE account_reconciliations (  -- statement-balance claims (§3.7, decision 61)
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id       UUID NOT NULL REFERENCES accounts(id),
  as_of            DATE NOT NULL,          -- the statement date (end of day); never future
  basis            TEXT NOT NULL DEFAULT 'posted'
                   CHECK (basis IN ('posted', 'pending_inclusive')),  -- §7.3's two bases
  target_balance   NUMERIC(20,4) NOT NULL, -- the statement figure in LEDGER sign
  adjustment       NUMERIC(20,4) NOT NULL, -- what was posted to reach it; 0 = already matched
  adjustment_tx_id UUID UNIQUE REFERENCES transactions(id),  -- NULL iff adjustment = 0
  created_by       UUID REFERENCES api_tokens(id),
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- One claim per account and date, whatever the basis: a second basis's
  -- adjustment would move the first's balance too.
  CONSTRAINT account_reconciliations_one_per_date UNIQUE (account_id, as_of),
  CONSTRAINT account_reconciliations_adjustment_shape
    CHECK ((adjustment = 0) = (adjustment_tx_id IS NULL))
);

CREATE TABLE audit_log (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  at               TIMESTAMPTZ NOT NULL DEFAULT now(),
  actor_token_id   UUID REFERENCES api_tokens(id),
  action           TEXT NOT NULL,
  entity_type      TEXT NOT NULL,
  entity_id        UUID,
  before_after_enc BYTEA,   -- Fernet over the before/after JSON (ORM attr: before_after; #137)
  -- issue #122: call rows vs change rows (decision 31).
  -- read|write = one row per authenticated HTTP request / MCP tool call,
  -- carrying `detail` (method/tool, path, status, args summary, ms, rows).
  -- change = the per-entity before/after rows record() has always written,
  -- stamped with the surrounding call's request_id.
  kind             TEXT NOT NULL DEFAULT 'change'
                   CHECK (kind IN ('read', 'write', 'change')),
  request_id       UUID,
  detail_enc       BYTEA,   -- Fernet over the call detail JSON (ORM attr: detail; #137)
  disclosed        TEXT[],  -- MCP call rows: fields included beyond the privacy
                            --   defaults (0038, #437). Names only, so plaintext; the
                            --   audit view filters "what my AI chose to see" on it.
  replacement_id   UUID     -- plaintext skeleton id lifted out of before_after.after (#137):
);                          --   the register's category-provenance badge filters on it
-- audit_log_at_id (at, id): keyset pagination + retention prune.
-- audit_log_actor_at (actor_token_id, at): virtual-session grouping.
-- audit_log_request (request_id) WHERE request_id IS NOT NULL: drill-ins.
-- audit_log_replacement_id (replacement_id) WHERE replacement_id IS NOT NULL.
```

-- readmodel schema (issue #13): versioned read views, a PUBLIC CONTRACT for
-- Grafana / direct-SQL consumers, readable by the NOLOGIN role purser_read
-- (operators enable login at deploy time). Columns are pinned by tests and
-- survive internal refactors:
--   readmodel.v2_transactions_flat          (flat view, chain heads only)
--   readmodel.v1_spending_by_category_month
--   readmodel.v1_net_worth_daily
--   readmodel.v1_forecast_daily             (60-day deterministic projection)
--   readmodel.v1_holdings_latest
--
-- Split rows (#274, migration 0035): v2_transactions_flat reports
-- category_path NULL for a row with more than one category leg (it used to
-- pick one with an unordered LIMIT 1); same columns, same contract.
--
-- Feed funds valued at 0 (#392, migration 0039): v1_holdings_latest values a
-- feed row with quantity > 0 and market_value 0 or NULL as quantity x the
-- stored close in effect on its as_of, matched on upper(btrim(symbol))
-- (manual first on a same-date tie, rounded half to even), NULL without one,
-- as the app's readers do (§8, decision 92); the price lookup runs on the
-- rows DISTINCT ON keeps, not on history. Same columns, same types, same
-- contract.
--
-- CONTRACT CHANGE (#137, migration 0016): v1_transactions_flat is dropped and
-- replaced by **v2_transactions_flat** = the same view MINUS the `payee` and
-- `description` columns, which moved under encryption and can't be served by an
-- app-side-Fernet SQL view. The other four v1_* views hold no narrative column,
-- so they keep their names and contracts. Grafana's cash-flow and upcoming-bills
-- dashboards were repointed to v2 / the plaintext skeleton in the same PR.

The Direct-to-Bank feed extension (migration 0006: `learned_maps`,
`skill_cache`, connection skill binding, per-connection token binding) was
**removed by migration 0008** (product pivot, issue #97). 0008 retired the
historical `provider='direct'` rows as `status='disabled'` provenance;
migration **0010** later purged them and their captures outright (owner
decision, issue #101 — decision 25). The `raw_fetches` append-only invariant
was not weakened: 0010 is a documented one-off, not a delete path.

### 3.2 Categories are virtual accounts — curated, finite, described (0.5.0)

There is no categories table. A category is an `accounts` row with
`is_virtual = true`, `type = category_expense | category_income`, hierarchy via
`parent_id` and an `ltree` `path`. Spending rollups are single indexed subtree
queries (`path <@ 'expenses.dining'`). The flat API exposes categories by path;
the posting graph treats them as accounts.

**Path input (issue #214).** Every caller-supplied path — an explicit create
`path`, the path form of a "path or id" reference (bracket inner refs
included), a subtree filter — passes `core.ledger.validate_path` before SQL
casts it: one or more labels joined by single dots, each label 1–1000
characters drawn from letters, digits, `_` and `-` (PostgreSQL 16's ltree
grammar), at most 65 535 labels, never the empty path. A violation is a 422
naming the offending segment.
Beyond ASCII, ltree defers to the database locale; text the validator admits
but the locale refuses (any non-ASCII label under a C-locale database, a bare
combining accent) meets a narrow ltree-refusal handler — 422 over REST, a
clean tool error over MCP — which is a backstop, never the primary check.

The set is **curated and finite** (issue #110): Purser ships a default
hierarchy (`purser/core/categories.py`, 100 nodes whose structure is adapted
from the Plaid Personal Finance Categories taxonomy — with transfer and
tracked-loan-payment groups deliberately excluded, since those are structural
transfers here). Migration 0012 seeds it once, skipping existing paths, so
installs merge additively and user deletions stick. Every category carries a
one-line `description` in Purser's own words (issue #429; Plaid's wording is
not reused) written for the AI classifier (§11) and shown in the Config →
Categories tab, which is the curation surface: rename/re-describe/add freely;
delete only while unused (no children, postings, or schedule templates);
re-parenting/path renames are deliberately unsupported in v1 (schedule
templates store category paths) — a real account moves (§3.4, decision 72),
a category never does. The classifier
is constrained to the tree, agents have no category-creation tool, and
`POST /categories` is a write-scoped REST route with no MCP tool — finiteness
is structural.

### 3.3 Hybrid ledger: flat outside, double-entry inside

API/MCP clients submit the flat form:

```json
{ "account": "citicard", "date": "2026-07-01", "amount": -14.00,
  "payee": "Chipotle", "category": "expenses.dining" }
```

The server expands it to a balanced posting pair (`citicard −14.00`,
`expenses.dining +14.00`). Transfers are one transaction with two real-account
postings (a split loan payment adds exactly one category posting — below).
Reads return the flat view by default; a `postings` detail is available on
request.

**Bracket transfers (0.5.0).** In the flat view a transfer's "category" is the
other real account in Quicken-style brackets (`[assets.savings]`, surfaced as
`transfer_account_path`). Writes accept the same sugar: setting `category` to
`[<account path or id>]` on `update_transaction`/PATCH converts the row into a
transfer to that account — absorbing into an existing transfer with a matching
unbacked side, else merging with a live one-sided counterpart in the target
account, else converting in place (pending re-points the category leg; posted
becomes a reversal + transfer-shaped replacement). A plain category on a
transfer-shaped row converts it back; the `account` field names the side that
keeps the row. Conversions are their own operation — no other field may change
in the same call. Transfers carrying linked bank rows must be unlinked first.

**Transfer amount edits (decision 78).** An expected or pending plain transfer
takes an amount edit in place (`update_transaction`/PATCH, and MCP
`override_instance` for a schedule instance): `amount` is the leg of the side
`account` names — a schedule instance defaults to its schedule's account, any
other transfer must name one — that leg takes it and the other leg its
negation, so the row stays zero-sum by construction. The direction is fixed:
a sign that would reverse the transfer is refused, quoting the leg. Refused
outright: a transfer carrying linked bank rows and a split loan payment (the
bank rows *are* the amounts; unlink first), and any edit of a posted transfer
(unlink it, or convert one without bank rows back to a category).

**Split loan payments (decision 62, issue #269).** A loan payment's two bank
legs do not cancel: checking shows the whole cash outflow, the loan account
shows only the principal it was paid down by, and the difference is interest
or fees. The shape is fixed:

- **one transaction** with exactly three postings;
- **the exact cash leg** — the paying account's bank amount, unchanged;
- **the exact principal leg** — the loan account's bank amount, unchanged;
- **one balancing posting** to `remainder_category`, an expense category
  (`category_expense`, never the `uncategorized` placeholders), for
  `−(cash + principal)` — zero-sum **by construction**, because the
  remainder *is* the negated sum of the two bank legs
  (`core.expand_split_transfer`);
- **no synthetic full-payment liability leg.** Posting the whole payment
  (+cash) to the loan and then an offsetting interest posting would put a
  number on the loan that its bank never reported; the loan register would
  stop mirroring the loan's bank rows line for line. Each bank leg is
  recorded exactly as its bank reports it.

Bounds are strict: `0 < principal < |cash|`. Equal legs are an ordinary
transfer (supplying `remainder_category` is refused), and a principal leg at
or above the cash that paid it is not a loan payment — booking the difference
would invent negative "interest" (−50.00 cash against +650.12 would book
−600.12). Every violation is a 422 naming the bound.

The split is **explicit-only**: `POST /matching/confirm-transfer` / MCP
`confirm_transfer` with `remainder_category`, write scope (a token-less caller
is refused structurally). The auto-linker, absorb, bracket conversion and the
AI classifier never produce one. It is audited (`transfer.confirm`, carrying
`incoming_amount`, `remainder`, `remainder_category`) and evented
(`transfer.confirmed` with `remainder` / `remainder_category` — keys present
only on a split). Like any merge it voids both originals with lineage, so
unlink (§4) restores both bank rows exactly and the account purge (§3.4,
decision 33) unlinks it before deleting. It cannot be recategorized or
re-pointed in place — unlink it and confirm again.

**Transfer classification.** A row is a transfer **exactly when its postings
are a shape Purser creates** (`core.transfer_shape`): PLAIN — two real legs on
different accounts that cancel — or SPLIT — the three postings above. Leg
counts are not the test: a multi-posting row that merely touches two real
accounts is not a transfer. (#245's paycheck template keeps one real leg, as
a split transaction does, below: its savings leg is a transfer of its own,
decision 87.) One classifier serves the flat view (`is_transfer`,
`transfer_account_path`), the MCP rows (`counterparty`), unlink, conversions
and the purge. In the flat view a split carries `transfer_account_path` (the
other bank account), `category_path` (the remainder category) and
`remainder_amount` (that posting, positive = expense); MCP
`search_transactions` rows add `remainder`. The matcher keeps seeing it as a
two-real-leg transfer (§5, decision 56): a loan-payment schedule's instance
settles against it through the cash leg.

**Split transactions (decision 87, issue #274).** One bank amount spread over
several categories — a paycheck's gross and taxes, a store run that was half
groceries — is **one real leg plus two or more category legs**, zero-sum by
construction: the real leg carries the amount and each line books its
negation to its category (`core.expand_split`, which refuses fewer than two
lines, a zero line, or lines that do not sum to the amount;
`ledger.create_split_transaction` persists it). It is not a transfer
(`transfer_shape` returns None), and it never has a second real leg: money a
split sends to another real account is a transfer of its own (the QIF
importer carves those lines out, §7.2). One primitive serves the QIF split
import now and #245's multi-posting templates later.

- **Read-only in v1.** `_flat_legs` refuses an edit with "split rows are
  read-only", the categorize pass and a categorize rule's apply-history leave
  split rows out (apply-history counts them `skipped_splits`), and the
  transfer pool (absorb and auto-link, §5) never takes one: each of those
  would rewrite or void the category legs. A wrong split row is purged and
  re-imported.
- **Presentation.** `TransactionOut.category_path` is null and `split_lines`
  is the category-leg count (`core.split_line_count`, 0 for every other
  shape); MCP `search_transactions` rows carry `split: n` instead of
  `category`; the detail's `postings` hold the legs; the register shows a
  read-only "Split (n)" that lists them; `readmodel.v2_transactions_flat`
  reports `category_path` NULL (migration 0035). Spending reports already sum
  category legs, so each line counts under its own category.

### 3.4 Account lifecycle (0.6.0)

Real accounts (never categories — the `accounts_virtual_lifecycle_null` CHECK
enforces it) carry two nullable timestamps, `hidden_at` and `closed_at`, and
five operations. Only **Move** changes a path, and never a category's (§3.2).

- **Rename** — `PATCH /accounts/{id}` edits `name` / `description`; MCP
  `update_account` (write scope, issue #353) edits `name` / `description` /
  `hidden` and nothing else. It takes **no `path`**: outside integrations
  (the card tracker among them) map accounts by path, so a move stays a
  deliberate REST/UI act, never a side effect of an agent rename; categories
  are refused there (they rename in the app, §3.2). The name is display
  text, but it is also live matching input — transfer auto-link looks for
  the counterparty account's name in bank text (§5, decision 28), as does
  the new-liability scan (§6) — so a rename changes which bank descriptions
  signal a transfer to the account. Audited `account.update`.
- **Move** (issue #284, decision 72) — `PATCH /accounts/{id}` with `path`
  re-roots the account and its whole subtree: every descendant keeps its path
  relative to it, and the account's `parent_id` becomes the account at the
  new parent path, if one exists (descendants keep theirs). The path passes
  `validate_path` (422, §3.2). Refused: an **occupied destination** — any
  account outside the moved subtree at the new path or beneath it (409, so a
  move never silently adopts existing accounts as children); a move under
  itself; a subtree holding a category; and a destination under a
  **category root** — the first label of any category's path (`expenses`,
  `income`) — because the `<@` subtree reads there (category rollups, the
  register's category filter) would count the account's own postings as
  category totals (422). Real-account roots carry no semantics — net worth
  classifies each account by the sign of its value, behaviour keys off
  `type` — so `assets` ↔ `liabilities` or a new root is a pure relabel.
  Everything the ledger stores refers to accounts by id (postings,
  schedules, rules, memories, reconciliations, valuations, holdings), so
  history and balances follow untouched: a move marks no balances (§3.5).
  The one place a real account's path is stored as live input — an **open
  suggestion's typed draft** (decision 57) — is re-pointed in the same
  transaction (`suggestion.repoint` audit rows), so accepting it can neither
  miss the account nor land on whatever later takes the old path; decided
  suggestions, events and audit rows keep the path they were written with.
  The paths move in two statements through a staging root, because
  `accounts_path_uniq` is checked row by row (`a.b → a` with `a.b.b` present
  would collide mid-statement). Write scope, like rename; audited
  `account.move` (every moved account's from/to), evented `account.moved`.
- **Hide** (`hidden_at`) — display-only. The account drops out of the **default
  `GET /accounts` listing** and *only* that surface; it still syncs, still
  categorizes, still counts in net worth, still appears in `GET /accounts/{id}`
  and as a transfer target. `?include_hidden=true` shows it. This is the one
  filtered surface — a decluttering affordance, not a semantic change.
- **Close** (`closed_at`) — makes the account **read-only**: no new posting may
  land on it. Closing is a guided sequence: (1) refuse if any *active* schedule
  targets it or any *live pending* transaction touches it; (2) auto-void its
  `expected` instances (a legal `expected → void` transition); (3) if the live
  (pending+posted) balance is nonzero, post ONE **ordinary balancing
  transaction** for `-balance` to a caller-chosen adjustment category (default
  path `expenses.adjustments`, auto-created along its ltree path) — an ordinary
  posted row, not a special ledger event — **before** stamping `closed_at`;
  (4) re-check the balance is *exactly* zero; (5) if SimpleFIN-mapped, unmap
  (clear `connection_id`/`external_id`, §7.1). Read-only is enforced at the
  single posting chokepoint (`_persist_transaction`, which also covers in-place
  edits, reversal corrections, transfers, imports, and status transitions) and
  backstopped by the `postings_closed_account_guard` INSERT trigger. System
  pools (transfer pairing/absorb, schedule materialization, shortfall scans,
  auto-categorization) skip closed accounts so no job crashes on the chokepoint.
  **Corrections to a closed account's history require reopening it first.**
- **Reopen** — clears `closed_at`; the account accepts writes (and corrections)
  again, and can be remapped through the normal feed map path.

**Net worth:** for a closed account, from its close date onward the valuation
and brokerage-holdings branches are skipped — the closing adjustment already
zeroed its postings, so its natural contribution is 0, and stale
valuations/holdings dated after the close must not resurrect it. Net worth
*before* the close date is untouched — history stays intact.

**Duplicate-account deletion** is deliberately NOT a lifecycle state and NOT an
API endpoint: it is a guarded admin CLI (`purser admin delete-account`, decision
33) because hard delete FREES content hashes for re-import, which voiding never
could.

### 3.5 Derived balance tables (0.8.0, decisions 37/38)

Two derived tables — never authoritative, rebuildable from postings at any
time. They are separate tables PRECISELY because the posted-immutability
guards (§4) forbid updating a balance column on `transactions`/`postings`
once posted.

**`account_running_balances`** stores, for every (real account, chain-head
transaction with status pending|posted having a posting on the account), the
account's `delta` in that transaction and the running `balance` after it,
accumulated in the total order `(date ASC, created_at ASC, transaction_id
ASC)` — the id tiebreak is load-bearing: `created_at` is
transaction_timestamp, identical across one import batch. The register
orders by exactly the reverse (the order is written once, in `balances`:
`REGISTER_ORDER_ASC_SQL`/`REGISTER_ORDER_DESC_SQL` for the store's SQL and
`transaction_order_desc` for the register's ORM query), so on screen
`balance[i] = balance[i-1] + amount[i]` holds over the default view and the
newest row equals `account_balances()` (pending included). Point-in-time
balance at D = the last stored row dated <= D (0 before first activity) —
recorded, never recomputed per request (`GET /accounts/{id}/balance?as_of`, MCP
`account_balance`, register Balance column). Registered divergence
(decision 37): a posted correction that EDITS the date leaves the original
posted at its old date (demoted from chain head, never voided), so inside
[old, new) a sum-over-all-rows figure (`net_worth_at`) still counts the
original while the heads-only register does not — deliberate: the
correction asserts what was always true. Virtual accounts are excluded (a
category cumsum is meaningless).

**`balance_watermark`** is a singleton with two independent dirty cursors,
LEAST-updated transactionally by every balance-affecting writer
(`balances.mark_dirty`, colocated with the writer's audit record):

- `running_dirty_from` — consumed SYNCHRONOUSLY by a sessionmaker-level
  `before_commit` hook: no-op unless the session marked; else one set-based
  delete + window-function re-insert from the cursor, anchored per account
  on the last surviving row. A register read can never observe staleness,
  and a bulk import pays ONE recompute, not per-row work.
- `networth_dirty_from` — consumed ASYNCHRONOUSLY by
  `networth.consume_watermark` (§8): claim-and-clear in a short
  transaction, then re-materialize cursor→today with per-day commits
  (re-marked on failure). Kicked post-commit by QIF/CSV import, feed
  sync/backfill, valuation create, and close/reopen (FastAPI
  BackgroundTasks — the API process may run no scheduler in the split
  topology), inline by the scheduled sync and the delete CLI, and nightly
  by `materialize_job`. `series()`/`series_sampled()` treat stored rows at
  or after the cursor as absent (computed live instead), so reads are never
  stale even before a consumer runs.

The watermark row's UPDATE lock is the recompute mutex. Writers needing NO
mark (documented so nobody "fixes" them later): expected↔void transitions
(matching confirm/unmatch, close's expected-voids — expected rows are
outside the balance set), hide/rename (display-only, pinned by test),
account moves (paths are labels — postings key on account ids; pinned by
test), category CRUD; categorization identity moves are covered by the
reversal-pair funnel. The nightly job also heals a leftover running cursor
and runs a parity probe (newest stored balance vs `account_balances()` per
account; mismatch → log + full rebuild + networth re-mark). Deferred:
unifying `net_worth_daily` onto a per-account daily rollup of this table
(valuations/holdings/close-cutoffs make that a larger refactor for no
owner-visible gain).

The table also serves **series** reads (decision 51, issue #198) with no new
storage and no second watermark: `balances.balance_series` resolves a list of
dates with one `unnest` + `LATERAL` last-row-≤-d seek per date against the
existing `(account_id, date, created_at, transaction_id)` index — sparse
store, dense answer, the `balance_asof` rule batched. `change_points` range-
scans the same index for the full per-change resolution, `window_extremes`
takes the min/max over the END-OF-DAY balance of EVERY day with activity in
the window — each date's last row in the total order, the value
`balance_series` reports for it (decision 63) — and `largest_moves` reads
`delta` for attribution. Every day, not the sampled ones, so downsampling is
safe to publish — the extremes survive it. End-of-day, not per-row, so
intra-day ordering never creates an extreme: one day's rows are ordered by
when they were recorded, not when the bank processed them, so a gross pay
leg posted before its same-day deductions is not a high and a duplicate
charge posted before its same-day reversal is not a low. The per-change
resolution keeps those intra-day steps; only the extremes rank days. Density
is chosen by `core.sampling.sample_dates`, shared with
`networth.series_sampled`. Virtual accounts stay excluded from history for
the same reason they are excluded from the table. The series inherits
decision 37's register semantics, **including** the documented heads-only
divergence from `net_worth_at` inside a date-edited correction's
`[old_date, new_date)` window.

### 3.6 Memories (0.14.x, issue #176, decision 48)

Short, user-owned notes of financial nuance — *"the monthly $750 to Fidelity
the child is a 529 contribution"* — that Purser serves to connected agents as
context. **Rules act, review flags ask, memories inform.** A memory that would
change what lands in the ledger is a rule in the wrong table.

**Non-executable by construction.** No code path parses memory text. There is
no evaluator, no import hook, no scheduler entry that reads one. If a nuance
should *act*, it graduates into a rule (§5 / decision 46) via
`rules.memory_id`, and the memory stays as the rationale. Graduation changes
nothing about the memory: archiving it does not disable its rules, because
rules are the executable layer and must not change behavior because a note was
tidied.

**Shape.** `memories.text` (encrypted, §9) plus zero or more `memory_links`
rows to an account or a payee. Categories *are* accounts (§3.2), so one
nullable pair covers all three link kinds the feature needs; the API labels a
link `category` when the target is virtual. Unlinked memories are global. A
link table rather than nullable FKs because the motivating example — "Amex
Platinum is paid from example checking" — is about *two* accounts, so a single
`account_id` column would be wrong on the first real memory.

**Caps** (service-layer, test-pinned): 500 chars per memory, 100 active, 8
pinned, 25 outstanding drafts. The draft cap is the anti-flood valve: an agent
proposing drafts cannot fill the table, and neither can an optional authoring
pass (§11.3).

**Lifecycle.** A memory is created as a `draft` unless the caller passes
`activate` (#436): `propose_memory` defaults to a draft, and its guidance
tells the agent to activate only when the user explicitly asked it to
remember something — an active memory is served as context to every future
session, and an agent activating memories from untrusted text (bank
descriptions are merchant-controlled) could persist an injection. A draft is visible in
the Config UI and via `list_memories(include_drafts=true)` but is **never**
served — not in `describe()`, not in the default listing, not to the reviewer,
not to the classifier. That is what stops an agent (or Purser) writing its own
future context unasked. Every memory write needs `write`. Archive
(`archived_at`) is the normal removal path and is reversible; hard `DELETE`
requires `admin` and is recoverable only within `audit_retention_days`.

**Relevance is exact.** A memory is relevant to a transaction iff one of its
links is that transaction's real-account id, its category-leg account id, or
its `payee_id`. FK equality — no substring, no name similarity, no embeddings
(decisions 35/40). One consequence must be stated because it surprises people:
**category-linked memories cannot reach the classifier**, since at
categorization time the category is the unknown being decided. Account and
payee links (and pinned/global memories) do reach it.

**Serving is budgeted** (guardrail 6). 100 memories is roughly 12k tokens, so
`describe()` carries a count plus the pinned few (≤8 rows, ≤1,000 chars) and a
pointer to `list_memories`, which is itself capped (≤25 rows, ≤2,500 chars)
and reports exact truncation. All-100 is unreachable through any single call.
Ordering is deterministic — `pinned DESC, updated_at DESC, id` — so a
truncated payload is stable between calls.

**Staleness is a lint, never an action.** A link to a closed or hidden account
marks the memory `stale: true` on read (a computed join, no column). Purser
never edits or archives the user's prose on its own.

**Scope.** Reads need `read` and writes `write`, like every surface (§9; #437
retired the unredacted-read rule this surface used to carry).

**Naming.** Unrelated to `categorize.payee_memory`, the deterministic
payee→category history lookup. Neither is renamed; the collision is noted once
here.

**Authorship.** User- or agent-initiated by default; Purser detects
deterministic triggers (e.g. "you have recategorized WHOLEFDS → Groceries four
times") and offers them as *hints* with templated text, computed on demand
from existing `audit_log` rows — no model, no new tables. When a provider is
configured and the operator opts in, Purser may also author drafts itself
(§11.3); the draft gate above is what makes that safe, and the connected-agent
path remains primary because it has the conversation and is the better author.

### 3.7 Account reconciliation (issue #268, decision 61)

"The statement balance of account A at the end of D was X" is an explicit,
audited ledger write — never an importer side effect, and never a marker
that can go stale silently.

- **Scope.** Open checking, savings, cash and credit-card accounts: the
  postings-balance types §7.3's drift check compares. A loan/property balance
  is a valuation and brokerage adds market value (§8), so a statement for
  those is recorded as a valuation instead. Categories never reconcile (CHECK).
- **Statement convention in, ledger sign inside.** `statement_balance` is the
  figure as printed: positive = money held, and for a liability (credit card,
  loan) positive = the amount owed. Purser negates liability figures into
  ledger sign, where a card is negative when owed (§7.3); passed through
  unconverted, a card's positive figure would commit a ~2x plug. Responses
  echo `statement_balance`; every other amount is in ledger sign.
- **Basis.** Each claim names its basis — `posted` (default, the statement
  balance) or `pending_inclusive` (posted plus pending, a bank's "current
  balance") — §7.3's two bases. The ledger balance on D in a basis is the sum
  over chain heads (no row reverses them) dated on or before D with status
  `posted`, respectively `pending`/`posted`: the stored running balance's own
  definition (§3.5) restricted by status. It is computed live, in one batched
  statement (`reconciliation.ledger_balances`) shared by the write, its dry
  run, the retry check and every read, because the store is consumed only at
  commit and the write reads its own uncommitted adjustment back.
- **The write.** `POST /accounts/{id}/reconcile` and MCP `reconcile_account`
  (write scope) lock the account row (`FOR NO KEY UPDATE`: two reconciles
  exclude each other, while an import inserting postings — whose FK check
  takes `KEY SHARE` — never waits on it, so it cannot deadlock against the
  balance watermark), compute `adjustment = target − ledger(D, basis)` and,
  when nonzero, post ONE ordinary flat transaction dated D to the system
  category `expenses.reconciliation_adjustments` (auto-created along its path,
  like close's `expenses.adjustments`), payee "Balance reconciliation",
  `origin = system`. A posted row on D moves both bases, so the target is
  exact in either. The `account_reconciliations` row, the marker
  (`reconciled_through = D`), the `account.reconcile` audit row and the
  `account.reconciled` event commit atomically with it. A zero difference is
  recorded the same way with `adjustment = 0` and no transaction (the shape
  CHECK): the clean reconcile is the common case.
- **Review, not trust.** `origin = system` puts the adjustment in the
  unreviewed set (§4), so a person sees every plug whoever asked for it. The
  matcher excludes adjustments explicitly (§5): the system origin would
  otherwise offer a plug to settle an expected bill of the same amount.
- **Dry run is the commit path.** `dry_run` runs the same computation,
  conflicts included, and stops before the first write.
- **Never stale silently.** A claim keeps its target, and every read that
  serves the marker — `GET /accounts`, `GET /accounts/{id}`,
  `GET /accounts/{id}/reconciliations` (history, newest first) and MCP
  `list_accounts` — re-checks the reconciliation live and reports
  `ledger_balance`, `difference` (ledger − target) and `in_balance`. A
  backdated import, edit or correction, a vanished pending or an archive
  replay that moves the balance on D therefore shows on the next read.
  Nothing is rewritten and no event fires: staleness is a computed fact, and
  resolving it stays a human decision.
- **Retries and conflicts.** One claim per account and date (UNIQUE
  `(account_id, as_of)`, whatever the basis). Repeating a recorded claim —
  same date, basis and target — returns `created: false` while the ledger
  still agrees, and 409 `reconciliation_moved` with the difference once it has
  moved: a moved ledger is never answered with a quiet 200. A different target
  or basis for a recorded date is 409 `reconciliation_conflict`; a date before
  the marker is 409 `reconciliation_out_of_order` (the marker only moves
  forward, so a new adjustment never lands before a recorded claim); a future
  date is 422.
- **Removal.** The guarded delete-account CLI (decision 33) deletes an
  account's reconciliations before its transactions (the adjustment FK), and
  every suggestion naming the account as subject or counterparty, open or
  decided, before the account row: their foreign keys carry no `ON DELETE`
  (#399).

## 4. Transaction lifecycle & immutability

`expected → pending → posted → void`

- **expected** — future entries: manual or materialized schedule instances,
  and the rows a QIF/CSV file marks **uncleared** (§7.2, decision 60 — the
  bank has not seen them yet; a later file that marks one cleared posts it). Amounts are estimates. Consumed by the matcher
  and the forecaster. A schedule instance is the user's to edit (§6): a
  token-bearing amount, date or category edit pins it (a description or
  payee edit is a label and does not — decision 79; `reset_instance` unpins
  it), a void keeps its slot filled ("skip this one"),
  and only the rule's own resync (edit/pause/end) voids-and-releases a slot.
  An amount edit of a transfer instance moves both legs (§3.3, decision 78).
- **pending** — imported-but-not-cleared (SimpleFIN `pending: true`), and
  manual or agent entries the bank has not posted. It no longer also means
  "awaiting the user's OK": a write principal's requested status is honoured
  (#436, decision 90). Agent-created rows keep their matching behaviour: a
  candidate to settle an expected bill must be `origin` import or system, or
  a two-real-leg transfer, so an agent's flat row — pending or posted —
  never settles a bill, and the pending lifecycle rule voids only
  import-origin pendings. Freely editable — a transfer's
  amount moves both legs, unless it carries linked bank rows (§3.3). A manual import's
  uncleared rows are *expected*, never pending: the pending lifecycle rule
  below voids any import-origin pending a sync no longer lists, whatever
  source the row came from.
- **posted** — immutable. Any change (including recategorization) is executed
  as an atomic reversal + replacement pair linked via `reverses_id`. A posted
  row is corrected at most once (`transactions_reverses_id_uniq`); further
  edits continue the chain from the replacement. The flat view collapses
  reversal chains; the ledger keeps the forensic history. A posted transfer
  is not corrected through an edit: it is unlinked, or converted back to a
  category when it carries no bank rows (§3.3, decision 78). The DB trigger
  allow-lists exactly one mutation of a posted row: `status -> 'void'` set
  together with `superseded_by` in the same UPDATE (match supersede, transfer
  merge). A bare void of a posted row — no lineage link — is an error, at the
  API (422) and at the database.
- **void** — dead rows kept for lineage: superseded expecteds, merged transfer
  halves, vanished pendings, reversal legs.

**Unlink (0.5.0).** Undoing a transfer merge/absorb resurrects each voided
original as a fresh clone carrying its prior status — and its `content_hash`
**moves** from the void row to the clone (the global unique index forbids
duplication, and the clone now *is* that bank line, so import idempotency and
the pending lifecycle stay exactly right; the void row remains as forensic
lineage). A merge-created transfer is then voided with `superseded_by` → the
resurrected outgoing side; a conversion-created transfer survives with its
side back to unbacked. The `transfer.unlinked` event payload suppresses the
automatic relink paths only — explicit confirm/convert can always relink. A
split loan payment (§3.3, decision 62) unlinks the same way: both bank rows
come back exactly as they were, and its interest/fee posting is voided with it.
An expected the transfer **settles** is not an original, though a match
supersedes it too (decision 65): it is never resurrected — that used to post
a phantom copy of the instance's legs — and when a merge-created transfer is
voided its settlement goes with it, released exactly as `unmatch` releases
one (the match rejected, the expected re-opened in its slot) while a
proposal naming it is rescinded. A void row settles nothing; the next
matching pass re-decides. `has_linked_originals` and the register's unlink
control apply the same test, so a matched bill payment offers no unlink.

**Pending lifecycle rule:** on each fetch, pendings from prior fetches that no
longer appear and have no matching posted transaction are auto-voided (with an
event). Banks drop pendings that never post; ledgers that keep them drift.
Every open pending on a postings-balance account is re-checked on every sync —
the fetch reaches back to the oldest one the bridge can still serve, while a
brokerage, loan or property account's pendings never widen it (decision 74) —
and the bank posting a pending under its own identity supersedes it like any
other posted successor (§7.1, decision 70).
A voided pending can be the bank row of a live match (§5 matches expecteds
against pending rows too); the settlement goes with it (decision 69) —
carried to the posted successor, or released so the bill is expected again.
Voiding a matched pending by hand (`POST /transactions/{id}/void`) settles it
the same way with no successor: a confirmed match is released with the pin
kept and a proposal is rescinded, the voiding token as actor (#343).

**Review marks (0.14.0, issue #175).** Rows that land in the ledger *without
human eyes* form an **unreviewed set** — a filter over the registers, not a
second queue (0.5.0 deleted the queue deliberately). Membership is exactly:
live (`pending`/`posted`, unsuperseded) chain heads with
`origin IN ('system','rule')`, or `origin='import'` with a non-NULL
`source_fetch_id`. That last clause is load-bearing and reuses
decision 45's feed-row definition: QIF and CSV imports are *also*
`origin='import'`, and a 6,081-row Quicken import must not become 6,081 review
items — the user chose those rows. `origin='agent'` is excluded too: the
agent wrote them with the user's authority, every one is attributed in the
audit log, and two mechanisms over one row double-count. No `expected` row is ever a member, whatever its origin: a
schedule instance (`origin='rule'`) is a forecast, which a match, a skip or a
rule resync voids and only a token can post — and posting marks it.

**The predicate lives in one place**, `purser/review.py`: `scope_clause()`
mirrors the 0021 partial index, and `unreviewed_clause()` adds the chain-head
and no-mark tests. Every surface *evaluates* it rather than restating it — the
count in `describe()`/`whats_new`, `review_batch`, the aged sweep,
`GET /transactions?reviewed=false|true`, and the `needs_review` boolean on
every `/transactions` row (list and detail), which is what the register's
"needs review" badge renders. No client derives membership from `reviewed_at`:
a missing mark means *unreviewed* only inside the scope, and most rows —
manual, agent, QIF/CSV, `expected` — never need one (#248).

Marks live in `transaction_reviews`, **not** as columns on `transactions`.
They are annotations *about* a row rather than ledger content — they move no
money and belong outside the immutability envelope — and, decisively, the
posted guard's allow-list (§4 above) makes any new `transactions` column
immutable once posted, which is precisely the state the rows needing marks are
in. Being annotations, they go with their row: the guarded admin deletes
(decisions 34, 45, 72) remove a deleted row's mark first — migration 0021
backfilled one onto every row that existed then, and the foreign key has no
cascade.

- A row **leaves** the set when anything token-bearing writes to it — edit,
  post, transfer confirm, match confirm (confirming a proposal or matching by
  hand marks the bank row, `via='match'`), unlink — or on an explicit
  `mark_reviewed`. Marking is idempotent and monotonic; there is no un-review
  in 0.14 (re-surfacing is better expressed by the row's content changing).
- **Corrections inherit review state only from automation.** In
  `reverse_and_replace_with_specs`, a token-bearing actor's replacement is
  *reviewed* — the correction **is** the review — while a system/rule actor's
  replacement carries the original's state, so automation neither grants nor
  revokes it. A rule recategorizing an unreviewed import row leaves it needing
  review; a rule touching a reviewed row does not nag again.
- **Unlink copies each void child's review row onto its clone**, exactly as
  `content_hash` moves: decision 29's rule is that the clone *is* that bank
  line, so its review state is that bank line's state.
- **A pending's mark follows it to its posted successor (decision 82).** When
  the feed supersedes a pending by its posted row — successor pairing or the
  bank posting it under its own identity, both through `reconcile.void_pending`
  — the pending's mark is copied to the successor's live **head** (post-sync
  categorization may already have corrected it) when that head is in scope,
  has no mark of its own, and carries the same category legs. A different
  category means the posted row says something nobody reviewed — its category
  came from rules or payee memory — so it comes back for review.
- **Every copied mark is whole** — actor (`reviewed_by`), `via`,
  `reviewed_at`, note, escalation — with `carried_from` naming the source row;
  correction inheritance, unlink and the pending carry share one copier
  (`review._copy_mark`), audited `transaction.reviewed` with a NULL actor and
  the copy's `reason` (`correction`, `unlink`, `pending_posted`). A copy never
  overwrites an existing mark.
- **Escalation, not a second tier.** A reviewer that is unsure sets
  `escalated_at`. The row is reviewed — it leaves the unreviewed set, so no
  queue is recreated — but surfaces in a small "flagged for your attention"
  list, with the reviewer's reason in the encrypted note. It stays small by
  construction: opt-in per row, at the reviewer's discretion.
- **Automation that merely touches a row never marks it reviewed** (import,
  `categorize.run`, `auto_link_pass`, rule application, the matcher's
  auto-confirm). A settlement carried into a merge or an absorb (decision 65)
  moves the decision, not a review: pairing rows into a transfer is a
  judgement of its own, so the transfer keeps its own state. A pending's
  posted successor is the same bank line, so it takes the pending's mark
  (decision 82) — with or without a match on it. Automation that *reviews* it does —
  `via='ai_review'`, and only when the operator enabled the backend reviewer
  (§11.2). `via` carries that difference into the audit trail permanently.
- The reviewer's guidance closes the loop back to rules: a fix you make
  repeatedly should graduate into one (`propose_rule`). The flags are the
  record of what rules *failed* to catch.

## 5. Matching engine

Expected transactions reconcile against imported posted/pending reality.

- **Candidates:** same account, date within window (default ±5 days),
  normalized-payee similarity, amount closeness. **Amount is weighted softly**
  — payee + date agreement with a differing amount is still a confident match,
  carrying a variance. The pool is bank reality — `origin='import'` rows and
  the `origin='system'` rows re-derived from them (unlink clones; a
  reconciliation adjustment is `system` too but is excluded as Purser's own
  balancing entry, §3.7) — **plus any live two-leg transfer regardless of
  origin** (#232, decision 56): the
  auto-linker's merges are `system`, a human confirm's carry the confirming
  actor, and both are what a card payment expected on checking must settle
  against. Only the current correction-chain head is eligible: complete
  original/reversal/replacement pairs preserve bank lineage regardless of the
  replacement actor. Originals and reversal halves never settle bills (#419).
  Rows are scored **through the perspective leg** — the leg on the
  account being matched — so a one-leg import and a two-leg merge are
  comparable; a two-leg expected is tried from each of its accounts and the
  best score wins. Two rows that share a counterparty account agree on "who"
  (payee term = 1.0), which is how an expected transfer pairs with a merged
  transfer that carries no payee at all.
- **Tiers:** score ≥ auto threshold → auto-match; mid → `matches.state =
  proposed`, mirrored by a `kind='match'` card in the suggestions inbox (§6,
  decision 86); low → unmatched. A bank row a person **rejected** for an
  expected's slot — or the row that superseded it (its posted successor, the
  transfer it merged into) — is never a candidate for that slot again, in
  either tier; an explicit `match` can still pair them. An open proposal whose bank row
  is live already counts that row in the balance, so its expected is
  **settled pending confirmation** (decision 76): the forecast and the
  digest's upcoming bills leave it out (`matching.proposed_settlements`, the
  `matches` row being the source of truth). A manual match of the very pair
  the matcher proposed confirms that proposal — the unmatched report names
  the pair's bank row (`proposed_posted_id`) so an agent can — while a bank
  row proposed for a different expected is refused, naming it.
- **On match:** expected is voided with `superseded_by` → posted; posted is
  truth (**posted amount wins**); `matches` row records score and variance.
  Variance feeds schedule estimates and threshold events.
- **The bank row moves on, the settlement follows (decision 69).** A match
  may name a `pending` row, and the feed can later void it (§4); as with a
  merge (decision 65), a void row settles nothing. The reconciler captures
  the live match before the void. After it, a **confirmed** match moves to
  the pending's posted successor when no other confirmed match holds that
  row (`matches.posted_id` and the expected's `superseded_by`, audited
  `match.carry` with `reason` `pending_posted`; a proposal sitting on the
  successor yields, and its expected is matched again at once). A
  **proposal** is rescinded (`match.rescind`) and, when the pending posted,
  its expected is matched again at once. Otherwise the confirmed match is
  **released** through `release_settlement` (`match.unmatch` with `reason`
  `pending_vanished` or `successor_matched`): the instance comes back as a
  fresh `expected` clone in its slot (void is terminal) and keeps its pin,
  so the bill is due again and the next matching pass can settle it against
  what the bank really posted. A carry keeps the variance: the successor
  carries the pending's exact amount. The feed acts as the system actor. A
  **manual void** of a matched pending (`transition_status` pending → void,
  #343) runs the same settlement with no successor and `reason`
  `pending_voided`, the voiding token as actor: a proposal is rescinded and
  not re-decided at once, a confirmed match is released with its pin kept —
  the person decided about the bank row, not the instance.
- **What an estimate learns from (decision 69):** a confirmed match counts at
  the live **posted** chain head its bank row leads to — a superseded pending
  to its posted successor, a merged row to its transfer, a corrected row
  (`reverses_id`) to its replacement — read as one amount from the schedule
  account's side (decision 65); a head two matches reach counts once. A
  pending head is provisional and a void head settled nothing, so neither
  counts: a match to a pending row moves the estimate only once the posted
  row lands, and that is when `recurring.price_changed` fires — whether the
  feed posts it or a person does (`POST /transactions/{id}/post`:
  `transition_status` probes every estimated schedule with a confirmed match
  whose bank row leads to the row before the change and emits at most once
  per schedule after it, #330) or a transfer absorb does (`_absorb_row`
  probes the transfer and the absorbed row together: posting a pending
  transfer, or moving a matched pending under a posted one, moves the head
  the same way, #343).
- **Transfer detection (deterministic tiers, 0.5.0):** opposite-amount pairs
  across accounts within a date window are the candidate pool. Pairs whose
  text also carries a **name signal** — a transfer/payment-style keyword
  token (`transfer`, `xfer`, `tfr`, `payment`, `pmt`, `pymt`, `autopay`) or
  the counterparty account's normalized name — are **auto-linked** during
  import and the daily sweep: one merged transaction with two real-account
  postings (`origin = system`), the one-sided originals voided with links.
  Amount+date-only pairs stay confirm-first candidates. **Absorption:** an
  imported row matching an existing transfer's *unbacked* side (no voided
  import child on that account — a structural check, no marker column) with
  a name signal is voided into that transfer instead of duplicating it.
  The old "never silent" rule is now **never unlogged**: every auto-merge
  and absorb writes an audit row, emits an event
  (`transfer.auto_linked` / `transfer.absorbed`, payloads carrying the
  originals' prior statuses for exact undo), is visibly badged in the
  register, and is reversible via unlink. Pairs a human explicitly
  unlinked are suppressed from the AUTO paths only — explicit
  confirm/convert can always relink. The auto paths act on **posted rows
  only**: consuming a bank-pending row in a merge would turn the known
  pending→posted rehash edge into a duplicated leg, so pending pairs stay
  confirm-first candidates until they post. Kill switch:
  `transfer_auto_link` (Config registry, default on). The name-signal test is
  `core.matching.has_transfer_signal`, token-boundary aware via space padding
  so `chase` never matches inside `purchase return`.
- **Declared transfer pairs (0.14.0, issue #174, decision 46; per-pair window
  #210, decision 66):** an active `transfer_link` rule names an
  ordered-insensitive account pair for which the **name-signal requirement no
  longer applies**, and may give that pair its own **date window**
  (`window_days`, 0–14; unset = the global ±3 days). Everything else about
  the candidate pool is unchanged and still applies: exact opposite amount,
  posted rows only, cross-account, and unlink suppression — a pair a human
  unlinked stays unlinked, because the suppression checks are ANDed *before*
  the signal test and the rule replaces only that test (and, for its own
  pair, the window). The window applies in **two stages, so it never changes
  what the standard window decides**: the absorb and pair phases first run
  over ±3 days exactly as before — there a declared pair links without a name
  signal only when its rule's window covers the gap, so a rule can also
  *narrow* its pair — and only then does a widened rule link its pair farther
  apart, among the rows the standard stage left **without any partner** (and,
  for absorbs, into the open legs it left unclaimed). A row the standard stage
  paired — linked, or left as a confirm-first candidate — is never taken by a
  farther one, and every undeclared pair keeps ±3 days, explicit conversions
  included. `transfer_link` rules are unordered (they carry no conflicting
  action, so a pair is covered if *any* active rule covers it, with the widest
  window among them) and, unlike the other rule types, retroactive for free:
  `auto_link_pass` already sweeps all live one-sided posted rows nightly, so
  an activated rule simply starts being true. That is why activating one
  **requires an explicit confirm of the projected link count** — a merge is
  balance-neutral, so §7.3's drift check cannot detect a bad one, and
  unbounded activation applies a drift-blind operation to all history at
  once. The count is taken **with the rule's window**, by running the pass's
  own selection (`transfers.plan_auto_links`, which `auto_link_pass` then
  executes) with the rule declared and counting the merges *and* absorbs it
  justifies — so the number confirmed is the number linked, and
  `preview_rule` / `POST /rules/preview` given a pair return that same count
  before the rule exists. An active rule's pairing scope (pair,
  `amount_exact`, `effective_from`, `window_days`) is frozen: an edit is
  refused until the rule is disabled, so widening it goes back through the
  confirm. `effective_from` bounds the blast radius. Motivating cases:
  recurring brokerage ↔ checking transfers whose feed descriptors ("BROKER TRANSFER
  LLC MONEYLINE" / "MONEY LINE RECEIVED EFT") carry no transfer keyword and
  fail the counterparty-name test; a monthly checking → card autopay whose
  legs posted four days apart (#210).
- **Apply-history covers all live history; the preview is a sample (#356).**
  A `categorize` or `rename` rule reaches history only through an explicit
  `apply-history`, which walks **every** live row in keyset batches
  (`(date, id)` descending, no cap) and reports `scanned`. Its dry run and
  its commit select from the same walk, and selection finishes before
  anything is written, so decision 35's preview-exactness holds. The
  predicate preview (`preview_rule` / `POST /rules/preview`) is a windowed
  sample of the newest 5,000 live rows and says so with
  `scanned`/`scan_capped`.
- **A transfer schedule is a pair signal (issue #270, decision 65):** an
  instance of an active schedule with a counterparty (§6) **covers** a pair
  when it names the same two accounts in the same direction, exactly the
  pair's amount, and both legs sit inside the ±3-day transfer window of its
  date. In the standard stage's pair phase it replaces the name-signal test
  exactly as a `transfer_link` rule does — everything else still applies;
  absorption keeps the name and rule signals, and the widened stage admits
  declared pairs only — and the auto-link records `signal`
  (`rule:<id>` > `name` > `schedule:<id>`, the first that applies: a covering
  declaration is credited first, as in the absorb phase and the projected
  count). It needs no activation confirm because it cannot sweep history: an
  instance vouches only while it is open (materialized ahead, or overdue) or
  for a pair holding the very row that settles it, and the merge it covers —
  in either stage, whatever linked it — settles it at once through the
  ordinary matcher, so it covers one merge. Exact amount keeps an estimated
  instance whose bank amount differs out; that pair links by name, by rule,
  or by confirm.
- **Held legs stay in the pool; the settlement follows the bank line
  (decision 65).** The nightly job matches before it links, so when the two
  legs of a scheduled transfer post on different days the first is usually
  already matched (proposed — exact amount and date alone score 0.55 — or
  auto) to the two-leg instance when its counterpart arrives. A one-sided
  bank row whose live match is to a plain expected transfer, on one of its
  sides in the same direction, is **held**: it stays in the pairing pool —
  both pair phases of `transfers.plan_auto_links` (and so a rule's projected
  count), the candidate list, a conversion's counterpart — but pairs only
  with a row on that expected's other account, never with another held row
  (`MatchInput.pair_account_id`, enforced inside the greedy pairing loop,
  not after it), and is never absorbed. Every merge carries the originals'
  matches: a **confirmed** match whose expected the merge covers moves to
  the merge (`matches.posted_id` and the expected's `superseded_by`, audited
  `match.carry`); a **proposal** names a row that no longer exists, so it is
  rescinded and its expected re-matched at once (on the merge, an auto match:
  a shared counterparty is payee agreement); any other confirmed match stays
  where it was decided. The instance is settled exactly once, by the merged
  transfer, and a matched merge settles nothing else. A row held for a bill
  never leaves its bill.
- **A manual confirm proposes a payee-scoped pair rule (#271, decision 67).**
  Purser keeps no hidden memory of confirmed transfers. An ordinary
  `confirm_transfer` (REST and MCP; a split proposes nothing) upserts one
  `transfer_link` suggestion (§6) whose draft is a rule for the confirmed
  account pair with `payee_scope_id` = the outgoing row's payee (else the
  incoming one's; no payee → no suggestion), resolved through its aliases.
  A **payee-scoped** declaration covers a candidate pair only when either
  leg's payee is in the scope payee's alias group (`rules.alias_group`,
  resolved when the rule loads), so a rename whose apply-history aliases the
  payee keeps the rule in scope; `pair_declared` / `declared_window` take the
  legs' payees for exactly this test, and nothing else about the two stages
  changes. The scope only ever narrows a pair, and it is part of the frozen
  pairing scope and of the activation projection. Nothing links because of a
  confirm until a human accepts the suggestion (a DRAFT rule) and confirms
  its projected count, and unlinking a confirmed pair leaves no behaviour to
  unlearn: there is only the visible, rejectable suggestion.
- **Split loan payments (decision 62, §3.3):** unequal legs never auto-link,
  absorb or pair as candidates — every automatic path above requires exact
  opposite amounts. An explicit `confirm_transfer` with `remainder_category`
  joins them, keeping both bank amounts exact and booking the difference to
  that interest/fee category; same lineage, review mark and unlink as any
  confirm.
- **Unmatched reporting, both directions, with aging:** expected-but-missing
  (late bills) and posted-but-unexpected (new-spend triage).

## 6. Schedules & forecasting

- Recurrence is RFC 5545 RRULE (parsed/expanded with `dateutil`), anchored on
  the schedule's persisted `dtstart` (#231, decision 55): `FREQ` must be
  `DAILY` or coarser (sub-daily rules are rejected at 422), and the RFC
  `UNTIL=…Z` form is accepted.
- **Rule vs instance (decision 55).** The schedule is the durable rule;
  instances materialize as `expected` transactions on a rolling horizon
  (`MATERIALIZE_HORIZON_DAYS`, nightly + on activate/edit), idempotent on
  `(schedule_id, occurrence_date)` across every status. Instances are what a
  user edits: **override** (amount/date via the ordinary transaction edit;
  a transfer instance moves both legs, its amount read from the schedule
  account's side unless the edit names the other — §3.3, decision 78)
  pins the instance (`schedule_pinned_at`) and the rule never touches its
  amount, date or slot again; **reset** (`reset_instance`, write scope,
  decision 79) undoes that — the pin is cleared and the date (its slot),
  name, payee, category and current estimate are the rule's again, or the
  instance is voided and its slot released when the rule no longer produces
  the slot or estimates nothing (an instance whose legs lost the rule's
  shape is released and its slot materialized afresh), audited
  `schedule.instance_reset`; **skip** (void) keeps the slot filled so the
  occurrence does not come back; **mark paid** is a manual match. **Labels
  follow the rule** (decision 79): a description or payee edit does not pin,
  and renaming a schedule or changing its payee relabels every live
  instance, pinned ones too, whose description still equals the old name or
  whose payee still equals the old rule payee (normalized) — a label the user
  set differs and is kept; audited as one `schedule.relabel` row. A
  payee-less flat schedule's name *is* its instances' payee, so it feeds
  match scoring (§5) and `whats_new`'s bills-due list. The rule side: **estimated**
  schedules refresh their live un-pinned instances whenever the estimate
  moves (audited `schedule.refresh`) — the estimate reads posted chain heads
  only (§5, decision 69); a rule **edit** resyncs — slots the new
  rule no longer produces are voided *and released* (`occurrence_date =
  NULL`, audited `schedule.resync`) so a later rule can fill them again,
  survivors are re-estimated, missing slots materialize; **pause** releases
  every live un-pinned instance and **resume** re-materializes; **end** is
  terminal (`ended_at`). Pinned instances survive all of it — they are the
  user's rows, skipped explicitly or not at all. `next_due` is a cache of the
  earliest live instance (an overdue one is still due), else the next rrule
  occurrence, refreshed by materialization and by every instance write path
  (edit, void, match, unmatch).
- **Transfer schedules (decision 56).** A schedule is flat (`category_path`)
  or a transfer (`counterparty_account_id`, the other real account), never
  both. Transfer instances are two-leg `expected` transfers — the schedule
  account's leg is `amount` (negative = leaves it), the counterparty leg its
  negation — so they appear in both accounts' forecasts and match the merged
  transfer the bank rows auto-link into (§5); the `whats_new` week of upcoming
  bills reads such an instance from the schedule account's leg, the paying
  side, so a card or loan payment is a bill there and a transfer into the
  schedule's account is not (#357). The schedule is itself the
  signal that links those bank rows, opaque descriptors and legs that post on
  different days included, and the merge settles the instance once (decision
  65); a schedule's matched history reads one amount per settling row, from
  the schedule account's side, so a two-leg merge never feeds an estimate an
  opposite pair. `estimate_basis =
  counterparty_balance` sizes the instance from the counterparty's reported
  balance ("pay the card in full"): a card owing money has a negative balance,
  which *is* the checking leg; nothing owed means no instance until it goes
  negative again, and the estimate refresh keeps live instances on the
  balance as it moves. That is the card's *current* balance, not its
  statement: the statement balance from the last close is **recorded on the
  instance** (decision 80) — `record_statement_balance` (REST `POST
  /schedules/statement-balance`, MCP `record_statement_balance`, commit
  scope, credit cards only) addresses the payment by id or by the card plus
  the statement's due date (the live instance of a transfer schedule paying
  that card nearest it, within `STATEMENT_DUE_WINDOW_DAYS` = 14; an equally
  near pair is refused), moves both legs through the ordinary transfer edit
  (the card leg takes the balance, decision 78), pins the instance and keeps
  the figure, its closing date and its source in `instance_statements`,
  audited `schedule.statement_balance`. `reset_instance` drops it, and
  `matching.release_settlement` copies it onto the clone whenever it keeps the
  pin. A loan payment, or any other correction, is an ordinary override.
  Either way the exact amount is also what lets the instance vouch for its
  bank pair, since decision 65 needs it. Changing a schedule's shape releases every un-pinned
  instance (their legs are wrong) and re-materializes.
- **Suggestions (decision 57).** Detection — recurring payee/amount/cadence
  patterns in history — and `create_schedule` with `propose=true` do not create
  schedules; they land **suggestions**: one row per fingerprint (the account
  plus who it is with — `acct:<id>` for a transfer, `payee:<blind index>`
  otherwise) carrying a typed `ScheduleDraft`, evidence, a deterministic
  confidence, `seen_count`, snooze, and the decision. Re-detection upserts
  the open row; a rejected row is durable suppression (`reject_scope =
  counterparty` covers the whole (account, counterparty) pair); a pattern an
  existing schedule already covers is never suggested, and a `propose=true`
  `create_schedule` for a payee or counterparty the account already has a
  schedule for (active, paused or ended) is refused with a 422 naming that
  schedule (#250). An agent's proposal carries no confidence — NULL, since
  nothing scored it, so it sorts after scored rows and its card claims no
  certainty — and may carry a `note` of up to 200 characters, its evidence
  for the person deciding (a statement line, say), kept in the encrypted
  evidence and surfaced as `evidence_summary.note`. `decide` is commit
  scope: `accept` / `edit_then_accept` apply the draft through
  `create_schedule` unchanged (edits validated by the same schema) and record
  the schedule as `result_entity_id`; `reject` is remembered; `snooze` is a
  date and never auto-accepts. Proposing is the caller's choice per call
  (#436): an agent acting on its own initiative proposes, the user (or the
  agent on the user's request) decides. A suggestion is decided only through
  `decide` (REST `POST /suggestions/{id}/decide`, MCP `decide_suggestion`):
  the one-release wrappers that also took a suggestion id — MCP
  `confirm_schedule`, REST `POST /schedules/{id}/activate|reject` — were
  retired in 0.18 (#347), and a paused schedule resumes through `POST
  /schedules/{id}/resume` or MCP `end_schedule(action="resume")`. Events
  `suggestion.created` (dedup_key = fingerprint) and
  `suggestion.decided`; `whats_new` carries the open count, `list_recurring`
  and forecast coverage the open schedule count.
- **Suggestion kinds (decision 67).** `kind` selects the typed draft and the
  apply: `decide` dispatches on it. `schedule` carries a `ScheduleDraft` and
  accepts into `create_schedule` as above. `transfer_link` (source
  `confirm`, #271) is proposed by a manual transfer confirm (§5): its
  `TransferLinkDraft` is the rule — `from_account`/`to_account` (paths),
  `payee`/`payee_id` (the scope), and `amount_exact`/`window_days`/
  `effective_from`, unset unless edited (the only editable fields: the pair
  and payee are the suggestion's identity). Its fingerprint is the UNORDERED
  account pair plus the alias-resolved scope payee, so repeat confirms in
  either direction refresh one open row (`seen_count`, the confirmed pairs as
  evidence) and a later alias is caught by comparing alias groups, not
  fingerprints. Accept creates the rule through `rules.create` as a **draft**
  and reports `project_links` for it; it activates only through
  `rules.set_state` with that count confirmed (decision 46) — never from the
  inbox. Reject is remembered (`exact` = this pair and payee group,
  `counterparty` = this pair, any payee); a `transfer_link` rule already on
  the pair for that payee group (draft, active or disabled — an archived rule
  is a deleted one) suppresses a proposal; snooze is a date as for
  schedules.
- **Match cards (decision 86).** `match` (source `detected`, detector
  `matcher`) mirrors an open match proposal; the `matches` row stays the
  source of truth, because a proposal *holds* its rows (the one-live-match
  indexes, the candidate pool, decision 65's holds, the carries). Its
  `MatchDraft` is the pair as the card shows it — `expected_id`/`posted_id`,
  the `account` both were scored on, a `label`, each row's date and amount
  there, the `variance` — and the confidence is the match score; nothing is
  editable (`edit_then_accept` is refused: another bank row is `match`). The
  fingerprint is the slot for a schedule instance (`match:slot:<schedule>:
  <occurrence>:<bank row>`, which an unmatch clone keeps) and the expected
  otherwise (`match:tx:<expected>:<bank row>`). The matcher writes the card at
  the proposal insert, and a **sweep** at the end of every matching pass
  (`sync_match_suggestions`) creates the missing ones — the backfill for
  proposals made before cards existed — and **withdraws** an open card whose
  proposal is gone or no longer actionable (its expected skipped, its bank
  row voided). A bank row held for an expected transfer (decision 65) gets no
  card while its other leg can still arrive (`TRANSFER_WINDOW_DAYS` from its
  date): the merge usually settles it. Every path that moves a proposal off
  `proposed` calls one hook, `suggestions.on_proposal_closed` — the sibling
  rescind and the confirm in `_execute_match`, `reject_match`, `_rescind`
  (the feed's and a manual void's, decision 69), `_carry_settlements`, and
  unlink's `_release_settlements_of`: a token's confirm makes the card
  `accepted` (`result_entity_type = match`), a token's reject `rejected`
  (exact — written even when the proposal had no card), and any system
  action `withdrawn` (audited `suggestion.withdraw`). `decide` on a card
  confirms the proposal (`confirm_match`: the bank row is reviewed,
  `via='match'`) or rejects it (`reject_match`, `reject_scope` must be
  `exact`), passing the card so the hook skips it; snooze only hides the card
  — the proposal stays live and keeps settling its instance in the forecast
  (decision 76 reads `matches`, never the inbox). Deciding a card whose
  proposal is already gone withdraws it and answers 422. A **rejection is
  memory**: the matcher's candidate selection drops a rejected bank row for
  that slot, and the row that superseded it (§5). A re-proposal of a
  withdrawn or accepted pair re-opens its card. The bespoke paths stay —
  REST `/matching/proposed`, `/{id}/confirm|reject` and `match` are the
  ledger operations the card calls — and `match.proposed` is still emitted
  beside `suggestion.created`.
- **Detection v2 (`core/recurrence.py`, #234).** History (live pending/posted
  rows that are not schedule instances, 400 days) is grouped per **(account,
  counterparty)** over the account's perspective leg: one-sided rows by
  alias-resolved payee, two-leg transfers by the other real account —
  proposed once, on the payer's side. Same-day rows are summed (split legs).
  Cadence buckets: weekly, every two weeks, **semi-monthly** (the days of
  month cluster into two groups while the weekday drifts →
  `BYMONTHDAY=d1,d2`), monthly, every two / three / six months, yearly (two
  occurrences suffice). A gap that is a whole multiple of the period is a
  *miss*, not a disproof: a run is regular when ≥ 80% of gaps sit in the
  bucket and misses are rare; a pattern whose last occurrence is older than
  1.5 periods is lapsed and never proposed. **Amount is evidence, not a
  gate**: a coefficient of variation ≤ 5% is a `fixed` amount at the median,
  anything else `estimated` (`counterparty_balance` when the counterparty is
  a card, else `rolling_avg_3`). Confidence is deterministic — 0.4 ×
  evidence (occurrences / 8, capped) + 0.35 × in-bucket ratio + 0.25 × (1 −
  cv) — and ≥ 0.4 proposes. `dtstart` is the last occurrence; the draft
  carries the rows' most common category. Triggers: nightly
  `schedules.detect_daily` (07:05 UTC, after matching/auto-link and
  materialize, before the shortfall scan), `POST /schedules/detect` / MCP
  `detect_schedules` (write), and the **new-liability hook** — creating or
  mapping a `credit_card`/`loan` scans merged transfers to it and one-sided
  outflows whose payee names it (`core.matching.mentions_counterparty`,
  loose form) and lands `source = account_added` suggestions. Money stays
  `Decimal` throughout (the spread is a Decimal population stdev).
- Bill sources by design: manual · detected · **agent-written via MCP** (the
  reference OpenClaw skill parses statement emails and calls
  `create_schedule` / expected-transaction upserts — server never parses
  email) · future provider plugin seam (FDX / Plaid Liabilities if they become
  consumer-reachable).
- `forecast_balance(account, horizon)` = current balance + live expected
  instances on the account + **projected** rule occurrences for slots past the
  materialized horizon (#235: every active schedule touching the account,
  either side of a transfer, expanded on the fly for slots that hold no
  instance of any status — a skipped slot stays silent, the counterparty side
  sees the negated leg) → daily series with annotated events, honoured up to
  365 days — `schedules.validate_horizon` (1..`FORECAST_MAX_DAYS`) is the one
  horizon check, run by the tool, the `purser://forecast` resource and
  `forecast_account`, with REST bounded by the same constant (decision 77).
  Events carry `projected` (no row yet) and `overdue` (dated before
  today, still due, lands on day 0) and `basis`, where the amount comes from
  (`schedules.instance_basis`, decision 80): `statement` (a recorded card
  statement still sets it), `override` (a pinned instance), else the rule's —
  `fixed` (a non-schedule expected row too) or the schedule's estimate basis
  (`last`, `rolling_avg_3`, `rolling_avg_12`, `counterparty_balance`). REST and
  the `purser://forecast` resource carry it on every event; the MCP payload
  only for `statement` and `override`. **Estimate range band** (decision
  85): an event whose amount the rule estimates — an un-pinned instance or a
  projected occurrence of an `estimated` schedule — carries `range` (low,
  high) = `core.forecasting.estimate_range`, the min and max of the newest
  `ESTIMATE_RANGE_WINDOW` = 6 settled amounts (`matched_actuals`, posted
  heads only) together with its own amount, from at least 2 actuals and only
  when they differ; a card payment's band is therefore its past payments
  together with the card's current balance. Fixed amounts, pinned
  instances, recorded statements and non-schedule rows carry none, and the
  counterparty side of a transfer sees the band negated, ends swapped.
  `schedules.schedule_estimate` (amount, basis, actuals) is the one
  estimate, cached per schedule across a forecast; `instance_amount`
  delegates to it. When any event in the horizon is ranged, a pessimistic
  pass (every ranged event at its low) and an optimistic pass (at its high)
  give each day `low`/`high` and the forecast `pessimistic_low` /
  `optimistic_low`; MCP events carry `range` only when ranged, `low.range`
  is `{low, high, low_date}`, and when the pessimistic low crosses the floor
  the point low does not, the headline names the driver ("could dip to … if
  Citi payment comes in high" — the ranged event on or before `low_date`
  with the largest adverse spread; an inflow "comes in low"). Shortfall
  alerts stay on the point series. The MCP payload lists the events that
  fit its character budget — the one that makes the low always, then the
  largest, optionally only those at or above `materiality` — and accounts
  for the rest in `omitted {count, net}` beside `end_balance` (decision 77);
  the payload carries `coverage`
  (`schedules_active`, `instances`, `projected`, `suggestions_open`,
  `matches_pending`) and the headline names a flat line for what it is when
  nothing is modeled. **An instance an open proposal settles pending
  confirmation is left out** (decision 76) — overdue or due inside the ±5-day
  match window alike: its proposed bank row is live (pending or posted, not
  superseded) and posts to the account, so it is already in the opening
  balance, and the line is exactly what confirming the match would give
  (the posted amount wins; a variance is never carried as a remainder). A
  valuation account keeps such instances, its opening figure being a stated
  value. They are counted in `coverage.matches_pending`, with their net in
  `matches_pending_net` (present only when nonzero), and named in the
  headline; a rejected proposal brings the instance back on the next read.
  **Deterministic only** in v1. Crossing `accounts.min_balance` emits
  `forecast.shortfall`.

## 7. Feed layer

Two feed paths land in the **same** raw-first pipeline (every payload archived
verbatim in `raw_fetches`; the importer derives transactions by content-hash;
ledger, matching, forecasting, net worth, and MCP are unchanged): Connect
(SimpleFIN, background sync) and manual QIF import.

Manual QIF/CSV uploads are raw-first as of migration 0023 (issue #127,
decision 53): `import_qif` archives the verbatim file to `raw_fetches`
(`source='qif'|'csv'`, no connection) before any ledger write, skipping the
archive when an identical prior upload exists (content-hash idempotent on
the envelope, mirroring the ledger's dedup). The ledger rows a manual import
creates keep `source_fetch_id` **NULL** — doubly load-bearing: PR-7's cutoff
queries and the #175 review scope both read a non-NULL `source_fetch_id` as
"feed row".

**Retired-origin rows (issue #284, decision 72).** The rows the removed
Direct-to-Bank ingest wrote carry `source_fetch_id` NULL too — migration 0010
purged their captures (decision 25) — so decision 45's feed repair, which
selects by fetch, cannot reach them, and nothing else may remove a posted
row. `purser admin purge-retired-rows <account>` removes them by **creation
batch**: the account's `origin='import'` rows with no source fetch whose
`created_at` equals a named batch timestamp (every row one import commits
shares its transaction's `now()`, and no other import does). Manual imports
match the same predicate, so the command first lists the account's batches
with the change-audit actions recorded at the same instant (`qif.import`,
`connection.ingest_direct` — best-effort, the trail is pruned after
`audit_retention_days`), and the operator names the batch. Removal follows
the ledger's own rules: `expected`/`pending` rows take the normal void path
(`transition_status`, system actor) and stay as lineage; `posted` rows go
through the guarded delete the feed purge uses; `void` rows are left. A live
match or a live transfer holding a batch row refuses unless `--unlink`, which
rejects proposals, unmatches confirmed matches (the bill a row settled is
expected again) and unlinks transfers (the counterpart's bank line survives on
its own account; the batch row's resurrected clone, found by the content hash
that moves to it, goes with the batch). A corrected row, a row posting to
another real account and a corrected transfer always refuse. Dry run by
default; the apply run recomputes the plan, refuses when it no longer equals
the preview, and rolls back when what ran differs from it. Audited
`account.purge_retired_rows`, evented `account.retired_rows_purged`;
`raw_fetches` is never touched.

### 7.1 Connect (SimpleFIN)

- Protocol client, no SDK: one-time claim-token exchange → encrypted access
  URL; scheduled fetch; respect Bridge limits (≤24 req/day, ≤90-day ranges).
- The fetch schedule is admin-configurable (`sync_schedule` in `app_settings`,
  Config UI): daily at HH:MM UTC (default 06:30), every 12h (06:30/18:30),
  every 6h, or hourly (:30) — every preset lands a run just before the 06:45
  matching job, and hourly (24/day) sits exactly at the Bridge budget; the
  per-connection fetch guard fails soft beyond it. Job id stays
  `feed.sync_daily` (contract test) regardless of cadence. Changes reschedule
  the running scheduler live in the single-process topology; in the split
  serve/worker topology they apply at worker restart (the API reports which
  via `sync_reschedule_live`).
- Every payload lands in `raw_fetches` first, verbatim, append-only.
- Importer derives transactions with **content-hash dedup**
  (external account + date + amount + normalized description, plus the
  occurrence ordinal below). SimpleFIN transaction IDs are not stable —
  never trust them for identity.
- **Occurrence ordinal** (issue #275, decision 68): identical rows are
  legitimate — two same-day coffees at one price — so within one payload the
  n-th row whose identity equals an earlier row's is **occurrence n**, hashed
  with `#n` appended to the canonical string (`core.occurrence_part`).
  Occurrence 1 appends nothing, so every hash stored before the ordinal
  existed is unchanged. Rows are numbered per account in payload order,
  pending or not — the order in which the importer has always claimed hashes
  — and the count restarts in every fetch. Identical rows share a date, so a
  fetch carries all of them or, cut mid-day by a window edge, a prefix whose
  ordinals already exist: the 7-day overlap re-fetch lands on stored hashes,
  and per identity the ledger holds the largest count any one fetch carried.
  The import loop and the vanished-pending sweep (`_current_pending_hashes`)
  read one per-payload hash list, so a pending's stored identity is the one
  the sweep looks for.
- Idempotent by construction: re-running any import creates zero duplicates.
- **Holdings source** (issue #390, decision 88). A brokerage account whose
  `holdings_source` is `manual` takes no feed snapshot: its holdings come from
  its positions (§8), one source per account. Its feed holdings rows are
  counted as `holdings_skipped_manual` (sync result and audit row) and stay
  in the archived fetch; its cash transactions import as usual. One source
  per date too, judged by the set in effect on the fetch's date
  (`networth.holdings_set_on`, the readers' newest set at or before it):
  when that set is hand-tracked, a fetch taken before the account went back
  to the feed (`fetched_at` at or before `holdings_source_changed_at`; a
  re-import, as the map path does) is inside the manual period, adds nothing
  and counts as skipped the same way, while the first fetch taken after it
  ends the manual period once (§8, `manual_period_open`): the manual rows
  from its date on go, a manual tombstone ends each hand-tracked position it
  does not report, and its set imports (`manual_period_rows_deleted`,
  `manual_period_tombstones` in the sync result; audited
  `account.manual_period_end`). A later fetch that day reporting a
  tombstoned symbol replaces the tombstone. No (account, date, symbol) ever
  has two rows. `import_fetch` locks the connection's mapped accounts (FOR NO
  KEY UPDATE, id order) before its first watermark mark, so it reads the
  source under the lock a switch takes (#413).
- **Pending re-check** (issue #282, decision 70). A sync's window starts at
  the resync overlap (`last_fetch_at − 7d`) **or earlier**: at the oldest open
  import pending on the connection's **postings-balance** accounts (checking,
  savings, cash and credit card: `drift.ELIGIBLE_TYPES`, the §7.3 set;
  decision 74), less `PENDING_MATCH_WINDOW_DAYS` (7), never further back than
  `BRIDGE_MAX_RANGE_DAYS` — so every such open pending in reach is re-checked
  against the bank on every sync, and a posted successor dated before the
  overlap is fetched with it. A pending older than one fetch can serve widens
  nothing and stays as it is: every sync would otherwise pay for a maximal
  fetch that could never settle it. A brokerage, loan or property account's
  pendings never widen the window: holdings or valuations value those
  accounts, so their pendings move no balance, and a brokerage feed can list
  every fund purchase as pending forever, which kept every sync at the 90-day
  maximum. They are re-checked whenever the window covers them. The
  `connection.sync` audit row records the `window_start` fetched and the
  `overlap_start` it would otherwise have had. A pending
  the payload no longer lists is paired with its posted successor before it
  is voided: same account, exact amount, dated within 7 days either way,
  posted import, not already claimed by another **feed** pending — a matched
  expected's `superseded_by` link to that row claims the bill, not the bank
  line, so it never hides the successor. A posted row whose identity — its
  per-payload hash, occurrence ordinal included (decision 68) — an open
  pending on the same account holds is the bank posting that pending
  unchanged: the hash **moves** to the new posted row (the unlink precedent,
  decision 29) and the pending is superseded by it, on every import path
  (sync, backfill, map re-import, replay), with `identity_moved` on the void's
  audit row — it used to be skipped as a duplicate, after which the pending
  was voided as vanished and the transaction lost. These voids count in
  `voided_pendings`, the new posted row in `imported`. A pending voided with
  no successor emits `pending.vanished`, which `whats_new` reports as
  `pendings_vanished` (count, plus lines above the materiality bar, largest
  first, capped at 3). A pending already voided as vanished is never revived
  by a later sighting: void is terminal, and the digest is where a dropped
  row that really posted gets noticed.
- **Masked-reference dedup** (issue #171, decision 44): a provider that
  re-delivers a row with its reference digits masked between fetches
  (`ER13529574` → `ERXXXX9574`) changes the content hash. On a hash miss,
  the importer skips the row when a feed-origin row on the same account
  with the same date and real-leg amount (ANY status — voided merge legs
  still squat identity) has a description equal position-for-position
  except mask-character-over-digit (`core.masked_variant`). Deterministic
  identity under masking — not fuzzy similarity (decision 40's boundary
  stands); counted as `skipped_masked_duplicate` in sync and backfill
  results.
- **Re-description guard** (issue #331, decision 74). The description is part
  of a row's identity, so a posted row the bank re-describes after the ledger
  holds it (`…PORTLANDOR` → `…PORTLAND CREDIT`) arrives as a new identity. The
  resync overlap always had that exposure for its 7 days; a widened window
  also re-fetches the **tail** behind it, the rows dated before the overlap
  start the sync would otherwise have used. On a hash miss, a posted tail row
  is skipped as **re-described** when the account already holds, from an
  earlier fetch, a posted feed row (`source_fetch_id` set, hash held) with the
  same date and exact ledger amount, a different `core.normalize_payee`
  description, and no listing in this payload — neither under its own
  identity nor as a masked re-delivery (decision 44). The bank lists the new
  row in its place. Each such original stands in for one skipped row per
  import. A late-posted row with no such neighbour, or beside one the bank
  still lists, imports as before; so does a row that an open import pending
  the payload no longer lists may take as its posted successor (same amount,
  within `PENDING_MATCH_WINDOW_DAYS`), because #282's pairing comes first.
  Pending rows and rows inside the overlap are never checked. The sync passes
  its overlap start to every import it makes (`tail_before`, gap windows
  included). Any other import of a fetch (backfill, map re-import, replay)
  takes the overlap behind the fetch itself, `fetched_at` −
  `RESYNC_OVERLAP_DAYS`, which is never earlier than the boundary its sync
  used, so re-importing an archived fetch skips at least what its sync
  skipped. Counted as `skipped_redescribed` in sync and backfill results and
  the replay summary. The `connection.sync` audit row lists each skip under
  `redescribed`: account, date, amount and the `original_id` it re-describes.
  Residual: a bank line the ledger holds only inside a merged transfer has
  void leg rows, which are no neighbour, so its re-described copy imports as
  before.
- **Rules shape the row before it is created** (0.14.0, decision 46). The
  `_import_account` order is therefore: `feed_start` skip → zero-amount skip →
  dedup lookup on the row's (occurrence-qualified) content hash →
  `feed_invert` → masked-duplicate guard → re-description guard (posted tail
  rows) →
  **rename rules** (rewrite the payee) → **categorize rules** → payee memory
  (`category_for_new_row`, now consulted on the *renamed* payee) →
  `create_flat_transaction` → provenance audit row. Two properties follow from
  that placement and both are load-bearing. Rules run strictly **after**
  hashing, so decision 43's hash-on-raw guarantee is untouched — activating,
  editing or deleting a rule can never re-import history or disturb
  `_current_pending_hashes`. And rename precedes payee memory, so several raw
  descriptors renamed onto one friendly payee make that payee's memory
  *stronger*: `payee_memory` counts posted rows per `payee_id`. The row's
  `origin` stays `import` — it is a bank row a rule shaped — and the shaping
  is recorded beside the existing provenance payload.
- **Historical backfill** (`POST /connections/{id}/backfill`, ProposeToken —
  same scope as sync; issue #126): the incremental sync only reaches ~90 days
  back, so backfill pages **older** history in ≤90-day windows walking
  *backward* from the first sync's start. Each window ends one day into
  already-covered territory (1-day overlap; the idempotent import makes the
  re-fetch free), is archived verbatim as a `raw_fetch`, then imported with
  **`window_start=None`** — which makes the importer skip vanished-pending
  reconciliation. This is load-bearing: a backfill payload is an old,
  pending-free window (`include_pending=False`), and reconciling live pendings
  against it would wrongly void every one. Backfill **never touches
  `last_fetch_at`** (that anchors the forward window) or `connection.status`,
  and emits no sync events. It **shares the daily fetch budget** with sync via
  the `raw_fetches` count and reserves 2 fetches (scheduled sync + one manual).
  Progress persists in `backfill_cursor` (resumable across runs); two
  consecutive empty windows latch `backfill_complete` (history exhausted). A
  mid-run bridge error stops with a partial result, keeping the cursor. The
  result surfaces a **`qif_overlap`** warning — manual-QIF ledger rows
  (`origin='import'`, `source_fetch_id` NULL) inside the backfilled range that
  may duplicate — but never skips on it (content-hash still dedups identical
  rows; only near-duplicates slip through).
- **Per-account feed sync-from — `accounts.feed_start`** (issue #154, decision
  40): a nullable date; the importer skips feed rows dated **strictly before**
  it, so one boundary partitions an account's history `manual < boundary ≤
  feed` — the persistent mirror of §7.2's per-request cutoffs (decision 35's
  "strictly older" manual rule is its exact complement; a row dated ON the
  boundary imports from the feed). Enforced in `_import_account`, the single
  chokepoint all three feed paths share (sync, backfill, map re-import), and
  **before** `auto_link_pass` runs — a skipped row never exists for the
  transfer pass to pair, which is what makes a latch-reset re-page harmless
  (the 2026-07-14 incident: re-paged windows re-imported 121 rows whose
  earlier copies had been purged, and auto-link consumed some into
  `origin='system'` merges). Skips are counted (`skipped_before_feed_start`
  in sync and backfill results + audit). NULL = unscoped (prior behavior).
  Import-time only: setting it never deletes rows already in the ledger.
  Admin-set via `PATCH /accounts/{id}` (feed configuration, like connect/map;
  rename/hide stay write-scope). One narrow edge: a live import pending dated
  before a newly set `feed_start` whose posted twin is then skipped voids as
  `pending_vanished` instead of superseding — reconciliation is
  payload-derived, so the guard can never void live pendings; the documented
  rollout (set at the manual/feed boundary when mapping) never hits this.
- **Per-account feed sign inversion — `accounts.feed_invert`** (issue #170,
  decision 43): some feeds deliver an account's transaction stream
  sign-inverted (observed: Fidelity brokerage/HSA/401(k) accounts reporting
  money received as negative — which breaks balances AND makes transfer
  legs same-signed, so §5's pair detection can never see them). With the
  flag set, `_import_account` negates the amount **strictly after**
  content-hashing and dedup: a row's identity stays the bytes the bank
  sent, so flipping the flag never re-imports history, and pending-hash
  reconciliation (raw-amount-based) keeps matching stored hashes. The
  income/expense classification sees the corrected sign. Admin-set via
  `PATCH /accounts/{id}` like `feed_start`; the QIF importer's per-request
  `invert` (decision 35) is this flag's manual-import mirror.
- **Per-account drift-basis pin — `accounts.drift_basis_override`** (issue
  #307, decision 83): pins the basis §7.3's drift check reports on
  (`posted_only` | `pending_inclusive`; NULL = learned from sync history).
  Admin-set via `PATCH /accounts/{id}` with `feed_start`'s explicit-null
  rule (null clears, absent leaves it unchanged); refused (422) on types the
  drift check never runs on. Semantics in §7.3.
- **Account-focused backfill** (issue #154): `BackfillRequest.accounts` sends
  the protocol's repeatable `account` params so onboarding one account needn't
  re-fetch the rest. Advisory — a bridge may ignore the filter and return
  everyone; `feed_start` still guards the import, so the mechanisms compose.
  A filtered run's empty streak stops (`empty_history`) **without** latching
  `backfill_complete`: filtered emptiness proves only those accounts
  exhausted. The cursor still advances (it records "asked", not "returned").
- **Closing an account unmaps it** (§3.4): its `connection_id`/`external_id`
  clear, and the feed account reappears in `discover_accounts` as unmapped —
  honest, since a closed account takes no more postings. Reopen, then remap
  through the normal map path (`map_account` refuses a closed target) to
  resume syncing.

### 7.2 Manual bank download (QIF + CSV import v2)

Mapping-first and cutoff-aware (issues #125, #138). The user downloads a QIF or
CSV file from their bank and imports it via `/import/qif` or `/import/csv`
(preview + commit). Same content-hash identity and idempotency as Connect, so
manual imports and SimpleFIN syncs interleave on one account with zero
duplicates. **QIF and CSV share one planner** — CSV is only a second parser
producing the same parsed file (`purser/core/csv_import.py` → the `core.qif`
dataclasses), so every rule below (mapping, cutoff, invert, transfer
degradation, the preview==commit contract) applies identically to both. The two
REST surfaces differ only in the parse step and CSV's extras (`column_map`,
per-account skip); the audit action records `qif.import` or `csv.import`
accordingly, and CSV keeps the `qif:{file_account}` content-hash namespace so
the same history re-imported in either format dedups.

- **Mapping.** Every QIF file account maps to a Purser account (`account_map`);
  the preview drives the account selector (or inline account creation). A
  single-account file uses the parser's `__default__` name. An investment
  account may be left out (next bullet).
- **Split rows (issue #274, decision 87).** A QIF row's split lines (`S`
  category, `E` memo, `$` amount; `S` opens a line, `E`/`$` attach to the
  open one, a repeated `E`/`$` opens a blank one) categorize it, and its `L` —
  `--Split--`, or in some exporters the first line's category — is ignored.
  The planner decomposes the row (model B):
  - each **transfer line** `S[X] $a` becomes an ordinary transfer leg from the
    row's account to X, carved out of the row. It pairs with X's mirror row
    through the symmetric transfer hash, so two-leg creation, cutoff
    degradation, skipped counterparts, dedup and occurrence numbering are the
    existing transfer rules; its one-leg identity is keyed apart from the
    row's. This is what stops a split transfer line from moving the money
    twice (the mirror used to book its own transfer while the row stayed
    whole);
  - the **category lines** — a blank `S` (or a line to the row's own
    account) on the uncategorized placeholder, never payee memory, and
    whatever the lines leave unexplained on the placeholder too (counted
    `split_remainders`) — form the row's category part of `T − Σ transfer
    lines`: two or more lines are a split transaction (§3.3), one line an
    ordinary flat row. Its identity is the row's flat hash over `T`, so a file
    imported before split lines were read (one `--Split--` row) dedups; such
    old rows stay until purged by batch (decision 72) and re-imported;
  - the row lands in **one bucket**, decided by its category part; the carved
    lines are a sub-count, `split_transfers`, so `in_file` still equals the
    file's row count. When the transfer lines account for all of `T` there is
    no category part and the first transfer line carries the row's bucket
    (category lines that net to zero then book nothing).
- **Investment sections (issue #274, decision 84).** A QIF `!Type:Invst`
  section (case-insensitive) is investment actions — Buy, Sell, Div, ShrsIn,
  XIn/XOut — never bank rows. The parser keeps every dated record in it as a
  flagged row (`QifTransaction.investment`; amount read leniently, 0 when `T`
  and `U` are absent or garbage; no category or transfer; it never raises),
  maps the Invst/Port/401(k)/403(b)/Mutual account types to `brokerage`, and
  lists the file's investment accounts (`QifFile.investment_accounts`: owners
  of an Invst section plus accounts an `!Account` block declares with one of
  those types). The planner filters flagged rows first — before the unmapped
  skip, the reminders and the cleared-mark rule — into `skipped_investment`,
  and the preview leaves them out of the sign block, the category list and the
  date range. An investment account the request does **not** map is left out
  like a mapped-to-null account (`_unmapped_investment`): commit no longer
  requires a mapping for it, and a bank leg into it degrades to a one-leg flat
  on the bank side (`one_leg_transfers`). Mapped, bank legs into it still form
  two-leg transfers under the unchanged transfer hash, so re-imports dedup.
  Preview and commit derive the set from the same request, so preview==commit
  holds. Left out by default because a holdings-tracked brokerage adds its
  holdings' market value on top of its postings (§8), so importing historical
  contributions there would overstate net worth by every one of them.
- **Cutoffs.** Each mapped account may carry a `cutoff` date: a row imports
  only when its date is **strictly older** than the cutoff — the feed owns
  everything from the cutoff forward. The server **never imposes a cutoff that
  isn't in the request** (wizard/agent back-compat). The preview *suggests* one
  per account: `default_cutoff` = the account's earliest **feed-sourced**
  transaction date (`source_fetch_id` set, any status incl. void) minus a
  **3-day margin** — QIF carries transaction dates while SimpleFIN posted dates
  run 1–3 business days later — or null when the feed hasn't touched the account.
- **Invert.** Some banks export a card/loan file with spending as positive. A
  per-account `invert` flips every row's amount at consumption time — before
  hashing, category-root selection (`amount > 0` → income), payee memory,
  transfer direction, and opening-balance amounts. The preview computes a sign
  diagnostic `{positive, negative, net, suspect}` over non-transfer rows (raw,
  so the suggestion is stable) and sets `suspect` when a credit_card/loan target
  looks income-positive (`net > 0` or positives outnumber negatives); the UI
  pre-checks invert then. **Footgun:** `_transfer_hash` is abs()-based so
  inversion is transfer-hash-neutral, but flat/opening hashes are **not** —
  re-importing the same file with invert flipped is a *disjoint* hash set and
  duplicates. The preview's `skipped_dedup` count (0 across a flip) makes it
  visible; document it, don't silently swallow it.
- **Transfers across cutoffs** (both legs carry the `[account]` convention;
  evaluated against BOTH mapped accounts' effective cutoffs, a missing cutoff
  passing):
  - *older than both* → the 2-leg transfer as before, plus a guard that skips
    (counts `skipped_dedup`) when **either leg's flat content hash already
    exists**, so a prior split-degraded import isn't re-materialized as a
    duplicating 2-leg.
  - *inside both* → skipped entirely (`skipped_cutoff`); the feed owns that
    window.
  - *split* (older than one side's cutoff, inside the other) → degrades to a
    **one-leg flat** on the older side only (standard flat content hash,
    payee/memo preserved), counted `one_leg_transfers`; the post-import
    `auto_link_pass` (§5) absorbs/pairs it with the feed counterpart when the
    amount+window agreement is joined by a name signal. **Never** the 2-leg
    form here.
  - opening balances (Quicken self-transfers) respect cutoffs like any row.
  - **zero-amount rows are structurally non-importable** (issue #144): the
    ledger rejects zero postings, so the planner filters them before
    hashing/grouping into `skipped_zero_amount` — a $0 Quicken marker row can
    never 422 a commit or half-form a transfer pair, regardless of
    cutoff/invert options.
- **Identical rows (issue #275, decision 68).** A file's identical rows are
  occurrences 1..n exactly as in §7.1, numbered in file order and in the
  order the planner claims hashes, so occurrence 1 is the row that held the
  unqualified hash before: flat rows and opening balances share one count
  (they share the `qif:` hash space), and the k-th leg a file account carries
  for one transfer identity (counterpart, date, absolute amount) joins
  transfer k, together with the k-th such leg in the counterpart's register.
  Two identical same-day transfers are therefore two, and so is a same-day
  round trip between two accounts (one transfer identity, since the transfer
  hash ignores direction). A leg's flat hash — the identity of its one-leg
  form, and the key the two-leg guard checks — is numbered only among its own
  transfer's legs. Re-importing a file, or a later export overlapping it,
  lands on existing hashes; a file carrying a pair that an older import
  merged lands the dropped row once.
- **Quicken reminders and the cleared flag (issue #249, decision 60).**
  - *Reminders are not history.* A Quicken CSV export can carry reminder
    rows — upcoming scheduled bills/deposits the register shows ahead of
    time — flagged by a mark in its `Scheduled` column (anything but blank or
    an explicit `no`/`n`/`false`/`0`). The planner filters
    them before hashing into `skipped_scheduled` (after the investment
    filter and the unmapped skip, before every other rule), so a reminder
    never lands, never half-forms a transfer, and never enters the dedup
    identity, in preview and commit alike. They are not turned into schedules or suggestions; detection (§6)
    proposes recurring patterns from real history.
  - *The cleared flag is honoured per file account.* QIF reads `C` (`*`/`c`
    cleared, `X`/`R` reconciled, case-insensitive; blank or absent =
    uncleared); CSV reads a `Clr`/`Cleared` column, case-insensitively (the
    same marks, `y`, or `cleared`/`reconciled`/`yes`/`true`; blank, `n`, `u`,
    or `uncleared`/`no`/`false` = uncleared; any other value is a 422, never a
    guess). When at least one row of a file account is marked cleared, its
    unmarked rows are uncleared; an account with **no marks at all** carries
    no clearing information and every row counts as cleared — bank-generated
    exports never mark rows, and Quicken leaves a never-reconciled account
    (cash) unmarked, so neither turns its whole history into forecast items. A
    CSV without a cleared column is all-cleared for the same reason.
  - *Uncleared rows land `expected`*, counted `uncleared` (the subset of the
    created buckets that land expected, not a bucket of its own): the bank
    has not seen the row (an uncashed check), so it stays out of the
    posted/pending balance §7.3's drift check compares against the bank,
    appears in the forecast (an overdue one lands on day 0), ages into
    `bill.late` like any unmatched expected, and the matcher (§5) supersedes
    it with the posted bank row when it clears — exactly how a schedule
    instance settles. Cleared rows land `posted` as before. **Never
    `pending`:** `feed/reconcile.py` voids every `origin='import'` pending
    dated in a sync window that the fetch no longer lists, without asking
    which source the row came from, so an uncleared file row landed pending
    would be voided by the next sync of its account.
  - A two-leg transfer is `expected` only when **no** leg the file carries is
    cleared: once either bank has seen it the money has moved, posting keeps
    that side's balance right, and the other side's bank row is absorbed when
    it arrives (§5). A split-degraded one-leg flat follows its own leg.
    Opening balances always post — bookkeeping, not a bank line, so no bank
    row would ever clear one.
  - Status is not part of the content hash, so re-importing the same file
    still creates nothing (an expected row dedups even after the matcher has
    voided it).
  - *A later file that marks the row cleared promotes it* (issue #310): when a
    row's identity matches an `expected` row a file import created and the
    file now **marks** the row cleared, commit posts that row in place —
    audited `transaction.posted` with reason `cleared_in_file` and the import
    format as source, the `transaction.posted` event, balances dirtied from
    its date — and preview and commit count it `promoted` (a bucket; per leg
    for a transfer). Transfers follow the creation rule: an expected two-leg
    transfer is promoted when any leg the file carries is now marked cleared,
    a split one-leg flat when its own leg is. Only a mark is evidence: an
    account without marks reads as cleared by convention, which says nothing
    about a row having cleared since, so it never promotes (a CSV without a
    cleared column carries no marks). Never promoted: schedule instances,
    feed rows, rows on a closed account, and rows touching a feed-connected
    account — there the matcher settles the expected row against the bank's
    posted row when it arrives, and posting it here as well would count that
    bank line twice. A promoted row is `posted`, so the next re-import dedups
    it as before.
- **Preview is DB-aware but strictly read-only.** It resolves the mapping
  (unknown targets → 422, exactly like import), computes `default_cutoff` + the
  sign block, and returns REAL counts by batch-checking content hashes (chunked
  `IN` queries). Per file account: `{name, type, mapped, transactions,
  default_cutoff, counts, sign}` where `counts = {in_file, would_import,
  skipped_dedup, skipped_cutoff, skipped_unmapped, skipped_zero_amount,
  skipped_scheduled, skipped_investment, transfers, opening_balances,
  one_leg_transfers, promoted, uncleared, split_transfers,
  split_remainders}` — every file row lands in exactly one bucket (`in_file`
  is their sum); `uncleared` counts the created rows that land `expected`, and
  the two `split_*` counts are sub-counts of split rows (decision 87).
  **Contract:** the preview counts for a request equal the commit's
  per-account results for the *same* request — the preview is an exact
  dry-run (both share one planner; only commit writes). The import result adds
  `skipped_cutoff`, `skipped_unmapped`, `skipped_zero_amount`,
  `skipped_scheduled`, `skipped_investment`, `one_leg_transfers`, `promoted`,
  `uncleared`, `split_transfers`, `split_remainders`, and the `per_account`
  breakdown.
- **CSV formats (issue #138).** `/import/csv` accepts two layouts, auto-detected
  by header row; each row's file account, date, amount, payee, and (Quicken)
  category/memo become exactly what the QIF parser would produce:
  - *Amex activity export* — `Date, Description, Amount[, …]`: flat rows, a
    single `__default__` account, no categories or transfers. Charges export
    positive, so `sign.suspect` fires against a credit_card target and the UI
    pre-checks invert; the §5 auto-link is the transfer mechanism here.
  - *Quicken CSV export* — `Date, Payee, Amount[, Category, Memo, Account,
    Clr, Scheduled]`: an optional `Account` column groups rows per file
    account, and `Category`'s `[Account]` bracket notation is a transfer (the
    same parse as QIF), so CSV inherits the full 2-leg / cutoff /
    split-degradation behavior. A `Clr`/`Cleared` column carries the cleared
    flag and a `Scheduled` column marks reminder rows (the reminders and
    cleared-flag rule above).

  When neither header matches, the caller supplies an explicit `column_map`
  (`{date, amount, payee, memo?, category?, account?, cleared?, scheduled?}` →
  header names); until then the preview returns 422
  `{code: "csv_layout_unrecognized", columns: […]}` so the UI offers a
  column-mapping form seeded with the detected headers. Dates
  accept ISO `YYYY-MM-DD` unambiguously and slashed dates via the mdy/dmy
  toggle; amounts are `Decimal` only (strips `$`/commas, reads `(1,234.56)`
  accountant negatives; float-looking garbage is a clear 422). **No
  opening-balance convention**: Quicken's self-transfer opening-balance encoding
  has no CSV equivalent, so CSV never emits opening balances — mind the
  resulting offset vs a known statement balance (post one manually if wanted).
- **Per-account skip (issue #138).** Importing a subset of a multi-account file
  is explicit: an `account_map` entry whose value is null marks that file
  account "don't import". Its rows are removed before planning and counted
  `skipped_unmapped` (in preview counts *and* the import result's per-account
  breakdown, so preview==commit still holds); a transfer whose counterpart is
  skipped degrades to a **one-leg flat** on the imported side via the same split
  machinery. QIF's request map is non-nullable, so skip is a CSV-only surface,
  but the shared planner supports both — and QIF uses it implicitly for an
  investment account the map leaves out (decision 84).
- **Raw-first** (decision 53). Both formats archive the verbatim upload to
  `raw_fetches` (`source` `qif`/`csv`, `connection_id` NULL) before any ledger
  write, and re-uploading an identical file archives nothing new. The ledger
  rows keep `source_fetch_id` NULL: the archive link is the content hash plus
  the import's audit row (`raw_fetch_id`).
- *(History: a Direct-to-Bank path — OpenClaw running signed Bank Skills and
  POSTing envelopes to `/v1/ingest/direct` — shipped in 0.2.x and was removed
  in 0.3.0; see §12 and migration 0008.)*

### 7.3 Post-sync balance-drift check (0.9.0, issue #149, decision 39)

Every successful sync ends by reconciling the feed-reported per-account
`balance` against the ledger (`feed/drift.py`, called from `sync_connection`
only — backfill pages and map re-imports never run it). It is the external
counterpart of §3.5's internal parity probe: it catches missed transactions,
duplicates, seam gaps, and sign errors on every sync instead of at the next
manual reconciliation (#146). **Warning only, never a gate**: no sync failure,
no auto-adjustment, no ledger writes; a bug in the check must not fail a sync
(the whole pass is exception-guarded). Resolution stays a human decision;
reconciling the account to its statement (§3.7) is the audited way to act on
it.

- **Eligibility.** Mapped `(connection_id, external_id)`, open
  (`closed_at IS NULL`), type ∈ {checking, savings, cash, credit_card}.
  Excluded: loan/property (their reported balance already feeds the valuation
  path, §8), brokerage (reported balance is market value, holdings-driven,
  §8), unmapped accounts, accounts with no parseable `balance`. Credit-card
  signs need no flip: SimpleFIN reports cards negative-when-owed, the same
  convention as our liability postings (a nonstandard bridge reporting cards
  positive would surface as an honest ~2x drift). The type set
  (`drift.ELIGIBLE_TYPES`, the postings-balance accounts) also scopes which
  open pendings widen a sync's window (§7.1, decision 74) and which accounts
  reconcile to a statement (§3.7).
- **Dual-basis compare.** Banks differ on whether the reported balance
  includes pendings, so both live postings sums are computed in one grouped
  query — posted-only and pending-inclusive (`SUM … FILTER`; NOT the stored
  running balances, which are recomputed only at the `before_commit` hook and
  are therefore stale mid-sync, and pending-inclusive only). Clean iff either
  basis matches exactly (Decimal numeric equality). Otherwise the drift is the
  delta on the account's **basis** (next bullet). `available-balance` is
  ignored entirely — for cards it is credit-remaining, a different quantity.
  The canonical delta string is `str(delta.quantize(0.0001))`, used
  identically in the observation block, the event payload, and the dedup key.
- **Basis per account, from sync history** (issue #283, decision 64). Whether
  a bank's balance counts pendings is a property of the bank, so the basis
  belongs to the account, not to one sync. Over the account's last 20
  observations (`BASIS_WINDOW_SYNCS`: this sync plus the account's entries in
  the newest 19 `connection.sync` rows), each basis's delta — reported minus
  that ledger sum, recomputed from the stored `reported`/`posted`/`pending` —
  is compared step by step, and the basis whose delta **changed on fewer
  consecutive-sync steps** wins: the bank's true basis moves only when the gap
  itself does (a missed or in-flight transaction), the other basis also moves
  with every pending. Changes, not distinct values: a hold that comes and goes
  every sync has two values but changes on every step, and a changing delta is
  what opens episodes. Equal counts — a first sync, every step moving both
  bases alike, a quiet account whose bases never differ — keep the `basis` the
  previous observation recorded; with none recorded the per-sync rule decides:
  smaller |delta|, tie → pending-inclusive (the primary balance definition
  everywhere else). The choice is recorded as `basis` on every observation,
  clean ones included; the history is the existing audit memory, so no schema
  change and no warm-up after an upgrade. The basis only picks which delta is
  reported — cleanliness stays either-basis, so a basis guess can never create
  drift on an account whose other basis matches.
- **Per-account pin** (issue #307, decision 83). When an account's history is
  short or misleading, an admin pins the basis: `accounts.drift_basis_override`
  (`posted_only` | `pending_inclusive`, the drift vocabulary, so the pinned
  value equals the reported one; set through `PATCH /accounts/{id}`, admin
  scope, checking/savings/cash/credit-card only, audited in `account.update`).
  A pin only chooses which delta is reported — cleanliness stays either-basis.
  The history rule keeps running under it: each pinned observation also
  records `pinned: true` and `learned` (the basis the history picks), and the
  tie-break reads `learned` from a pinned observation and `basis` from any
  other. Clearing the pin (explicit null) therefore resumes exactly where the
  history would be, inheriting nothing from the pin. Unpinned observations
  carry neither key, so their blocks are unchanged.
- **Emission gate** (an in-flight transaction must not false-positive). For a
  nonzero delta, emit only when (a) the account has no ledger activity at all
  (nothing can be in flight; a freshly mapped empty account with a nonzero
  bank balance alerts immediately), (b) the feed's `balance-date` (unix epoch
  → UTC date) is **strictly** newer than `MAX(transactions.date)` over the
  account's live postings — same-day cannot order intraday events at date
  granularity, so it does not confirm; this also keeps newly-onboarded
  accounts quiet while their delta shrinks sync-over-sync — or (c) the
  identical canonical delta was observed by the previous sync (two consecutive
  syncs). Missing/garbage `balance-date` simply disables (b); the consecutive
  rule decides.
- **Cross-sync memory = the recent `connection.sync` audit rows.** The check
  returns a per-account observation block (`{delta|null, emitted, reported,
  posted, pending, balance_date, basis}`, plus `pinned`/`learned` on pinned
  accounts) that `sync_connection` stores under
  `balance_drift` in that row's `after`; the next sync reads it back (filter
  on plaintext action/entity columns; `before_after` decrypts via the ORM) —
  the newest row for the emission gate and the episode dedup, the newest 19
  for the basis choice. Datetime-granular (multi-sync days work),
  backfill-immune (backfill audits as `connection.backfill`), transactional
  with the sync (retry-safe), and it degrades conservatively if audit
  retention prunes an idle connection (a shorter history only means fewer
  steps for the basis choice).
  Rejected alternatives: an events-table memory cannot gate its own first
  emission; re-deriving from the previous raw fetch cannot distinguish
  backfill fetches (decision 32: request params unrecorded) and is unsound
  once pendings mutate to posted.
- **Episode-scoped dedup.** Exactly one `balance.drift` event per (account,
  delta) *episode*: emission is suppressed only while the previous observation
  carries the same delta with `emitted: true` (the flag is carried forward on
  every sync, including — for candidate/emitted entries — syncs whose payload
  omits the account, so one payload hiccup cannot amnesia an episode). A
  changed delta emits fresh (new dedup key); a drift that resolves and later
  reappears at the same delta is a NEW episode and alerts again — a bare
  existence check on `dedup_key` would suppress that second signal forever.
  `events.dedup_key = "{account_id}:{delta}"` remains the structural grouping
  id. Payload: account path, reported, both ledger bases, delta, `mode` (the
  account's basis), `basis_pinned: true` only when that basis is the pin,
  balance_date, raw_fetch_id, currency (encrypted at rest; nothing in it is
  filtered on).
- **Surfaces.** The event (wildcard webhook subscribers get it free); the
  `whats_new` digest (`balance_drifts`, capped at 3 + truncated flag, each
  line naming its `basis` — the event's `mode`, present only when recorded —
  and `pinned: true` only when the event's `basis_pinned` says so; the
  headline reads "(posted-only basis, pinned)"); the audit trail (observation
  block); and `GET /connections/{id}/discovered`, which carries
  `balance_date`, `ledger_balance` (pending-inclusive), `drift`,
  `drift_basis`, `drift_basis_pinned` and `drift_basis_learned` per account —
  **ungated and recomputed live** from the latest archived payload vs the
  current ledger, on the basis the same sync history (or the pin) selects
  (tri-state: both null = not applicable; ledger set + drift null = clean;
  both set = drifting; `drift_basis` and `drift_basis_learned` are set
  whenever `ledger_balance` is). The Config → SimpleFIN "Feed accounts" table
  renders it with the basis (and a "pinned" badge) next to the drift, and a
  per-row basis select (Auto, showing the learned basis, or a pin) that sends
  the `PATCH`; deliberate asymmetry: the UI
  shows current raw drift immediately (it is the convergence meter while
  onboarding/backfilling an account), the event stream stays quiet until
  drift stabilizes. `SyncOut` is unchanged.
- **Documented residuals.** A just-authorized transaction reflected in the
  bank's balance but not yet in its transaction list can emit one transient
  event when the balance-date is strictly fresh (self-resolves; episode dedup
  prevents repeats). A vanished-pending void can pair one drift event with the
  `pending.vanished` event — both describe the same feed inconsistency. Rapid
  manual double-syncs can confirm via (c) early: this moves *when* the single
  episode event appears, never adds events. Concurrent same-connection syncs
  share the existing check-then-emit race posture (matching/schedules idiom).
  A basis switch changes the reported delta, so it opens one new episode at
  the corrected delta — this is how an account corrects a basis the per-sync
  fallback guessed before its history could tell the bases apart. Setting,
  changing or clearing a pin is such a switch whenever the two bases differ. A sync
  where the other basis matches by coincidence (the pending total exactly
  equal to the gap) is clean and ends the episode; the gap reopens it.

## 8. Investments, property, loans, net worth

- Brokerage: normal cash transactions + a `holdings_snapshots` time series
  from ONE source per account, `accounts.holdings_source` (decision 88):
  `feed` (SimpleFIN snapshots, the default) or `manual` (positions recorded by
  hand and projected into the same table, below). No lot accounting, no
  buy/sell postings, no TWR/IRR in v1.
- **Manually tracked holdings** (issue #390, decisions 88/89). For a
  brokerage account whose bank sends cash but no holdings.
  - *Exclusive source.* Readers take every row at an account's newest
    `as_of` (`holdings_addends`, `latest_holdings`, the balance series), so a
    date carrying rows of both sources would drop symbols. An admin switches
    the source (`PATCH /accounts/{id}`, field-conditional admin scope like
    `feed_start`, audited `account.update`). Feed → manual touches no row:
    the projection starts the day after the account's last FEED snapshot, so
    feed history is never rewritten. Manual → feed needs a feed mapping
    (422 without one) and touches no row either; it records the moment
    (`accounts.holdings_source_changed_at`, database clock) and opens the
    period (`accounts.manual_period_open`). The hand-tracked set stays in
    effect, so the switch day keeps its value, until the first fetch taken
    after that moment ends the manual period in the importer, once
    (`projection.end_manual_period`, audited `account.manual_period_end`):
    the manual rows dated on or after the fetch's date go, a MANUAL zero row
    (a tombstone with its `position_id`) dated that day ends each
    hand-tracked position the fetch does not report, the fetch's set imports,
    and net worth is re-marked from that date. A later fetch that day
    reporting a tombstoned symbol replaces the tombstone. A feed that sends
    cash and no holdings so leaves the account holding nothing from its first
    fetch on; the history before stays, one source per date and at most one
    row per (account, date, symbol). Tombstones never move the feed floor, so
    a later switch to manual re-derives the hand-tracked history with no
    hole. An account whose period is still open and whose feed mapping goes
    (unmapped, remapped, closed) ends it that day, every position tombstoned. Positions and events stay, read-only until the
    account is switched back. `list_holdings` reports the newest set's own
    source, not the account setting.
  - *Positions and events.* A position is one symbol on one account (one
    open per symbol). Its share count is the history of its append-only
    `position_events`: `set_quantity` (absolute, optional total
    `cost_basis`), `buy`, `sell` (deltas), ordered by
    `(occurred_on, created_at, id)`. A correction is a reversal row; neither
    it nor the event it reverses counts. Basis is average cost: a buy adds
    `quantity × price + fees` when priced (else the basis becomes unknown), a
    sell removes `basis × sold / held` rounded to 4 places, so removed and
    remaining basis always sum to the basis before. A `set_quantity` with a
    `cost_basis` sets the basis; without one, a re-count of the count held
    just before keeps the running basis, a changed count makes it unknown,
    and a count of zero has a basis of zero. Every write checks the
    WHOLE timeline after it — a backdated sell can push a later date below
    zero — and is refused (422) naming the first such date; an event dated
    after today is refused, because the projection runs only to today. A
    position closes only while it holds nothing, and a symbol's positions
    on one account never overlap in time: after EVERY event, recorded or
    reversed, the days the position occupies (`core.occupied_span`: the
    days it holds shares, from its effective events, plus the day it sold
    out) must not meet the days any other position in the symbol occupies,
    or the event is refused (409), since a date holding both would count the
    shares twice. The check looks both ways, so a position reopened or
    backdated into another's span is refused once it would hold shares
    there; a closed position's span ends at its sell-out day, not at a later
    re-confirmed zero; the sell-out day itself is refused to a re-buy, since
    that day's set would carry both; a reversed event and its reversal are
    not history, so a position that never held shares occupies nothing. Every write locks the
    account, then reads the position again (`service.lock_position`), so a
    write that waited on the lock sees what the writer before it committed:
    a close refuses it, an earlier `opened_on` stays. Writes are write scope at
    every surface (a position has no pending state; decision 73's reasoning):
    REST `/accounts/{id}/positions`, `/positions/{id}[/events]`,
    `/position-events/{id}/reverse`; MCP `record_position` ("I hold N as of
    D": opens the position if needed, then a `set_quantity`).
  - *The projection* (`purser.holdings.projection`). Manual rows are a pure
    function of the events, the closes, the feed floor and today, so they are
    regenerable: rows are compared on (date, position) and only the ones that
    differ are rewritten. Bounds: from the first effective event (or the day
    after the last feed snapshot, if later) to today (or the close date). A
    row set is written on every event date and every price date of a symbol
    some position holds that day; every set carries a row for EVERY position
    held that day; the day a position sells out gets a zero tombstone (else
    the previous set would stay newest and bring the shares back, in net
    worth and in `readmodel.v1_holdings_latest`); the first set also lists
    known positions at zero, so it replaces whatever came before. A held
    position with no price gets `market_value` NULL: it adds nothing, and the
    read surfaces report it as unpriced (`no_price`), never as 0. The
    projection runs synchronously in the writer's transaction, under the
    account row lock (FOR NO KEY UPDATE, ids in order, taken before the
    balance watermark — reconciliation's lock order, §3.7, so a sync that
    holds the watermark while its posting inserts take KEY SHARE never waits
    in a cycle), and lowers `networth_dirty_from` to the first changed date:
    net worth heals through `consume_watermark` (decision 38) with no manual
    rematerialization. Readers are unchanged (decision 50's overlay).
  - *Prices* (decision 89). `security_prices` holds manual closes
    (`record_price`, an upsert of that date) and provider closes. Resolution
    is NEWEST DATE FIRST; the source order — manual, then a provider, then
    feed-implied — only breaks a tie between closes of one date, so an old
    manual close never pins a symbol. A feed-implied close is market value /
    quantity of a feed snapshot of the same symbol that day, computed on read
    and never stored. The provider is `none` (default) or `tiingo`
    (`price_provider`, env or Config → Prices); its API key is Purser's own
    write-only secret in `app_settings`, never an env value, a log line or a
    response. The provider sees ticker symbols only. The daily job
    `prices.fetch_daily` (06:40 UTC, after the sync, before the 07:30
    materialization; also an admin's `POST /prices/fetch`) asks the provider,
    for every symbol a position on a manual account has held (closed
    positions and closed accounts included), for the dates its positions
    need less the date ranges the provider has answered for good
    (`core.needed_ranges`, `core.fetch_windows`). Each position needs its
    held span — from its first day holding shares to its last: today while
    it holds them, its account's close date for a closed account, else the
    day before the date that sold it down to zero, read from its EFFECTIVE
    events (`core.held_span`) — from a week before its first day (the close
    in effect on a weekend or holiday is dated before it; clamped at the
    first representable date). Two positions' spans stay apart. The answered
    ranges are a `datemultirange` per (symbol, source) in `price_answered`,
    never the span between the oldest and the newest stored close, so an
    island of old closes (a reversed history, an account switched back to
    the feed) covers its own dates only and a gap next to it is still asked
    for. An answer is final for dates at least three days old (a provider
    publishes the latest closes late) and up to the newest close it
    returned (`core.answered_through`); an empty answer or a not-found for
    such dates counts as answered, so a range it has nothing for (before a
    listing, a weekend after a sell-out, an unknown ticker) is asked for
    once while a too-recent "nothing" is asked again. The windows go newest
    first; each is asked, stored and recorded inside its own try, so a
    window the provider refuses, or whose store fails, never starves
    another or the run, and a failed window is asked again on the next run. One
    failing symbol never blocks another — whatever its provider call raises
    fails that window only (`price.fetch_failed`, once per symbol per day,
    with a `reason`) — and one absurd close fails no more than its row: the
    parser rounds a close to six places in a local decimal context, then
    drops one that does not fit a price, counts it and reports it the same
    way, and stores the rest. Every close is range-checked again where it
    enters valuation (`PriceBook`), so a feed-implied one (dust shares, a
    large value) cannot be absurd either, and a market value that does not
    fit an amount at its close is no price for that row (`core.valued`),
    never a failure of the whole account. Every failure surface carries the
    reason: the run's `failed[]`, its `price.fetch` audit row,
    `GET /prices/status` `last_failure` and Config, Prices. The job then
    re-projects every manual account (this also picks up
    feed-implied prices and heals a missed trigger, at no cost when nothing
    changed); then emits `price.stale` once per symbol per stale close. A
    close is stale when it is older than `price_stale_days` (7). Only a
    symbol HELD today (an open position on an open manual account with more
    than zero shares, `prices.open_symbols`) is fetched daily, can turn stale
    and is listed in `GET /prices/status`; one sold down to zero but not yet
    closed is history, like a closed one. Nothing calls a provider from an
    ordinary request path.
- **Feed funds valued at 0** (issue #392, decision 92). A plan's feed can
  report a held fund at market value 0: a plan-only fund (a collective trust,
  say) that no market-data provider prices. The readers value such a FEED
  row — `quantity > 0`, `market_value` 0 or NULL (the importer stores a
  numeric 0 or a missing value as NULL) — as `quantity × close`, the stored
  close (`security_prices`, manual or a provider) in effect on the row's own
  `as_of`, by decision 89's rule (newest date first, manual over a provider
  on the same date), quantized like every market value. The row's symbol is
  matched as `record_price` stores one (stripped, upper case); a feed symbol
  that can never be a ticker (a description) is `unpriceable` — reported so
  in `list_holdings`, `GET /holdings`, `holding.unpriced` and `whats_new`,
  never with advice to record a price. Feed-implied closes do not apply
  (such a row implies none), every close is range-checked where it is valued
  (`PriceBook`), and a value that does not fit an amount keeps its close
  with no value (`value_out_of_range`). The rule lives in two places, changed
  together and pinned by parity tests: Python, `core.feed_value` through
  `holdings.overlay.Valuer` (the one helper behind `as_held` and
  `overlay_totals`), and SQL, `holdings.overlay.FEED_VALUE_SQL` (folded into
  `holdings_addends`' aggregate, so a day costs one statement) and the
  migration-0039 view. It is a
  read-side overlay, like feed-implied prices: the stored row is never
  rewritten, and a non-zero feed value is never overridden. Every reader of
  a feed row's value applies it: `networth.latest_holdings` (and through it
  `list_holdings` and `GET /holdings`, where an overlaid row carries `price`,
  `price_as_of`, `price_source` and turns stale after `price_stale_days`),
  `holdings_addends` (the account surfaces, `net_worth_at`, the
  `net_worth_daily` materialization, attribution), the account balance
  series and `readmodel.v1_holdings_latest` (migration 0039, the same
  overlay in SQL, half-to-even rounding, so Grafana agrees). The other
  readers of `market_value` stay as they are: feed-implied prices read
  non-zero feed rows only, and the importer, the projection and the
  delete-account CLI never value a row. A zero-valued feed row with no
  stored close is unpriced: no value (`market_value` null, `no_price` in
  `list_holdings`), never a silent 0; the daily price job emits
  `holding.unpriced` (through `events.emit_once`, as `price.stale` and
  `price.fetch_failed` are) once per account and symbol when it turns unpriced
  (dedup key: account, symbol and the newest stored close on or before the
  row, `none` without one, so the line re-arms once the position was
  priced; `events.emit_once` serializes overlapping runs on an advisory
  lock, #405), and `whats_new` lists it (`unpriced_holdings`). An agent
  records the fund's unit price from the plan's site with
  `record_price(symbol, date, close)`; weekly is enough, the newest close
  carries forward. `record_price` marks net worth dirty from its date when a
  zero-valued feed row of the symbol is dated then or later (counted in its
  audit row and answer as `feed_accounts_revalued`), so `net_worth_daily`
  heals through the watermark; a provider close the daily job stores does
  the same (one statement for every symbol that got closes,
  `overlay.first_revalued`). An overlaid fund's close turns stale like a
  manual position's (`prices.watched_books`: `price.stale`,
  `/prices/status`, `whats_new`), judged on the stored closes the overlay
  reads. No provider fetch is made for these symbols, and the importer is
  unchanged.
- Property: asset account + manual `valuations` series. Loans: liability
  account, SimpleFIN-synced or manual valuations.
- **Only loan and property accounts take valuations** (decision 81):
  `networth.VALUATION_TYPES` is the one set every reader below filters on and
  the feed's balance valuation uses, and an account's type never changes, so
  `networth.record_valuation` refuses every other real type (422 over REST, a
  tool error over MCP) before the sign rule, naming the two types and where
  that account's balance comes from instead: its transactions, plus its latest
  holdings snapshot for brokerage. Valuation rows already stored on another
  type stay where they are and are never read.
- **Valuations are stored in ledger sign** (decision 71): a property positive,
  a loan negative while money is owed — the sign SimpleFIN reports (§7.3) and
  every reader below assumes. A manual valuation
  (`POST /accounts/{id}/valuations` or MCP `record_valuation`, decision 73,
  both through `networth.record_valuation`, the one write path) takes
  `convention`:
  `ledger` (default) is the value as stored, and a positive value on a
  liability is a 422 naming both corrections rather than an asset;
  `statement` is the figure as printed (a liability positive when owed),
  converted by reconciliation's own `to_ledger_sign` (§3.7). The response,
  the audit row and the `valuation.recorded` event carry the stored figure.
- **Account surfaces honor the valuation model** (decision 42): the accounts
  API/MCP listing, single-account GET, and `/balance` report an OPEN
  loan/property account's latest valuation as its balance
  (`balance_method="valuation"`, `balance_as_of` = valuation date), falling
  back to postings when no valuation exists; `postings_balance` rides
  alongside for flows that operate on postings (close). The overlay is
  presentation-only: `ledger.account_balances()` and the running-balance
  store stay postings-sums (parity probe and `close_account` depend on it),
  and the register's per-row Balance column stays postings history.
- **…and the holdings model** (decision 50; feed or manual snapshots alike,
  decision 88): the same surfaces add an OPEN
  brokerage account's latest holdings market value ≤ as_of *on top of* its
  postings basis (`balance_method="postings+holdings"`, `balance_as_of` =
  snapshot date), so brokerage cash and securities are reported together as
  net worth already did. A valuation REPLACES the basis; a market value ADDS
  to it — one rule, `networth._apply_overlays`, shared by every surface.
  Two bases exist and differ deliberately: `networth.reported_balances`
  overlays the LIVE postings sum (`ledger.account_balances()`, every
  pending/posted row regardless of date) and backs the listings, the register
  headline and the forecast's opening balance; `networth.account_balance_at`
  overlays the STORED running balance at a date (§3.5) and backs
  `/accounts/{id}/balance` and MCP `account_balance`. The forecast keeps the
  live basis on purpose: a future-dated *pending* row is not an `expected`
  row, so the projection's event loop never sees it, and dropping it from the
  opening balance too would erase it from the forecast entirely.
- Net worth at time T = Σ postings ≤ T (real accounts) + latest holdings
  snapshot ≤ T (market value) + latest valuation ≤ T. Materialized into
  `net_worth_daily` (Grafana's flagship source; keeps the MCP tool fast) —
  **watermark-driven** (decision 38, §3.5): every balance-affecting write
  LEAST-updates `networth_dirty_from`; `consume_watermark` re-materializes
  cursor→today (nightly job, post-import/sync/backfill/valuation/close
  kicks), and stored rows at/after a set cursor are never served (the
  series readers fall back to live compute). A backdated import therefore
  self-heals the whole affected range with no manual re-materialization.
  A **closed** account contributes nothing from its close date onward (its
  postings were zeroed at close; valuation/holdings branches are skipped past
  that date), while T *before* the close date stays exactly as it was (§3.4).

## 9. Security model

First-admin bootstrap is serialized by a PostgreSQL transaction advisory lock
(namespace 1347773011, key 1) before checking whether users exist. This prevents
concurrent unauthenticated setup requests from creating multiple administrators;
operators must still complete setup before exposing a fresh instance (#429).

- **Scopes:** `read` · `write` · `admin`. Recommended agent default:
  `read` + `write` (#436, decision 90). `write` is every write — what
  `commit` granted before; `propose` and `commit` are retired, and migration
  0037 converted every token holding either to `write`, one
  `token.scopes_migrate` audit row each. Minting a token with a retired
  scope is refused with a message naming `write`. Proposing is not a scope:
  the agent chooses it per call (`activate` false on `propose_rule` /
  `propose_memory`, `propose=true` on `create_schedule`, or the suggestions
  inbox), and the safety net for direct writes is undo — immutable posted
  rows corrected by reversal pairs (§4) and the attributed audit below.
  `read:redacted` is retired too (#437, decision 91): migration 0038
  converted it to `read`, because its only effect — external account
  identifiers omitted — is now the default for every API token.
- **Minimal disclosure to agents** (#437, decision 91). Everything an agent
  reads lands in its model vendor's context, so the MCP surface returns only
  what a task needs. There is no approval step: the agent decides, the tool
  guidance (MCP `instructions`, `describe()`'s conventions, `GET /llms.txt`,
  the skill) teaches it to choose, and two user settings (app settings
  registry, audited, both **off** by default; Config → Settings) set the
  defaults. They are defaults the agent may override per call, not ceilings.
  - **`privacy_purchase`:** each payee is returned as a stable reference
    (`py_...`, §3.1 `encryption_keys`) in place of its name; descriptions,
    posting memos, schedule and suggestion-draft names and rules' match text
    are omitted. Accounts and categories stay (they carry most of the value,
    and the user controls their names).
  - **`privacy_amount`:** single-transaction values are omitted — row and
    posting amounts, the register `balance` column (it gives each row's
    amount away by subtraction), forecast-event and schedule(-instance)
    amounts, a match's variance, rules' amount bounds. **Aggregates stay
    exact** (`spending_summary` totals, `account_balance`, `net_worth`,
    `balance_history` series and extremes, the forecast's start/end/low
    balances): a total *is* the answer, so calling one is the disclosure
    choice. Amounts are exact or omitted, never rounded or noised.
  - External account identifiers are omitted for every agent unless
    included; REST omits them for every non-session token without `admin`
    (feed configuration needs them).
  - **The purser:// series** apply the same defaults, read directly or
    through `read_series` (which passes its `include` on): the forecast's
    daily series drops event labels and amounts (each day's projected
    balance stays); the per-transaction balance history, whose deltas are
    the amounts, collapses to end-of-day balances when amounts are
    withheld.
  - Every omitted field is **named** — `withheld: [...]` on the response (on
    each item of a bare-list result) — never silently null (#176 Q1). Only
    fields this response actually lost are named.
  - **`include`** (`payee` · `description` · `amount` · `external_ids`) on
    every tool that emits a privatizable field returns those fields whatever
    the defaults. A call that included a field the defaults withhold stamps
    the field names on its call row (`audit_log.disclosed`), which the audit
    view filters on.
  - **By reference.** A payee reference is accepted wherever an argument
    names a payee (`payee`, `payee_name`, `payees`) and resolved server-side;
    a reference inside free text (`description`, `memo`, `note`, `text`,
    `name`, `match_text`) is refused, so it can never become a real name. A
    plain name the user typed is still a valid filter.
  - **`present(transaction_ids, note?)`** returns a stateless
    `PURSER_PUBLIC_URL/selection?id=…&note=…` link (ids and note only, up to
    50 rows, ~2 KB) that the SPA renders in full detail from
    `GET /transactions/selection`, a session-only route — an API token gets
    403, because the point is that the values reach the user without passing
    through the model.
  - **Enforcement.** `audited_tool` takes a required `emits` declaration
    (`privacy.Emits`; `privacy.NOTHING` for none) — the same no-default-open
    structure as `scope`. The wrapper scrubs centrally, before
    `_ensure_text_block` serializes `structuredContent` into text, so no tier
    leaks. (1) Declared key paths are dropped (running balances are dropped
    with amounts but never used to scrub other strings: they are
    aggregates), and the dropped values remembered. (2) Every string is then
    scrubbed by value, case-insensitively with word boundaries: payee names
    become references; narrative — schedule names, open suggestions' draft
    names and labels, live expected/pending rows' descriptions (forecast and
    bill labels), rule text, and the descriptions dropped in (1) — and the
    withheld amounts (dropped in (1), plus bill, suggestion and rule
    amounts) become `[withheld]`. A string that is itself a plain number is
    never value-scrubbed, so an aggregate that happens to equal one item
    stays exact; static guidance (`describe`'s conventions) is never
    scrubbed. Error messages get the full value scrub for every tool, plus
    every amount-shaped number when amounts are withheld; an output-contract
    failure names the path, never the values. The names dictionary is
    rebuilt only when a fingerprint (row count and newest `xmin` of payees,
    schedules, rules, suggestions and live expected/pending rows) moves. A canary test (`tests/test_privacy.py`) seeds
    distinctive values, calls every emitting tool with both settings on and
    no `include`, and asserts none appears in any tier or error; the
    registry's `EMITS` map makes an emitting tool the test does not call a
    failure.
  - Scope: the MCP surface. Purser's own pages show the signed-in person
    full detail, and REST serves full detail (bar external ids) to any token
    that can read. Known side channels, accepted: counts (`q` matches,
    `preview_rule`), per-group totals of a single row, a forecast's
    `omitted.net` when one event is left out, and balances by subtraction in
    the aggregate series.
- **Memories** (0.14.x, decision 48; #437). Reads need `read`, writes
  `write`, like every surface. With purchase privacy on, payee names inside
  a memory's text reach an agent as references. `propose_memory` stays a
  draft unless the user asked the agent to remember something (#436).
- **MCP tools declare their scope and what they emit** (0.14.0, issue #188;
  #437). `audited_tool` takes a required `scope`, enforced before the tool
  body runs, and a required `emits` (above), so a tool that omits either
  does not register at all. Scope enforcement being structural rather than a
  convention repeated in each tool closure is guardrail 4 applied to the MCP
  surface.
- Structural guarantee: token minting is admin-only (`POST /tokens` requires
  the `admin` scope, decision 52) — an agent can never create tokens, so it
  can never grant itself more access or clone its own. Agents receive
  their tokens from the admin UI or the wizard's connect-agent step.
- **Who sees and revokes a token** (issue #355, decision 75). Any token may
  revoke itself. A `session` token (a person's sign-in) lists and revokes
  every token of its own user, so the Tokens page works for every user,
  non-admin members included. An `admin`-scoped token revokes any token and,
  like a session, lists its own user's. Any other token — an agent's — sees
  only itself in `GET /tokens` and can revoke only itself; every other id gets
  the same `404 token not found` as an unknown id, so a refusal reveals
  nothing. The rule lives in `admin/service.may_manage_token` /
  `manages_user_tokens`; `POST /auth/logout` revokes only the calling token.
  Every agent token is minted under the admin's own user, which is why a plain
  same-user check is not enough: it let one leaked read-only token sign the
  admin out and revoke every sibling agent.
- Every mutation is audit-logged with actor token, action, entity,
  before/after. Browser sessions resolve to token identities too — one audit
  mechanism.
- **Reads are audited too** (issue #122, decision 31). Every authenticated
  request writes one `read`/`write` **call row**: REST via pure ASGI
  middleware (after the response flushes, own short transaction — an audit
  failure never fails a request; requests that resolve no principal produce
  nothing; 403s ARE logged), MCP via the `audited_tool` wrapper (tool name,
  clipped args — values capped, credential material never logged — ok/error,
  duration). The wrapped tools keep byte-identical schemas (snapshot test).
  A shared `request_id` groups each call row with the `change` rows it
  produced. The admin UI (and `GET /audit/sessions`) groups rows into
  **virtual sessions**: per actor, split after 30 minutes of silence —
  web sessions span their token lifetime; long-lived API/MCP tokens split
  into conversation-shaped bursts; scheduler rows (`actor_token_id IS NULL`)
  render as System.
- **Audit retention is finite and uniform**: `audit_retention_days` (config
  registry, default 90, floor 7) — `audit.prune_daily` (08:15 UTC) deletes
  older rows in id-batches. Consequences, owner-acknowledged: write audit is
  no longer forever; register category-provenance badges age out with their
  audit rows (same render path as never-categorized); read rows anchor dead
  login sessions for the retention window, after which `sessions.cleanup`
  hard-deletes them — self-healing, no cleanup change. The `events` table is
  unaffected and remains the durable "never unlogged" record for transfer
  automation.
- **Token kinds & session lifecycle.** `api_tokens.kind` (`session` | `api`)
  is server-controlled — only the login paths mint `session` — and origin
  attribution (HUMAN vs AGENT) plus schedule source key off it, never off the
  client-chosen token name. Sessions carry an absolute TTL
  (`PURSER_SESSION_TTL_DAYS`, default 7; single source for token expiry and
  cookie max-age) plus a sliding inactivity window
  (`PURSER_SESSION_IDLE_HOURS`, default 8), both enforced at auth time.
  `GET /tokens` returns live tokens only (`include_expired=true` reveals
  retained rows). A session cookie (HttpOnly, SameSite=Lax, Secure under an
  `https://` public URL) is ambient, so an unsafe-method request
  authenticated by it is refused (403) when the browser says another page
  sent it: `Sec-Fetch-Site` other than `same-origin`/`none`, or, without
  that header, an `Origin` that is neither the request's own nor
  `PURSER_PUBLIC_URL`'s (#429; Lax alone lets a sibling subdomain's page
  through). A bearer token, a read, or a client sending neither header is
  unaffected. Every request body is capped at `PURSER_MAX_REQUEST_BYTES`
  (32 MiB, 413 before it is read), because FastAPI reads a body before it
  resolves authentication. The hourly `sessions.cleanup` job hard-deletes dead
  sessions nothing references (their own login/logout audit bookkeeping goes
  with them) and stamps the rest expired: rows referenced by
  `transactions.created_by`, `matches.decided_by`, or other audit entries are
  attribution anchors and are never deleted.
- **Password sign-in is throttled** (issue #212, decision 59).
  `POST /auth/login` counts failures per email (casefolded) and per client
  address (the ASGI peer; IPv6 grouped by /64) in an in-memory
  `admin.throttle.LoginThrottle`, one per app. At `PURSER_LOGIN_MAX_FAILURES`
  (5) for an email or `PURSER_LOGIN_IP_MAX_FAILURES` (20) for an address, the
  next attempt gets `429` + `Retry-After` (whole seconds) **without the
  password being checked**, for a lockout of `PURSER_LOGIN_BACKOFF_SECONDS`
  (30) doubling per further failure up to `PURSER_LOGIN_BACKOFF_MAX_SECONDS`
  (900). A refused attempt neither counts nor extends the lockout. Attempts
  are charged on admission and refunded when the password verifies, so
  concurrent requests cannot race past a limit. Success clears the email's
  count, never the address's; a count is forgotten after
  `PURSER_LOGIN_FAILURE_RESET_SECONDS` (3600) with no failure and no lockout in
  force (measured from the lockout's end). All five are env-only; `0` turns a
  limit off. The app parses no forwarding header: uvicorn's own
  `FORWARDED_ALLOW_IPS` (default `127.0.0.1`) decides whether a proxy's
  `X-Forwarded-For` rewrites the peer, and behind a proxy it does not trust,
  every client shares the proxy's address — the per-address limit then acts
  instance-wide. Failed and throttled attempts resolve no principal, so the
  middleware writes no call row for them; the route audits them itself as an
  unattributed change row (`auth.login_failed` / `auth.login_throttled`,
  entity `user`, no entity id) carrying the email as entered and the client
  address — plus `retry_after` and `limited_by` when throttled — and never the
  password; with no actor, it groups under System.
  OIDC sign-in and Bearer tokens do not pass through the throttle.
- **Sign-in does not reveal whether an account exists** (issue #304). Every
  admitted password attempt runs exactly one argon2 verification: an unknown
  email, or an account with no password, is verified against a fixed dummy
  hash (`security.dummy_password_hash()` — the hash of a discarded random
  secret, made by the same hasher as real passwords so it carries the same
  cost parameters, and derived at app start so no attempt pays for it) and
  refused whatever that returns. Every refusal is the same
  `401 invalid email or password`, with the same `auth.login_failed` audit
  row, and the throttle above counts per email without looking the account
  up, so a `429` says nothing either. Skipping the hash for an unknown email
  would answer roughly ten times faster than a wrong password, a probe the
  throttle only slows down.
- **OIDC sign-in validates per OIDC Core 3.1.3.7** (#416, #429;
  `purser/admin/oidc.py`). Code flow with state, nonce and PKCE S256, all
  three in one HttpOnly cookie: `__Host-purser_oidc_state` (`Secure`,
  `Path=/`, no Domain) when cookies are secure, so a sibling subdomain cannot
  plant an attempt; on plain HTTP `purser_oidc_state` scoped to `/auth/oidc`.
  The callback reads only the name its own mode sets. Discovery must name the
  configured issuer (trailing slash aside) and, for an `https` issuer, `https`
  endpoints; discovery and the JWKS are fetched per sign-in (no key cache to go
  stale), with a timeout and no redirect following. The id_token (joserfc) is
  `RS256`/`ES256` only, key by `kid`; `iss`, `aud`, `sub`, `exp`, `iat`,
  `nonce` are essential; several audiences need `azp` = client id. `exp` has
  no leeway, `iat`/`nbf` tolerate 60 s of skew. Failures are fixed messages
  (400 state, 401 id_token, 403 account refused, 502 provider) and logs name
  the check, never a code or token.
- **SSO account resolution** (#211, #429 decision 95;
  `service.login_with_oidc`). An identity is (issuer, subject), the issuer
  being the validated `iss` without its trailing slash — always the
  configured provider's. In order: (1) the linked pair signs in; (2) a legacy
  link with a NULL issuer and this subject is claimed and the issuer recorded
  (`user.oidc_issuer_recorded`) — only the configured provider can have made
  it, migration 0041 having backfilled every link it could attribute;
  (3) the account whose email matches case-insensitively, only when the OP
  asserts `email_verified` (boolean `true`): unlinked → link; linked through
  another issuer → relink, the provider-migration path (`user.link_oidc` with
  `previous_issuer`); linked to another subject of this issuer, or a legacy
  link → **refused** 403 and audited `user.link_oidc_refused`, nothing changes;
  (4) otherwise provision a member only with `oidc_auto_provision`. Only an
  admin's `DELETE /users/{id}/oidc` (`user.unlink_oidc`, 409 when unlinked)
  frees a refused account; the account and password stay. Email matching is
  case-insensitive for password sign-in, linking, provisioning and the
  admin's user create (409), backed by the unique index on `lower(email)`;
  emails are stored as entered.
- **Encryption at rest is required, not optional** (#93). `PURSER_ENCRYPTION_KEY`
  (Fernet; comma-separated keys accepted, first encrypts) must be set or every
  role command fails fast pre-bind. The `encryption_canary` row binds the key to
  the database — a wrong key fails closed at startup with both fingerprints
  named. The first-run wizard reveals the key exactly once for recording (410
  after acknowledgment; ack audited).
- **Narrative / skeleton split** (#137, decision 36 — supersedes 22's
  ledger-plaintext stance). The *narrative* layer — the identifying words —
  is encrypted at rest via the `EncryptedText`/`EncryptedJSON` ORM
  TypeDecorators (transparent bind=encrypt / result=decrypt on `*_enc` BYTEA):
  `payees.name`, `transactions.description`, `postings.memo`, `schedules.name`,
  `audit_log.before_after`, `audit_log.detail`, `events.payload`,
  `memories.text` (§3.6), `transaction_reviews.note` and the narrative fields
  on `rules` (`match_text`, `payee_name`, `note`) — joining the
  raw bank envelopes (`raw_fetches.payload_enc`), SimpleFIN access URLs, webhook
  secrets, and config secrets already encrypted. The *numeric skeleton* —
  amounts, dates, statuses, ltree paths, ids, `content_hash` — stays plaintext
  because every SQL invariant, aggregate, and readmodel view computes on it, and
  app-side Fernet cannot be decrypted in-database.
  - Equality and dedup survive ciphertext through three plaintext skeleton
    columns and a keyed hash: **`payees.name_bidx`** (a blind index
    `HMAC(bidx_key, normalize_payee(name))` that replaces the old
    `normalized_name` — leaving *that* plaintext would leak what `name_enc`
    hides), **`events.dedup_key`** and **`audit_log.replacement_id`** (structural
    ids lifted out of the now-encrypted payloads so the once-only emit guards and
    the register's provenance badge still filter in SQL), and
    **`transactions.content_hash` as a keyed HMAC** (`keyed_hash(sha256_identity)`
    — a dictionary attack on a DB dump can no longer confirm a guessed
    (date, amount, description) tuple).
  - Both HMAC keys are random 32-byte **wrapped DEKs** in `encryption_keys`
    (Fernet-wrapped, loaded lazily + fail-closed). Deriving them from the primary
    key would silently invalidate every blind index and content hash on each
    rotation; wrapping them makes rotation re-encrypt ciphertext + re-wrap two
    rows, leaving indexes and hashes provably byte-identical (`purser admin
    rotate-key`).
  - **Casualty, measured (#137, §1e; two-phase interim #141):** register/MCP
    substring search over descriptions can no longer be an `ILIKE`; it is a
    decrypt-and-filter in two phases. SQL narrows by account/status/date/payee;
    the app streams only `(id, description)` of that ordered set, substring-
    matches the decrypted descriptions, and loads full rows (postings, their
    accounts, the payee) for the returned page alone — exact totals, the SQL
    order, unchanged response schemas; `q=None` keeps the pure-SQL path. The
    register's search box is debounced 250 ms, so a typed word is one search,
    not one per keystroke. Hydrating the whole narrowed set was the cost: the
    one-phase version measured ~0.9s at 10k and ~6.1s at 50k narrowed rows;
    the two-phase one measures ~0.25s at 10k and ~1.3–2s at 50k (the
    `tests/test_narrative_crypto.py` benchmark, median of five runs;
    `PURSER_BENCH_50K=1` adds 50k), and the Fernet decrypt itself (~80ms per
    10k rows) is now the floor. The token blind index (HMAC-per-word in a child
    table) stays deferred until one of three triggers fires: audited `q`
    searches with a p95 above ~300ms over 30 days, the 10k benchmark above
    ~300ms, or a product need for word search across all history at agent call
    rates (#141). Substring-within-word search still works under
    decrypt-and-filter (a token index would lose it).
  The Config → Settings page surfaces this posture read-only (issue #123): live
  canary check, key fingerprint, acknowledgment state, and the encrypted-scope
  list (now naming the narrative scopes + the keyed content hash) — with the
  explicit note that encrypting the database files themselves is a
  storage/deployment concern outside the app's ability to attest.
- **Runtime config is admin-scope, curated, and audited** (#102, #105).
  `/config` (GET/PUT/DELETE) edits the `app_settings` overrides for a fixed
  registry: sync schedule, agent privacy defaults (#437), AI provider settings, session lifetimes, Grafana
  URL, and OIDC (issuer / client id / client secret). Env vars stay the
  bootstrap defaults; a DB row overrides. `ai_api_key` and
  `oidc_client_secret` are write-only — Fernet-encrypted at rest, never
  echoed by any endpoint, masked to set/unset in audit rows. Every change
  writes one `config.update` / `config.reset` audit entry. OIDC is safe to
  edit live: `OidcClient` is built per request and password login is
  independent, so a bad value cannot lock the admin out; an explicit null
  override on issuer/client id disables SSO even when env configures it.
  Infrastructure settings (database URL, encryption key, bind host/port,
  public URL, `log_level`) are deliberately env-only: they are deployment
  concerns, and a DB override that only takes effect at restart would be a
  trap.

## 10. MCP toolset (v1)

Read: `list_accounts` (a reconciled account carries its latest
reconciliation's live check, decision 61) · `account_balance` (point-in-time
from the stored running balances, 0.8.0) · `balance_history` (ONE account over
time from the same store — adaptive density, end-of-day extremes over the
whole window, movers; 0.15.0, decisions 51/63) · `search_transactions` (rows carry the register
`balance` under a real-account perspective, 0.15.0) · `spending_summary`
(server-side group-by category|payee|month; baseline mode vs the user's own
history, 0.2.0) · `net_worth` (point/series + attribution, 0.2.0) ·
`forecast_balance` (events fitted to the response budget with `omitted` /
`end_balance`, optional `materiality`, decision 77; `basis` on a supplied
amount, decision 80; `range` on an estimated event and `low.range`,
decision 85) · `read_series` (a
`purser://` series URI's body byte for byte, for clients and gateways that
forward tools but not resources; budget-exempt, decision 77) ·
`list_recurring` (subscription radar over ACTIVE
schedules with monthly-normalized totals + price-change history, 0.2.0;
`source` is provenance, never a status, and `suggestions_open` counts the
schedule suggestions awaiting a decision, #347) · `whats_new`
(cursor-chained agent digest over the events stream, 0.2.0) ·
`list_holdings` (each open brokerage account's newest snapshot set — the
rows' source, value, positions, `stale_price` / `no_price` counts — with the
largest positions itemized inside the budget; one named account is paged
instead — keyset paging in symbol order, `cursor` / `next_cursor` (the
last symbol listed), stable across writes between pages, every page inside
the same budget, each
row with its `cost_basis`, `totals` that account's own (a named closed
account is marked `closed`, `totals` 0.00); three tiers, so an empty answer
still carries its headline; decisions 88/89) · `list_schedules` ·
`get_transaction` (postings +
provenance) · `list_categories` · `list_unmatched` (a row with an open
proposal names its bank row, `proposed_posted_id`, decision 76, and the
proposal, `proposal_id`, decision 86) · `search_docs`
(documentation search over the bundled build-time index, 0.11.0) ·
`list_rules` / `preview_rule` (deterministic rules and a dry-run of a
predicate over history, 0.14.0; given `pair_accounts`, the projected link
count of a `transfer_link` rule with its `window_days`, decision 66;
`list_rules` shows a pair rule's `payee_scope`, decision 67, and every
rule's `applied` count, §3.1) · `review_batch` (the review loop: unreviewed
rows with their signals and a checklist inline, 0.14.0) · `list_memories`
(recorded financial context, 0.14.x) · `list_suggestions` (the inbox: open
suggestions with headline, draft, evidence summary, confidence; 0.16.0,
decision 57; `kind` = schedule | transfer_link, decision 67 | match, decision 86)
Write: `create_transaction` · `update_transaction` (atomic reversal pair when
posted; `category='[account]'` is the transfer-conversion sugar, 0.5.0) ·
`create_schedule` (active; `propose=true` lands a suggestion instead, #436; `dtstart`, `counterparty_account`, 0.16.0) · `update_schedule` /
`end_schedule` (`action` = pause | resume | end, #347; `update_schedule`
reports how many instances a rename `relabeled`, decision 79) · `skip_instance`
/ `override_instance` (per-occurrence edits, 0.16.0, decision 55) /
`reset_instance` (undo an override, write scope, decision 79) ·
`record_statement_balance` (a card statement balance on the payment due
nearest `due_date`; write scope, credit cards only, decision 80) ·
`decide_suggestion` (accept | edit_then_accept
| reject | snooze, write scope, 0.16.0; a `transfer_link` accept returns a
DRAFT rule and its projected link count, decision 67; a `match` accept or
reject decides the proposal and returns `match{id,state,variance}`, decision
86) · `match` (confirms an existing proposal of the same pair, decision 76;
can pair a rejected one, decision 86) / `confirm_transfer` (optional
`remainder_category`: a split loan payment, decision 62; an ordinary confirm
proposes a payee-scoped `transfer_link` suggestion and returns its id,
decision 67) · `reconcile_account` (statement-balance
reconciliation with dry run, write scope, decision 61) · `record_valuation`
(a loan's or property's value on a date, `convention` ledger or statement,
write scope, decision 73) · `record_position` ("I hold N shares as of D" on a
manually tracked brokerage account; opens the position when needed; commit
scope, decision 88) / `record_price` (a manual close of a symbol on a date;
write scope, decision 89) · `update_account` (rename / re-describe / hide a
real account; no `path`, categories refused, write scope, §3.4, issue #353)
· `trigger_sync` ·
`propose_rule` (lands a draft rule that never executes until activated, or
an active one with `activate=true`, #436; 0.14.0; `window_days` on a
`transfer_link`, decision 66) · `mark_reviewed`
(bulk, idempotent, write scope, 0.14.0) · `propose_memory` (a draft, or
active with `activate=true` when the user asked, #436) /
`archive_memory` (0.14.x)
Disclosure (#437, decision 91): every tool that emits a privatizable field
takes `include` and names what it withheld in `withheld`; `present`
(`transaction_ids`, `note`) returns a link that shows the user rows in full
detail without the values passing through the model.
Meta: `describe` — data model, conventions, category tree, account list in one
compact call; a fresh agent session orients in one round-trip. From 0.14.x it
also carries three small keys pointing at the surfaces above: `rules`
(active/draft counts), `review` (unreviewed count), and `memories` (active
count plus the pinned few, §3.6).

Every tool declares its required scope on the decorator that registers it
(§9, issue #188), together with what it emits for minimal disclosure (#437).

Doc strategy: the `llms.txt` / tool guide is generated from the same source as
`describe`. OpenAPI descriptions are written for agents (when/why, not just
what). Every tool response shape gets a token-budget review; target < 500
tokens for typical answers. The docs *site* (`docs/`, issue #156) is the
user/agent-facing layer above this: Starlight, with its own llms.txt /
llms-full.txt, per-page raw markdown (`.md.txt`), and the OpenAPI spec
published at `/openapi.json`; the MCP catalog page is generated from the live
registry by `scripts/export_mcp_catalog.py` and drift-gated in CI. In-band
doc search is `search_docs` over a checked-in build-time index
(`purser/mcp/docs_index.json`, decision 41); `PURSER_DOCS_URL` decorates
result links.

**Every tool result carries at least one text block** (issue #391). A
bare-list tool that finds nothing answers with the text `[]` beside
`structuredContent` `{"result": []}`, the same shape FastMCP gives a
non-empty list; a `CallToolResult` with structured content but no content
blocks gains that content's JSON as text. `audited_tool` applies both after
its output-schema check. FastMCP alone sends an empty list as zero blocks,
and ContextForge rebuilds a result from its blocks, so it then dropped
`structuredContent` too and schema-validating clients failed whenever the
honest answer was "none".

### Presentation layer (0.2.0, issue #72)

Viz-worthy tools (`forecast_balance`, `net_worth`, `spending_summary`,
`list_recurring`, `whats_new`) answer in three tiers: a one-sentence text
headline, compact `structuredContent` (declared TypedDict contracts in
`purser/mcp/toolschemas.py`, validated per call), and
`_meta.ui.resourceUri` naming a self-contained HTML widget served as an MCP
resource (`ui://purser/<name>`, mime `text/html;profile=mcp-app`) per the
MCP Apps extension (SEP-1865). Shipped widgets: `forecast`, `recurring`,
`net-worth`, `spending`, `digest`, `balance-history` (0.15.0 — period pills,
biggest-mover drill-through, a faint full-resolution underlay from the
`purser://` resource, and an optional dashed forecast tail fetched CLIENT-side
through `callServerTool` so the two payload shapes never merge). Their shared
chart chrome — scales, gridlines, axis labels, per-segment below-zero shading,
the dashed reference and the line, and the estimate range band (`band`:
`[x, low, high]` drawn in the line's shape behind it, filled with the
`--chart-band` token that `theme.css` defines for light and dark, decision
85) — lives in `frontend/widgets/lib/chart.ts`; each widget still draws its
own markers, whose placement rules are widget-specific. The forecast widget
redraws with the band once it has read the daily series (its `low`/`high`),
names an event's basis and range in its tooltip, and warns when only the
band crosses the floor; the SPA Schedules page shades the same band from
REST `days[].low/high`. A `WIDGETS` entry with no `frontend/widgets/<name>/index.html`
is a test failure (the bundle fixture stubs files and the build auto-discovers
directories, so nothing else tied the two together). Widgets are built at image
build time from `frontend/widgets/` and carry no account data — Apps hosts feed them the
tool's structuredContent and proxy their actions back through
`callServerTool`, so widget interactions inherit the token scope model.
Full-resolution series stay out of structuredContent behind `purser://`
resources (`purser://forecast/{account}/{horizon}`,
`purser://net-worth/daily`, `purser://balance-history/{account}/{period}`),
which share the /mcp Bearer auth and are exempt from the token budget. The
read-scoped **`read_series(uri)`** tool serves the same bodies byte for byte
(FastMCP's own `read_resource`, a single text content, no
`structuredContent`) to clients and gateways that forward tools but not
resources; it accepts `purser://` URIs only, shares the exemption, and
refuses a body over 200,000 characters with a hint to narrow the window.
Widgets read a series through `resources/read` and fall back to
`read_series` (decision 77). Inside the budget, `forecast_balance`'s events
are fitted to 1,900 characters of JSON rather than capped by count: the
low's event first, then the largest by |amount| (ties by date), in date
order, with `truncated` only when the budget dropped a qualifying event and
`omitted {count, net}` whenever any event is unlisted, so `start_balance +
Σ events + omitted.net = end_balance`. A `daily_uri` it hands out always
resolves: the tool and the resource share one horizon check. A
`series_uri` a tool hands out always resolves: a balance history's `period`
is a named window ending today (`30d`/`90d`/`1y`/`all`) or the explicit
`START_END` window `balance_history` was called with, and the tool and the
resource validate those bounds with the same function, so the resource
accepts exactly the windows the tool does (issue #289). Both resolve a named
period with one function too, so the URI serves the very window the tool
summarized: `all` runs from the ledger's earliest activity with no clamp, and
the one bound every window shares is the resource's 5,000-point cap, oldest
first, flagged `truncated` (issue #322). `net_worth`'s `all` and
`purser://net-worth/daily` likewise share one window function, but keep a
ten-year bound on both sides (`networth.SERIES_CAP_DAYS`), because each
net-worth day is a full valuation computed live when it is not stored; on
both, `all` never starts after today (issue #343). A
balance history's full resolution is per-CHANGE rather than per-day: that is
the stored grain, it is what the register shows, and each point carries a
`transaction_id` drill target. Hosts without Apps support get a complete answer from
tiers 1–2 alone.

## 11. Optional AI capabilities (BYO)

Purser ships several model-driven capabilities behind **one gate**:
`ai_provider != "none"` in the Config registry (§9). Each capability beneath
that gate carries its own switch and is **off by default**, so turning a
provider on grants nothing by itself.

- Providers: OpenAI-compatible endpoint (Ollama, LM Studio, vLLM, OpenRouter,
  OpenAI) + native Anthropic. Off by default.

**Shared rules — every capability obeys all five** (decision 49):

1. **Off by default**, twice over: the provider gate plus the capability's own
   switch.
2. **Model output is always auditable.** Provenance — source, model, provider,
   confidence, and the memory ids that informed it (§3.6) — lands in the audit
   row for the action it produced.
3. **Every action is reversible**, because model output travels the same paths
   a human correction does: pending rows edit in place, posted rows get the
   standard reversal + replacement pair.
4. **A failing provider degrades to the deterministic path and never breaks a
   sync.** `categorize.run`'s `provider_down` latch is the pattern: stop
   calling, finish the pass, leave the rest untouched.
5. **Model output never calls `confirm_transfer`.** A model *may* identify a
   transfer by setting a row's category to `[account]` — a **one-sided**
   conversion naming a counterparty account, not a merge of two rows. That
   conversion may be reconciled against an existing counterpart by the same
   deterministic pairing `auto_link_pass` already applies (posted rows only,
   exact opposite amount, inside the date window, unlink-suppression
   respected). *Which* rows pair stays deterministic; the model only names the
   account. §5's tiers keep sole ownership of collapsing two feed rows.

The asymmetry in rule 5 is a detectability argument, not caution about model
judgment. A wrong transfer *categorization* posts into the counterparty
account and moves its ledger balance, so §7.3's post-sync drift check catches
it. A wrong *merge* is balance-neutral: every balance stays correct while two
unrelated rows are silently one, and nothing detects it. The capability being
granted is the one Purser can already find a mistake in.

### 11.1 Categorize (0.5.0; transfers 0.14.0, issue #183)

- Deterministic pass first (payee→category memory learned from accepted
  history); model only for misses. The deterministic pass also runs **at
  import time** (0.5.0): rows are *born* categorized when history knows the
  payee — no correction ever needed.
- The prompt lists the allowed categories as `path — description` lines
  (§3.2's curated descriptions are written for exactly this consumer), plus
  **real accounts rendered as `[path]`** — the transfer sugar of §3.3, minus
  the row's own account, hidden and closed accounts. The reply is constrained
  to that list: a category from the tree, or a bracket account. Never a new
  category.
- Relevant **memories** ride along as prompt context (§3.6): pinned/global
  memories plus those linked to the row's account or payee. Category-linked
  memories cannot reach this consumer — at categorization time the category is
  the unknown being decided.
- **Bracket answers clear a higher confidence bar than ordinary categories.**
  A transfer categorization restructures the row and moves a second account's
  balance, where an ordinary category error is one field and one inline edit.
  Below the bar the row stays uncategorized and surfaces for review (§4).
  This threshold is what rule 5's reconciliation leans on.
- **Auto-apply (0.5.0, owner decision):** categorization applies directly —
  pending rows edit in place; POSTED rows get the standard
  reversal+replacement correction with replacement `origin = rule`
  (system-actor scheduled pass after every sync, or write scope for manual
  runs). A bracket answer routes through
  the `[account]` transfer sugar rather than swapping the virtual leg — a
  transfer conversion has no virtual leg to swap. Provenance (source, model,
  provider, confidence, memory ids) lands in the audit row; the register
  surfaces it via `?with_category_provenance=true` → `category_source`, and
  every value is one inline edit away from correction. Accept/correct history
  accumulates in the audit log (future local-model fine-tuning corpus).
- Precedence, with deterministic rules (decision 46) ahead of everything:
  **rule > payee memory > AI classifier > uncategorized.** A rule must be able
  to beat a wrong payee memory — that is the "I keep correcting this, graduate
  it into a rule" path §4's review guidance points at.

### 11.2 Review (0.14.0, issue #175)

When a provider is configured *and* `ai_review` is on, the review pass runs
server-side after each sync instead of waiting for a connected agent. It
consumes the same `review_batch` payload an agent would, clears what the
context supports, and **escalates** the rest to the user (§4) rather than
guessing. A per-run cap bounds provider calls per sync.

This is the one place automation may mark a row reviewed — see §4: automation
that merely *touches* a row never does; automation that *reviews* it does, via
`via='ai_review'`, and only when the operator enabled it. `via` carries the
difference into the audit trail, so a row the backend cleared is never
confusable with one a human looked at.

Where a transfer is ambiguous — more than one candidate row in the target
account could absorb a bracket categorization — the reviewer escalates instead
of choosing. Categorization names an *account*; `confirm_transfer` names two
specific *rows*, and rule 5 does not grant it.

### 11.3 Memory curate / suggest / dream (0.14.x, issue #176)

`ai_memory_assist: off | curate | suggest | dream` — one escalating tier
rather than three booleans, because the capabilities nest by how much autonomy
they take and there is no coherent combination the ladder cannot express.

- **curate** — tidy what exists: propose merges for semantic near-duplicates,
  propose archiving memories whose links are all stale. Always a proposal,
  never a direct edit to an active memory.
- **suggest** — the deterministic hint triggers of §3.6 with the model writing
  the prose. The trigger stays SQL and countable ("recategorized WHOLEFDS →
  Groceries 4 times"), so every AI-suggested memory still carries a reason you
  can inspect.
- **dream** — open-ended synthesis over recent activity, no deterministic
  trigger. Highest risk, top of the ladder, opt-in on its own.

Containment is structural, not policy: an AI-authored memory lands
`state='draft'`, and **a draft is never served as context**. For AI-written
text to influence a later pass a human must activate it, so the
self-reinforcement loop requires human approval at every hop. A per-run
proposal cap keeps a single pass from consuming the 25-draft budget and
starving the agent path.

The agent path stays **primary**. A server-side pass sees only rows; a
connected agent has the conversation and is the better author of the same
text. The two are not competitors — the server pass is the fallback for a user
with no connected agent, or for nuances that surface between sessions.

## 12. Decision log

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | Build new (Purser) rather than extend Firefly III / Actual / Beancount | Agent-first tool surface and trust model are the product; no incumbent designed for an agent operator. Raw-feed layer keeps the ledger re-derivable. |
| 2 | Hybrid ledger: double-entry inside, flat API outside | Zero-sum invariant + correct transfers without imposing accounting semantics on agents. Retrofitting double-entry later is a migration nightmare. |
| 3 | PostgreSQL only; raw feed as JSONB in the same DB | One backup story, transactional atomicity between raw insert and import bookkeeping, queryable archive. No second datastore. |
| 4 | Currency structure from day 1, USD-only behavior in v1 | Multi-currency later = add fx_rates + reporting conversion, not a migration. |
| 5 | Posted = reversal-only | Strongest agent-trust guarantee: information can be added, never destroyed. API absorbs the ergonomics (atomic reversal pairs). |
| 6 | Matching engine is central; posted amount wins; variance recorded | Reconciliation is the job. Variance is signal (estimates improve, alerts fire). |
| 7 | Categories as virtual accounts (ltree) | One hierarchy mechanism; rollups are one indexed query. |
| 8 | Investments as snapshots; lots deferred (nullable lot_id hedge) | SimpleFIN provides snapshots, not lots; lot import is where migrations die; agent questions are answerable from snapshots. |
| 9 | Bills: no provider exists for self-hosters (FDX/1033 enjoined & aggregator-gated) → manual + detected + agent-parsed email via MCP | The agent stack IS the bill provider; server stays clean of email parsing. Provider plugin seam reserved. |
| 10 | Python / FastAPI; one image, role commands (serve/worker/migrate) | Single-process simplicity for Mac self-hosters; 12-factor seam for compose/k8s splits without image proliferation. |
| 11 | React (Vite/TS/shadcn/Tailwind) static SPA served by FastAPI; same public API as agents | Design potential + agent-assisted development velocity; no Node at runtime; UI dogfoods the API. |
| 12 | Durable events table + webhooks (not LISTEN/NOTIFY only) | Agents replay missed events; alert primitives in app, alert judgment in agent. |
| 13 | Docker Compose is the supported release path: app + Postgres, amd64 Linux and arm64 containers on Apple Silicon (#426). Native macOS packaging is retired; Helm remains experimental. | One installation and recovery model across supported hosts; each architecture requires native qualification evidence. |
| 14 | Grafana over in-app viz; read views as a public contract | Audience already runs Grafana; keeps UI administrative. |
| 15 | *(superseded by 23)* Direct-to-Bank as a second `provider='direct'` feed path ingesting by POST, not a new feed client | Preserves every downstream invariant; Direct is a new *entry* into the raw-first pipeline, not a new pipeline. |
| 16 | *(superseded by 23)* Direct ingest is feed-authority (propose token + per-connection binding, imports `token=None` → posted) | Mirrors the SimpleFIN sync precedent; the token/binding/signed-skill gate the submission, the rows carry the bank's status. |
| 17 | *(superseded by 23)* Skills emit one canonical `purser.direct.v1` envelope; per-bank logic lives in the skill | Purser needs one direct importer mapping, not one per bank. |
| 18 | *(superseded by 23)* Learned maps are structure-only, allowlist-scrubbed, stored append-only (`learned_maps` guard) | Makes the privacy claim true, not aspirational; the map is a reusable description, never the user's data. |
| 19 | *(superseded by 23)* Skills are Ed25519-signed; verified at cache time, cached in Postgres (`skill_cache`) | One datastore, transactional with the connection's `skill_ref`; a compromised registry can't inject a credential-stealing skill without breaking signatures. |
| 20 | *(superseded by 23)* Credentialing incl. OTP is the user's choice (OpenClaw/their vault); Purser never owns or automates it | OTP is an ordinary user secret; `unattended_capable` is a session-reliability flag, not an MFA-dodge. |
| 21 | MCP Apps widgets as ui:// resources from the same process; three-tier tool responses; vanilla-TS single-file bundles | Rendered experience in any Apps host with zero new infra; text+structuredContent keep text-only agents first-class; self-contained bundles survive host CSPs and stay auditable. |
| 22 | *(revised by 36)* Encryption at rest required for all installs; raw envelopes encrypted, ledger + readmodel deliberately plaintext | The archive holds verbatim bank data (the crown jewel); an optional key means most installs never set it. Readmodel views are a public SQL contract that app-side Fernet can't serve — plaintext there is a documented choice, not an oversight. |
| 23 | Direct-to-Bank removed entirely (0.3.0, migration 0008, issue #97) — learner, skills, registry/builder, `/v1/ingest/direct`, billing stub | Product pivot: SimpleFIN is the primary ingest path and manual bank download is the QIF importer, not a Purser-driven flow. The skill pipeline's maintenance surface (signing, registry, scrubber, wizard) wasn't justified by it. Rows 15–20 record the removed design; historical `provider='direct'` rows stay disabled as provenance. |
| 24 | Token `kind` column (migration 0009, issue #99) replaces the `session:` name-prefix as the human-vs-agent discriminator; token rows are never deleted while referenced — lifecycle hygiene is idle expiry + filtered listing + the `sessions.cleanup` job, not GC | The name prefix was client-spoofable (an API token named `session:x` produced HUMAN-origin writes). Rows anchor `audit_log.actor_token_id` / `transactions.created_by` / `matches.decided_by`, so deleting them would sever ledger attribution; only dead sessions with no references (pure login/logout bookkeeping) are hard-deleted. |
| 25 | Retired `provider='direct'` rows and their captures purged outright (migration 0010, issue #101) — an owner-decided one-off exception to the raw_fetches append-only invariant, guard triggers disabled and re-enabled within one transaction | The disabled rows were D2B test-era artifacts still surfacing in the admin panel; provenance retention (row 23) was no longer justified for them. Any ledger rows they produced survive with `source_fetch_id` NULLed; accounts survive unlinked. The invariant itself is unchanged — no application code path may mutate raw_fetches. |
| 26 | Runtime config as env-default + DB-override (`app_settings`, migration 0011, issue #102): curated key registry, admin-scope `/config` API, write-only Fernet secrets, audited changes, sync schedule as presets with a live in-process reschedule | Self-hosters shouldn't need a redeploy to change a sync cadence or plug in an AI key, but free-form runtime config breeds drift: the registry keeps infra settings (DB URL, encryption key, bind, OIDC, `log_level`) env-only, and presets (not raw cron) keep the schedule inside Bridge budget and ahead of the 06:45 matching job. |
| 27 | OIDC (issuer / client id / client secret) moved from the env-only list into the Config registry (issue #105, 0.4.1); `public_url` stays env | Row 26 grouped OIDC with infra by default; it is actually an application integration that reads config per request (`OidcClient` built in each auth route) with password login as an independent fallback — so live edits are safe, misconfiguration is recoverable from the UI, and a null override doubles as an SSO kill switch. The redirect-URI base (`public_url`) is genuinely deployment-bound and stays env-only. |
| 28 | Transfers auto-link deterministically at import when amount+window agreement is joined by a name signal (keyword token or counterparty account name); amount-only pairs stay confirm-first; late counterparts are absorbed into unbacked transfer legs (issue #108, 0.5.0) | SimpleFIN rows are authoritative and the remaining human work is categorize + link; a deterministic, evented, one-click-reversible auto-link does the linking by default without weakening auditability. "Never silent" becomes "never unlogged" — the event stream, register badges, and unlink replace the mandatory confirm. `transfer_auto_link` config kill switch preserves the old behavior. |
| 29 | Bracket-category transfer conversion (`category='[account]'` on update) + unlink that MOVES each original's `content_hash` onto its resurrected clone (issue #109, 0.5.0) | One field carries categorization AND transfer identity in the flat view (the Quicken convention users already know), so agents and the register UI need no second mechanism. On unlink, hash duplication is impossible (global unique index) and a hashless clone would break feed idempotency and the pending lifecycle — moving the hash makes the clone *be* the bank line while the void row keeps the forensic lineage. |
| 30 | Categories are a curated, finite, described set: `accounts.description` + shipped default hierarchy adapted from Plaid PFC (transfers/tracked-loan-payments excluded), seeded once by migration 0012 skip-existing (issue #110, 0.5.0) | A finite tree with classifier-oriented descriptions is what makes auto-categorization trustworthy: the model picks from a closed list whose entries say what belongs and what doesn't, and the same text serves humans in the Config tab. Migration-time seeding (not startup ensure) means user deletions stick; skip-existing means installs with their own trees merge additively. Plaid's taxonomy is the industry standard for exactly this classification job. |
| 31 | Audit covers every authenticated READ as call rows (`kind` read/write/change + `request_id` + `detail`, migration 0013, issue #122); retention is uniform and finite (`audit_retention_days`, default 90d, floor 7d, nightly prune) | Owner decision (2026-07-12): an agent-operated ledger needs "who saw what, when" — not just mutations — grouped Session → call → data to be usable. One table keeps the hierarchy queryable; the `change` kind (not plain read|write) keeps write-call counts honest since a single call emits N entity rows. Finite retention trades forever-audit for bounded growth; the `events` table stays the durable transfer log, and provenance badges/session anchors age out gracefully. |
| 32 | Historical backfill pages ≤90-day windows *backward* from the first sync's start, tracked by a `backfill_cursor`/`backfill_complete` pair on `connections` (migration 0014) rather than derived from the archive; imports with `window_start=None`; ProposeToken parity with sync; a `qif_overlap` warning (never a skip) flags manual-QIF rows in range (issue #126) | `raw_fetches` archives payloads but not the request *params*, so "how far back have we asked" is not reconstructable — a cursor column is the honest place for it, and `last_fetch_at` must stay reserved for the forward window (reusing it would corrupt incremental sync). `window_start=None` is mandatory: an old, pending-free backfill payload must never drive vanished-pending reconciliation or it voids every live pending. Backfill is agent-triggerable at the same propose scope as sync (it only lands import rows through the same idempotent pipeline). Order-of-operations guidance: **backfill to exhaustion before QIF-importing older history**; QIF-then-backfill stays a documented duplicate risk that content-hash dedup catches for identical rows and the overlap warning surfaces for near-duplicates. |
| 33 | Account lifecycle timestamps are real-accounts-only (`hidden_at`/`closed_at`, CHECK-enforced, migration 0015, issue #124, 0.6.0); close = precondition-gated void-of-expecteds + a **guided balancing adjustment posted as an ordinary transaction** + unmap-on-close + exact-zero check, with read-only enforced at the single posting chokepoint and an INSERT-trigger backstop; hide is a **list-endpoint-only** filter | The ledger already has one balancing primitive (a posted transaction); reusing it for the closing adjustment means net worth, matching, and audit need no special case — a closed account is just one whose postings sum to zero and take no more entries. Enforcing at `_persist_transaction` (plus the DB trigger) covers every write path — edits, corrections, transfers, imports, transitions — in one place, so "closed" can't be proposed around. Hide stays cosmetic on purpose: filtering anywhere but the default listing would silently drop the account from net worth and sync, which is a data change masquerading as a view. Unmap-on-close keeps feed discovery honest (a closed account should not look mapped). |
| 34 | Duplicate-account removal is a **guarded admin CLI** (`purser admin delete-account`, dry-run default + `--yes`, one transaction with posted-guard triggers disabled/re-enabled inside it), never an API endpoint (issue #124, 0.6.0); hard delete FREES the `qif:`-namespaced content hashes so the rows can be re-imported onto the right account | Voiding a mis-imported account's rows would squat their content hashes forever (decision 29 hash semantics), so the correct QIF could never re-import — only a hard delete frees them. That is a destructive, irreversible operation with cross-row lineage (merge-transfer counterparts must be unlinked so the *other* account's bank line survives; match rows and `superseded_by`/`reverses_id` pointers must be cleared), so it belongs behind a deliberate, backed-up CLI with a pre-flight report — not a one-request API that an agent or a mis-click could fire. `raw_fetches` is never touched: the archive stays the source of truth. |
| 35 | QIF import v2 is mapping-first with per-account **cutoffs** + sign **invert** (issue #125, 0.5.x): a row imports only when *strictly older* than its account's cutoff; the preview *suggests* `default_cutoff` = earliest feed-sourced date − 3d but the **server never imposes an unrequested cutoff**; a *split* transfer (older one side, inside the other) degrades to a one-leg flat the §5 auto-link later claims | Manual QIF and the SimpleFIN feed share one account, so the import needs a clean rule for "history the file owns" vs "the window the feed owns." A deterministic per-account cutoff with a small posted-vs-transacted margin divides them without fuzzy cross-source de-dup, and keeping it caller-supplied (preview suggests, import obeys only what's sent) preserves wizard/agent back-compat and keeps the dry-run exact (preview counts == commit results). Degrading splits to one leg hands the overlap to the existing transfer auto-link instead of inventing a second reconciliation path; a flat-hash guard stops a later both-older re-import from re-materializing that leg as a duplicating 2-leg. Invert handles banks that export cards/loans sign-flipped — abs()-based transfer hashes stay inversion-neutral, but flat/opening hashes don't, so the preview surfaces the re-import-with-invert duplication footgun rather than hiding it. |
| 36 | Narrative-layer encryption (issue #137, migration 0016, 0.7.0) — supersedes 22's ledger-plaintext stance. Encrypt the *identifying words* (payee names, descriptions, memos, schedule names, audit/event payloads) at rest under the existing Fernet key via ORM TypeDecorators; keep the *numeric skeleton* (amounts, dates, statuses, ltree paths, ids, content_hash) plaintext. Preserve equality/dedup with: a **blind index** `payees.name_bidx = HMAC(bidx_key, normalize_payee(name))` replacing `normalized_name`; plaintext **`events.dedup_key`** / **`audit_log.replacement_id`** lifted out of the encrypted payloads; and **`transactions.content_hash` composed into a keyed HMAC** `keyed_hash(sha256_identity)`. Both HMAC keys are **wrapped DEKs** in `encryption_keys`, not HKDF-from-primary. Readmodel `v1_transactions_flat` → **`v2`** (minus payee/description). Register substring search becomes decrypt-and-filter. | Decision 22 kept the ledger plaintext because the raw envelope was "the crown jewel" and readmodel is a public SQL contract; the owner now wants the narrative layer — which is *also* identifying — protected, while the skeleton every invariant/aggregate/view computes on stays plaintext (app-side Fernet can't decrypt in-database). Blind indexes keep payee identity resolvable by equality without leaking the name; wrapped DEKs (not derived from the primary) are the crux — deriving them would force a global rehash of every blind index AND content hash on each key rotation, breaking exactly the "idempotency provably unchanged" guarantee, whereas wrapping makes rotation re-encrypt ciphertext + re-wrap two rows and leaves every hash byte-identical. Composing content_hash into a keyed HMAC is a pure injective value map at migration time (no historical hash inputs to reconstruct — impossible for feed rows), preserving the partial-unique index and idempotency while hardening against a dictionary attack on a DB dump. The `v2` readmodel drops only the two encrypted columns (the other four v1 views hold none). Substring search is the accepted casualty (§1e): a decrypt-and-filter keeps exact totals + response schemas and even preserves substring-within-word matching (a token index wouldn't); measured ~0.9s at 10k narrowed rows, so a token blind index is a filed follow-up rather than a v1 requirement. |
| 37 | Point-in-time balances are **stored, chain-heads-only running balances** (`account_running_balances`, migration 0017, issue #146, 0.8.0): one row per (real account, chain-head pending\|posted transaction), total order `(date, created_at, transaction_id)`; the register's Balance column, `GET /accounts/{id}/balance?as_of` and MCP `account_balance` answer from the store; virtual accounts excluded; `net_worth_daily` unification deferred (§3.5) | Owner requirement: the register shows the balance at each transaction and any past date is answerable — recorded to the DB, never recomputed per request. The posted-immutability guards force a separate derived table (the `net_worth_daily` precedent). Heads-only accumulation is the only definition under which the on-screen invariant `balance[i] = balance[i-1] + amount[i]` can hold and the newest row provably equals `account_balances()` (reversal pairs cancel completely at end-of-time); the divergence vs `net_worth_at` strictly inside a date-edited correction window is documented and deliberate — the correction asserts what was always true. The id tiebreak resolves genuinely ambiguous `created_at` ties inside one import batch. |
| 38 | **One balance dirty-watermark, two consumers** (`balance_watermark`, migration 0017, issues #145/#146): every balance-affecting writer calls `balances.mark_dirty` (LEAST-update, colocated with its audit record; chokepoint inventory in §3.5 incl. the documented no-mark list); a sessionmaker `before_commit` hook consumes `running_dirty_from` synchronously inside the writing transaction; `networth.consume_watermark` consumes `networth_dirty_from` asynchronously (nightly + post-commit kicks + inline in scheduled sync / delete CLI) with claim-and-clear, per-day commits, re-mark on failure; series readers treat stored rows past the cursor as absent; the nightly job heals leftovers and parity-probes (mismatch → rebuild) | Owner policy (#145): net worth recomputes whenever balances change — the 3-day nightly window silently served stale history after any backdated write (observed as a step artifact after the #143 import). Marking is O(1) and transactional with the write; recomputing is set-based (a 400-row historical import costs one window-function pass sync + one background day-loop async, not per-row work). The sync consumer means a register can never be observed stale; the async consumer never holds the watermark lock across its minutes-long loop; the staleness-guarded readers close the gap before any consumer runs. Expected↔void paths and hide are no-marks because expected rows are outside the balance set and hide is display-only (both pinned by tests). |
| 39 | **Post-sync balance drift is a dual-basis warning, never a gate** (issue #149, 0.9.0, §7.3): after every successful sync, each mapped/open cash-card account's feed-reported balance is compared against BOTH live postings bases (posted-only, pending-inclusive; clean if either matches; smaller-\|delta\| reported, tie → pending-inclusive — since decision 64 the reported basis is chosen per account from sync history, this rule being its fallback); emission is gated by a **strictly**-fresher `balance-date` (> last ledger activity, UTC dates) OR the same canonical delta on two consecutive syncs; the cross-sync memory is a `balance_drift` observation block on the existing `connection.sync` audit row (no schema change); dedup is **episode-scoped** via a carried `emitted` flag, with `events.dedup_key = "{account_id}:{delta}"` as the structural id; loan/property/brokerage/closed/unmapped excluded; the discovered/Config surface shows the same comparison ungated | Owner decision (2026-07-13): the manual reported-vs-ledger probe was the single most valuable check of the #146 round — automated, it catches missed transactions, duplicates, seam gaps and sign errors on every sync, but it must warn, never block or write. Banks disagree on pending inclusion, so single-basis comparison misreports; the either-basis rule plus the freshness/consecutive gate makes an in-flight transaction structurally unable to false-positive, and strict-> keeps a newly onboarded account quiet while its delta shrinks toward the bank as history lands. The audit row is datetime-granular, backfill-immune, and transactional with the sync, where an events-table memory cannot gate its own first emission and re-deriving from the previous raw fetch cannot distinguish backfill fetches (decision 32) and is unsound under pending→posted mutation. Episode-scoped dedup lets a resolved-then-reappeared drift alert again while a persistent one never spams; the ungated live UI line means suppression never hides state. |
| 40 | **Per-account feed source scoping** (issue #154, migration 0018, 0.10.0, §7.1): nullable `accounts.feed_start` — the importer skips feed rows dated *strictly before* it at the `_import_account` chokepoint (before `auto_link_pass`), counted as `skipped_before_feed_start` in sync AND backfill results; admin-set/cleared via the existing `PATCH /accounts/{id}` (field-conditional scope check); plus account-focused backfill (`BackfillRequest.accounts` → repeatable SimpleFIN `account` params, advisory; filtered empty streaks never latch `backfill_complete`). Import-time only — never deletes existing rows. | The 2026-07-14 incident: a latch-reset re-page re-fetched ranges whose feed rows had been purged after manual import, content-hash idempotency had nothing to match (`qif:` vs feed identities), 121 duplicates landed, and `auto_link_pass` — which runs inside every import — consumed some into `origin='system'` merges that are invisible to feed-row predicates. The guard therefore sits at the one chokepoint all three feed paths share (sync, backfill, map re-import), not on the backfill request: it covers every path including future ones, and acts before a duplicate can half-form a transfer. One persistent per-account date is the mirror of decision 35's per-request cutoff (`manual < boundary ≤ feed` — "strictly older" there, "on-or-after" here; gap-free, overlap-free); manual cutoffs stay caller-supplied per decision 35, while the feed side is persistent because the feed fetches unattended. Best-effort cross-source dedup was re-considered and re-rejected (owner, 2026-07-15): cross-source rows share no stable identity (source-namespaced hashes, posted/transacted skew; even same-source twins needed `dup N of M` memo disambiguation), and a fuzzy skip converts visible, cleanable duplicates into silent data loss — fuzzy signals stay warnings (decision 32's `qif_overlap`), deterministic rules decide what lands. Scope: `feed_start` is feed configuration, so it requires admin like connect/discovered/map even though it rides the write-scope account-update route. |
| 41 | **Docs system: Starlight site + bundled search index** (issue #156, 0.11.0): user/agent docs live in `docs/` (Astro Starlight; llms.txt/llms-full.txt, per-page `.md.txt`, Pagefind, REST reference rendered from the checked-in `frontend/openapi.json`, MCP catalog page generated by `scripts/export_mcp_catalog.py` — whose `--format json` mode also regenerates the tool-schema snapshot). `search_docs` answers from a **checked-in build-time index** (`purser/mcp/docs_index.json`, generated by `scripts/export_docs_index.py` from the docs markdown; ASCII-only headings enforced so Python anchors equal Starlight's). `PURSER_DOCS_URL` (env-only, like `public_url`) decorates result links; unset = site-relative. | Offline-first: package data ships in wheel AND image with zero build plumbing, so uvx and air-gapped installs get doc search; deterministic + CI-drift-gated exactly like `frontend/openapi.json`. Runtime fetch rejected (network dependency in a read tool, untestable); Docker-stage generation rejected (wheel installs would lack the index). The docs site and the app's runtime `/llms.txt` stay separate artifacts: one documents the product, the other orients a connected session. |
| 42 | **Valuation-aware account balances** (issue #169, 0.13.0; extended to brokerage by decision 50): every account surface — REST `GET /accounts` / single GET / PATCH / reopen responses / `GET /accounts/{id}/balance`, MCP `describe`/`list_accounts`/`account_balance`, and the UI headline — reports an open loan/property account's latest valuation ≤ as_of as its `balance` (`balance_method="valuation"`, `balance_as_of`), postings fallback when none; `postings_balance` rides alongside; overlay lives in `networth.valuation_balances`, and `ledger.account_balances()` / the running-balance store stay strictly postings-sums | Only the net-worth engine honored §8's valuation model, so a SimpleFIN-synced mortgage with a balance-only feed showed $0 and a loan with only payment-split postings showed a positive "balance" on every account surface while `/networth` disagreed — the observed mortgage/auto-loan confusion. Overlaying at the presentation chokepoints keeps the postings invariants (running-balance parity probe, close's balancing adjustment) byte-identical while making every surface agree with net worth. Closed accounts keep their postings answer: the close adjustment zeroed them, and a stale valuation must not resurrect the account (same cutoff as `net_worth_at`). |
| 43 | **Per-account feed sign inversion** (issue #170, migration 0019, 0.13.0, §7.1): `accounts.feed_invert` bool — `_import_account` negates the feed amount strictly AFTER content-hashing and dedup lookup; income/expense classification uses the corrected sign; admin-set via the same field-conditional `PATCH /accounts/{id}` gate as `feed_start`; real-accounts-only CHECK | Fidelity's SimpleFIN stream reports deposits negative on all three transaction-bearing accounts, corrupting postings balances and making both transfer legs same-signed — invisible to §5's outgoing↔incoming pairing. Hash-on-raw is the crux: identity is what the bank sent, so a flag flip (or purge+replay repair) never duplicates history, and `_current_pending_hashes` stays consistent with stored hashes. A persistent per-account flag mirrors decision 35's per-request QIF `invert` exactly as decision 40's `feed_start` mirrors its cutoffs: the feed fetches unattended, so its config is persistent. |
| 44 | **Masked-reference duplicate guard at feed import** (issue #171, 0.13.0, §7): on a content-hash miss, skip the incoming row when a same-account, same-date, same-amount feed-origin row (any status) has a description that is the incoming one under digit-masking — `core.masked_variant`: equal length, every position equal or mask-char (X/x/*) over digit, ≥1 masked position; counted `skipped_masked_duplicate` | Fidelity re-delivered the 2026-08-03 $750 transfer twice ~12h apart, once with the reference visible and once masked — different hash, duplicate row; with 6-hourly syncs and monthly recurring transfers the class recurs. Positional mask-equality is deterministic identity, not fuzzy matching, so decision 40's re-rejection of fuzzy dedup stands: distinct visible digits never match, length must agree, and the guard stays same-source/same-account. Skipping (not warning) is correct here because the row's identity is provably already in the ledger. |
| 45 | **Feed repair is a guarded CLI pair: `purge-feed-rows` + `replay-archive`** (issue #172, 0.13.0): purge hard-deletes an account's feed-origin rows (`origin='import' AND source_fetch_id IS NOT NULL`, any status, optional `--since`, merge-transfers unlinked first, dry-run default, one transaction with the posted-guard triggers disabled inside it, audited `account.purge_feed_rows`); replay re-imports a connection's `raw_fetches` oldest-first via `import_fetch(window_start=None, replay=True)`, committing per fetch, audited `connection.replay_archive`; `replay=True` re-derives ledger rows only — no balance valuation and no holdings snapshot, which describe the fetch moment the live sync already recorded (#328) | Extends decision 34's stance: destructive remediation is a deliberate, backed-up CLI, never an API. §7's "the ledger is a projection of the archive" becomes operational — `map_account` re-imports only the latest fetch and `backfill` re-fetches from the bridge, but a wrong-imported history (the Fidelity sign inversion, #170) needs the ARCHIVE re-derived: purge frees the squatted hashes, and because hashes are computed on the raw feed bytes (decision 43), replay re-lands the same identities with corrected signs — no SimpleFIN retention risk, no duplicate risk, safe to re-run. Feed-origin-only scoping protects QIF/manual/agent rows, whose identities the archive cannot re-derive. Valuations and holdings are not part of that projection: purge never removes them, and the valuation step compares a fetch's balance with the account's LATEST valuation, so replaying an old fetch inserted its old balance again as a redundant valuation dated that fetch's day, one per fetch per loan, which no path can delete (#328). |
| 46 | **Deterministic server-side rules** (issue #174, migration 0020, 0.14.0; a declared pair's date window is per rule since decision 66, and it may be payee-scoped since decision 67): `rules` with three types (`categorize` \| `rename` \| `transfer_link`), a **structured, regex-free** predicate (account scope · token-boundary match over `normalize_payee` · amount · direction · `effective_from`) ANDed over set fields, first match by `(priority, created_at, id)`; executed at `_import_account` (rename → categorize, after the masked-dup guard, before payee memory) and in the nightly `categorize.run` / `auto_link_pass`; precedence **rule > payee memory > AI > uncategorized** (§11); a `categorize` rule may name a **real** account and routes through the same `[account]` sugar decision 49 wires for the classifier; retroactive application is an explicit dry-run-first `apply-history`, except `transfer_link`, which is retroactive for free via the existing sweep and therefore requires an explicit confirm of the projected link count before activating. Retroactive `rename` **aliases the payee entity** (`payees.merged_into_id`), never re-points `transactions.payee_id` | The product needs automation that *decides*, and decisions 35/40 say only deterministic, user-declared boundaries may decide — so the predicate is structured rather than regex: it is explainable in an audit row and in §4's reviewer guidance, has no backtracking DoS at the import chokepoint, and cannot be written against raw bank bytes that `normalize_payee`/`feed_invert`/decision 44 move underneath. Priority beats most-specific-wins because a rule's effect must be explainable by pointing at one row, not at a hidden ranking function. Rules run **after** content hashing, so decision 43's hash-on-raw guarantee extends unchanged — activating, editing or deleting a rule can never re-import history. Rename targets the payee, never the description, because three deterministic identities read that string (`content_hash`, `masked_variant`, decision 45's replay). Payee aliasing is the only retroactive rename that works at all: migration 0005's allow-list makes `transactions.payee_id` immutable on posted rows, so re-pointing is structurally impossible on exactly the rows it targets; skipping guts the feature's own rationale (consolidation is what strengthens `payee_memory`, which counts posted rows per payee); and a reversal+replacement pair per row to change a label is register noise for zero ledger benefit. Aliasing touches no transaction, engages no guard, needs no reinterpretation of §4, and is reversible. The retroactive asymmetry restates the immutability rule: linking is additive, lineage-preserving and reversible (`unlink_transfer`), while recategorizing posted rows is a correction and corrections stay explicit. The activation confirm exists because a merge is balance-neutral — §7.3 cannot detect a bad one (decision 49). |
| 47 | **Review flags are an unreviewed *set* over the registers, stored in a side table** (issue #175, migration 0021, 0.14.0): `transaction_reviews(transaction_id PK, reviewed_at, reviewed_by, via, note_enc, escalated_at)`; scope = live (`pending`/`posted`, unsuperseded) chain heads with `origin IN ('system','rule')` or `origin='import' AND source_fetch_id IS NOT NULL` — so no `expected` row, schedule instances included, is ever in it — defined once in `purser/review.py` (`scope_clause` + `unreviewed_clause`) and evaluated, never restated, by every surface below; any token-bearing write marks a row reviewed (`update_transaction`, `transition_status`→posted, `confirm_transfer`, `confirm_match`/`manual_match` (the bank row), unlink); corrections inherit review state only from a system/rule actor; unlink copies it onto the clone with the content hash (both copies are whole and a pending's mark follows it to its posted successor — decision 82); migration backfills every existing row so the feature starts silent and only ever concerns rows arriving after it ships; an aged sweep auto-marks past `review_max_age_days` (default 90) as `via='aged'`, counted in `whats_new` because the sweep firing is itself the signal that review has lapsed; served as `GET /transactions?reviewed=false`, a server-computed `needs_review` on every `/transactions` row, list and detail (what the register badge renders, #248), `POST /transactions/mark-reviewed` (commit), and MCP `review_batch` / `mark_reviewed` | 0.5.0 replaced the queue with registers, so review must be a filter over them, not a second row source. Membership ships per row because the obvious client test — no `reviewed_at` — is wrong for every row outside the scope: it flagged schedule instances, manual, agent and QIF/CSV rows that no surface counted (#248). A `reviewed_at` **column is impossible**: `transactions_posted_guard` (0005) allow-lists only `status`/`superseded_by` via `to_jsonb`, so every future column is immutable on posted rows — exactly the rows needing marks — and carving out the guard would spend the property migration 0005 deliberately bought for an annotation that moves no money. `source_fetch_id` scoping reuses decision 45's feed-row definition so a 6,000-row manual Quicken import isn't 6,000 review items, and excluding `origin='agent'` avoids double-counting rows the propose/commit lifecycle already gates. The requirement is not *human* review but review **with the relevant context present** — so an AI mark clears a row fully (a second mandatory tier recreates the deleted queue), `review_batch` declares which context was actually available rather than degrading silently, and `escalated_at` lets a reviewer hand a row back without becoming that second tier. Automation that merely *touches* a row never marks it reviewed; automation that *reviews* it does, via `via='ai_review'` and only when the operator enabled the backend reviewer (decision 49) — `via` keeps the two distinguishable forever in the audit trail. |
| 48 | **Memories are non-executable, user-owned context under a hard serving budget** (issue #176, migration 0022, 0.14.x, §3.6): encrypted `memories.text` (≤500 chars, ≤100 active, ≤8 pinned, ≤25 drafts) with `memory_links` rows to accounts (categories included, §3.2) or payees; served as pinned-only in `describe()` (≤8 rows / 1,000 chars), on demand via `list_memories` (≤25 rows / 2,500 chars), to §4's reviewer and to the §11 classifier by **exact FK match**; the whole surface requires unredacted `read`; `propose` lands `draft` and a draft is never served; graduation to behavior is a #174 rule carrying `rules.memory_id` | The user's nuances are the context an agent can't derive from the ledger, but free-form text that *acts* would defeat the deterministic stance (35/40) — so memories inform and rules decide, and no code path ever parses the text. Serving is the real risk: 100 memories is ~12k tokens, so `describe()` carries only a count plus the pinned few and every wider read is budgeted and truncation-accounted (guardrail 6). The propose/commit gate is what keeps an agent — or Purser's own optional authoring tiers (decision 49) — from writing its own future context: a draft is never served, so the loop needs human approval at every hop. Requiring unredacted `read` for the whole surface rather than withholding only the text is the one option that decouples this from #14: serving links without bodies leaks the association (that two accounts are related) while losing the useful part, and a redacted principal is by definition one not trusted to reason over private context. Links go in a table because the motivating example spans two accounts, and categories are accounts, so one nullable pair covers all three link kinds. Archive-not-delete because user prose is not derivable from anything else in the system. |
| 49 | **Optional AI is a tier under one gate, with five shared rules** (issue #183, 0.14.0, §11): §11 widens from "AI categorization" to "Optional AI capabilities (BYO)" — one gate (`ai_provider != "none"`) with per-capability switches beneath it, all off by default (categorize · review · memory curate/suggest/dream). Shared rules: off by default; output always auditable with provenance; every action reversible; a failing provider degrades to the deterministic path and never breaks a sync; **model output never calls `confirm_transfer`**. The classifier's option list gains real accounts as `[path]`, `_parse_reply` accepts the bracket form, and `_apply_category` delegates to the `[account]` transfer sugar instead of swapping the virtual leg — with a **higher confidence bar for bracket answers** than for ordinary categories | Three capabilities behind one switch were about to restate the same posture three times, so it is stated once here and referenced by 46/47/48. The bullet this replaces ("model output never merges transfers — LLM transfer identification is suggest-only") conflated two operations and left the classifier structurally unable to give a right answer: §3.2 deliberately excludes transfer categories *because* the category field handles them via `[account]`, and the option list is virtual-accounts-only — so a transfer row's only legal outputs were wrong ones, and #176's memories ("the monthly $750 to Fidelity the child is a 529 contribution") could never pay off. Identifying a transfer is **one-sided** — name a counterparty account, produce an unbacked leg; merging is dedup across two feeds. A bracket conversion may still be reconciled against a live counterpart, but only by `auto_link_pass`'s own rules (posted-only, exact opposite amount, in-window, unlink-suppression respected), so the model names the account and the deterministic machinery picks the rows; `confirm_transfer`, which names two specific rows, is not granted. The split is a detectability argument, not caution about model judgment: a wrong transfer categorization moves the counterparty account's balance and §7.3's drift check finds it, whereas a wrong merge is balance-neutral and invisible — which is why the confidence bar on bracket answers is load-bearing rather than decorative, and why ambiguous pairs are escalated (§4) instead of guessed. |
| 50 | **Account surfaces value a brokerage account at market** (issue #196, 0.15.0, §8; amends 42): open brokerage accounts add their latest holdings market value ≤ as_of on top of the postings basis (`balance_method="postings+holdings"`, `balance_as_of` = snapshot date) on every account surface — `GET /accounts`, single GET, `/accounts/{id}/balance`, MCP `describe`/`list_accounts`/`account_balance`, and the forecast's opening balance. The overlay returns an **addend** (`networth.holdings_addends`), not a total, and `net_worth_at` delegates to it. Same closed-account cutoff as decision 42. | Decision 42 fixed loan/property and left brokerage reporting a bare postings sum, so `account_balance` under-reported the very same account `net_worth` valued correctly — the identical confusion 42 was written to end, one account type over. An addend rather than a total because the postings basis differs per surface: the listings and the register headline overlay the LIVE sum, the point-in-time surfaces overlay the STORED running balance at a date, and forcing one basis on both would silently change the other's answer. `net_worth_at` delegating to the same function is what makes "brokerage market value at T" have one implementation instead of two that drift. The forecast moves onto the overlay but KEEPS the live basis: a future-dated pending row is not an `expected` row, so the event loop never sees it, and taking it out of the opening balance too would erase it from the projection — the reason the opening balance matches the register headline rather than `/balance`. |
| 51 | **Per-account balance history is a sampled read over the stored running balances; no new storage** (issue #198, 0.15.0, §3.5; extremes made end-of-day by decision 63): `core.sampling.sample_dates` (shared with `networth.series_sampled`) picks dates at the coarsest density that fits, then `balances.balance_series` resolves each with one `unnest` + `LATERAL` last-row-≤-d seek on the existing index; `networth.account_balance_series` branches ONCE on account type (valuation / postings+holdings / postings) and applies the close cutoff per DATE; `change_points` is the full per-CHANGE resolution behind `GET /accounts/{id}/balance/series`, capped at 5,000 with an explicit `truncated`. | Decision 37 already records the whole series — the gap was a read shape, not a data gap, which is the strongest argument 37 was right. §3.5's deferral of a per-account daily rollup is untouched: this adds no table and no second watermark, and the cost is O(points × log n) on an index that already exists. Extremes are computed over ALL rows rather than the sampled subset, which is what makes publishing a downsampled series honest — "low 1,842.10 on Jul 19" stays true in a 31-point series that never contains the 19th. Full resolution is per-transaction, not per-day, because that is the stored grain, it is what the register shows, and each point carries a `transaction_id` drill target (`purser://forecast/…` is daily only because the forecaster is a day-loop). The type branch is once-per-series rather than per-date because an account does not change how it is valued mid-window; the close cutoff is per-date because history before a close keeps its overlay. |
| 52 | **Token minting is admin-only** (issue #207, 0.15.x, §9): `POST /tokens` requires the `admin` scope; the scope-subset rule ("never grant beyond your own") is retired as the minting gate, and unknown-scope validation lives solely in `admin/service.create_token` (422). Agents receive tokens from the admin UI or the wizard's connect-agent step; the OpenClaw-skill self-bootstrap flow is retired with it. | The subset rule alone made a leaked token's blast radius unbounded in time: any live token — including read-only service credentials — could mint unlimited non-expiring clones at its own scope, and revoking the parent left the clones alive with nothing tying them back for cleanup (no lineage column; audit rows are a record, not a revocation tree). With multiple service tokens now deployed, admin-only minting closes the hole at the chokepoint instead of adding lineage machinery: the Tokens UI already runs as an admin session, so the human flow is unchanged, and cascade revocation becomes unnecessary because non-admin tokens can no longer have descendants. |
| 53 | **Manual imports are raw-first** (issue #127, migration 0023, §7): `raw_fetches` becomes the archive for every payload — `connection_id` nullable, `source` discriminator (`simplefin`\|`qif`\|`csv`), CHECK tying the two; `import_qif` archives the verbatim upload before any ledger write, skipping when an identical prior archive exists; the `{fmt}.import` audit row carries the `raw_fetch_id`. Ledger rows from manual imports keep `source_fetch_id` NULL. | §7's preamble promised raw-first "for both paths" and the QIF path silently didn't deliver (the honest-drift note this decision retires) — and guardrail 3 already stated the rule this codifies. Keeping `source_fetch_id` NULL is the load-bearing half of the design: PR-7's cutoff queries and the #175 review scope both read a non-NULL `source_fetch_id` as "feed row", so a 6,081-row Quicken import must not become 6,081 review items nor flip cutoff semantics — the archive link is content hash + audit trail, never a transactions FK. The archive dedups on content hash so re-running an import stays a no-op end to end, mirroring the ledger's idempotency rather than inventing a second convention. |
| 54 | **Fan-out is cursor-driven** (issue #219, migration 0024): `webhook_subscriptions.cursor_at` is a watermark over `events.at` — each `fan_out` pass scans only `at >= cursor_at` per subscription, advances the cursor to the newest event scanned, and inserts deliveries with ON CONFLICT DO NOTHING on the `(subscription_id, event_id)` unique; a new subscription's cursor defaults to now(), so it receives no historical backlog; the migration backfills existing subscriptions to their newest delivered event's `at`. | The anti-join over the full events table grew without bound and re-ran per subscription per pass, and a fresh subscription drowned in the entire event history as deliveries — replay is what `GET /events` (decision 12) exists for, so fan-out re-serving history was duplicated responsibility. The `>=` boundary plus DO NOTHING is deliberate: `events.at` is transaction time, so same-timestamp ties on the boundary are re-scanned next pass and deduped by the constraint — which simultaneously closes the IntegrityError race between concurrent passes without any locking. |
| 55 | **Schedule instances belong to the user; the rule owns only what the user has not touched** (issue #231, migration 0025, §4/§6): `schedules.dtstart` is persisted and anchors every expansion (`FREQ` allow-listed to DAILY or coarser; RFC `UNTIL=…Z` accepted); `transactions.occurrence_date` is the rule slot and the idempotency key across every status; `transactions.schedule_pinned_at` marks a token-bearing edit; a user skip (void) keeps its slot, a rule resync (edit/pause/end) voids-and-releases it; estimated schedules refresh live un-pinned instances; `next_due` is a cache refreshed from the instance write paths; `status` gains terminal `ended` + `ended_at` | `dtstart` was validated then dropped (#221), so anchor-sensitive rules silently anchored to row creation and the phase drifted; persisting it (rather than deleting the parameter, the other way #221 could go) is what detection v2 needs to propose "every other Friday from the last observed payday". Idempotency keyed on `date` made an instance date edit free the slot for a duplicate, so the slot needed its own column — and once slots exist, skip vs resync is the crux: both are voids, but a skip must never come back while a rule edit undone must be able to re-fill exactly the slots it released, hence a released slot is `NULL` rather than a second status. Pinning is the invariant that lets estimate refresh exist at all without a rule silently overwriting "this Citi payment is 8,067.37"; it is a timestamp, not a flag, so the audit trail says when the user took the row. `next_due` stays a column because three list surfaces order by it; deriving it on read would put a subquery under each. |
| 56 | **Recurring transfers are schedules with a counterparty, and matching sees every row through the perspective leg** (issue #232, migration 0026, §5/§6): `schedules.counterparty_account_id` (CHECK-exclusive with `category_path`); instances are two-leg expected transfers created through `ledger.create_transfer`; `estimate_basis=counterparty_balance` reads the counterparty's reported balance; the matcher's candidate pool is `origin IN (import, system)` OR any live two-leg row, `MatchInput` carries the leg on the account being matched plus the other real account, a shared counterparty scores as payee agreement, a two-leg expected is tried from each side, `unmatch` clones any posting shape, and `manual_match` views both rows through their shared account; MCP `search_transactions` shows a transfer's perspective leg and `counterparty` instead of nulls | The 30-day drivers on a checking account are transfers — card payments, loan payments, 529/IRA contributions — and they were invisible twice: detection and matching both filtered to `origin='import'` with exactly one real leg, so a card-payment schedule could never be detected and, once the bank rows auto-linked into a merge, could never be matched: the expected stayed "overdue", landed today in the forecast on top of the posted outflow already in the opening balance, and the projection double-counted every card. Modelling the transfer as a two-leg expected (not a flat outflow to a `transfers` category, which §3.2 deliberately does not have) keeps §3.3's invariant that a transfer is two real legs everywhere, makes the instance show up on both accounts' forecasts for free, and lets the matcher supersede it with the merge exactly as it supersedes a flat expected with an import row. "Perspective leg" is the one idea that makes one-leg imports and two-leg merges comparable without special cases: an amount only means something from an account's point of view, which is also why `search_transactions` returned nulls for transfers — it had no perspective. Origin cannot define the pool because a human-confirmed merge carries the human's origin; two real legs can, and it is the same structural test `is_transfer_shaped` already uses. `counterparty_balance` is deliberately the *reported* balance, not a statement balance Purser does not have: it is honest as an estimate, refreshes nightly, and the user overrides (pins) the instance when the statement says otherwise. |
| 57 | **Suggestions are a first-class entity, and "proposed" is a suggestion, not a schedule status** (issue #233, migration 0027, §3.1/§6/§10; the first further kind, `transfer_link`, is decision 67): `suggestions(kind, status open\|accepted\|rejected, fingerprint UNIQUE, source, detector, confidence, subject/counterparty ids, encrypted typed draft + evidence, snooze_until, seen_count, decision fields, reject_scope, result entity)`; detection and propose-scoped `create_schedule` upsert by fingerprint; `decide(accept\|edit_then_accept\|reject\|snooze)` is write scope and accept applies the draft through `create_schedule`; legacy `proposed`/`rejected` schedule rows migrate into suggestions and `schedules.status` is CHECKed to `active\|paused\|ended`; `schedule.detected` retired for `suggestion.created`/`suggestion.decided` | Purser already ran four bespoke confirm paths and a "proposed" schedule was a row with no evidence, no confidence, no snooze, and rejection memory only by accident (the rejected row happened to block re-detection by payee). Each detector added would have grown that linearly. A suggestion carries state the target object must not: what convinced Purser, how sure it is, when to ask again, and what the user said last time — and the fingerprint is what makes "never re-propose what I rejected" a property rather than a hope. The draft is typed and accept is a pure apply so the audit trail says exactly what was created from exactly what was shown; a re-derivation at accept time could silently create something else. Snooze is a date on purpose: expiry that auto-accepted would be a second, hidden auto-activation path, and the whole design rests on nothing that moves the forecast activating without a commit decision. The universal-`status=proposed` alternative (no table, a cross-entity read view) was weighed and loses exactly the four things — evidence, confidence, snooze, rejection memory — that keep an inbox from becoming spam. Scope this round is schedules only; transfer candidates and proposed matches keep their paths and fold in as later kinds (proposed matches did in decision 86, as mirror cards — their paths stay). |
| 58 | **The MCP server is built on standalone FastMCP 4, not the SDK-bundled class** (issue #255, §10): `purser.mcp._registry` imports `fastmcp.FastMCP`; `audited_tool` derives each widget tool's `outputSchema` from its `Annotated[CallToolResult, <TypedDict>]` return and validates `structuredContent` against it server-side before the result leaves the tool; `mount.py` builds `mcp.http_app(path="/", stateless_http=True)` behind the Bearer wrapper with FastMCP's Host/Origin request guard carrying the same allowlist as before; the FastAPI lifespan enters the MCP app's lifespan. The wire surface is unchanged: `/mcp/`, stateless, 38 tools with the same names/parameters/defaults, `ui://` and `purser://` resources, three-tier results. Tests drive the server with the SDK v2 client (`streamable_http_client` over `httpx2`) so interop with a plain SDK client stays proven. | MCP Python SDK v2 removed `mcp.server.fastmcp` (the FastMCP 1.0 class bundled in v1), and `mcp>=1.28.1` had no upper bound — the first lock refresh would have broken the server at import. Standalone FastMCP is where that class's features live and are maintained; it serves the handshake-era protocol ContextForge speaks and the sessionless 2026-07-28 era from one process. The SDK's own `MCPServer` would have been a second rewrite of the same layer; a `<2` cap alone only deferred the break. Standalone FastMCP infers no outputSchema from `Annotated[CallToolResult, X]` and does not validate `structuredContent` server-side (SDK v2 clients validate it client-side), so both moved into `audited_tool` to keep decision 41's "drift fails loudly" guarantee at the server. Known wire deltas: tool `meta` gains a `fastmcp` namespace; input schemas drop property titles and declare `additionalProperties: false` (ContextForge now rejects unknown arguments it used to pass). |
| 59 | **Password sign-in throttling is in-memory and per process — no DB table** (issue #212, §9): `admin.throttle.LoginThrottle`, one per app on `app.state`, counts failed `POST /auth/login` attempts per casefolded email and per client address (the ASGI peer, IPv6 grouped by /64); at 5 / 20 failures the next attempt is refused with `429` and `Retry-After` (integer delta-seconds) **before any password check**, for 30 s doubling per further failure to a 900 s cap; a refused attempt neither counts nor extends the lockout; attempts are charged on admission and refunded when the password verifies; success clears the email's count, never the address's; a count is forgotten after 3600 s with no failure and no lockout in force (measured from the lockout's end); five env-only `PURSER_LOGIN_*` knobs, `0` switching a limit off; the app parses no forwarding header, so uvicorn's `FORWARDED_ALLOW_IPS` is the only proxy trust list; failed and throttled attempts write unattributed `auth.login_failed` / `auth.login_throttled` change rows carrying the email as entered and the address, never the password. | A throttle only has to take an online guesser's throughput away, and that survives losing its state: a restart hands back at most one fresh budget (5 guesses per email), and a restart loop is its own alarm. A DB table would make every failure — and, under a flood, every refused attempt — a counter write on the hottest unauthenticated path, and add a migration, a cleanup job and row-lock contention, all for state whose loss is harmless. The price is per-replica counts: under Helm with `api.replicas` > 1 each replica allows its own budget, so N replicas allow N times the guesses; the documented deployments run one API process, and a multi-replica default is the trigger to move the counts into shared storage. Charging on admission closes the check-then-act race: argon2 verification is slow by design, so counting only completed failures would let a burst as wide as the threadpool (40 by default) through at every lockout expiry — 40 times the cap's rate. Refusing before the password check is what makes a lockout real (no oracle while blocked) and sheds the argon2 cost of a flood; `Retry-After` in whole seconds is the RFC 9110 form browsers and agents honour directly. Success leaves the address count alone so one valid credential cannot launder a spray across other accounts, and the quiet period runs from the lockout's end so a capped lockout is never forgotten the moment it lapses. The app trusts no forwarding header of its own: uvicorn's proxy-header middleware already rewrites the ASGI client for trusted peers only, and a second parser would either duplicate that list or believe a header any client can forge — so behind a proxy uvicorn does not trust, the per-address limit degrades to instance-wide (documented) instead of becoming bypassable. The audit rows are change rows beside the existing `auth.login` rather than call rows, so the middleware's no-principal-no-row rule stays exact and the rows age out with the rest of the trail. |
| 60 | **Manual imports honour the file's cleared flag, and Quicken reminders are not history** (issue #249, §4/§7.2): a Quicken CSV row whose `Scheduled` column is marked is filtered before hashing into `skipped_scheduled` — never imported, never half a transfer, outside the dedup identity, counted identically by preview and commit; the cleared flag (QIF `C`: `*`/`c` cleared, `X`/`R` reconciled; the CSV `Clr`/`Cleared` column) is honoured **per file account**, an account with no marks at all reading as all-cleared; cleared rows land `posted`, uncleared rows land **`expected`** (origin `import`, no schedule, counted `uncleared`); a two-leg transfer is expected only when no leg the file carries is cleared, a split one-leg flat follows its own leg, and opening balances always post; status stays out of the content hash, so re-import remains a no-op; a later file that **marks** such a row cleared posts it in place (#310: audited `transaction.posted` with reason `cleared_in_file`, counted `promoted` in preview and commit — a bucket, per leg for a transfer, creation's transfer rule) unless it is a schedule instance or a feed row or touches a closed or feed-connected account; a CSV without a cleared column carries no marks. No migration and no matcher, reconcile, review-scope or balance change. | A Quicken register import landed future reminder rows and never-cleared rows as `posted`, and the post-sync drift check (§7.3) — posted/pending ledger vs the bank's reported balance — could only report the difference after the fact. Reminders are the register's view of the future, not history; turning them into suggestions (§6) is a separate decision, so this round skips them and keeps the count visible in the preview. `expected` is the only status that is true for an uncleared row: `posted` claims the bank has it (the bug), and `pending` collides with the feed's vanished-pending sweep (`feed/reconcile.py`), which voids every `origin='import'` pending dated in a sync window that the fetch no longer lists without asking which source the row came from — the next sync of a connected account would void every uncleared file row. As `expected` the row behaves exactly like a schedule instance: outside the balances (so drift compares like with like), in the forecast (an outstanding check is money already committed; overdue lands on day 0), aged by `bill.late`, and superseded by the matcher when the posted bank row arrives — and manual expected rows without a schedule were already a supported shape, which is why no consumer had to change. The per-account no-marks rule exists because an absent mark is ambiguous: bank-generated files never mark rows and Quicken leaves never-reconciled accounts (cash) unmarked, so reading blank literally would turn those accounts' whole history into forecast items. A transfer posts when any leg is cleared because the alternative strands the cleared side — before the feed's start there is no bank row to match, so that balance would stay short for good — whereas a posted transfer only runs ahead on the uncleared side until its bank row is absorbed (§5). Promotion (#310) closes what the first cut left as a residual: status is not identity, so a later export in which a row had since cleared deduped against the expected row, and on an account without a feed nothing ever settled it — the user had to post or void it by hand. The identity match is exact and the newer file is the evidence, so the row is posted in place, never duplicated. Only an explicit mark counts: an unmarked account is cleared by convention, and a narrow export holding only an account's still-uncleared rows reads as unmarked, so taking the convention as evidence would post the very rows #249 kept out of the balance. Feed-connected accounts are left to the matcher because the bank's own row arrives there: the matcher supersedes the expected row with it, and a promoted row would sit beside it as a second posted copy of one bank line. |
| 61 | **Account reconciliation is an audited claim whose marker is re-checked on every read** (issue #268, migration 0028, §3.7): `account_reconciliations(account_id, as_of, basis posted\|pending_inclusive, target_balance, adjustment, adjustment_tx_id NULL iff adjustment = 0)`, UNIQUE `(account_id, as_of)`, plus `accounts.reconciled_through`; input `statement_balance` in statement convention (liabilities negated into ledger sign); one system-origin adjustment to `expenses.reconciliation_adjustments` (none when the ledger already matches — the clean reconcile is recorded too), in the review set and excluded from the matcher's pool; `dry_run` is the same code path stopping before writes; future dates 422; the account row is locked `FOR NO KEY UPDATE`; every read that serves the marker re-checks the latest claim live (`in_balance`, `difference`); repeating a claim is `created: false` while it holds and 409 `reconciliation_moved` once the ledger has moved; `account.reconciled` event | The first cut (#252, PR #253) posted the plug and advanced a marker that nothing read again, so a backdated import, edit or purge-and-replay moved a reconciled balance with no trace, and an exact retry answered 200 — the one moment the drift was in front of the caller, it was hidden. Storing the target and recomputing on read is what makes the marker honest without a trigger on every ledger write: the check is one batched sum over the same chain heads the register uses, so it cannot disagree with the balance a person sees, and nothing is rewritten behind their back. The first cut also refused the most common case — a statement that already matches — only because `adjustment_tx_id` was `NOT NULL UNIQUE`; a nullable column with a shape CHECK records it. It compared a pending-inclusive balance against statements that usually report posted (a −30 hold plus a 100.00 statement posted a +30 plug), hence an explicit basis per claim, the two bases §7.3 already distinguishes. It took a ledger-signed figure, so a card statement's positive amount owed committed a ~2x plug outside the review set, hence statement convention, a dry run and a system origin. One claim per date rather than per (date, basis): a second basis's adjustment is a posted row that moves the first claim's balance too, and "the latest reconciliation" must be unambiguous for the read check. The lock is `FOR NO KEY UPDATE` because a `FOR UPDATE` row lock conflicts with the `KEY SHARE` lock every posting insert's FK check takes, which deadlocks a sync holding the balance watermark against a reconcile waiting for it. Future dates are refused because the marker only moves forward: a far-future claim would block every real statement date before it. |
| 62 | **A split loan payment is one transaction: the exact cash leg, the exact principal leg, and one balancing interest/fee posting — and a row is a transfer only when its postings are a shape Purser creates** (issue #269, §3.3/§5): `confirm_transfer` (REST + MCP, write scope) takes an optional `remainder_category`; with it, unequal bank legs merge through `ledger.create_split_transfer` / `core.expand_split_transfer` into three postings whose category leg is `−(cash + principal)`; bounds `0 < principal < \|cash\|` are strict (equal → ordinary transfer, larger → refused, both 422); the category must be a `category_expense` other than the uncategorized placeholder; explicit-only (a token-less caller is refused, no automatic path produces one); audit and `transfer.confirmed` carry `remainder` / `remainder_category` on splits only; unlink restores both originals and the account purge unlinks splits first; `core.transfer_shape` (PLAIN \| SPLIT \| none) is the one classifier behind `is_transfer`, MCP `counterparty`, unlink, conversions and the purge; the flat view adds `remainder_amount`, MCP rows `remainder`, and the register shows the remainder under the counterparty | A loan payment's bank legs never cancel — checking pays principal plus interest, the loan shows principal — so an opposite-amounts-only confirm left both rows unlinkable, and the obvious repair (post the full payment to the loan, then book interest out of it) writes a liability number no bank reported, so the loan register stops mirroring its bank line for line. Keeping both bank legs exact and letting the category absorb the difference is the only shape where every real-account posting is something a bank said, and zero-sum then holds by construction rather than by a check. Bounds are the whole safety story: without them a −50.00 leg against +650.12 booked −600.12 of "interest", and because a merge is balance-neutral on the bank accounts, §7.3's drift check cannot catch a wrong one — hence explicit-only and write scope, as for every merge a human names (decision 49). Classification moves from leg counts to shapes because #245's multi-posting templates will write rows with two real accounts and several categories: "two real legs" would call a paycheck a transfer and hide its deductions behind `[account]`, so only the shapes Purser itself writes qualify, and everything else is shown as what it is. The matcher keeps its structural "two real legs" pool (decision 56) on purpose: a loan-payment schedule must settle against the split through the cash leg. |
| 63 | **Balance-history extremes are end-of-day; intra-day ordering never creates one** (issue #272, §3.5/§10; amends 51): `balances.window_extremes` ranks the END-OF-DAY balance of every date with activity in the window — the date's last stored row in decision 37's total order (`DISTINCT ON (date)` over the in-effect order), which is exactly the value `balance_series` reports for that date — and returns the min and max, earliest date on a tie, null only when the window holds no activity. That is what MCP `balance_history` reports as `low`/`high`, and so the low its headline names and the balance-history widget marks. The per-change resolution (`change_points` behind `GET /accounts/{id}/balance/series` and `purser://balance-history/…`) keeps every intra-day row unchanged. | Within one day the store orders rows by when they were recorded (`created_at`, then the id), not by when the bank processed them, so ranking per-transaction balances reported bookkeeping order as history: a paycheck imported as a gross pay line ahead of its same-day withholding and deductions showed a high the account never closed at, and a duplicate charge ahead of its same-day reversal showed a low it never closed at either. End-of-day is the grain the sampled series already speaks — every point is a date's closing balance — and the grain `forecast_balance` already uses for its projected low, so an extreme is now always a value the daily line actually passes through and history and forecast agree on what a low is. Decision 51's guarantee is kept: every active day is ranked, not only the sampled ones, so downsampling still cannot hide an extreme. The intra-day steps stay where they are honest — the full per-change series, where each point carries its own drill target. |
| 64 | **The drift basis is a per-account property learned from sync history, not a per-sync smaller-\|delta\| pick** (issue #283, §7.3): over the account's last 20 observations (this sync plus the newest 19 `connection.sync` rows), the basis whose delta (reported minus that ledger sum, recomputed from the stored `reported`/`posted`/`pending`) changed on fewer consecutive-sync steps wins; equal counts keep the previous observation's `basis`; none recorded → smaller \|delta\|, tie → pending-inclusive; the choice is recorded as `basis` on every observation block (no schema change); cleanliness stays either-basis; the event's `mode`, `whats_new` drift lines (`basis`, present only when recorded) and `GET /connections/{id}/discovered` (`drift_basis`) report it; a per-account pin is deferred (it needs an `accounts` column; added by decision 83) | Found in the #208 investigation: a card issuer whose balance leaves pendings out held a constant posted-only gap across hundreds of syncs, but the per-sync rule picked pending-inclusive whenever the pending total made that delta smaller, so the reported delta moved with every pending charge and opened a new `balance.drift` episode each time. Whether a bank counts pendings is a property of the bank, so it belongs to the account and is learned from history the audit memory already keeps — decided on the first sync after upgrade, no migration. Stability is counted in changes, not distinct values, because changes are what open episodes (a recurring hold that comes and goes has two values but a change every sync). Ties defer to the previous basis so the label never flips without evidence; either-basis cleanliness means a basis guess can never manufacture a drift on an account whose other basis matches. |
| 65 | **A transfer schedule's instance is a pair signal, and a settlement follows its bank line into a merge** (issue #270, §4/§5/§6; amends 56): an instance of an active schedule with a `counterparty_account_id` **covers** a pair when it names the same two accounts in the same direction, exactly the pair's amount, and both legs sit inside the ±3-day transfer window of its date; it then replaces the name-signal test in the standard stage's pair phase of `transfers.plan_auto_links` like a `transfer_link` rule (absorption and decision 66's widened stage unchanged), vouching while open or for a pair holding the row that settles it; `transfer.auto_linked` and its audit row carry `signal` — `rule:<id>`, `name` or `schedule:<schedule_id>`, the first that applies, so a covering declaration is credited first as in the absorb phase and the projected count — and a merge an open instance covers, in either stage, settles it at once through the ordinary matcher. A one-sided bank row whose live match (proposed or confirmed) is to a plain expected transfer, on one of its sides in the same direction, is **held**: it stays in the one pairing pool (`matching.transfer_pool` — both pair phases of the plan and so `project_links`, transfer candidates, and the conversion counterpart) but pairs only with a row on that expected's other account and never with another held row (`MatchInput.pair_account_id`, checked inside `detect_transfer_pairs` beside decision 66's per-pair window), and it is never absorbed. `_merge_transfer_pair` carries the originals' matches on every path: a confirmed match the merge covers moves to it (`posted_id` and the expected's `superseded_by`, audited `match.carry`), a proposal is rescinded and its expected re-matched at once, any other confirmed match stays as decided. Unlink resurrects only merged/absorbed originals — never an expected the transfer settles — and releases what a voided merge settled (`matching.release_settlement`, the body of `unmatch`); `has_linked_originals` and the register's linked flag use the same test. `schedules.matched_actuals` reads one amount per settling row, from the schedule account's side. No schema change. | The nightly job matches before it links, so when the two bank legs of a scheduled transfer post on different days — about half of real linked transfers post 1–3 days apart — the first leg was proposed (exact amount and date alone score 0.55) or auto-matched against the two-leg instance, matched rows left the transfer pool, and the counterpart never linked: the review of #253 reproduced 6/20 + 6/21 legs → 0 links, 0 candidates, leaving the far side as income or spending. Making the matcher wait for both legs would need a clock and a guess about whether the far side has a feed at all (a 529 without one is settled by its single leg for good), so matching stays greedy — decision 56's "a one-leg import may settle a two-leg instance" — and pairing learns to see held legs instead; that is order-independent: whichever leg arrives first and whenever the matcher runs, the pair links once both are present. Moving a confirmed match rather than unmatch-and-rematch keeps one settlement per slot ("settled exactly once"), and once the merge is matched no second expected can claim it; a proposal is re-decided by the matcher rather than special-cased. The pair constraint lives inside the greedy loop for #173's reason — a post-filter silently loses the true counterpart. Unlike a `transfer_link` rule the schedule signal needs no activation gate: it cannot sweep history, because open instances exist only on the horizon or overdue, a settled one vouches only for the row that settled it, and each vouches once. Exact amount keeps estimated instances out (their bank amount differs); those link by name, by rule, or by confirm. The schedule signal keeps the ±3-day window; decision 66 widens declared pairs only. Unlinking a settled merge used to resurrect the instance as a posted transfer, a phantom second copy of the money that also defeated the pair's suppression; a void row settles nothing, so the instance is due again. And a merged settlement has two real legs: reading both fed an estimate an opposite pair per payment — `last` could flip a transfer's direction and a rolling average fell to zero, which stops materialization. |
| 66 | **A `transfer_link` rule sets the date window of the pair it declares — and only that pair — and a widened window acts in a second stage that never changes what the standard window decides** (issue #210, migration 0029, §3.1/§5/§10; amends 46): `rules.window_days` (INTEGER; NULL = the global ±3-day `TRANSFER_WINDOW_DAYS`, else 0–14 = `TRANSFER_WINDOW_MAX_DAYS`; `transfer_link` only — the service refuses anything else with a `RuleError`, 422 at REST (a strict integer) and an error at MCP, and the `rules_window_shape` CHECK stands behind both) is the most days apart the declared pair's two legs may post and still link; it composes with `amount_exact`/`effective_from`, and overlapping rules on one pair take the widest window. `auto_link_pass` executes one planner, `transfers.plan_auto_links`: the absorb and pair phases first run over ±3 days exactly as before (a declared pair links there without a name signal only when its rule's window covers the gap, so 0–2 narrows the pair), then a widened rule pairs its accounts farther apart among the rows the standard stage left without any partner, and absorbs only into open legs it left unclaimed; the pool is ordered (date, created_at, id). The activation confirm counts that same plan with the rule declared first — every merge and absorb it justifies — so the projected count is the linked count; `preview_rule` (`pair_accounts`) and `POST /rules/preview` (`pair_account_a`/`pair_account_b`) return that count and a sample before a rule exists. An active `transfer_link` rule's pairing scope (pair, `amount_exact`, `effective_from`, `window_days`) is frozen: an edit is refused until the rule is disabled, and re-activating re-confirms. The rule's audit rows carry its `window_days`. | Option 1 of #210, the preference of #174's implementation-input comment: a declared pair is a user-declared boundary — the argument that already dropped the signal requirement — and a window on it is as deterministic and auditable as the pair itself; the motivating monthly card autopay posted its legs four days apart, and a declaration that still leaves it unlinked reads as a broken rule. Option 2 (state the fixed window) kept the window as the bound on a false pairing, but that bound belongs with the declaration, which the activation confirm already guards. The cap is 14 days: two calendar weeks covers slow ACH and holiday settlement, and it is at most half the shortest month, so a monthly pair's leg can be in window of only one occurrence of its counterpart; past that, an exact-amount pairing no longer identifies a transfer and becomes a judgement call, which belongs to an explicit `confirm_transfer`. Two stages rather than one wider greedy pass: in a single pass a declared row up to 14 days away could take an out-row that the standard window links by name signal to a same-day counterpart, so the global window would no longer be unchanged for everything else; restricted to the rows a pass leaves without any partner, a widened window only adds links to that pass. Counting through the pass's own plan rather than a parallel re-implementation keeps preview = commit as the selection grows — decision 46's projection counted merges only, while a declared pair also absorbs. Freezing an active rule closes the route around the confirm (activate at 3 days, then widen to 14). |
| 67 | **A manual transfer confirm proposes a payee-scoped `transfer_link` rule in the suggestions inbox; Purser keeps no hidden memory of confirmed transfers** (issue #271, migration 0030, §3.1/§5/§6/§10; amends 46 and 57): an ordinary `confirm_transfer` (REST and MCP — a split proposes nothing) upserts ONE suggestion of the new kind `transfer_link` (`source = confirm`, no confidence), fingerprinted on the unordered account pair plus the scope payee — the outgoing row's payee, else the incoming one's, resolved through its aliases; no payee, no suggestion — so repeat confirms refresh one open row (`seen_count`; evidence = the confirmed pairs) and the confirm response carries its `suggestion_id`. Its typed `TransferLinkDraft` is the rule: the pair, `payee_id`, and `amount_exact`/`window_days`/`effective_from` unset unless edited (the only editable fields). `rules.payee_scope_id` (`transfer_link` only, CHECK `rules_payee_scope_shape`) narrows a declared pair to rows where either leg's payee is in the scope payee's **alias group**, resolved when the rule loads; it is part of the frozen pairing scope, the activation projection and the rule's audit shape, and `RuleOut`/`list_rules` show it as `payee_scope`. `suggestions.decide` dispatches on `kind` with a typed draft per kind (`ScheduleDraft`, `TransferLinkDraft`): a `transfer_link` accept creates the rule as a **draft** through `rules.create` and returns `project_links` for it — it activates only through `rules.set_state` with that count confirmed. Rejection is remembered (`exact` = the pair and payee group, `counterparty` = the pair, any payee), compared by alias group so a later rename cannot reopen it; a `transfer_link` rule on the pair covering that payee group (draft, active or disabled; archived is deleted) suppresses a proposal; snooze is a date. Unlink touches neither suggestion nor rule. The deprecated schedule wrappers decide schedule suggestions only; forecast coverage and `list_recurring` count schedule suggestions only. | Review D of #253: the hidden `transfer_payee_memory` learned from one confirm, applied retroactively through the nightly sweep without decision 46's projected-count gate (which exists because a merge is balance-neutral and §7.3 cannot see a bad one), was never unlearned on unlink, and stopped matching once a rename changed the payee. A suggestion is visible, reversible and gated: nothing links until a human accepts the draft and confirms its projected count, a rejection is durable, and an unlink has nothing to unlearn. Rules stay the only automatic mapping (decision 46) — the inbox only proposes one. The scope is a payee id matched by alias group, not a text predicate: payee identity is already the blind-indexed entity that rename aliasing consolidates, and a predicate on the raw descriptor stops matching rows a rename rewrote. Its reach is aliasing's: a payee is merged only by a rename's apply-history, which sees live rows, so a payee found only on merged transfer originals, or renamed only going forward, keeps its own group — a confirm under the new name then proposes a rule for it (§14). One payee rather than both legs' keeps the fingerprint a single identity, and either leg may carry it. A whole-pair rule from one confirm would be the broad claim the review objected to, so a payee-less pair proposes nothing; an unordered fingerprint follows the unordered rule it drafts. `amount_exact` starts unset because one confirm says who a transfer is with, not how much it will be. Transfer candidates stay out of the inbox (#246 phase 2); proposed matches are cards since decision 86. |
| 68 | **Identical rows in one payload are distinct identities: an occurrence ordinal joins the content hash** (issue #275, §7.1/§7.2): within one payload — one account's rows in a SimpleFIN fetch, one QIF/CSV file — the n-th row whose occurrence-1 hash equals an earlier row's is occurrence n, hashed with `#n` appended to the canonical string (`core.occurrence_part`; the abs()-based transfer hash takes the same suffix); occurrence 1 appends nothing, so no stored hash changes and nothing is migrated; rows are numbered in the order the importers already claimed hashes — the feed per account in payload order, pending or not; files with flat rows and opening balances on one count, each file account's k-th leg of one transfer identity joining transfer k, and a leg's flat hash counted among its own transfer's legs only; the feed's import loop and its vanished-pending sweep read one per-payload hash list; the masked-reference guard (decision 44) is unchanged. No new count, field or migration. | Two identical same-day charges from one merchant are ordinary (two coffees, two fares), and content identity merged them: the #208 investigation found live card accounts short by exactly such charges, and a replay could not bring them back because it re-imports with the same hashes. An ordinal keeps identity content-derived — SimpleFIN ids are unstable (§7.1) — and numbering within a payload is what keeps overlapping windows idempotent: identical rows share a date, so a fetch carries all of them or, cut mid-day, a prefix whose ordinals already exist, and per identity the ledger converges on the largest count any one payload carried. Occurrence 1 hashing exactly as before is the compatibility contract: the first sync after upgrade re-fetches its 7-day overlap and must land on the stored hashes, so the only change it makes is the dropped twins inside that window. Older dropped twins are not recovered automatically: a replay of the archive would land them, but it would land old fetches' pending twins as well, and a replay never sweeps pendings (`window_start=None`), so that recovery stays a deliberate per-account fix (#208). Numbering follows the old claim order so the row that already holds the unqualified hash keeps it. That is also why a leg's flat hash is not counted across transfers: which transfer claimed the unqualified hash depended on file layout and on which transfers split, so a wider count could renumber a leg whose one-leg row already exists. A same-day round trip between two accounts becomes two transfers for the same reason two coffees become two rows — the transfer hash ignores direction to stay inversion-neutral (decision 35), so the way back used to merge into the way there. Residual: when identical rows in one fetch differ in pending status and the bank reorders them between fetches, the sweep can void the pending early, superseded by its posted twin (same balance); a pending that posts under an unchanged identity is #282's. |
| 69 | **A settlement follows its bank row through the pending lifecycle, and an estimate learns only from posted chain heads** (issue #273, §4/§5/§6; no schema change): the reconciler captures the live match on a pending BEFORE voiding it (`reconcile.void_pending`), then `matching.settle_voided_pending` carries a confirmed match to the posted successor when no other confirmed match holds it (`matches.posted_id` and the expected's `superseded_by`, `match.carry` with reason `pending_posted`; a proposal on the successor yields and is re-decided at once), rescinds a proposal (`match.rescind`, re-decided at once when the pending posted), or releases the confirmed match through decision 65's `release_settlement` (`match.unmatch` with reason `pending_vanished`/`successor_matched`; `keep_pin` carries the instance's pin onto the clone); `matched_actuals` follows each confirmed match's bank row to its lineage head (`superseded_by`, then the `reverses_id` correction chain, walked set-wise), counts only a `posted` head, reads it as one amount from the schedule account's side (decision 65) and counts a head two matches reach once; the estimate probe brackets the carry, so `recurring.price_changed` fires when the posted amount lands, and it brackets a manual pending → posted transition the same way (#330) and a transfer absorb — `transfers._absorb_row`, shared by the auto-link pass and an explicit conversion, probes the transfer and the absorbed row together (#343); a manual void of a matched pending (`transition_status` pending → void) settles its match the same way with no successor — a proposal rescinded and not re-decided, a confirmed match released with `keep_pin` — with reason `pending_voided` and the voiding token as actor (`settle_voided_pending(actor_token_id=, reason=)`, #343) | The matcher pairs expecteds with pending rows, but the reconciler voided a vanished pending without looking at its match: the instance stayed void, "settled" by a dead row, so the bill never came back, never went late and stayed out of every forecast — a dropped payment read as a paid bill. `matched_actuals` meanwhile summed whatever row a match named, whatever its status: a voided pending still fed the estimate, a pending's provisional amount moved it, and a superseded or corrected row was read instead of its successor or replacement. Carrying keeps a settled bill settled with no human step when the bank merely rehashed the row — the same rule decision 65 applies when a merge consumes a matched row; releasing rather than resurrecting keeps void terminal and lets the next matching pass find the real posting (a tip, a different amount) through the normal tiers, and the release goes through the one `release_settlement` that unmatch and unlink share. The pin is kept on these paths because nothing the user decided about the instance changed: the feed's release is automatic, and a manual void is a decision about the bank row — refusing it until the match is undone would make the person do by hand what the feed does on its own. An absorb moves a head exactly as a manual post does, so it takes the same probe. Following lineage rather than filtering on the named row matters because post-sync categorization corrects every posted feed row that lands uncategorized (a reversal plus a replacement): a filter would have dropped each of those actuals. |
| 70 | **Every open pending is re-checked on every sync, and the bank posting a pending under its own identity supersedes it** (issue #282, §4/§7.1; no schema change): the live fetch starts at min(resync overlap, oldest open import pending on the connection's accounts − `PENDING_MATCH_WINDOW_DAYS`; postings-balance accounts only since decision 74), never before `today − BRIDGE_MAX_RANGE_DAYS` (an older pending widens nothing and is left as it is); the reconciler pairs a vanished pending with its posted successor (same account, exact amount, ±7 days) where only another feed pending's `superseded_by` counts as a claim; a posted payload row whose identity — the occurrence-qualified hash `_row_hashes` gives it (decision 68) — an open import pending on the same account holds moves the hash to a new posted row and supersedes the pending via the same `void_pending` path (audit `identity_moved`, match follows per decision 69) on every import path; `pending.vanished` feeds a `whats_new` section `pendings_vanished` (count, material lines largest first, cap 3); a vanished void is never revived | Found in the #208 investigation: the feed re-fetched only the last 7 days, so a pending older than that was never looked at again — card accounts carried pendings for weeks after the bank had posted or dropped them, double-counting against their posted rows. Some posted rows never landed at all: one dated before the overlap was never fetched, and one whose date, amount and description equalled its pending's was skipped as a duplicate of that pending — which the reconciler then voided as vanished, losing a refund. Widening the one live fetch is budget-neutral (a second fetch would break the hourly preset's 24/day), lasts only while an old pending is open on a postings-balance account, and is bounded so a pending the bridge can no longer serve cannot make every sync maximal. The account scope is decision 74's: a brokerage feed that lists every fund purchase as pending forever made the widening permanent, so every sync archived a maximal payload and re-fetched a 90-day tail in which a re-described posted row landed as a duplicate. Moving the identity instead of promoting the pending in place keeps ONE pending→posted shape (a new posted row with the bank's values, the pending superseded) whatever the hash does, so posted amount wins and matches re-point the same way. The claim check was over-broad: a bill matched to the posted row also points `superseded_by` at it, and that hid the successor of a pending the bank had in fact posted. Reviving a vanished void on a later sighting was rejected: void is terminal, and a revival would race the manual repairs of already-lost rows; the digest line is how such a row is noticed. |
| 71 | **A valuation is stored in ledger sign, and a positive figure on a liability is refused, never guessed** (issue #276, §8): `networth.record_valuation` — the one write path for manual valuations (REST `POST /accounts/{id}/valuations`, and MCP `record_valuation` since decision 73) — takes `convention` = `ledger` (default) \| `statement`; `statement` is the figure as printed, converted by reconciliation's own `to_ledger_sign` over its `LIABILITY_TYPES` (`credit_card`, `loan` negated, every other type unchanged); in `ledger`, a positive value on a liability is 422 with a hint naming both corrections (the negative figure, or the same figure with `convention: "statement"`); zero and every asset figure pass unchanged; the stored row, the response, the audit row and the `valuation.recorded` event carry the ledger-sign figure. The SimpleFIN path is unchanged: it stores the feed's reported balance, already in ledger sign (§7.3). | Every reader of `valuations.value` — `net_worth_at`, the decision-42 overlay on every account surface, the per-account balance series, and the pay-in-full schedule basis, which counts only a negative balance as owed — assumes ledger sign, so `+250000` on a mortgage silently made it an asset and stopped its pay-in-full instances. Reconciliation (decision 61) takes statement convention as its only input because a statement figure is all it ever receives; this endpoint shipped in ledger sign and callers already send negative liability figures, so switching its default to the statement convention would flip each of them into the same bug in reverse, silently. Refusing only the ambiguous case — a positive liability figure with no convention named — keeps those callers working, gives a statement figure an explicit path converted by the very function reconciliation uses (so one statement means one ledger figure everywhere), and never guesses a sign. A negative statement figure on a liability is a credit balance and is stored positive, exactly as a reconciliation reads it. |
| 72 | **Retired-origin rows are purged by creation batch, and a real account moves with its subtree** (issue #284, §3.4/§7; extends decisions 34/45): `purser admin purge-retired-rows <account>` lists the account's `origin='import'` rows with no `source_fetch_id` grouped by `created_at`, each batch labelled with the change-audit actions recorded at that instant; `--batch <created_at>` (repeatable) previews counts and a sample; `--apply` voids `expected`/`pending` rows through `transition_status` (system actor), hard-deletes `posted` rows through the shared guarded delete and leaves `void` rows; a live match or a live transfer holding a batch row refuses unless `--unlink` (proposals rejected, confirmed matches unmatched, transfers unlinked and the resurrected clone purged by the content hash that moved to it); corrected rows, rows posting to another real account and corrected transfers always refuse; preview, plan and executed counts must agree or nothing commits; audited `account.purge_retired_rows`, evented `account.retired_rows_purged`. The shared guarded delete (delete-account, purge-feed-rows, this command) removes a deleted row's review marks first. `PATCH /accounts/{id}` takes `path`: validated by `validate_path` (#214), the subtree re-rooted through a staging root with relative paths kept and `parent_id` re-derived, 409 when any account outside the subtree sits at or under the destination, 422 for a destination under a category root, a move under itself or a subtree holding a category; open suggestion drafts re-pointed; write scope; audited `account.move`, evented `account.moved`; no balance mark. | **Batches.** `origin='import'` with a NULL source matches every QIF/CSV row too (decision 53 keeps theirs NULL on purpose), and the Direct rows carry no other marker — 0010 purged the captures that would have named them. A transaction timestamp is the one structural identity a batch has: every row an import committed shares it and no other import does. Listing batches with their audit actions makes "which rows are these" an operator decision taken against evidence, not a heuristic deciding for them (decision 40's stance). **Removal follows the ledger's rules:** a pending row is freely editable, so the normal void path suffices and keeps lineage; a posted row has no void path (§4), and a duplicate that must go is decision 34's case for the guarded delete; a void row already moves no money. **Links are undone explicitly or not at all** — the unlink-first precedent of decisions 34/45: a match or a merge records a decision about the row, and undoing it has a defined result (the bill is expected again; the counterpart's bank line returns to its account). A correction cannot be undone that way: its reversal negates the original, so deleting the original alone would leave a standalone reversal unbalancing the account, and whether the replacement is also a duplicate is a judgement the tool must not make — so it refuses and names the rows. **Review marks:** migration 0021 backfilled a mark onto every row that existed and the foreign key has no cascade, so the guarded delete failed on every reviewed row — every row older than 0.14 among them; a mark is an annotation about a row (decision 47) and goes with it. **Moves.** A path is a label in a tree while the ledger stores ids, so a move is a relabel: no history or balance changes, hence no mark. The one semantic boundary in the tree is the category roots — `<@` reads there (category rollups, the register's category filter; spending summaries already filter `is_virtual`) would count a real account's postings as category totals. Real roots carry none: net worth classifies by sign and behaviour keys off `type`, so `assets` ↔ `liabilities` is allowed — it is exactly #284's repair, a card at the ltree root moving under `liabilities.`. An occupied destination is a state conflict (409, like decision 61's); requiring an empty destination subtree means a move never silently adopts other accounts as children. Categories stay fixed because schedule templates store their paths (§3.2). The staging root exists because `accounts_path_uniq` is checked row by row. Open suggestion drafts are the one place a real account's path is stored as live input; without the re-point an accept would miss the account or land on whatever later took the old path. |
| 73 | **Agents record valuations through MCP `record_valuation`, on the one write path** (issue #323, §8/§10; amends 71's "no MCP tool writes valuations"): a write-scoped tool (`audited_tool(scope="commit")`, so a call row `mcp.record_valuation` joins the service's `valuation.create` change row and `valuation.recorded` event under one request id) taking `account` (UUID or path), `as_of` (ISO date), `value` (decimal string) and `convention` = `ledger` (default) \| `statement`; it calls `networth.record_valuation` with `source='manual'`, so decision 71's sign rule and its refusal reach the agent word for word; the service now also refuses a value with more than 4 decimal places or outside NUMERIC(20,4) (`value has more than 4 decimal places`, `value is out of range`), which REST's request model already refused before it; the answer carries the stored ledger-sign figure; no `source` parameter and no net-worth watermark kick. The reconcile how-to's loan advice, and reconciliation's own refusal of a loan or property account, name the tool. | `how-to/reconcile-an-account.md` tells agents that loan and property balances are valuations, which reconciliation cannot set, and to record a valuation instead, but only REST could, so an agent following its own guide had no move left except a balance-adjusting transaction, which a loan's valuation overlay hides (decision 42). A tool on the existing write path, rather than rewording the how-to into "ask a human", keeps the advice true for agents and humans alike, and adds no second implementation of the sign rule. Commit, not propose: a valuation has no pending state or review queue — it replaces the account's balance on every surface and in net worth the moment it lands — so a propose-scoped write would have nowhere safe to land, exactly like `reconcile_account`. `statement` exists because agents read figures off statements, and reusing decision 71's conversion means one statement figure becomes one ledger figure whether it is reconciled or valued. The checks moved into the service because MCP takes a decimal string with no request model: Postgres would have rounded a fifth decimal place away silently (guardrail 1) and overflowed into a raw SQL error. `source` stays `manual`: `api` is the feed's, and a `source='statement'` parameter beside `convention='statement'` invites confusing provenance with sign. No watermark kick, like every MCP write: stored net-worth rows at or after the dirty cursor are never served, so readers compute live until the nightly job re-materializes (§8). |
| 74 | **Only postings-balance accounts widen a sync, and the widened tail skips re-described copies of rows the ledger holds** (issue #331, §4/§7.1/§7.3; amends decision 70; no schema change): `_open_pendings_start` counts only open import pendings on checking, savings, cash and credit-card accounts (`drift.ELIGIBLE_TYPES`, the drift check's postings-balance set); `import_fetch(tail_before=…)` starts a payload's tail: the sync passes the start its resync overlap would have had, to the live fetch and its gap windows, and every other import of a fetch (backfill, map re-import, replay) takes `fetched_at − RESYNC_OVERLAP_DAYS`; a posted tail row on a hash miss is skipped when its account holds, from an earlier fetch, a posted feed row on that date for the exact ledger amount whose `normalize_payee` description differs and which the payload lists neither under its own identity nor as a masked re-delivery (decision 44), one skip per such original, unless an open import pending the payload no longer lists may take the row as its successor (same amount, ±`PENDING_MATCH_WINDOW_DAYS`); counted `skipped_redescribed` in the sync and backfill results (`SyncOut`, `BackfillOut`) and the replay summary; the `connection.sync` audit row records `overlap_start` and, under `redescribed`, each skip's account, date, amount and `original_id` | Found the day decision 70 deployed. One brokerage feed (a 401(k)) lists every fund purchase as pending forever, so an open pending always sat near the edge of the bridge's 90-day range and every sync fetched the maximum. Each sync re-fetched a 90-day tail, and a card credit the bank had re-described weeks after it posted landed as a duplicate, because the description is part of the identity. **Scope:** a pending on an account valued by holdings or valuations moves no balance, so re-checking it is not worth a permanent maximal fetch; it is still settled whenever the window covers it. **Guard:** it keys on what a re-description leaves behind, the original's identity gone from a payload that covers its date and a new identity on that date for that amount, not on how alike the two descriptions are (decision 40 keeps fuzzy matching out). A late-posted row has no such original, a second purchase posting late beside one the bank still lists is kept, and a masked re-delivery still lists its original. One original per skip keeps a re-described row and a distinct late row on one day from cancelling each other. Rows inside the overlap are left as they were: that is where fresh same-day rows arrive (decision 44's distinct transfer), and this issue is about what only the widening reaches. A row a vanishing pending may take is kept because #282's successor pairing must see it; the cost is the occasional duplicate where a re-described row and a same-amount pending meet in one sync. **Every path:** a guard only the sync applied would let map re-import and replay land exactly the copies the sync skipped, breaking guardrail 3 across paths. A boundary derived from the fetch is never earlier than the sync's own, so any re-import skips at least what the sync skipped. Residual: a line the ledger holds only inside a merged transfer has void leg rows, which are no neighbour, so its re-described copy imports as before. |
| 75 | **A token revokes itself; a session token manages its own user's tokens; admin scope manages any; agent tokens see and revoke only themselves** (issue #355, §9; no schema change): `DELETE /tokens/{id}` passes when the target is the caller, when the caller holds `admin`, or when the caller is a `session` token of the target's user (`admin/service.may_manage_token`); `GET /tokens` lists the caller's whole user for a session or admin-scoped token and only the caller's own row for any other token (`manages_user_tokens`); a refused id gets the same `404 token not found` as an unknown one. The admin listing is otherwise unchanged — still the caller's own user, with no cross-user list parameter. | Every agent token is minted under the admin's own user (decision 52), so the old same-user rule let any token — a read-only one included — enumerate and revoke the admin's browser session and every sibling agent: no escalation, but a write performed by a read-scoped token, and one leaked credential could lock out every agent and the admin's sign-in. Keying the user-wide right on `kind` rather than on a scope keeps the Tokens page working for non-admin household members, whose sessions carry no `admin`: a person signed in owns their user's tokens, an agent acting under that user does not. `kind` is server-controlled (only the login paths mint `session`), so a client cannot claim it. Self-revocation stays open to every token so an agent can always retire its own credential, and the identical 404 means a refused probe cannot confirm that a token id exists. |
| 76 | **An open match proposal settles its instance pending confirmation: the forecast leaves it out and counts it, and `match` confirms a same-pair proposal** (issue #352, §5/§6/§10; no schema change): `matching.proposed_settlements(session, account_id, expected_ids)` maps each expected with a live `matches.state = proposed` row to its bank row when that row is pending or posted with `superseded_by IS NULL` and, given an account, posts to it and the account's opening balance is built from postings (a valuation account settles nothing; `account_id = None` accepts any live bank row); `forecast_account` leaves those instances out of the items — overdue and due-within-the-window alike, so the line is what confirming would produce — and reports `coverage.matches_pending` (always) and `matches_pending_net` (a Decimal, present only when nonzero, serialized as a decimal string by MCP and the resource and as `Decimal | null` by REST), with a headline clause pointing at `list_unmatched`, whose expected rows (MCP, and REST `UnmatchedOut`) carry `proposed_posted_id` — the open proposal's bank row, the posted id `match` takes, not the match id; MCP omits the key when unset; the digest's upcoming list applies the same helper; `manual_match` (REST `/matching/match`, MCP `match`) confirms the bank row's live proposal when it names the same expected and refuses one for a different expected with an error naming it; projected slots are unaffected (the instance still fills its slot); the `matches` row is the source of truth, never the suggestions inbox; no auto-confirm is added (the AUTO tier already confirms at ≥ 0.80) | The opening balance already sums every live pending and posted row, and a proposal's bank row is exactly such a row, so forecasting its expected too subtracted the money twice (a connected agent observed an understated forecast, and the nightly shortfall scan read the same line). Leaving the instance out rather than keeping a zero-amount event keeps the events array the chart; counting it keeps the omission honest. Agents had no way to decide a proposal over MCP, so the same-pair `match` is what makes the coverage hint actionable. A rejected pair could be proposed again by the next nightly pass until #246 added rejection memory (decision 86). |
| 77 | **Series are readable as a tool, forecast events are fitted to the budget, and one function bounds the horizon** (issue #354, §6/§10; no schema change): a read-scoped MCP tool `read_series(uri)` (`audited_tool(kind="read", entity="series")`, defined in `purser/mcp/resources.py` beside the resources it mirrors) accepts `purser://` URIs only — `ui://` and every other scheme are refused — reads them through FastMCP's own `read_resource`, and returns the resource body byte for byte as the single text content of a `CallToolResult` with no `structuredContent`; like the resources it is exempt from the response budget, bounded by their caps plus a hard ceiling of 200,000 characters (`_READ_SERIES_MAX_CHARS`), above which it refuses with a hint to narrow the window; `forecast_balance` replaces its ten-event cap with `_fit_events`: the low's event first, then qualifying events (`materiality`, a decimal string: \|amount\| ≥ it) by \|amount\| descending, ties by date, added while the serialized payload stays within 1,900 characters (`_FORECAST_BUDGET_CHARS`) and presented in date order; `truncated` is true only when the budget left out a qualifying event; `omitted {count, net}` is present whenever an event is not listed, for either reason, and `end_balance` always, so `start_balance + Σ events.amount + omitted.net = end_balance`; the headline names the omitted count and net and points at `read_series(daily_uri)`; `schedules.validate_horizon` (1..`FORECAST_MAX_DAYS` = 365) is the one horizon check — the tool, the `purser://forecast` resource (which used to accept 366) and `forecast_account` call it and REST bounds `days` by the same constant — so every `daily_uri` the tool hands out resolves; the resource also carries `end_balance` and `low`; widgets read a series through `resources/read` and fall back to `read_series` | Gateways that forward tools but not resources (the ContextForge proxy) left `daily_uri`, `series_uri` and the widgets' underlays unreachable, and a count cap hid a 2,123.52 card payment behind ten larger moves while saying nothing about what it dropped. A separate tool rather than an inline series keeps the widget contract inside the budget — Apps hosts hand `structuredContent` to the widget and the model alike — reuses the resources' serializers so the two cannot drift, fixes all three series URIs at once, and keeps the budget exemption in one documented place. The fitter measures what the budget actually limits, characters, so long names cost events instead of overflowing, and `omitted`/`end_balance` make what is not itemized explicit. "One call" becomes two (`forecast_balance`, then `read_series(daily_uri)`) by design. An unvalidated `days` crashed below 1 and handed out unresolvable URIs above 366. |
| 78 | **An expected or pending transfer takes an amount edit in place, from a chosen leg, with its direction fixed; bank-backed, split and posted transfers are refused** (issue #350, §3.3/§4/§6; amends 56; no schema change): `ledger.update_transaction` dispatches an amount edit of a transfer to `transfers.set_amount_in_place` — `amount` is the leg of the side `account` names (`TransactionUpdate.account`, its second meaning), else for a schedule instance its schedule's `account_id`, else the edit is refused ("name the side whose leg amount is"); that leg takes `core.quantize(amount)` (nonzero) and the other leg its negation; a sign that differs from the current leg is refused, quoting the leg, and nothing changes; every check runs before a posting, date or label moves; a SPLIT transfer ("unlink it and confirm again"), a transfer with linked originals ("unlink it first") and any edit of a POSTED transfer (`transfers.posted_edit_refusal`: unlink when it carries bank rows, else convert it back to a category) are refused with messages that say so; pin, review mark, `balances.mark_dirty` for a pending row and the `transaction.update` audit (both legs, before and after) are unchanged. MCP `override_instance` reads its amount as the schedule account's leg and returns `amount`; the SPA's override forms send only the fields that changed, the register adding `account` = its own account. | Decision 56 and §6 promise that the statement case is an override of the instance, but `_flat_legs` assumed one real leg plus one category leg, so every amount edit of a two-leg instance failed with "transfers cannot be recategorized" — and the SPA prefilled and always sent the amount, so even a date-only override of a card payment failed there. An amount means nothing without a perspective (decision 56), hence the chosen leg; a schedule's own account is the perspective its instances are listed and estimated in, so it is the default. Refusing a direction flip rather than taking the absolute value keeps a typo from silently becoming its opposite: the error quotes the leg, and a real reversal is a different transfer. Bank-backed and split transfers keep their bank amounts by construction (a split's legs are deliberately unequal), so the honest correction is an unlink, as for recategorizing them (§3.3); a pending transfer with no bank rows (a propose-scoped manual transfer) is as freely editable as a flat pending row. Overriding to the statement amount is also what lets the instance vouch for its bank pair, because decision 65 needs the exact amount. |
| 79 | **Instance labels follow the rule; only amount, date and category edits pin; `reset_instance` hands an instance back** (issue #353, §4/§6/§10; amends 55; no schema change): `update_schedule` runs `_relabel_instances` after its resync whenever the name or the rule payee (`_rule_payee_name`: the schedule's payee, else a flat schedule's name, none for a transfer) actually changes, whatever the status — every live instance, pinned ones included, whose description equals the old name takes the new one, and a flat instance whose payee normalizes to the old rule payee is re-pointed to the new one; it never touches the pin, amounts, dates or slots, writes one `schedule.relabel` row (before `{name, payee}`, after `{name, payee, instances}`) and nothing when no instance changed; MCP `update_schedule` returns `relabeled`. `ledger.update_transaction` pins a schedule instance only for an amount, date or category edit — description and payee are labels. `schedules.reset_instance` (REST `POST /transactions/{id}/reset-instance`, MCP `reset_instance`, both write scope; a Reset button on pinned rows in the Schedules page) takes a live instance of an active schedule, clears the pin and sets the date to `occurrence_date`, the description to the name, the payee to the rule payee, a flat instance's category to the rule's and the amount to `instance_amount` through `_set_instance_amount` (both legs of a transfer); a slot outside `_rule_slots` or a zero estimate voids and releases the instance (`_void_and_release(reason="reset")`), and an instance whose legs no longer have the rule's shape (a pinned survivor of a reshape) is released and its slot re-materialized; audited `schedule.instance_reset`. | Decision 55's "the rule never touches it again" made a schedule rename stop at the instances: they kept the old name in `forecast_balance` for the materialized ~60 days (projected slots already used the live name), and a payee-less bill kept it in `whats_new`, which labels by payee first. Relabelling by hand made it worse, because every token-bearing edit pinned, so the relabelled instances stopped re-estimating, and nothing could ever clear a pin. A label is not an override — it changes no amount, date or slot the rule's refresh would undo — so labels follow the rule, and pinned instances too, or every amount-overridden card payment would keep the old name; comparing with the old value is what tells the rule's label from one the user chose, without a new column. Reset is the remedy for instances already pinned by label edits and the general undo of an override; it re-derives everything from the rule rather than keeping a pre-override copy, so it is right even after the rule itself changed. |
| 80 | **A card statement balance is recorded on the payment instance it is due on, with its provenance, and every forecast event says where its amount comes from** (issue #351, migration 0031, §3.1/§6/§10; amends 56): `instance_statements(transaction_id PK → transactions ON DELETE CASCADE, statement_balance > 0 as printed, statement_date, source human|agent, recorded_by, recorded_at)`; `schedules.record_statement_balance` (REST `POST /schedules/statement-balance`, MCP `record_statement_balance(statement_balance, account, due_date, statement_date?)`, both write scope) takes the instance by `transaction_id` (REST only) or by a credit card plus the statement's due date — the live expected instance of a transfer schedule whose leg on that card is positive, nearest the due date within `STATEMENT_DUE_WINDOW_DAYS` = 14, an equally near pair refused with their dates — and refuses a non-card account, a flat instance, a loan payment, a matched or void row, a balance ≤ 0 (pointing at `skip_instance`) or with more than 4 decimal places, and a `statement_date` in the future or after the instance's date; it sets the amount through `ledger.update_transaction` with the card as the perspective leg (decision 78: both legs, pin, review mark, `transaction.update`), upserts the row and audits `schedule.statement_balance` (before = the prior row, after = `{statement_balance, statement_date, source, previous_amount, amount}`, amounts the schedule account's leg). `reset_instance` deletes the row (and records it in its audit `before`); `matching.release_settlement` copies it onto the clone when `keep_pin` holds. `schedules.instance_basis(schedule, tx, statement)` is the one basis helper: `statement` when a leg equals the recorded balance, else `override` when pinned, else `fixed` (also a non-schedule expected row) or the schedule's estimate basis; `ExpectedItem`/`ForecastEvent` carry it, REST `ForecastEventOut.basis` and the `purser://forecast` resource always, MCP forecast events only for `statement`/`override`; REST `InstanceOut` adds `basis`, `statement_balance`, `statement_date`, `statement_source`. No `estimate_basis = statement_balance`, no feed source. | `counterparty_balance` is the card's current balance — pendings and charges since the close included — while the payment is the statement balance, so a card payment ran high every cycle (8,441.34 forecast against an 8,347 statement), and the override decision 56 pointed to left no trace of why the amount changed. SimpleFIN's account object has no statement balance or closing date, so the figure has to come from the user or the agent reading the statement email; keyed by card and due date, that is one call, with no id lookup first. Per instance rather than per account and cycle: an account-level record would need an engine to decide which instance a statement belongs to (an overdue previous-cycle payment would take the new statement), and the instance is what the forecast and the matcher already read. A separate `statement_balance` basis would add nothing — `counterparty_balance` plus a recorded statement already is "statement, else current balance". Recording goes through the transfer edit so the exact amount also lets the instance vouch for its bank pair (decision 65). MCP names only the supplied bases because the agent must explain those amounts and the fitted event list pays for every key; an estimate's basis is the schedule's. Write scope: the figure pins the payment the forecast and the shortfall scan rely on. |
| 81 | **Valuations are refused off loan and property accounts, brokerage included** (issue #343, §8; amends 71 and 73; no schema change): `networth.record_valuation`, the one write path for manual valuations (REST `POST /accounts/{id}/valuations`, MCP `record_valuation`), refuses an account whose type is outside `networth.VALUATION_TYPES` (`loan`, `property`) right after the virtual-account check and before decision 71's sign rule, with a message naming the two types and where that account's balance comes from (its transactions, and for a checking, savings, cash or credit-card account `reconcile_account` to bring it to a statement; its latest holdings snapshot as well for brokerage); nothing is stored, audited or emitted; the feed's balance valuation uses the same set instead of its own literal; valuation rows already stored on other types stay and are never read; REST's field descriptions no longer describe credit-card valuations | Every reader of `valuations.value` (`net_worth_at`, the decision-42 overlay, the per-account balance series) filters on `VALUATION_TYPES`, and an account's type can never change, so a valuation on any other type was stored, audited and announced as `valuation.recorded`, then silently ignored. Decision 71 and its docs even described credit-card valuations, which encouraged the dead write. Refusing beats documenting it: there is no reader to document. Brokerage is refused too, because its balance is postings plus holdings market value (decision 50), and a valuation would neither replace nor add to either. A credit card is refused for its type before the sign rule runs, so the caller is pointed at reconciliation rather than at a sign it should not be choosing. Stored rows are left alone rather than deleted: they are inert, and deleting user data needs a reason beyond tidiness. |
| 82 | **A pending's review mark follows it to its posted successor, and every copied mark is whole** (issue #342, migration 0032, §3.1/§4; amends 47, reverses #286's "a settlement carried onto a pending's posted successor moves no review"): when the feed supersedes a pending by its posted row — successor pairing and #282's identity move, both through `reconcile.void_pending`, the only place a pending is superseded by its own posted bank line — `review.carry_to_successor` copies the pending's mark to the successor's live head (`schedules.chain_heads`: post-sync categorization may already have corrected the posted row) when that head is inside `scope_clause()`, has no mark of its own, and carries the same set of category-leg (virtual) account ids as the pending; the copy is whole — `reviewed_by`, `via`, `reviewed_at`, note, `escalated_at` — with the new `transaction_reviews.carried_from UUID REFERENCES transactions ON DELETE SET NULL` naming the source, audited `transaction.reviewed` with a NULL actor and `after={via, carried_from, reason, reviewed_by, reviewed_at, escalated}`, no new event; correction inheritance from a system/rule actor (`inherit`, reason `correction`) and unlink (`copy_to_clone`, reason `unlink`) go through the same copier, `review._copy_mark`; a merge or an absorb never carries a mark, and a copy never overwrites one | Decision 29's rule is that a superseding row *is* that bank line: a row someone reviewed, or confirmed a match on, while it was pending came back as "needs review" the moment the bank posted it, and the reconciler already hands the settlement to the successor on exact amount and account. Three things the plain carry would get wrong: the chosen successor can be a reversed original once categorization corrected it, and the unreviewed predicate never asks about a reversed row, so the mark goes to the head; the posted row's category comes from rules or payee memory (which counts posted rows only), not from the pending, so a person's category fix on the pending never reaches it — the equal-category guard re-surfaces exactly the rows whose content nobody reviewed, the rule `mark()` already states; and the old copiers wrote `reviewed_by=NULL` and `reviewed_at=now()` (unlink also dropped escalation), so a carried mark would have lost its actor and time the same night through `inherit`. A column rather than the audit row because the table has no JSON field, audit rows are pruned after `audit_retention_days`, and a carried `reviewed_at` older than the row's `created_at` needs its explanation on the row; `ON DELETE SET NULL` keeps the guarded deletes unchanged. Merges and absorbs pair rows into a transfer, a judgement of its own, so they keep "moves the decision, not a review". |
| 83 | **An admin can pin an account's drift basis; the learned rule keeps running under the pin** (issue #307, migration 0033, §3.1/§7.1/§7.3; ends 64's deferral): nullable `accounts.drift_basis_override` in the drift vocabulary (`posted_only` \| `pending_inclusive`, not reconciliation's `posted`), CHECKed to those values and NULL on categories; set and cleared through `PATCH /accounts/{id}` (admin scope, field-conditional like `feed_start`; explicit null clears; 422 off checking/savings/cash/credit-card), audited in `account.update`. `compare_with_history(pinned_basis=)` always computes the learned basis and reports the pin when set; cleanliness stays either-basis; pinned observations add `pinned: true` and `learned`, and the tie-break reads `learned` on them; the event adds `basis_pinned: true` and `whats_new` drift lines `pinned: true` (both only when pinned); discovered adds `drift_basis_pinned`/`drift_basis_learned`; the SimpleFIN Feed accounts table gets a per-row select. No MCP tool | Decision 64 learned the basis but left no recourse when history is short (a new account) or misleading. Admin scope because it is feed configuration, like `feed_start`/`feed_invert`, and its only viewing surface (discovered) is admin. The pin chooses only which delta is reported, so decision 64's guarantee holds: a wrong pin can never create drift on an account whose other basis matches. Recording `learned` beside the pin keeps the history rule's tie-break on evidence rather than on the pin, so clearing it is exactly as if it had never been set. One vocabulary with the reported basis means the stored pin equals what every surface prints. |
| 84 | **QIF investment sections are skipped and counted, and an unmapped investment account is left out** (issue #274 part S, §7.2; no schema change): the parser flags every dated record of a `!Type:Invst` section (`QifTransaction.investment`, lenient amount, no category or transfer, never raises), maps Invst/Port/401(k)/403(b)/Mutual to `brokerage` and lists `QifFile.investment_accounts`; the planner filters flagged rows first into `skipped_investment` (`ImportCounts`/`ImportResult`), and an investment account absent from `account_map` joins the skip set, so commit needs no mapping for it and bank legs into it degrade to one-leg flats; mapped, bank legs still form two-leg transfers | Before, Invst records were parsed as bank rows: an unsigned `T` turned Buy, Sell and Div into deposits categorized as income, XIn/XOut transfer direction depended on leg order, and the XIn mirror of a split's `S[Brokerage]` line booked a second Checking-to-Brokerage transfer on top of Checking's own net row. Investment lot history stays out of scope (issue #8). Leaving an unmapped investment account out by default, rather than requiring a mapping, keeps net worth honest: a brokerage balance is postings plus holdings (§8, decision 50), so historical contributions imported as postings would be counted again in the holdings. |
| 85 | **An estimated forecast amount carries a range band: min/max of its newest six settled amounts together with the point, and the forecast runs a pessimistic and an optimistic pass beside the point line** (issue #247, §6/§10; closes the §14 "estimate range band" item; no schema change): `core.forecasting.estimate_range(actuals, amount)` = min/max of the newest `ESTIMATE_RANGE_WINDOW` = 6 actuals plus the event's own amount (so the point is always inside), `None` with fewer than `ESTIMATE_RANGE_MIN_ACTUALS` = 2 or no spread; `schedules.schedule_estimate(session, schedule) -> ScheduleEstimate(amount, basis, actuals)` replaces the body of `instance_amount` (which delegates), and `forecast_account`/`_projected_items` share one estimate per schedule; a band is computed only when `instance_basis` is an estimate basis (`last`, `rolling_avg_3`, `rolling_avg_12`, `counterparty_balance`) — never for `fixed`, `override` or `statement` — from the schedule account's leg, negated with its ends swapped on a transfer's counterparty side; `ExpectedItem`/`ForecastEvent.range`, `ForecastDay.low`/`high` (the pessimistic/optimistic running balances, each ranged item at its signed min/max, `None` when nothing in the horizon is ranged), `Forecast.pessimistic_low`/`optimistic_low` (`ForecastPoint`); REST `ForecastEventOut.range`, `ForecastDayOut.low`/`high`, `ForecastOut.pessimistic_low`/`optimistic_low` (nullable); the `purser://forecast` resource carries `range` on every event (null when unranged) and per-day `low`/`high` only when the forecast has a band; MCP events carry `range: {low, high}` only when ranged, `low.range` is `{low, high, low_date}` (the pessimistic and optimistic minimums), and the headline adds "It could dip to X on D if <event> comes in high|low." when the pessimistic low is below the floor and the point low is not, naming the ranged event on or before D with the largest adverse spread (`amount − range.low`); `forecast.shortfall` and `first_shortfall` stay on the point series. | A point estimate is a false certainty for a bill that varies, and the chat in #235 asked for the range. Min/max of history is deterministic and explainable (§13): the band is the bill's own recent extremes, not a model. Including the point keeps it inside the band, so pessimistic ≤ point ≤ optimistic holds by construction (a property test pins it). A card payment's `counterparty_balance` band uses its past payments together with the current balance rather than the balance's recent low and high, which swing from about zero to the peak every cycle and would make a meaningless band. Known amounts — fixed, overridden, stated — have no spread to show. Two actuals are the least that can disagree, and six cover half a year of a monthly bill without dragging in last year. Alerts stay on the point line so a wide band cannot raise a shortfall the expected amounts do not; the headline and `low.range` make the risk visible instead. MCP carries `range` only when set because #354's fitter pays for every key in events. |
| 86 | **A match proposal has a mirror card in the suggestions inbox, a system-closed card is withdrawn, and a rejection is remembered** (issue #246 phase 1, migration 0034, §3.1/§5/§6/§10; amends 57 and 67's "proposed matches stay out of the inbox"; no change to `matches`): `suggestions.kind` gains `match` and `suggestions.status` gains `withdrawn`; a `MatchDraft` (`expected_id`, `posted_id`, `account`, `label`, each row's date and amount on that account, `variance`), the match score as confidence, nothing editable; fingerprint `match:slot:<schedule_id>:<occurrence_date>:<posted_id>` for a schedule instance (an unmatch clone keeps its slot) and `match:tx:<expected_id>:<posted_id>` otherwise, in plain text; `suggestions.upsert_match_suggestion` at the proposal insert in `_match_expected`, and `sync_match_suggestions` at the end of `run_matching` creates missing cards (the backfill, so no data migration) and withdraws open cards whose proposal is gone or not actionable; a bank row held for an expected transfer (`_hold_target`) gets no card within `TRANSFER_WINDOW_DAYS` of its date; one hook, `suggestions.on_proposal_closed`, at the six places a match leaves `proposed` (`_execute_match`'s sibling rescind and its confirm, `reject_match`, `_rescind`, `_carry_settlements`, `transfers._release_settlements_of`): a token's confirm → `accepted` (`result_entity_type = match`), a token's reject → `rejected` exact (created rejected when there was no card), any system action → `withdrawn` (`suggestion.withdraw`); `decide` passes `via_suggestion` so the hook skips the card being decided; `decide`: accept → `confirm_match` (the bank row reviewed `via='match'`, decision 47), reject → `reject_match` (`reject_scope` must be `exact`), snooze hides the card only, `edit_then_accept` is refused, and a card whose proposal is gone is withdrawn and answers 422 (the withdrawal is committed); rejection memory: `_best_for_expected` drops a bank row rejected for the expected's slot and the row that superseded it, in both the auto and the proposed tier, while an explicit `match` can still pair them; a re-proposal re-opens a withdrawn or accepted card; REST `SuggestionKind` gains `match`, the list's status filter `withdrawn`, `DecisionOut.match`, `UnmatchedOut.proposal_id`; MCP `list_unmatched` adds `proposal_id` when set and `decide_suggestion` returns `match{id,state,variance}`; the Dashboard renders a `MatchCard` and its Late bills leave out a bill with a proposal; `match.proposed` is still emitted; nothing retires | A rejected proposal came back every night: `reject_match` set the match rejected, but the matcher only looked at live matches, so the 06:45 pass proposed the same pair again and fired `match.proposed` again — and agents could not decide a proposal at all over MCP. The inbox already had what a proposal lacked (snooze, rejection memory, confidence, one decision surface for the UI and agents), but a proposal is a lock the ledger relies on — the one-live-match indexes, the candidate pool, the transfer holds, the carries, the pending lifecycle — so the card can only mirror it: the `matches` row stays the truth, and the forecast reads `matches`, never the inbox, so a snoozed card moves nothing. Closing cards at the six choke points rather than in the sweep keeps the inbox right between passes (a manual void, decision 69, withdraws its card at once); the sweep is the backfill and the safety net for paths with no hook (guarded deletes). A system-closed card is `withdrawn`, not deleted, so the inbox keeps its history and a re-proposal re-opens the same row. The slot fingerprint makes a rejection outlive an unmatch clone, and the one hop to the superseding row makes it outlive the bank posting the pending. A held transfer leg is left out because the merge usually settles it within days and the card would vanish. `match` and `confirm_transfer` do not retire: `match` is "mark paid" against any row, and the REST routes are the ledger operations the card calls. |
| 87 | **A split transaction is one real leg plus N category legs, and a QIF split's transfer lines are carved out as ordinary transfers (decomposition, model B)** (issue #274 part L, migration 0035, §3.3/§7.2; amends §3.3's #245 sentence): `core.expand_split`/`SplitLine` and `ledger.create_split_transaction` build the shape (two or more nonzero lines summing to the amount; not a transfer); split rows are read-only in v1 (`_flat_legs` says "split rows are read-only"; the categorize pass, a categorize rule's apply-history — `skipped_splits`, a row predicate — and the transfer pool leave them out); `TransactionOut.split_lines`, MCP `split: n`, the register's read-only "Split (n)", and `v2_transactions_flat.category_path` NULL. The QIF parser keeps `S`/`E`/`$` lines (`QifSplit`) and ignores `L` when they exist; the planner turns each `S[X]` line into an ordinary transfer leg (sub-count `split_transfers`), the category lines (blank and unexplained remainder on the placeholder, sub-count `split_remainders`) into the row's split or flat category part, identified by the row's flat hash over `T` | Reading the split lines fixes three things at once: `--Split--` no longer becomes an `expenses.split` (or `income.split`) category the UI can never remap; categories land per line; and a transfer line stops debiting checking twice. The faithful alternative (A) — one row with several real legs, the mirror row consumed — needs several import identities per transaction (a side table) and per-line cutoff rules, and contradicts the one-real-leg principle #245's templates are designed around; (B) reuses the transfer machinery whole, and the transfer hash stored on the transfer keeps it idempotent across files. Identity over `T` keeps files imported before this release deduping rather than doubling. Read-only because editing a split is #245's instance editing, and every existing editor assumes one category leg. |
| 88 | **A brokerage account's holdings come from ONE source — the feed, or positions tracked by hand — and manual snapshots are a regenerable projection of the positions' events** (issue #390 phase 1a, migration 0036, §3.1/§7.1/§8/§10; amends 8 and 50 as far as manual snapshots go): `accounts.holdings_source` (`feed` default \| `manual`), brokerage only (CHECK), switched through `PATCH /accounts/{id}` (admin scope, field-conditional like `feed_start`, audited in `account.update` with what the switch did); the importer stores no feed snapshot for a manual account (`holdings_skipped_manual`) and still imports its cash. `positions` (one open per account and symbol) and append-only `position_events` (`set_quantity` \| `buy` \| `sell`; a BEFORE UPDATE trigger refuses every edit; a correction is a reversal row; `created_at` is `clock_timestamp()`; `actor_token_id` is a session-hygiene attribution anchor) hold the history. Every write locks the account (FOR NO KEY UPDATE, ids in order, before the watermark) and then reads the position again (`lock_position`: a close committed meanwhile refuses it), checks the whole timeline (no date below zero, the 422 naming the first such date; no share count or cost basis beyond its column, a 422 rather than a database error), refuses a date after today and, after every event (recorded or reversed), any day the position would occupy that another position in the symbol occupies (409: positions never overlap; `core.occupied_span` is the held span plus the sell-out day), audits (`position.create` / `record` / `reverse` / `update` / `close`), emits `position.updated` (a rename too, `change='renamed'`; a name is stripped once at entry and the same name is no change), and re-projects in the same transaction. `holdings.projection.project_account` derives the `source='manual'` rows over each manual period (#402, `projection.manual_spans`): the dates after the feed's last run, and every earlier gap between runs that holds manual rows (a feed run: dates carrying feed rows with no manual-only date between them), each from the first effective event to today or the close date, one projection per period so each starts with a full set; so after manual → feed → manual a correction dated in the first period reaches its rows, while a feed account switched to manual never gains rows before its feed history: a set on every event date and every price date of a held symbol, every held position on every set, a zero tombstone on a sell-out date, the first set listing known positions at zero, `market_value` NULL without a price; only rows that differ are rewritten and `networth_dirty_from` drops to the first changed date. Switching manual → feed needs a feed mapping (422 without one), touches no row and records the moment on the database clock (`accounts.holdings_source_changed_at`) and that a manual period is open (`accounts.manual_period_open`): the hand-tracked set stays in effect, with no dip, until the first fetch taken after that moment (`raw_fetches.fetched_at`) ends the period in the importer, once (`end_manual_period`, audited `account.manual_period_end` with the rows deleted, the tombstones and the fetch, counted in the sync result): the manual rows dated on or after the fetch's date go, a MANUAL zero row (a tombstone, `position_id` set) dated that day ends each hand-tracked position the fetch does not report — a cash-only feed ends them all — and its set imports; a later fetch that day reporting such a symbol replaces its tombstone. The tombstones are manual rows, so they never move the feed floor: switched to manual again, the projection re-derives the hand-tracked history from the old floor with no hole. A fetch taken before the switch, re-imported later (the map path does), is inside the manual period, judged by the set in effect on its date (`networth.holdings_set_on`), and adds nothing. An account whose period is still open and whose feed mapping goes (unmapped, closed) ends it that day, with tombstones for every position; one remapped onto another feed account stays mapped, so its new mapping's first fetch ends it (#422). A period still open `price_stale_days` after the switch back (no fetch of the account arrives: the connection fails, or the bank stopped listing it) is reported once per account and switch by the daily price job, `holding.period_open` (dedup key: account and switch moment) and the `whats_new` `open_manual_periods` lines; the frozen set keeps counting until a fetch ends it, never ended by time alone (#422). At most one row per (account, date, symbol) everywhere; positions stay read-only. The importer locks the fetch's mapped accounts (FOR NO KEY UPDATE, id order) before its first watermark mark, and reads `holdings_source` and the period under that lock; the switch, `map_account`, `close_account` and the delete-account path take the account before the watermark too (#413, #403). Average-cost basis. Write scope everywhere: REST `/accounts/{id}/positions`, `/positions/{id}`, `/positions/{id}/events`, `/position-events/{id}/reverse`; MCP `record_position`; `list_holdings` becomes a three-tier tool fitted to the response budget, and one named account pages in symbol order (`cursor` / `next_cursor`, every page inside the budget; a page never repeats a position, but each reads the newest set when called, so a position added meanwhile below the cursor is not listed and the daily job can move later pages to a newer set, #422). The delete-account CLI removes positions and events with the account. | Some brokerages send an account's cash but no holdings — a cash-sweep brokerage: no snapshot at all, so net worth left the position out, and decision 81 rightly refuses a valuation on brokerage. One source per account keeps the read path byte-identical: every reader takes all rows at the newest `as_of`, so a date mixing sources would drop symbols. A projection into the same table, rather than a second read path, is what lets balances, net worth, `balance_history` and Grafana pick the value up unchanged (decision 50's overlay reads it as it reads feed sets). Rewriting only the rows that differ makes the nightly self-heal free when nothing changed and makes "regenerating twice yields identical rows" a tested property. Tombstones because without a zero row the previous set would stay newest and resurrect the shares, in net worth and in `v1_holdings_latest`. The lock order is reconciliation's (§3.7): a sync holds the watermark while its posting inserts take KEY SHARE on the same account rows, so a FOR UPDATE here would deadlock it. Admin scope because the switch changes what the importer writes, like `feed_start`/`feed_invert`/`drift_basis_override`. Commit, never propose: a position has no pending state and moves net worth the moment it lands (decision 73's reasoning), so a propose token gets a tool error, not a draft. No future dates: the projection runs to today, so a future event would only take effect if some later projection happened to cover its date. Decision 8 stands for everything else: these are share counts with a total basis; lot accounting stays a non-goal (§13). |
| 89 | **A security's close resolves newest date first, the source only breaks a same-date tie, feed-implied closes are computed on read, and the provider key is Purser's own write-only secret** (issue #390 phase 1a, migration 0036, §3.1/§8): `security_prices (symbol, price_date, source)`, `source` `manual` \| `tiingo` (CHECK, widened with each provider that lands); a manual close is an upsert of its date (`price.record`, before/after audited; REST `POST /prices`, MCP `record_price`, write scope), a provider close is insert-if-absent. `record_price` re-projects every manual account holding the symbol, and takes the symbol first (`pg_advisory_xact_lock(PRICE_LOCK, hashtext(symbol))`, #415) so that set is complete: a write that makes a manual account start holding a symbol (a new position, a switch back to manual) takes the same key shared, under its account lock, before it projects — it waits for a close being recorded and then reads it, or the close waits for it and then sees the account; a writer only takes the key on an account that does not hold the symbol yet, so neither waits on what the other holds. `core.holdings.PriceBook`: the newest close dated on or before the day wins; manual, then a provider, then feed-implied only on the same date (a feed-implied close also yields to a current manual or provider close and keeps its first day while unchanged: decision 94); a feed-implied close is market value / quantity of that day's feed snapshot of the symbol holding the most shares, never stored. `price_provider` (`none` default \| `tiingo`) and `price_stale_days` (7) are env + runtime settings (`PURSER_PRICE_PROVIDER`, `PURSER_PRICE_STALE_DAYS`); `price_api_key` is a standalone secret key in `app_settings` (Fernet at rest, write-only, masked in audit), with no env default. Tiingo: `GET /tiingo/daily/{ticker}/prices` (`.` spelled `-`), the unadjusted `close`, parsed with `json.loads(parse_float=Decimal)`, rounded in a local decimal context and range-checked after (an absurd close is dropped, counted and reported; the rest of the answer is stored; every close is checked again where it enters valuation, feed-implied ones included, and a value that does not fit an amount keeps its close with a NULL value and is reported as `value_out_of_range` — on `PositionOut`, in `list_holdings` and `record_position`, logged once when written — whose remedy is the share count, never `record_price`), the key only in the Authorization header — `PUT /config` refuses a key that is not printable ASCII without spaces (422, never echoing it); failures carry the symbol, the status and a fixed reason phrase (or an exception's class name) only, never the key or a provider message. `prices.fetch_daily` at 06:40 UTC, and an admin's `POST /prices/fetch`: for every symbol a position on a manual account has held (closed positions and closed accounts included), the dates its positions need — each position's held span (effective events, up to a closed account's close date) from a week before its first day — less the ranges the provider answered for good, a `datemultirange` per (symbol, source) in `price_answered` (an answer final for dates at least 3 days old and up to the newest close it returned; an empty answer or a not-found for such dates counts), newest window first, each inside its own try (`core.needed_ranges`, `core.fetch_windows`); a failing window — any exception from its provider call or its store — records `price.fetch_failed` (once per symbol, kind and day, with a `reason`; closes dropped as absurd are reported beside any other failure of the symbol, in `failed[]`, the audit row and `/prices/status` too) and blocks nothing; then every manual account is re-projected, each in its own transaction; then `price.stale` once per symbol per stale close (dedup key `symbol:close date`, `none` when there is no close); the run is audited `price.fetch`, and the provider closes it stored are audited `price.closes` (per symbol: how many, first and last date) in the very transaction that stores them, so a run that dies before its summary leaves no close unaudited (#405). Stale = the close in effect today is older than `price_stale_days`. Surfaces: `GET /prices/status`, `whats_new` `stale_prices`, `list_holdings` `stale_price` / `no_price`. | Applied literally, "manual > provider > feed-implied" lets one old manual close pin a symbol forever once daily closes arrive; newest-first with a same-date tie-break keeps a manual correction authoritative for its own day and no longer. Computing feed-implied closes on read keeps the feed rows the single record of what the bank reported, with nothing to keep in sync. Purser is the party that sends the key, so it is Purser's secret: stored like `ai_api_key` (encrypted, write-only), and deliberately not an env setting, so it never sits in a compose file or a process environment. The enum ships with what is implemented, so no config value has no code behind it. 06:40 lands after the 06:30 sync (fresh feed snapshots) and before the 07:30 materialization (which consumes the watermark the re-projections lower); re-projecting every manual account there also picks up feed-implied prices without a cross-account hook in the importer, and heals any account a missed trigger left behind. Answered ranges rather than one covered span, because a span joins an island of old closes to a never-asked gap. Unadjusted closes because the event history records quantities as they were held: a pre-split count times that day's close is that day's value. One stale event per (symbol, close date) is §7.3's episode rule: a new stale close is a new episode, an unchanged one never repeats. |
| 90 | **The agent is the user's own: writes are one `write` scope, and proposing is the agent's choice per call** (issue #436, migration 0037, §1/§4/§9/§10; no other schema change): `propose` and `commit` collapse into `write`, which grants everything `commit` did, and every token holding either was converted to `write` with a `token.scopes_migrate` audit row. `effective_status` is gone — a write principal's requested status is honoured, so `pending` means only "the bank has not posted it". The inbox and drafts stay for an agent acting on its own initiative: `propose_rule(activate=false)` and `propose_memory(activate=false)` default to drafts, and `create_schedule(propose=true)` lands a suggestion. The memory default stays draft because an active memory is context for every future session and bank text is merchant-controlled. The safety net is undo, not pre-approval: immutable posted rows, reversal pairs and the attributed audit. Landed before the public release so outside users never see the old scopes. | Owner decision 2026-10-24. Treating the user's own agent as untrusted made 15 tools unavailable to it and every write a proposal the user had to approve; server-side AI writes already worked this way (§11 rule 3). |
| 91 | **Minimal disclosure to agents: privatized defaults the agent overrides per call, references, `present`; `read:redacted` retired** (issue #437, migration 0038, §3.1/§9/§10): two settings, off by default, withhold payees (as `HMAC(payee_ref DEK, payee id)` references, stable across rotate-key) plus descriptions/memos/narrative names, and single amounts; aggregates stay exact; every omission is named in `withheld`; `include` returns a field whatever the defaults and stamps `audit_log.disclosed`; references are inputs everywhere a payee is named and refused in free text; `present` hands the user a link instead of the values; `audited_tool` requires an `emits` declaration and scrubs centrally before serialization, errors included, enforced by a canary test over the whole toolset. `read:redacted` becomes `read` (its effect, external ids omitted, is the default for agents). | Owner decisions 2026-10-24: the agent may see anything (#436), but what it reads lands in its vendor's context, so the default should let it work without disclosing — like using a password-manager secret without pasting it. A setting is a default, not a ceiling, and there is no reveal flow. Exact-or-omitted because a fake number is something an agent may reason from or repeat as true. Names are scrubbed by value because they surface outside their own keys (group keys, headlines, rule text, errors); the canary test, not reading each builder, is what proves no tier leaks. |
| 92 | **A feed row with no value (0 or NULL) while it holds shares is valued on read from stored prices; with none it is unpriced, never a silent 0** (issue #392, migration 0039, §3.1/§8; extends 89, no stored row changes): a FEED `holdings_snapshots` row with `quantity > 0` and `market_value` 0 or NULL is worth `quantity × close` on read — the `security_prices` close of its normalized symbol (stripped, upper case; a symbol that is no ticker is `unpriceable`, never sent to `record_price`) in effect on the row's own `as_of` by decision 89's resolution (newest date first, manual over a provider on the same date; feed-implied closes excluded: a zero row implies none), every close range-checked as at valuation, the product quantized like `core.market_value`, and a value that does not fit keeps the close with no value (`value_out_of_range`, decision 89; not unpriced) (`core.feed_value` through `holdings.overlay.Valuer`; in SQL `overlay.FEED_VALUE_SQL` and the 0039 view, pinned by parity tests). Applied by every reader of a feed row's value: `latest_holdings` (`list_holdings`, `GET /holdings`: `price`, `price_as_of`, `price_source`, stale after `price_stale_days`), `holdings_addends` (account surfaces, `net_worth_at`, `net_worth_daily`, attribution), the balance series, and `readmodel.v1_holdings_latest` (the same overlay in SQL with half-to-even rounding). Unpriced rows read as `market_value` null and `no_price`; the daily price job emits `holding.unpriced` once per (account, symbol) on the transition (dedup key with the newest stored close on or before the row, so it re-arms; `events.emit_once`, an advisory-lock check-then-insert, #405) and `whats_new` lists `unpriced_holdings`. `record_price` marks net worth dirty from its date when a zero-valued feed row of the symbol is dated then or later (`feed_accounts_revalued` in its audit row and answer); a stored provider close does the same. | A 401(k) feed reports its plan-only fund PLANX at 0.0000 in every snapshot since July while the share count grows, so net worth left out a large part of its cost basis. No provider prices a collective trust, so the price can only be recorded by hand, and the feed's rows must stay as the bank sent them (`raw_fetches` provenance, decision 50's overlays are read-side too). Valuing on read keeps every surface consistent from one rule — the same resolution decision 89 gives manual positions — and lets a backdated price heal history through the watermark instead of a rewrite. The row's own date bounds the close so a price recorded later never re-values an older set behind the user's back. 0 or NULL: a feed value of 0 for shares held is the feed's signal that it has none, and the importer stores a numeric 0 or a missing value as NULL; any non-zero value stays the feed's. Unpriced instead of 0 so the gap is visible (`no_price`, one digest line per transition). |
| 93 | **A correction recorded with `replaces` takes the reversed event's place in its day** (issue #401, migration 0040, §3.1/§8; amends 88's event order): `position_events.replaces_id` names a reversed event of the same position and date (the service refuses another position, another date, an event not yet reversed, a reversal, or one already replaced; a reversal row carries none, CHECK; one replacement per event, partial unique index); `core.holdings.effective_events` orders live events by `(occurred_on, slot, created_at, id)`, where `slot` is `created_at` or, for a replacement, the slot of the event it replaces (through a chain of replacements, so a corrected correction keeps the first slot). REST `POST /positions/{id}/events` takes `replaces`; `PositionEventOut.replaces_id`; the `position.record` audit row carries `replaces_id`. Without `replaces` a new event still goes after the day's live events. | The documented correction, reverse and record again, put the corrected event after every other event of that day, so fixing an opening count recorded before a same-day buy applied the count last and silently dropped the buy (110 shares became 100). Inheriting the slot fixes the correction without guessing intent: a plain re-record keeps its meaning (an event recorded later in the day), and a correction says what it corrects. Ordering by the replaced event's `created_at` rather than writing a new one keeps `created_at` true (insertion time) and the table append-only. Same date only: a correction that moves an event to another day has no slot there to inherit. |
| 94 | **A feed-implied close ranks below a current manual or provider close, and an unchanged one keeps the day it first appeared** (issue #406, §8; amends 89; no schema change): `core.holdings.PriceBook` still resolves the newest date first with the source breaking a same-date tie, but a feed-implied winner yields to the newest manual or provider close at or before the day when that close is at most `FEED_DEFER_DAYS` (7) older; and a feed-implied close equal to the symbol's previous feed-implied close is dropped, so it stays dated the day it first appeared. Every surface that resolves a close (projection, `position_states`, the #392 overlay's readers) goes through the book. | A feed-implied close is dated by the UTC fetch day of the 06:30 sync, not the session it prices, so the newest-date rule let it outrank the provider's and the user's close for the latest session, and a bank whose value lagged or froze was never stale. Dating it to the previous trading session needs an exchange calendar and the bank's own timing, which Purser does not know; ranking it lower while a real close is current, and dating an unchanged value by its first day, uses only what Purser stores and invents no value: a worthless asset stays at the value the bank reports, and a frozen one ages into `stale`. The window is the staleness default rather than the runtime setting so the book stays pure; a feed-only symbol (no provider, no manual close) keeps its feed-implied close exactly as before. |
| 95 | **SSO links are (issuer, subject); a verified email relinks only across providers; an admin can unlink; `__Host-` state cookie on https; emails case-insensitive** (issue #429 D1–D4, migration 0041, §3.1/§9): `users.oidc_issuer` joins `oidc_subject` and uniqueness moves to the pair (a NULL-issuer legacy link keeps subject-only uniqueness). The migration backfills every existing link with the configured issuer (the `app_settings` override, JSON null meaning none, else `PURSER_OIDC_ISSUER`), audited `user.oidc_issuer_backfill`; with none it leaves NULL, and the configured provider's next sign-in with that subject claims it. A new subject of the linked provider asserting the account's verified email is refused and audited; a different provider's is relinked (the migration path); `DELETE /users/{id}/oidc` is the admin's way out. On https the state cookie is `__Host-` + name at `Path=/`. Email comparison is `lower() = lower()` everywhere with a unique index on `lower(email)`; the migration stops, naming them, on case-only duplicates and never merges. | Owner decision 2026-10-27 on the #456 review. A subject is unique only at its issuer, so after a provider switch a colliding subject signed in as someone else (D1); relinking any new subject by email let a second identity at the same provider take a linked account over, while refusing every relink would have stranded OIDC-only users after a provider change (D2). `__Host-` needs `Path=/`, so the path scoping is kept only where the prefix cannot be used (D3). Case-sensitive matching left a provider's differently cased email unlinked and, with auto-provisioning, created a second account (D4). |

## 13. Non-goals (v1)

Budgeting/envelopes · lot-level investment accounting & performance math
(TWR/IRR) · multi-currency FX · server-side email/bill parsing · bank
aggregation beyond the SimpleFIN protocol · statistical (non-deterministic)
forecasting.

*(History: "ledger-view UI" was a non-goal until 0.5.0 — the per-account
registers replaced the review queue as the admin UI's action surface,
issue #112.)*

## 14. Open items

- Name diligence: PyPI / Docker namespace / domain checks for "purser"
- Apple Silicon verification of the published arm64 image (#426); native macOS packaging is retired
- Same-hash pending→posted edge inherited from SimpleFIN (posted twin dedups,
  pending voids as "vanished" with no replacement)
- Key-rotation tooling: MultiFernet decrypt-with-old-keys already works
  (comma-separated `PURSER_ENCRYPTION_KEY`); the re-encrypt batch job +
  canary rewrite is deferred (#93)
- Schedules v2 follow-ups (0.16.0 round, decisions 55–57): multi-posting
  schedule templates (a paycheck that splits into net pay, taxes, 401k and a
  savings transfer — a single-amount schedule cannot express it); folding
  transfer candidates into the suggestions inbox as a further `kind`
  (`transfer_pair`, #246 phase 2 — proposed matches are cards since decision
  86, and neither phase retires `match` or `confirm_transfer`);
  AI-tier suggestion authoring
  under decision 49 (#250's server pass; it never drafts a transfer
  schedule, so rule 5 holds trivially); `autopost_policy` is stored but not enacted — if
  `auto_post` ever posts an instance without a token, that row becomes a live
  `origin='rule'` row inside decision 47's scope and arrives unreviewed, so
  decide then whether automation-posted instances carry a mark (an `expected`
  instance is outside the scope, #248).
- A payee-scoped `transfer_link` rule follows its payee through aliases
  (decision 67), but a rename's apply-history aliases only the payees of LIVE
  rows, and a transfer payee's rows are usually all merged (void) — so
  renaming a transfer payee seldom merges it, and new rows under the new name
  fall outside the scope (a confirm then proposes a rule for the new name).
  Whether rename apply-history should also alias payees seen only on merged
  originals is a decision-46 question.
