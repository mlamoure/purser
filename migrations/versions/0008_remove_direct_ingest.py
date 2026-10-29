"""Remove the Direct-to-Bank feature (product pivot, issue #97).

Drops everything migration 0006 added for the bank-learner / bank-skill
pipeline: learned_maps, skill_cache, the connection skill binding, and the
per-connection ingest-token binding. SimpleFIN and QIF import are the two
remaining feed paths; raw_fetches stays untouched.

Deliberate deviations from restoring the pre-0006 (0001) connections shape:

- ``access_url_enc`` stays nullable: historical ``provider='direct'`` rows
  have no access URL, and restoring NOT NULL would fail on them.
- No provider CHECK is (re)added: 0001 never had one; the one 0006 added
  existed to allow 'direct', which is gone.
- Historical ``provider='direct'`` rows are kept, not deleted — their
  raw_fetches are append-only and FK-bound, so deletion is impossible by
  design. They are set ``status='disabled'`` so the scheduler skips them;
  the provider value remains as provenance for their archived fetches.

DESIGN.md §2/§3.1/§7/§9/§12/§14 updated in the same PR (schema rule).

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-11

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DDL = """
-- Guard triggers first, then the tables they guard, then the shared function
-- (DROP TABLE removes its own triggers; the explicit order documents intent
-- and lets the function drop cleanly once nothing references it).
DROP TRIGGER IF EXISTS learned_maps_no_mutate ON learned_maps;
DROP TRIGGER IF EXISTS learned_maps_no_truncate ON learned_maps;
DROP TABLE IF EXISTS learned_maps;
DROP TABLE IF EXISTS skill_cache;
DROP FUNCTION IF EXISTS learned_maps_guard();

-- Per-connection ingest token binding: only the direct wizard ever set it.
DROP INDEX IF EXISTS api_tokens_connection;
ALTER TABLE api_tokens DROP COLUMN IF EXISTS connection_id;

-- Drop the direct-shape CHECKs BEFORE the retiring UPDATE: even a NOT VALID
-- constraint (the downgrade recreates them as NOT VALID) validates updated
-- rows, and a post-downgrade direct row (path defaulted to 'connect',
-- skill_ref NULL) would fail it — the constraint must be gone first for the
-- migration to round-trip. Then retire historical direct connections (see
-- module docstring) and drop the columns (their own CHECKs drop with them).
ALTER TABLE connections DROP CONSTRAINT IF EXISTS connections_direct_shape;
ALTER TABLE connections DROP CONSTRAINT IF EXISTS connections_provider_check;
UPDATE connections SET status = 'disabled' WHERE provider = 'direct';
ALTER TABLE connections
  DROP COLUMN IF EXISTS auth_mode,
  DROP COLUMN IF EXISTS skill_ref,
  DROP COLUMN IF EXISTS path;
"""

# Recreates 0006's objects (structure, not data — skill_refs/maps/bundles are
# unrecoverable; removal is final). Exercised in CI: test_migration_backfill
# downgrades head -> 0006. The two table-level constraints are NOT VALID
# because surviving historical provider='direct' rows (path defaulted back to
# 'connect', skill_ref NULL) cannot satisfy them; new rows are still checked.
DOWNGRADE_DDL = """
ALTER TABLE connections
  ADD COLUMN path      TEXT NOT NULL DEFAULT 'connect'
             CHECK (path IN ('connect', 'direct')),
  ADD COLUMN skill_ref JSONB,
  ADD COLUMN auth_mode TEXT
             CHECK (auth_mode IN ('interactive', 'openclaw_vault', 'external_vault'));

ALTER TABLE connections
  ADD CONSTRAINT connections_provider_check
    CHECK (provider IN ('simplefin', 'direct')) NOT VALID;
ALTER TABLE connections
  ADD CONSTRAINT connections_direct_shape CHECK (
    (provider = 'direct' AND path = 'direct' AND skill_ref IS NOT NULL)
    OR (provider = 'simplefin' AND path = 'connect' AND access_url_enc IS NOT NULL)
  ) NOT VALID;

ALTER TABLE api_tokens ADD COLUMN connection_id UUID REFERENCES connections(id);
CREATE INDEX api_tokens_connection ON api_tokens (connection_id)
  WHERE connection_id IS NOT NULL;

CREATE TABLE learned_maps (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  received_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  bank_name       TEXT NOT NULL,
  bank_domains    TEXT[] NOT NULL,
  learner_version TEXT NOT NULL,
  observed_at     TIMESTAMPTZ NOT NULL,
  map             JSONB NOT NULL,
  map_hash        TEXT NOT NULL,
  status          TEXT NOT NULL DEFAULT 'captured'
                  CHECK (status IN ('captured', 'submitted', 'built', 'rejected')),
  submitted_at    TIMESTAMPTZ,
  builder_ref     TEXT,
  created_by      UUID
);
CREATE UNIQUE INDEX learned_maps_hash_uniq ON learned_maps (map_hash);
CREATE INDEX learned_maps_status ON learned_maps (status, received_at);

CREATE FUNCTION learned_maps_guard() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'UPDATE' THEN
    IF (to_jsonb(NEW) - 'status' - 'submitted_at' - 'builder_ref')
       IS DISTINCT FROM (to_jsonb(OLD) - 'status' - 'submitted_at' - 'builder_ref') THEN
      RAISE EXCEPTION 'learned_maps map payload is immutable';
    END IF;
    RETURN NEW;
  END IF;
  RAISE EXCEPTION 'learned_maps is append-only';
END $$ LANGUAGE plpgsql;

CREATE TRIGGER learned_maps_no_mutate
  BEFORE UPDATE OR DELETE ON learned_maps
  FOR EACH ROW EXECUTE FUNCTION learned_maps_guard();

CREATE TRIGGER learned_maps_no_truncate
  BEFORE TRUNCATE ON learned_maps
  FOR EACH STATEMENT EXECUTE FUNCTION learned_maps_guard();

CREATE TABLE skill_cache (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  skill_id        TEXT NOT NULL,
  version         TEXT NOT NULL,
  manifest        JSONB NOT NULL,
  bundle          BYTEA NOT NULL,
  signature       BYTEA NOT NULL,
  signed_by       TEXT NOT NULL,
  key_fingerprint TEXT NOT NULL,
  verified_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  fetched_from    TEXT NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX skill_cache_id_version_uniq ON skill_cache (skill_id, version);
"""


def upgrade() -> None:
    op.execute(DDL)


def downgrade() -> None:
    op.execute(DOWNGRADE_DDL)
