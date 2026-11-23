"""An index on ``transactions.created_at`` for the search text cache (#141).

The ``serve`` process keeps the searchable text of every transaction in
memory (DESIGN §9, decision 135). New rows reach it through a watermark on
``created_at``: before a search it reads the rows created since the last
look. Without an index that read is a sequential scan of the table (about
20 ms at 200k rows); with one it is an index range scan (under 1 ms).

Plaintext skeleton only: ``created_at`` was already plaintext.

DESIGN.md section 3.1 is updated in the same PR.

Revision ID: 0062
Revises: 0061
Create Date: 2026-11-23

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0062"
down_revision: str | None = "0061"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE INDEX transactions_created_at ON transactions (created_at)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS transactions_created_at")
