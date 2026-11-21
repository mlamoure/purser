"""Feed accounts an admin chose not to track (#560)."""

from alembic import op

revision = "0052"
down_revision = "0051"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE ignored_feed_accounts (
            connection_id UUID NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
            external_id TEXT NOT NULL,
            ignored_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (connection_id, external_id)
        );
    """)


def downgrade() -> None:
    op.execute("DROP TABLE ignored_feed_accounts;")
