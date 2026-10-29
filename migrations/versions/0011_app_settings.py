"""app_settings: persisted, admin-editable runtime configuration (issue #102).

One row per curated setting key (registry in ``purser/appsettings.py``).
Environment variables remain the bootstrap defaults; a row here overrides
them. Non-secret values live in ``value`` (JSONB); secret values (e.g. the
AI provider API key) live Fernet-encrypted in ``value_enc`` — exactly one of
the two is set. Every change is written through the audit log by the
application layer.

Revision ID: 0011
Revises: 0010
Create Date: 2026-07-11

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DDL = """
CREATE TABLE app_settings (
  key        TEXT PRIMARY KEY,
  value      JSONB,
  value_enc  BYTEA,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_by UUID REFERENCES api_tokens(id),
  CONSTRAINT app_settings_value_shape CHECK ((value IS NULL) <> (value_enc IS NULL))
);
"""


def upgrade() -> None:
    op.execute(DDL)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS app_settings")
