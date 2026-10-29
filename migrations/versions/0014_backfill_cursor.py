"""Backfill cursor: connections track historical-backfill progress (issue #126).

``raw_fetches`` archives the payload but NOT the request params, so "how far
back have we already asked SimpleFIN?" is unrecoverable from the archive. Two
columns on ``connections`` carry that state instead:

- ``backfill_cursor`` — the oldest start-date fetched so far (NULL until the
  first backfill run derives it from the first sync's window).
- ``backfill_complete`` — latches true once the institution's history is
  exhausted (two consecutive empty windows), so later runs no-op.

Revision ID: 0014_backfill_cursor
Revises: 0013
Create Date: 2026-07-12

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_backfill_cursor"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("connections", sa.Column("backfill_cursor", sa.Date(), nullable=True))
    op.add_column(
        "connections",
        sa.Column(
            "backfill_complete",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("connections", "backfill_complete")
    op.drop_column("connections", "backfill_cursor")
