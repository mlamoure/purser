"""Per-subscription fan-out cursor (issue #219, DESIGN decision 54).

``fan_out`` anti-joined the FULL events table per subscription per pass —
unbounded growth — and a brand-new subscription received the entire
historical backlog as deliveries. ``webhook_subscriptions.cursor_at`` is a
watermark over ``events.at``: each pass scans only events at/after it and
advances it to the newest event scanned; the ``(subscription_id, event_id)``
unique constraint eats the >=-boundary overlap via ON CONFLICT DO NOTHING.

Backfill: existing subscriptions start at their newest delivered event's
``at`` (they have already full-scanned everything, so anything between that
and now is delivered exactly once on the next pass); subscriptions with no
deliveries — and every future subscription via the DEFAULT — start at now(),
which is the #219 decision: a new subscription gets no historical backlog.

Revision ID: 0024
Revises: 0023
Create Date: 2026-08-26

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE_DDL = """
ALTER TABLE webhook_subscriptions
  ADD COLUMN cursor_at TIMESTAMPTZ NOT NULL DEFAULT now();

UPDATE webhook_subscriptions s
SET cursor_at = COALESCE(
  (SELECT max(e.at) FROM webhook_deliveries d
     JOIN events e ON e.id = d.event_id
    WHERE d.subscription_id = s.id),
  now()
);
"""

DOWNGRADE_DDL = """
ALTER TABLE webhook_subscriptions DROP COLUMN IF EXISTS cursor_at;
"""


def upgrade() -> None:
    op.execute(UPGRADE_DDL)


def downgrade() -> None:
    op.execute(DOWNGRADE_DDL)
