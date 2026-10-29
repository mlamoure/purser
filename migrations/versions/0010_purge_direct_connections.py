"""Purge retired provider='direct' connections and their captures (issue #101).

Migration 0008 retired the Direct-to-Bank connections as ``status='disabled'``
provenance rows because their raw_fetches are append-only and FK-bound. The
owner has since decided (2026-07-11) that these test-era rows and their
archived captures should be removed outright rather than kept as provenance.

This is a documented one-off exception to the raw_fetches append-only
invariant (DESIGN §12, decision 25) — NOT a reusable delete path. Both guard
triggers are disabled only around the specific statements that need them and
re-enabled in the same transaction; the invariant remains in force for normal
operation, and no application code path may mutate raw_fetches.

Order of operations (single transaction, idempotent, no-op when no direct
rows exist — fresh databases and CI):

1. Unlink ``transactions.source_fetch_id`` for captures being purged
   (``transactions_posted_immutable`` guards that column on posted rows, so
   it is disabled around the UPDATE). Ledger rows themselves are untouched.
2. Delete ``holdings_snapshots`` sourced from those captures (point-in-time
   test data, meaningless without their fetch).
3. Unlink ``accounts.connection_id`` — accounts and their transactions
   survive, unbound from the deleted connection.
4. Delete the ``raw_fetches`` (``raw_fetches_no_update_delete`` disabled
   around the DELETE).
5. Delete the ``connections`` rows.

Deleted row counts are RAISEd as NOTICEs for the migration log.

Revision ID: 0010
Revises: 0009
Create Date: 2026-07-11

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DDL = """
DO $$
DECLARE
  doomed_connections UUID[];
  doomed_fetches     UUID[];
  n_tx    INT;
  n_snap  INT;
  n_acct  INT;
  n_fetch INT;
  n_conn  INT;
BEGIN
  SELECT COALESCE(array_agg(id), '{}') INTO doomed_connections
    FROM connections WHERE provider = 'direct';
  IF cardinality(doomed_connections) = 0 THEN
    RAISE NOTICE 'purge_direct: no provider=direct connections; nothing to do';
    RETURN;
  END IF;

  SELECT COALESCE(array_agg(id), '{}') INTO doomed_fetches
    FROM raw_fetches WHERE connection_id = ANY (doomed_connections);

  -- The posted-immutability guard blocks source_fetch_id changes; the ledger
  -- rows themselves are kept, only their capture link is severed.
  ALTER TABLE transactions DISABLE TRIGGER transactions_posted_immutable;
  UPDATE transactions SET source_fetch_id = NULL
    WHERE source_fetch_id = ANY (doomed_fetches);
  GET DIAGNOSTICS n_tx = ROW_COUNT;
  ALTER TABLE transactions ENABLE TRIGGER transactions_posted_immutable;

  DELETE FROM holdings_snapshots WHERE raw_fetch_id = ANY (doomed_fetches);
  GET DIAGNOSTICS n_snap = ROW_COUNT;

  UPDATE accounts SET connection_id = NULL
    WHERE connection_id = ANY (doomed_connections);
  GET DIAGNOSTICS n_acct = ROW_COUNT;

  ALTER TABLE raw_fetches DISABLE TRIGGER raw_fetches_no_update_delete;
  DELETE FROM raw_fetches WHERE connection_id = ANY (doomed_connections);
  GET DIAGNOSTICS n_fetch = ROW_COUNT;
  ALTER TABLE raw_fetches ENABLE TRIGGER raw_fetches_no_update_delete;

  DELETE FROM connections WHERE id = ANY (doomed_connections);
  GET DIAGNOSTICS n_conn = ROW_COUNT;

  RAISE NOTICE 'purge_direct: connections=% raw_fetches=% holdings_snapshots=%',
    n_conn, n_fetch, n_snap;
  RAISE NOTICE 'purge_direct: transactions_unlinked=% accounts_unlinked=%',
    n_tx, n_acct;
END $$;
"""


def upgrade() -> None:
    op.execute(DDL)


def downgrade() -> None:
    # Irreversible: the purge destroys data (that was the point). Downgrading
    # past this revision restores nothing; it is a schema no-op.
    pass
