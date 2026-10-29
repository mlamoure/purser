"""Feed funds with no value valued from stored prices in the holdings view
(issue #392, decision 92).

Some plans' feeds report a held fund with no value: a plan-only fund no
market-data provider prices, sent as 0 or not at all (the importer stores a
numeric 0 or a missing value as NULL). Purser's readers value such a FEED row
(``quantity > 0``, ``market_value`` 0 or NULL) on read as ``quantity *
close``, the stored close (``security_prices``) in effect on the row's date,
matched on the symbol as ``record_price`` stores it (stripped, upper case):
newest date first, a manual close over a provider's on the same date, every
close range-checked, a value that does not fit an amount left without one.
``readmodel.v1_holdings_latest`` applies the same overlay, so Grafana agrees
with Purser: same columns, same order, same types, same grant (the readmodel
contract, DESIGN §3.1). The value rounds to 4 places half to even, as
Python's ``Decimal.quantize`` does for every market value. The price lookup
runs once per row the view returns, after DISTINCT ON picks each (account,
symbol)'s newest row, never once per historical row. No stored row changes.
The same rule lives in ``core.feed_value`` and ``holdings.overlay``
(``FEED_VALUE_SQL``); parity tests pin them together.

The downgrade restores 0004's definition.

Revision ID: 0039
Revises: 0038
Create Date: 2026-10-19

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0039"
# Re-chained onto 1b's 0037 when the branches merge (#390, #392).
down_revision: str | None = "0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OVERLAID = """
CREATE OR REPLACE VIEW readmodel.v1_holdings_latest AS
SELECT
  l.account_path,
  l.symbol,
  l.as_of,
  l.quantity,
  l.cost_basis,
  (CASE
     WHEN l.source = 'feed' AND l.quantity > 0
          AND (l.market_value IS NULL OR l.market_value = 0)
       THEN CASE WHEN o.value < 1e16 THEN o.value END
     ELSE l.market_value
   END)::numeric(20,4) AS market_value
FROM (
  SELECT DISTINCT ON (h.account_id, h.symbol)
    h.account_id,
    a.path::text AS account_path,
    h.symbol,
    h.as_of,
    h.quantity,
    h.cost_basis,
    h.market_value,
    h.source
  FROM holdings_snapshots h
  JOIN accounts a ON a.id = h.account_id
  ORDER BY h.account_id, h.symbol, h.as_of DESC
) l
LEFT JOIN LATERAL (
  -- quantity * close rounded to 4 places, half to even (s is that product
  -- times 10^4; exact numeric arithmetic).
  SELECT (trunc(best.s) + CASE
            WHEN best.s - trunc(best.s) > 0.5 THEN 1
            WHEN best.s - trunc(best.s) < 0.5 THEN 0
            WHEN mod(trunc(best.s), 2) = 0 THEN 0
            ELSE 1
          END) / 10000 AS value
  FROM (
    SELECT l.quantity * p.close * 10000 AS s
    FROM security_prices p
    WHERE l.source = 'feed' AND l.quantity > 0
      AND (l.market_value IS NULL OR l.market_value = 0)
      AND p.symbol = upper(btrim(l.symbol))
      AND p.price_date <= l.as_of
      AND p.close > 0 AND p.close < 1e14
    ORDER BY p.price_date DESC, (p.source <> 'manual'), p.source, p.close
    LIMIT 1
  ) best
) o ON true
ORDER BY l.account_id, l.symbol;
"""

_STORED = """
CREATE OR REPLACE VIEW readmodel.v1_holdings_latest AS
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
"""


def upgrade() -> None:
    op.execute(_OVERLAID)


def downgrade() -> None:
    op.execute(_STORED)
