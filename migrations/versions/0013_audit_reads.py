"""Audit call rows: kind + request_id + detail, read auditing (issue #122).

``kind`` distinguishes call rows (``read``/``write`` — one per authenticated
HTTP request or MCP tool call, carrying a ``detail`` payload) from the
per-entity ``change`` rows ``audit.record()`` has always written. Existing
rows are all change rows — the server_default backfills them in place.
``request_id`` groups a call row with the change rows it produced.

Indexes: ``(at, id)`` for keyset pagination + retention pruning (supersedes
the single-column ``audit_log_at``), ``(actor_token_id, at)`` for session
grouping, and a partial index on ``request_id`` for drill-ins.

Revision ID: 0013
Revises: 0012
Create Date: 2026-07-12

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "audit_log",
        sa.Column("kind", sa.Text(), server_default="change", nullable=False),
    )
    op.create_check_constraint(
        "audit_log_kind_valid", "audit_log", "kind IN ('read', 'write', 'change')"
    )
    op.add_column("audit_log", sa.Column("request_id", postgresql.UUID(as_uuid=True)))
    op.add_column("audit_log", sa.Column("detail", postgresql.JSONB()))

    op.create_index("audit_log_at_id", "audit_log", ["at", "id"])
    op.drop_index("audit_log_at", table_name="audit_log")
    op.create_index("audit_log_actor_at", "audit_log", ["actor_token_id", "at"])
    op.create_index(
        "audit_log_request",
        "audit_log",
        ["request_id"],
        postgresql_where=sa.text("request_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("audit_log_request", table_name="audit_log")
    op.drop_index("audit_log_actor_at", table_name="audit_log")
    op.create_index("audit_log_at", "audit_log", ["at"])
    op.drop_index("audit_log_at_id", table_name="audit_log")
    op.drop_column("audit_log", "detail")
    op.drop_column("audit_log", "request_id")
    op.drop_constraint("audit_log_kind_valid", "audit_log")
    op.drop_column("audit_log", "kind")
