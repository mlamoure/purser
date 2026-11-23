"""Agent-requested privacy (issue #613, DESIGN decision 125).

Privacy with agents becomes a per-call choice the agent makes (``withhold``
on MCP tools and REST read routes) instead of two user settings that withheld
fields until the agent asked for them back:

- The ``privacy_purchase`` and ``privacy_amount`` rows leave ``app_settings``
  (the settings no longer exist; the app already ignored unknown keys).
- ``audit_log.privacy_requested``: the fields a call asked to withhold.
- ``audit_log.identifiable``: the payee, description and amount fields a
  call's answer returned with real values.

Both columns are plain (the call detail is encrypted), so the audit view can
filter on "used privacy" and "returned identifiable data". NULL on rows
written before this revision and on rows that are not integration calls.

Downgrade drops the two columns. The settings rows are not restored: their
values are gone, and both settings defaulted to off.

Revision ID: 0060
Revises: 0059
Create Date: 2026-11-22

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0060"
down_revision: str | None = "0059"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("DELETE FROM app_settings WHERE key IN ('privacy_purchase', 'privacy_amount')")
    op.execute("ALTER TABLE audit_log ADD COLUMN privacy_requested TEXT[]")
    op.execute("ALTER TABLE audit_log ADD COLUMN identifiable TEXT[]")


def downgrade() -> None:
    op.execute("ALTER TABLE audit_log DROP COLUMN identifiable")
    op.execute("ALTER TABLE audit_log DROP COLUMN privacy_requested")
