"""Card payments sized from the statement cycle (#674, decision 136).

- ``accounts.statement_close_day``: the day of the month a credit card's
  statement closes, 1..31 (a day past the month's end is its last day).
  Optional; credit cards only.
- ``schedules.payment_amount`` / ``later_estimate`` / ``later_window``: how a
  card-payment schedule sizes its payments when its card has a closing day.
  Both modes are set together or not at all, and only on a transfer schedule.
- ``instance_statements.minimum_due``: the statement's minimum payment, when
  the person or agent recording the statement supplies it.

Plaintext skeleton only: none of these is narrative.

DESIGN.md section 3.1 is updated in the same PR.

Revision ID: 0063
Revises: 0062
Create Date: 2026-11-23

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0063"
down_revision: str | None = "0062"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
ALTER TABLE accounts
  ADD COLUMN statement_close_day SMALLINT
    CHECK (statement_close_day BETWEEN 1 AND 31),
  ADD CONSTRAINT accounts_statement_close_day_card
    CHECK (statement_close_day IS NULL OR type = 'credit_card');

ALTER TABLE schedules
  ADD COLUMN payment_amount TEXT
    CHECK (payment_amount IN ('statement_balance', 'minimum_due', 'fixed', 'current_balance')),
  ADD COLUMN later_estimate TEXT
    CHECK (later_estimate IN ('statement_average', 'same_cycle_last_year', 'fixed')),
  ADD COLUMN later_window SMALLINT
    CHECK (later_window BETWEEN 1 AND 24),
  ADD CONSTRAINT schedules_cycle_modes_together
    CHECK ((payment_amount IS NULL) = (later_estimate IS NULL)),
  ADD CONSTRAINT schedules_cycle_needs_counterparty
    CHECK (payment_amount IS NULL OR counterparty_account_id IS NOT NULL);

ALTER TABLE instance_statements
  ADD COLUMN minimum_due NUMERIC(20,4)
    CHECK (minimum_due >= 0 AND minimum_due <= statement_balance);
"""
    )


def downgrade() -> None:
    op.execute(
        """
ALTER TABLE instance_statements DROP COLUMN IF EXISTS minimum_due;
ALTER TABLE schedules
  DROP CONSTRAINT IF EXISTS schedules_cycle_needs_counterparty,
  DROP CONSTRAINT IF EXISTS schedules_cycle_modes_together,
  DROP COLUMN IF EXISTS later_window,
  DROP COLUMN IF EXISTS later_estimate,
  DROP COLUMN IF EXISTS payment_amount;
ALTER TABLE accounts
  DROP CONSTRAINT IF EXISTS accounts_statement_close_day_card,
  DROP COLUMN IF EXISTS statement_close_day;
"""
    )
