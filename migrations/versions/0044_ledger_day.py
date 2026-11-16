"""The readmodel's "today" follows the household time zone (issue #471, decision 105).

``readmodel.v1_forecast_daily`` started its 60-day projection at
``CURRENT_DATE`` — the date in the reading session's time zone (Grafana's
connection, usually the server's UTC) — while the app now takes the ledger day
from the ``timezone`` setting. The view must agree with the app, and a view
cannot take parameters, so the zone has to be readable in SQL:

- ``ledger_clock`` — one row holding the ``PURSER_TIMEZONE`` the running
  instance was started with (``env_timezone``, default ``UTC``). Every
  ``purser serve`` / ``worker`` / ``migrate`` writes it right after the
  upgrade (``cli.run_migrations``), so it is the environment default as the app
  sees it. A runtime override lives where every override lives, in
  ``app_settings`` (key ``timezone``), and wins.
- ``readmodel.ledger_today()`` — ``now()`` in the override, else the env
  default, else UTC, as a date. SECURITY DEFINER so ``purser_read`` needs no
  grant on ``app_settings`` (it reads one non-secret key, nothing else).
- ``readmodel.v1_forecast_daily`` — the same definition with
  ``CURRENT_DATE`` replaced by ``readmodel.ledger_today()``: same columns,
  same types, same grant (the readmodel contract, DESIGN §3.1). With the
  default UTC setting it returns exactly what it did, on a UTC server.

No stored row changes. The downgrade restores 0004's view and drops the
function and the table.

Revision ID: 0044
Revises: 0043
Create Date: 2026-11-08

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0044"
down_revision: str | None = "0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CLOCK = """
CREATE TABLE ledger_clock (
  singleton     BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
  env_timezone  TEXT NOT NULL DEFAULT 'UTC',
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO ledger_clock DEFAULT VALUES;

CREATE FUNCTION readmodel.ledger_today() RETURNS date
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
  SELECT (now() AT TIME ZONE COALESCE(
    (SELECT s.value #>> '{}' FROM public.app_settings s
      WHERE s.key = 'timezone' AND jsonb_typeof(s.value) = 'string'),
    (SELECT c.env_timezone FROM public.ledger_clock c),
    'UTC'
  ))::date
$$;
REVOKE ALL ON FUNCTION readmodel.ledger_today() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION readmodel.ledger_today() TO purser_read;
"""

_FORECAST = """
CREATE OR REPLACE VIEW readmodel.v1_forecast_daily AS
WITH days AS (
  SELECT generate_series({today}, {today} + 60, interval '1 day')::date AS day
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
  SELECT p.account_id, GREATEST(t.date, {today}) AS effective_date,
         SUM(p.amount) AS amount
  FROM postings p
  JOIN transactions t ON t.id = p.transaction_id
  WHERE t.status = 'expected' AND t.superseded_by IS NULL
  GROUP BY p.account_id, GREATEST(t.date, {today})
)
SELECT d.day, b.account_path,
       b.balance + COALESCE((
         SELECT SUM(e.amount) FROM expected e
         WHERE e.account_id = b.id AND e.effective_date <= d.day
       ), 0) AS projected_balance
FROM days d CROSS JOIN balances b;
"""


def upgrade() -> None:
    op.execute(_CLOCK)
    op.execute(_FORECAST.format(today="readmodel.ledger_today()"))


def downgrade() -> None:
    op.execute(_FORECAST.format(today="CURRENT_DATE"))
    op.execute("DROP FUNCTION readmodel.ledger_today()")
    op.execute("DROP TABLE ledger_clock")
