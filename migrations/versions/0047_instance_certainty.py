"""Per-occurrence estimate certainty and the original estimate (issue #512,
DESIGN decision 108).

A schedule occurrence (an ``expected`` instance) says what is uncertain about
it and why, then resolves into a confirmed occurrence or a matched actual
without losing the estimate it started from.

- ``schedules.date_type``: ``fixed`` (the rule's dates are the dates, the
  default and every existing schedule) or ``estimated`` (the rule only
  approximates when it lands: a reimbursement, a seasonal service). It mirrors
  ``amount_type`` for the date dimension.
- ``instance_certainty``: a side table like ``instance_statements`` (it
  annotates the instance; the amount and date stay on the transaction and its
  postings). One row per instance:

  - the ORIGINAL estimate, written once when the slot first materializes and
    never updated: the schedule account's leg, the date, where each came from
    (``original_amount_basis`` in the forecast's basis vocabulary,
    ``original_date_basis`` fixed | estimated) and the settled history behind
    a history-based amount (``original_window``: count and date range, no
    money). A row backfilled here records the instance as it stood at
    migration (``original_backfilled``), not a first estimate nobody kept.
  - per dimension, the state set on THIS occurrence (NULL = the rule's):
    ``estimated`` or ``confirmed``, the value it was set with, its source,
    as-of date, an encrypted note, and who set it when. The state counts only
    while the instance still carries that value, so an edit after a
    confirmation never leaves a stale "confirmed" behind.

- ``ON DELETE CASCADE``: the row goes with its transaction.

The upgrade backfills a row for every schedule instance. The downgrade
drops the table and the column; the certainty it held is lost.

Revision ID: 0047
Revises: 0046
Create Date: 2026-11-15

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0047"
down_revision: str | None = "0046"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SOURCES = "'history', 'statement', 'payroll', 'invoice', 'approval', 'user', 'agent', 'other'"

UPGRADE_DDL = f"""
ALTER TABLE schedules
  ADD COLUMN date_type TEXT NOT NULL DEFAULT 'fixed'
  CONSTRAINT schedules_date_type_check CHECK (date_type IN ('fixed', 'estimated'));

CREATE TABLE instance_certainty (
  transaction_id        UUID PRIMARY KEY REFERENCES transactions(id) ON DELETE CASCADE,
  schedule_id           UUID NOT NULL REFERENCES schedules(id),
  occurrence_date       DATE NOT NULL,
  history_enc           BYTEA,
  original_amount       NUMERIC(20,4) NOT NULL,
  original_date         DATE NOT NULL,
  original_amount_basis TEXT NOT NULL,
  original_date_basis   TEXT NOT NULL CHECK (original_date_basis IN ('fixed', 'estimated')),
  original_window       JSONB,
  original_backfilled   BOOLEAN NOT NULL DEFAULT false,
  original_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  amount_state          TEXT CHECK (amount_state IN ('estimated', 'confirmed')),
  amount_value          NUMERIC(20,4),
  amount_source         TEXT CHECK (amount_source IN ({SOURCES})),
  amount_as_of          DATE,
  amount_note_enc       BYTEA,
  amount_set_by         UUID REFERENCES api_tokens(id),
  amount_set_at         TIMESTAMPTZ,
  date_state            TEXT CHECK (date_state IN ('estimated', 'confirmed')),
  date_value            DATE,
  date_source           TEXT CHECK (date_source IN ({SOURCES})),
  date_as_of            DATE,
  date_note_enc         BYTEA,
  date_set_by           UUID REFERENCES api_tokens(id),
  date_set_at           TIMESTAMPTZ,
  CONSTRAINT instance_certainty_amount_whole CHECK (
    (amount_state IS NULL AND amount_value IS NULL AND amount_source IS NULL)
    OR (amount_state IS NOT NULL AND amount_value IS NOT NULL AND amount_source IS NOT NULL)
  ),
  CONSTRAINT instance_certainty_date_whole CHECK (
    (date_state IS NULL AND date_value IS NULL AND date_source IS NULL)
    OR (date_state IS NOT NULL AND date_value IS NOT NULL AND date_source IS NOT NULL)
  )
);

-- Backfill: every schedule instance gets its row, the instance as it
-- stands now (the schedule account's leg, its slot) with the basis the
-- forecast reports for it today.
INSERT INTO instance_certainty (
  transaction_id, schedule_id, occurrence_date, original_amount, original_date,
  original_amount_basis,
  original_date_basis, original_backfilled
)
SELECT t.id, s.id, COALESCE(t.occurrence_date, t.date),
       p.amount,
       t.date,
       CASE
         WHEN EXISTS (
           SELECT 1 FROM instance_statements st
           JOIN postings sp ON sp.transaction_id = st.transaction_id
           WHERE st.transaction_id = t.id AND sp.amount = st.statement_balance
         ) THEN 'statement'
         WHEN t.schedule_pinned_at IS NOT NULL THEN 'override'
         WHEN s.source = 'detected' AND s.amount_type = 'fixed' THEN 'history'
         WHEN s.amount_type = 'fixed' THEN 'fixed'
         ELSE COALESCE(s.estimate_basis, 'rolling_avg_3')
       END,
       CASE WHEN s.source = 'detected' THEN 'estimated' ELSE 'fixed' END,
       true
FROM transactions t
JOIN schedules s ON s.id = t.schedule_id
JOIN postings p ON p.transaction_id = t.id AND p.account_id = s.account_id
WHERE t.schedule_id IS NOT NULL;

CREATE INDEX instance_certainty_slot ON instance_certainty(schedule_id, occurrence_date);
CREATE FUNCTION protect_original_estimate() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF ROW(NEW.original_amount, NEW.original_date, NEW.original_amount_basis,
         NEW.original_date_basis, NEW.original_window, NEW.original_backfilled, NEW.original_at)
     IS DISTINCT FROM
     ROW(OLD.original_amount, OLD.original_date, OLD.original_amount_basis,
         OLD.original_date_basis, OLD.original_window, OLD.original_backfilled,
         OLD.original_at) THEN
    RAISE EXCEPTION 'original occurrence estimate is immutable';
  END IF;
  RETURN NEW;
END; $$;
CREATE TRIGGER instance_certainty_original_immutable BEFORE UPDATE ON instance_certainty
  FOR EACH ROW EXECUTE FUNCTION protect_original_estimate();
"""

DOWNGRADE_DDL = """
DROP TABLE IF EXISTS instance_certainty;
DROP FUNCTION IF EXISTS protect_original_estimate();
ALTER TABLE schedules DROP COLUMN IF EXISTS date_type;
"""


def upgrade() -> None:
    op.execute(UPGRADE_DDL)


def downgrade() -> None:
    op.execute(DOWNGRADE_DDL)
