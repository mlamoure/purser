"""The holdings view shows no set dated after the ledger day (#498 review, decision 104).

A hand-tracked sale whose cash lands after today now has its landing day's
set written ahead, so the transit ends the day the cash posts without waiting
for the next projection. Every Python reader takes sets at or before a day
(``holdings_set_on`` defaults to the ledger day); ``readmodel.v1_holdings_latest``
took each (account, symbol)'s newest row with no bound, so Grafana would show
the post-landing row early. This re-creates 0039's definition with
``WHERE h.as_of <= readmodel.ledger_today()`` (0044): same columns, same
types, same grant (the readmodel contract, DESIGN section 3.1). No stored row
changes. The downgrade restores 0039's view.

Revision ID: 0045
Revises: 0044
Create Date: 2026-11-10

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0045"
down_revision: str | None = "0044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BOUNDED = """
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
  -- A set dated after the ledger day (a sale's cash landing later, #498
  -- review) is not in effect yet.
  WHERE h.as_of <= readmodel.ledger_today()
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

_UNBOUNDED = """
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


def upgrade() -> None:
    op.execute(_BOUNDED)


def downgrade() -> None:
    op.execute(_UNBOUNDED)
