"""Direct-to-Bank ingest: connection path/skill binding, learned_maps, skill_cache.

Adds the second feed path (DESIGN §7.2, spec §8.3). No change to the ledger,
postings, or readmodel — Direct is a new *entry* (`provider='direct'`) into the
same raw-first pipeline, not a new pipeline.

- connections: `access_url_enc` becomes nullable (a direct connection has no
  SimpleFIN access URL); new `path` (connect|direct), `skill_ref` (repository
  coordinates for direct), `auth_mode`. A shape CHECK ties provider↔path↔the
  required secret so a row can't be half-configured.
- api_tokens: `connection_id` binds a direct connection's propose token to it;
  the ingest endpoint requires `token.connection_id = connection.id`.
- learned_maps: captured maps pending build. The map payload is immutable
  evidence; only `status`/`submitted_at`/`builder_ref` may change; no row is
  ever deleted (allow-list guard trigger, mirroring raw_fetches append-only).
- skill_cache: signed bundles Purser has verified (at cache time) and cached;
  one row per (skill_id, version).

DESIGN.md §2/§3.1/§7/§9/§12/§14 updated in the same PR (schema rule).

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-06

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DDL = """
-- connections: the Direct-to-Bank path (spec §8.3).
ALTER TABLE connections ALTER COLUMN access_url_enc DROP NOT NULL;
ALTER TABLE connections
  ADD COLUMN path      TEXT NOT NULL DEFAULT 'connect'
             CHECK (path IN ('connect', 'direct')),
  ADD COLUMN skill_ref JSONB,
  ADD COLUMN auth_mode TEXT
             CHECK (auth_mode IN ('interactive', 'openclaw_vault', 'external_vault'));

ALTER TABLE connections
  ADD CONSTRAINT connections_provider_check CHECK (provider IN ('simplefin', 'direct')),
  ADD CONSTRAINT connections_direct_shape CHECK (
    (provider = 'direct' AND path = 'direct' AND skill_ref IS NOT NULL)
    OR (provider = 'simplefin' AND path = 'connect' AND access_url_enc IS NOT NULL)
  );

-- Per-connection ingest token binding. A direct connection's propose token is
-- scoped to it; ingest_envelope rejects a token whose connection_id differs.
ALTER TABLE api_tokens ADD COLUMN connection_id UUID REFERENCES connections(id);
CREATE INDEX api_tokens_connection ON api_tokens (connection_id)
  WHERE connection_id IS NOT NULL;

-- Captured learner maps (scrubbed structure only; §5.1). Append-only evidence.
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
  -- submitting api_tokens.id (provenance). Deliberately NOT a foreign key: an
  -- append-only evidence table must not be cascade-coupled to the mutable
  -- identity tables (token rotation/deletion can't threaten the record).
  created_by      UUID
);
CREATE UNIQUE INDEX learned_maps_hash_uniq ON learned_maps (map_hash);
CREATE INDEX learned_maps_status ON learned_maps (status, received_at);

-- Guard: the captured map is immutable evidence. An UPDATE may touch only
-- status / submitted_at / builder_ref (the build lifecycle); everything else,
-- and any DELETE/TRUNCATE, is rejected. Future columns are covered by the
-- to_jsonb comparison.
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

-- Verified signed skill bundles Purser has cached. Signature is checked once,
-- at cache time (§8.2); one row per (skill_id, version).
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

DOWNGRADE_DDL = """
DROP TABLE IF EXISTS skill_cache;
DROP TABLE IF EXISTS learned_maps;
DROP FUNCTION IF EXISTS learned_maps_guard();

DROP INDEX IF EXISTS api_tokens_connection;
ALTER TABLE api_tokens DROP COLUMN IF EXISTS connection_id;

ALTER TABLE connections DROP CONSTRAINT IF EXISTS connections_direct_shape;
ALTER TABLE connections DROP CONSTRAINT IF EXISTS connections_provider_check;
ALTER TABLE connections DROP COLUMN IF EXISTS auth_mode;
ALTER TABLE connections DROP COLUMN IF EXISTS skill_ref;
ALTER TABLE connections DROP COLUMN IF EXISTS path;
ALTER TABLE connections ALTER COLUMN access_url_enc SET NOT NULL;
"""


def upgrade() -> None:
    op.execute(DDL)


def downgrade() -> None:
    op.execute(DOWNGRADE_DDL)
