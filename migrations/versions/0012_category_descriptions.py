"""Curated categories: description column + shipped default set (issue #110).

``accounts.description`` carries a one-line, classifier-oriented description
(used by the AI categorization prompt and the Config tree). The data-seed
inserts the default category hierarchy (``purser/core/categories.py``),
skipping any path that already exists — existing installs merge additively,
and because migrations run once, later user deletions stick.

Revision ID: 0012
Revises: 0011
Create Date: 2026-07-12

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("accounts", sa.Column("description", sa.Text(), nullable=True))

    from purser.categoryseed import seed_default_categories

    seed_default_categories(op.get_bind())


def downgrade() -> None:
    op.drop_column("accounts", "description")
