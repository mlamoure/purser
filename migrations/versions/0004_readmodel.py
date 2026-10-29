"""readmodel schema — versioned read views + read-only role (issue #13).

The v1_* views are a PUBLIC CONTRACT for Grafana (and anything else that
reads the database directly): their columns are pinned by tests and survive
internal refactors. The purser_read role is NOLOGIN; operators enable it
with ALTER ROLE purser_read LOGIN PASSWORD '...' (see README).
DESIGN.md §3 updated in the same PR (schema rule).

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-03

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DDL = """
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'purser_read') THEN
    CREATE ROLE purser_read NOLOGIN;
  END IF;
END $$;

CREATE SCHEMA IF NOT EXISTS readmodel;

-- Flat transactions: chain heads only (corrections collapse), void hidden.
CREATE VIEW readmodel.v1_transactions_flat AS
SELECT
  t.id,
  t.date,
  t.status,
  t.origin,
  py.name AS payee,
  t.description,
  real_leg.amount,
  real_acct.path::text AS account_path,
  cat_acct.path::text AS category_path,
  (SELECT COUNT(*) = 2 FROM postings p2
     JOIN accounts a2 ON a2.id = p2.account_id
    WHERE p2.transaction_id = t.id AND NOT a2.is_virtual)
   AND cat_acct.id IS NULL AS is_transfer,
  t.created_at
FROM transactions t
LEFT JOIN payees py ON py.id = t.payee_id
LEFT JOIN LATERAL (
  SELECT p.amount, p.account_id FROM postings p
  JOIN accounts a ON a.id = p.account_id
  WHERE p.transaction_id = t.id AND NOT a.is_virtual
  ORDER BY p.amount LIMIT 1
) real_leg ON true
LEFT JOIN accounts real_acct ON real_acct.id = real_leg.account_id
LEFT JOIN LATERAL (
  SELECT a.id, a.path FROM postings p
  JOIN accounts a ON a.id = p.account_id
  WHERE p.transaction_id = t.id AND a.is_virtual
  LIMIT 1
) cat_acct ON true
WHERE t.status IN ('pending', 'posted')
  AND t.id NOT IN (
    SELECT reverses_id FROM transactions WHERE reverses_id IS NOT NULL
  );

-- Spending by category and month (expense category legs are positive).
CREATE VIEW readmodel.v1_spending_by_category_month AS
SELECT
  to_char(t.date, 'YYYY-MM') AS month,
  a.path::text AS category_path,
  SUM(p.amount) AS total,
  COUNT(DISTINCT t.id) AS transaction_count
FROM postings p
JOIN accounts a ON a.id = p.account_id
JOIN transactions t ON t.id = p.transaction_id
WHERE a.is_virtual
  AND a.type = 'category_expense'
  AND t.status IN ('pending', 'posted')
GROUP BY 1, 2;

-- Net worth series (materialized nightly; Grafana's flagship source).
CREATE VIEW readmodel.v1_net_worth_daily AS
SELECT as_of, currency, assets, liabilities, net_worth, computed_at
FROM net_worth_daily;

-- Deterministic 60-day projection per real account: current balance plus
-- expected instances (overdue ones land today).
CREATE VIEW readmodel.v1_forecast_daily AS
WITH days AS (
  SELECT generate_series(CURRENT_DATE, CURRENT_DATE + 60, interval '1 day')::date AS day
),
balances AS (
  SELECT a.id, a.path::text AS account_path,
         COALESCE(SUM(p.amount) FILTER (WHERE t.status IN ('pending', 'posted')), 0) AS balance
  FROM accounts a
  LEFT JOIN postings p ON p.account_id = a.id
  LEFT JOIN transactions t ON t.id = p.transaction_id
  WHERE NOT a.is_virtual
  GROUP BY a.id, a.path
),
expected AS (
  SELECT p.account_id, GREATEST(t.date, CURRENT_DATE) AS effective_date,
         SUM(p.amount) AS amount
  FROM postings p
  JOIN transactions t ON t.id = p.transaction_id
  WHERE t.status = 'expected' AND t.superseded_by IS NULL
  GROUP BY p.account_id, GREATEST(t.date, CURRENT_DATE)
)
SELECT d.day, b.account_path,
       b.balance + COALESCE((
         SELECT SUM(e.amount) FROM expected e
         WHERE e.account_id = b.id AND e.effective_date <= d.day
       ), 0) AS projected_balance
FROM days d CROSS JOIN balances b;

-- Latest holdings snapshot per (account, symbol).
CREATE VIEW readmodel.v1_holdings_latest AS
SELECT DISTINCT ON (h.account_id, h.symbol)
  a.path::text AS account_path,
  h.symbol,
  h.as_of,
  h.quantity,
  h.cost_basis,
  h.market_value
FROM holdings_snapshots h
JOIN accounts a ON a.id = h.account_id
ORDER BY h.account_id, h.symbol, h.as_of DESC;

GRANT USAGE ON SCHEMA readmodel TO purser_read;
GRANT SELECT ON ALL TABLES IN SCHEMA readmodel TO purser_read;
ALTER DEFAULT PRIVILEGES IN SCHEMA readmodel GRANT SELECT ON TABLES TO purser_read;
"""


def upgrade() -> None:
    op.execute(DDL)


def downgrade() -> None:
    op.execute("DROP SCHEMA IF EXISTS readmodel CASCADE;")
