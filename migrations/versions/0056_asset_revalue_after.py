"""asset_details.revalue_after_days: how old an asset's value may get (#568).

When an asset's newest value — a value entry or a recorded value on a date —
is older than this, the daily check emits ``asset.stale`` once and
``whats_new`` lists the asset until its value is refreshed. NULL = never.
"""

from alembic import op

revision = "0056"
down_revision = "0055"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE asset_details
            ADD COLUMN revalue_after_days INTEGER
            CHECK (revalue_after_days BETWEEN 1 AND 3650);
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE asset_details DROP COLUMN revalue_after_days;")
