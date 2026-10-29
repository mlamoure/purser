"""net_worth_daily — nightly materialization of the net worth series.

Grafana's flagship source (DESIGN §8); keeps the MCP net_worth tool fast.
DESIGN.md §3 updated in the same PR (schema rule).

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-03

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE net_worth_daily (
          as_of       DATE NOT NULL,
          currency    CHAR(3) NOT NULL DEFAULT 'USD',
          assets      NUMERIC(20,4) NOT NULL,
          liabilities NUMERIC(20,4) NOT NULL,
          net_worth   NUMERIC(20,4) NOT NULL,
          computed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          PRIMARY KEY (as_of, currency)
        );
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS net_worth_daily;")
