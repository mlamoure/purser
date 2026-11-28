"""A second price provider and per-symbol price settings (#686, decision 139).

- ``security_prices.source`` and ``price_answered.source`` accept ``eodhd``.
- ``security_settings``: per symbol, a ticker alias per provider (the
  provider's own spelling, e.g. ``VOD.LSE``) and ``price_by_hand``: never
  sent to a provider. Plaintext: ticker symbols, like ``security_prices``.

DESIGN.md section 3.1 is updated in the same PR.

Revision ID: 0064
Revises: 0063
Create Date: 2026-11-23

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0064"
down_revision: str | None = "0063"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
ALTER TABLE security_prices
  DROP CONSTRAINT security_prices_source_values,
  ADD CONSTRAINT security_prices_source_values
    CHECK (source IN ('manual', 'tiingo', 'eodhd'));

ALTER TABLE price_answered
  DROP CONSTRAINT price_answered_source_values,
  ADD CONSTRAINT price_answered_source_values CHECK (source IN ('tiingo', 'eodhd'));

CREATE TABLE security_settings (
  symbol        TEXT PRIMARY KEY,
  price_by_hand BOOLEAN NOT NULL DEFAULT false,
  aliases       JSONB NOT NULL DEFAULT '{}'::jsonb,
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT security_settings_symbol_format CHECK (symbol ~ '^[A-Z0-9][A-Z0-9.-]{0,14}$'),
  CONSTRAINT security_settings_aliases_object CHECK (jsonb_typeof(aliases) = 'object')
);
"""
    )


def downgrade() -> None:
    op.execute(
        """
DROP TABLE security_settings;
DELETE FROM price_answered WHERE source = 'eodhd';
DELETE FROM security_prices WHERE source = 'eodhd';
ALTER TABLE price_answered
  DROP CONSTRAINT price_answered_source_values,
  ADD CONSTRAINT price_answered_source_values CHECK (source IN ('tiingo'));
ALTER TABLE security_prices
  DROP CONSTRAINT security_prices_source_values,
  ADD CONSTRAINT security_prices_source_values CHECK (source IN ('manual', 'tiingo'));
"""
    )
