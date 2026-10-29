"""Manually tracked brokerage holdings (issue #390 phase 1a, decisions 88/89).

Some brokerages send an account's cash through SimpleFIN but no holdings, so
Purser had no way to record what the account owns. This migration adds:

- ``accounts.holdings_source`` (``feed`` | ``manual``, default ``feed``),
  brokerage only. It is exclusive per account: a ``manual`` account's feed
  holdings are skipped at import, so the newest snapshot set is always one
  source's complete set and the read path stays unchanged.
  ``accounts.holdings_source_changed_at`` keeps the moment it last changed
  and ``manual_period_open`` whether a manual period still waits to be
  ended: a fetch taken before the switch back to the feed is inside the
  manual period, and the first one taken after it ends the period.
- ``positions``: one symbol held in one account. At most one OPEN position
  per (account, symbol).
- ``position_events``: the append-only share-count history (``set_quantity``
  | ``buy`` | ``sell``). A correction is a reversal row (``reverses_id``),
  never an edit: a BEFORE UPDATE trigger refuses every UPDATE, the shape of
  ``transactions_posted_guard``. Rows go only with their account, through the
  guarded delete-account CLI. ``created_at`` is ``clock_timestamp()`` so two
  events recorded in one transaction keep the order they were recorded in.
- ``security_prices``: daily closes per symbol and source (``manual`` |
  ``tiingo``). A feed-implied price is computed on read from feed snapshots
  and never stored here.
- ``price_answered``: per symbol and provider, the date ranges the provider
  has answered for good (a ``datemultirange``, Postgres 14+), so a range it
  has nothing for (before a listing, after a delisting, a weekend) is asked
  for once, and a gap between two answered ranges is still asked for.
- ``holdings_snapshots`` gains ``source`` (``feed`` | ``manual``),
  ``position_id``, ``price`` and ``price_as_of``. Manual rows are a derived
  projection of the events and prices, one row per (account, date, position).

DESIGN.md §3.1 and §8 updated in the same PR (schema rule).

Revision ID: 0036
Revises: 0035
Create Date: 2026-10-18

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0036"
down_revision: str | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE_DDL = """
ALTER TABLE accounts ADD COLUMN holdings_source TEXT NOT NULL DEFAULT 'feed';
ALTER TABLE accounts
  ADD CONSTRAINT accounts_holdings_source_values
  CHECK (holdings_source IN ('feed', 'manual'));
-- Brokerage only. Categories are never brokerage, so this also keeps them out.
ALTER TABLE accounts
  ADD CONSTRAINT accounts_holdings_source_brokerage
  CHECK (holdings_source = 'feed' OR type = 'brokerage');
-- When holdings_source last changed (NULL until it first does), and whether
-- a manual period still waits to be ended: a feed fetch taken before the
-- switch back to the feed falls inside the manual period and adds nothing;
-- the first one taken after it ends the period (decision 88).
ALTER TABLE accounts ADD COLUMN holdings_source_changed_at TIMESTAMPTZ;
ALTER TABLE accounts ADD COLUMN manual_period_open BOOLEAN NOT NULL DEFAULT false;

CREATE TABLE positions (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id  UUID NOT NULL REFERENCES accounts(id),
  symbol      TEXT NOT NULL,   -- upper-case ticker, plaintext like holdings_snapshots.symbol
  name        TEXT,
  opened_on   DATE NOT NULL,   -- the earliest event date (reversals never move it later)
  closed_at   TIMESTAMPTZ,     -- set only while the position holds nothing
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT positions_symbol_format CHECK (symbol ~ '^[A-Z0-9][A-Z0-9.-]{0,14}$')
);
CREATE UNIQUE INDEX positions_open_symbol_uniq
  ON positions (account_id, symbol) WHERE closed_at IS NULL;
CREATE INDEX positions_symbol ON positions (symbol);

CREATE TABLE position_events (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  position_id     UUID NOT NULL REFERENCES positions(id),
  occurred_on     DATE NOT NULL,          -- never in the future (the service refuses it)
  kind            TEXT NOT NULL,
  quantity        NUMERIC(20,6) NOT NULL, -- set_quantity: absolute; buy/sell: the delta
  price           NUMERIC(20,6),          -- per share, buy/sell only
  fees            NUMERIC(20,4),          -- buy/sell only
  cost_basis      NUMERIC(20,4),          -- set_quantity only: total basis after it
  reverses_id     UUID REFERENCES position_events(id),
  origin          TEXT NOT NULL,
  note_enc        BYTEA,                  -- Fernet (ORM attr: note)
  source_note_enc BYTEA,                  -- Fernet (ORM attr: source_note): the figure's origin
  actor_token_id  UUID REFERENCES api_tokens(id),
  created_at      TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  CONSTRAINT position_events_kind_values
    CHECK (kind IN ('set_quantity', 'buy', 'sell')),
  CONSTRAINT position_events_origin_values
    CHECK (origin IN ('manual', 'agent', 'import')),
  CONSTRAINT position_events_quantity_shape
    CHECK (quantity >= 0 AND (kind = 'set_quantity' OR quantity > 0)),
  CONSTRAINT position_events_price_positive CHECK (price IS NULL OR price > 0),
  CONSTRAINT position_events_fees_nonnegative CHECK (fees IS NULL OR fees >= 0),
  CONSTRAINT position_events_cost_basis_nonnegative CHECK (cost_basis IS NULL OR cost_basis >= 0),
  CONSTRAINT position_events_set_quantity_shape
    CHECK (kind <> 'set_quantity' OR (price IS NULL AND fees IS NULL)),
  CONSTRAINT position_events_trade_shape
    CHECK (kind = 'set_quantity' OR cost_basis IS NULL)
);
-- An event is reversed at most once.
CREATE UNIQUE INDEX position_events_reversed_once
  ON position_events (reverses_id) WHERE reverses_id IS NOT NULL;
CREATE INDEX position_events_position ON position_events (position_id, occurred_on);

CREATE FUNCTION position_events_append_only() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'position events are append-only; corrections are linked reversals';
END $$ LANGUAGE plpgsql;

CREATE TRIGGER position_events_immutable
  BEFORE UPDATE ON position_events
  FOR EACH ROW EXECUTE FUNCTION position_events_append_only();

CREATE TABLE security_prices (
  symbol     TEXT NOT NULL,
  price_date DATE NOT NULL,
  close      NUMERIC(20,6) NOT NULL,
  source     TEXT NOT NULL,
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (symbol, price_date, source),
  CONSTRAINT security_prices_close_positive CHECK (close > 0),
  -- Widened when another provider lands; feed-implied prices are never stored.
  CONSTRAINT security_prices_source_values CHECK (source IN ('manual', 'tiingo')),
  CONSTRAINT security_prices_symbol_format CHECK (symbol ~ '^[A-Z0-9][A-Z0-9.-]{0,14}$')
);

-- The date ranges a provider has answered for good, per symbol: closes, or
-- a final "nothing" (before a listing, a weekend, an unknown ticker), so a
-- range is asked for once, and a gap between answered ranges still is.
CREATE TABLE price_answered (
  symbol     TEXT NOT NULL,
  source     TEXT NOT NULL,
  answered   DATEMULTIRANGE NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (symbol, source),
  -- Widened with security_prices_source_values when another provider lands.
  CONSTRAINT price_answered_source_values CHECK (source IN ('tiingo')),
  CONSTRAINT price_answered_symbol_format CHECK (symbol ~ '^[A-Z0-9][A-Z0-9.-]{0,14}$')
);

ALTER TABLE holdings_snapshots
  ADD COLUMN source TEXT NOT NULL DEFAULT 'feed',
  ADD COLUMN position_id UUID REFERENCES positions(id),
  ADD COLUMN price NUMERIC(20,6),
  ADD COLUMN price_as_of DATE;
ALTER TABLE holdings_snapshots
  ADD CONSTRAINT holdings_snapshots_source_values CHECK (source IN ('feed', 'manual'));
-- A manual row is one position's projection; a feed row never names one.
ALTER TABLE holdings_snapshots
  ADD CONSTRAINT holdings_manual_has_position
  CHECK ((source = 'manual') = (position_id IS NOT NULL));
CREATE UNIQUE INDEX holdings_manual_uniq
  ON holdings_snapshots (account_id, as_of, position_id) WHERE source = 'manual';
-- Feed-implied prices: the feed rows of one symbol, by date.
CREATE INDEX holdings_feed_symbol
  ON holdings_snapshots (symbol, as_of) WHERE source = 'feed';
"""

DOWNGRADE_DDL = """
DROP INDEX IF EXISTS holdings_feed_symbol;
DROP INDEX IF EXISTS holdings_manual_uniq;
DELETE FROM holdings_snapshots WHERE source = 'manual';
ALTER TABLE holdings_snapshots DROP CONSTRAINT IF EXISTS holdings_manual_has_position;
ALTER TABLE holdings_snapshots DROP CONSTRAINT IF EXISTS holdings_snapshots_source_values;
ALTER TABLE holdings_snapshots
  DROP COLUMN IF EXISTS price_as_of,
  DROP COLUMN IF EXISTS price,
  DROP COLUMN IF EXISTS position_id,
  DROP COLUMN IF EXISTS source;

DROP TABLE IF EXISTS price_answered;
DROP TABLE IF EXISTS security_prices;
DROP TABLE IF EXISTS position_events;
DROP FUNCTION IF EXISTS position_events_append_only();
DROP TABLE IF EXISTS positions;

ALTER TABLE accounts DROP COLUMN IF EXISTS manual_period_open;
ALTER TABLE accounts DROP COLUMN IF EXISTS holdings_source_changed_at;
ALTER TABLE accounts DROP CONSTRAINT IF EXISTS accounts_holdings_source_brokerage;
ALTER TABLE accounts DROP CONSTRAINT IF EXISTS accounts_holdings_source_values;
ALTER TABLE accounts DROP COLUMN IF EXISTS holdings_source;
"""


def upgrade() -> None:
    op.execute(UPGRADE_DDL)


def downgrade() -> None:
    op.execute(DOWNGRADE_DDL)
