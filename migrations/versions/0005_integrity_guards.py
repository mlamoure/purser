"""Integrity guards — correction/match uniqueness + tighter posted triggers.

Closes the review findings of issues #35/#36/#37 (plus the #39 backstop):

- transactions_reverses_id_uniq: a posted transaction can be corrected once;
  a concurrent double-correction now fails with IntegrityError instead of
  silently double-reversing balances.
- matches_posted_live_uniq / matches_expected_live_uniq: at most one live
  (proposed/confirmed) match per posted row and per expected row.
- accounts_connection_external_uniq: one account per (connection, external
  id) — backstop for the feed-layer fix on issue #39's branch.
- postings_posted_guard now fires on INSERT too and checks both the old and
  new parent transaction, so postings can neither be added to nor re-pointed
  into a posted transaction. Carve-out: a transaction row created inside the
  current DB transaction (created_at = transaction_timestamp()) is still
  being assembled, so posted-at-creation inserts keep working.
- transactions_posted_guard is now an allow-list: the only change a posted
  row may take is status -> 'void' WITH a superseded_by lineage link (set in
  the same UPDATE). Everything else — including future columns and rewriting
  superseded_by on a live posted row — is rejected.

DESIGN.md §3/§4 updated in the same PR (schema rule).

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-05

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DDL = """
-- A transaction is corrected at most once: only one reversal may point at it.
CREATE UNIQUE INDEX transactions_reverses_id_uniq
  ON transactions (reverses_id) WHERE reverses_id IS NOT NULL;

-- At most one live (proposed/confirmed) match per side.
CREATE UNIQUE INDEX matches_posted_live_uniq
  ON matches (posted_id) WHERE state IN ('proposed', 'confirmed');
CREATE UNIQUE INDEX matches_expected_live_uniq
  ON matches (expected_id) WHERE state IN ('proposed', 'confirmed');

-- One ledger account per (connection, external id) — feed idempotency
-- backstop (issue #39; the app-layer fix lands on its own branch).
CREATE UNIQUE INDEX accounts_connection_external_uniq
  ON accounts (connection_id, external_id) WHERE external_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- postings_posted_guard: postings of a posted transaction are immutable, on
-- ALL operations and on BOTH sides of a re-point. A transaction row created
-- in the current DB transaction (created_at = transaction_timestamp()) is
-- still being assembled — posted-at-creation postings attach through that
-- carve-out.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION postings_posted_guard() RETURNS trigger AS $$
DECLARE
  tids UUID[];
  tid UUID;
  tx_status TEXT;
  tx_created TIMESTAMPTZ;
BEGIN
  IF TG_OP = 'INSERT' THEN
    tids := ARRAY[NEW.transaction_id];
  ELSIF TG_OP = 'DELETE' THEN
    tids := ARRAY[OLD.transaction_id];
  ELSE
    tids := ARRAY[OLD.transaction_id, NEW.transaction_id];
  END IF;

  FOREACH tid IN ARRAY tids LOOP
    SELECT status, created_at INTO tx_status, tx_created
      FROM transactions WHERE id = tid;
    IF tx_status = 'posted' AND tx_created <> transaction_timestamp() THEN
      RAISE EXCEPTION 'postings of posted transactions are immutable';
    END IF;
  END LOOP;

  IF TG_OP = 'DELETE' THEN
    RETURN OLD;
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;

DROP TRIGGER postings_posted_immutable ON postings;
CREATE TRIGGER postings_posted_immutable
  BEFORE INSERT OR UPDATE OR DELETE ON postings
  FOR EACH ROW EXECUTE FUNCTION postings_posted_guard();

-- ---------------------------------------------------------------------------
-- transactions_posted_guard: allow-list instead of deny-list. Once posted,
-- the ONLY permitted change is void-with-lineage: status -> 'void' together
-- with superseded_by, in the same UPDATE. Future columns are covered
-- automatically via the to_jsonb comparison.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION transactions_posted_guard() RETURNS trigger AS $$
BEGIN
  IF OLD.status = 'posted' THEN
    IF (to_jsonb(NEW) - 'status' - 'superseded_by')
       IS DISTINCT FROM (to_jsonb(OLD) - 'status' - 'superseded_by') THEN
      RAISE EXCEPTION
        'posted transactions are immutable; corrections are linked reversals';
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status THEN
      IF NEW.status <> 'void' THEN
        RAISE EXCEPTION
          'posted transactions are immutable; corrections are linked reversals';
      END IF;
      IF NEW.superseded_by IS NULL THEN
        RAISE EXCEPTION
          'voiding a posted transaction requires a superseded_by lineage link';
      END IF;
    ELSIF NEW.superseded_by IS DISTINCT FROM OLD.superseded_by THEN
      RAISE EXCEPTION
        'posted transactions are immutable; superseded_by is set only when voiding';
    END IF;
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
"""

# Original 0001 function bodies + trigger events, restored on downgrade.
DOWNGRADE_DDL = """
DROP INDEX IF EXISTS transactions_reverses_id_uniq;
DROP INDEX IF EXISTS matches_posted_live_uniq;
DROP INDEX IF EXISTS matches_expected_live_uniq;
DROP INDEX IF EXISTS accounts_connection_external_uniq;

CREATE OR REPLACE FUNCTION postings_posted_guard() RETURNS trigger AS $$
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

DROP TRIGGER postings_posted_immutable ON postings;
CREATE TRIGGER postings_posted_immutable
  BEFORE UPDATE OR DELETE ON postings
  FOR EACH ROW EXECUTE FUNCTION postings_posted_guard();

CREATE OR REPLACE FUNCTION transactions_posted_guard() RETURNS trigger AS $$
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
"""


def upgrade() -> None:
    op.execute(DDL)


def downgrade() -> None:
    op.execute(DOWNGRADE_DDL)
