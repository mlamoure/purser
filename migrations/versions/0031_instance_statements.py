"""Card statement balances recorded against a payment instance (issue #351,
DESIGN decision 80).

A card-payment schedule estimates from the card's CURRENT balance
(``counterparty_balance``), but the payment is usually the STATEMENT balance
from the last close, and SimpleFIN carries no statement figure. So the user or
an agent records it: ``instance_statements`` holds one statement per payment
instance (an ``expected`` transfer), with its provenance.

A side table, not columns on ``transactions``, for the reason migration 0021
gave for review marks: the row annotates the instance rather than being
ledger content, and the instance's amount itself is moved through the
ordinary transfer edit (decision 78).

- ``statement_balance`` is the figure as the statement prints it: the amount
  owed, positive. A statement with nothing owed is no payment at all (the
  instance is skipped instead), hence the CHECK.
- ``source`` is the kind of token that recorded it (``human`` for a session,
  ``agent`` for an API token). ``feed`` joins the CHECK only when a provider
  actually supplies statements.
- ``ON DELETE CASCADE``: the row goes with its transaction, so the guarded
  account purge needs no extra step.

Revision ID: 0031
Revises: 0030
Create Date: 2026-10-13

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE_DDL = """
CREATE TABLE instance_statements (
  transaction_id    UUID PRIMARY KEY REFERENCES transactions(id) ON DELETE CASCADE,
  statement_balance NUMERIC(20,4) NOT NULL CHECK (statement_balance > 0),
  statement_date    DATE,
  source            TEXT NOT NULL CHECK (source IN ('human', 'agent')),
  recorded_by       UUID REFERENCES api_tokens(id),
  recorded_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

DOWNGRADE_DDL = """
DROP TABLE IF EXISTS instance_statements;
"""


def upgrade() -> None:
    op.execute(UPGRADE_DDL)


def downgrade() -> None:
    op.execute(DOWNGRADE_DDL)
