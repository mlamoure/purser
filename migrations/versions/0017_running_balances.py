"""Stored running balances + the balance dirty-watermark (issues #146/#145,
decisions 37/38).

``account_running_balances`` — one row per (real account, chain-head
transaction with status pending|posted having a posting on the account);
``delta`` = the account's posting sum in the transaction, ``balance`` = the
running sum in the total order (date, created_at, transaction_id). The
posted-immutability guards (0005) forbid storing this on transactions/
postings, so it lives in its own derived table (net_worth_daily precedent).

``balance_watermark`` — singleton with two independent dirty cursors
LEAST-updated by every balance-affecting writer: ``running_dirty_from``
(consumed synchronously at commit by the sessionmaker hook) and
``networth_dirty_from`` (consumed asynchronously by
``networth.consume_watermark``). Seeded here with ``networth_dirty_from =
MIN(transactions.date)`` so the FIRST consumer run re-materializes the whole
``net_worth_daily`` history — a one-time heal executed by the job (minutes,
per-day commits), deliberately NOT by this migration.

The backfill runs the SAME canonical recompute the runtime consumer uses
(``purser.balances``, 0016-imports-purser.crypto precedent), so migration
and runtime results can never drift.

**Downgrade is safe** (contrast 0016): both tables are derived data — plain
DROPs; re-running upgrade rebuilds byte-identical state from postings.

DESIGN.md §3.1/§3.5/§8 + decision rows 37/38 updated in the same PR.

Revision ID: 0017
Revises: 0016
Create Date: 2026-07-13

"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy.orm import Session

from purser import balances

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE account_running_balances (
          account_id     UUID NOT NULL REFERENCES accounts(id),
          transaction_id UUID NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
          date           DATE NOT NULL,
          created_at     TIMESTAMPTZ NOT NULL,
          currency       CHAR(3) NOT NULL DEFAULT 'USD',
          delta          NUMERIC(20,4) NOT NULL,
          balance        NUMERIC(20,4) NOT NULL,
          PRIMARY KEY (account_id, transaction_id)
        );
        """
    )
    op.execute(
        """
        CREATE INDEX account_running_balances_order_idx
          ON account_running_balances (account_id, date, created_at, transaction_id);
        """
    )
    op.execute(
        """
        CREATE TABLE balance_watermark (
          id                  BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id),
          running_dirty_from  DATE,
          networth_dirty_from DATE,
          updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    op.execute(
        """
        INSERT INTO balance_watermark (id, running_dirty_from, networth_dirty_from)
        SELECT TRUE, NULL, MIN(date) FROM transactions;
        """
    )
    # Full backfill over all existing history via the canonical recompute.
    session = Session(bind=op.get_bind())
    balances.rebuild_running(session)
    session.flush()


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS account_running_balances;")
    op.execute("DROP TABLE IF EXISTS balance_watermark;")
