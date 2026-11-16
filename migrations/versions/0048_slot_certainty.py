"""Certainty evidence for projected schedule slots (issue #518, DESIGN
decision 109).

A forecast slot beyond the materialization horizon has no transaction, so
``instance_certainty`` (keyed by transaction) cannot hold its evidence.
``slot_certainty`` holds per-dimension evidence for one rule slot,
``(schedule_id, occurrence_date)``, with the same columns and constraints as
the per-dimension part of ``instance_certainty``. It never stores an amount
the forecast uses: ``amount_value`` is the projected leg the evidence was
given against, and a confirmation counts only while the projection still
carries it. Changed values retain the evidence as estimated, with its bound value.
``original`` is the immutable first retained projection, captured at annotation.
``instance_certainty.amount_overridden`` separates new metadata-only evidence from
value edits; NULL keeps existing evidence classification compatible.

- ``materialized_into`` / ``materialized_at``: set when the slot
  materializes and its evidence moves onto the instance's
  ``instance_certainty`` row. ``materialized_into`` NULL = the evidence is
  live on the slot (including a deleted instance). A released instance
  writes its evidence back here.
- ``history_enc``: encrypted superseded evidence, carried onto the instance.

The surrogate UUID key supports the existing encrypted-column key rotation;
the schedule/date pair remains unique.

No backfill: nothing existing changes, and no evidence is invented. The
downgrade drops the table; the evidence it held is lost.

Revision ID: 0048
Revises: 0047
Create Date: 2026-11-15

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0048"
down_revision: str | None = "0047"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SOURCES = "'history', 'statement', 'payroll', 'invoice', 'approval', 'user', 'agent', 'other'"

UPGRADE_DDL = f"""
ALTER TABLE instance_certainty ADD COLUMN amount_overridden BOOLEAN;
CREATE TABLE slot_certainty (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  schedule_id       UUID NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
  occurrence_date   DATE NOT NULL,
  original          JSONB NOT NULL,
  history_enc       BYTEA,
  materialized_into UUID REFERENCES transactions(id) ON DELETE SET NULL,
  materialized_at   TIMESTAMPTZ,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  amount_state      TEXT CHECK (amount_state IN ('estimated', 'confirmed')),
  amount_value      NUMERIC(20,4),
  amount_source     TEXT CHECK (amount_source IN ({SOURCES})),
  amount_as_of      DATE,
  amount_note_enc   BYTEA,
  amount_set_by     UUID REFERENCES api_tokens(id),
  amount_set_at     TIMESTAMPTZ,
  date_state        TEXT CHECK (date_state IN ('estimated', 'confirmed')),
  date_value        DATE,
  date_source       TEXT CHECK (date_source IN ({SOURCES})),
  date_as_of        DATE,
  date_note_enc     BYTEA,
  date_set_by       UUID REFERENCES api_tokens(id),
  date_set_at       TIMESTAMPTZ,
  UNIQUE (schedule_id, occurrence_date),
  CONSTRAINT slot_certainty_amount_whole CHECK (
    (amount_state IS NULL AND amount_value IS NULL AND amount_source IS NULL)
    OR (amount_state IS NOT NULL AND amount_value IS NOT NULL AND amount_source IS NOT NULL)
  ),
  CONSTRAINT slot_certainty_date_whole CHECK (
    (date_state IS NULL AND date_value IS NULL AND date_source IS NULL)
    OR (date_state IS NOT NULL AND date_value IS NOT NULL AND date_source IS NOT NULL)
  ),
  CONSTRAINT slot_certainty_date_is_slot CHECK (date_value IS NULL OR date_value = occurrence_date),
  CONSTRAINT slot_certainty_no_confirmed_history CHECK (
    NOT (amount_state = 'confirmed' AND amount_source = 'history')
    AND NOT (date_state = 'confirmed' AND date_source = 'history')
  )
);
CREATE FUNCTION protect_slot_original() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.original IS DISTINCT FROM OLD.original THEN
    RAISE EXCEPTION 'original projected estimate is immutable';
  END IF;
  RETURN NEW;
END; $$;
CREATE TRIGGER slot_certainty_original_immutable BEFORE UPDATE ON slot_certainty
  FOR EACH ROW EXECUTE FUNCTION protect_slot_original();
"""

DOWNGRADE_DDL = """
DROP TABLE IF EXISTS slot_certainty;
DROP FUNCTION IF EXISTS protect_slot_original();
ALTER TABLE instance_certainty DROP COLUMN amount_overridden;
"""


def upgrade() -> None:
    op.execute(UPGRADE_DDL)


def downgrade() -> None:
    op.execute(DOWNGRADE_DDL)
