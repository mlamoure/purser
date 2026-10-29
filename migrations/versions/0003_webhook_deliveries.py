"""webhook_deliveries — per-subscription delivery log with retry state.

Durable events table + webhook fan-out (DESIGN §12 decision 12): agents can
replay missed events; deliveries retry with backoff and keep their outcome.
DESIGN.md §3 updated in the same PR (schema rule).

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-03

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE webhook_deliveries (
          id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          subscription_id UUID NOT NULL REFERENCES webhook_subscriptions(id),
          event_id        UUID NOT NULL REFERENCES events(id),
          status          TEXT NOT NULL DEFAULT 'pending'
                          CHECK (status IN ('pending', 'delivered', 'failed')),
          attempts        INT NOT NULL DEFAULT 0,
          next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          last_attempt_at TIMESTAMPTZ,
          response_status INT,
          created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
          UNIQUE (subscription_id, event_id)
        );
        CREATE INDEX webhook_deliveries_due
          ON webhook_deliveries (next_attempt_at) WHERE status = 'pending';
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS webhook_deliveries;")
