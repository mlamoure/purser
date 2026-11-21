"""valuations.created_at: of two valuations on one date, the later recorded wins (#565).

Same-date rows were ordered by their random UUID. Existing rows take the time
of their `valuation.create` audit row (manual valuations); feed valuations
were never audited, so they take midnight UTC of their date, which keeps them
behind any audited row of that date and leaves same-date feed rows tied on
the id, as before. Nothing better is knowable for them.
"""

from alembic import op

revision = "0053"
down_revision = "0052"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE valuations ADD COLUMN created_at TIMESTAMPTZ;")
    op.execute("""
        UPDATE valuations v
        SET created_at = COALESCE(
            (SELECT min(a.at) FROM audit_log a
             WHERE a.action = 'valuation.create' AND a.entity_id = v.id),
            v.as_of::timestamp AT TIME ZONE 'UTC'
        );
    """)
    op.execute("""
        ALTER TABLE valuations
            ALTER COLUMN created_at SET NOT NULL,
            ALTER COLUMN created_at SET DEFAULT clock_timestamp();
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE valuations DROP COLUMN created_at;")
