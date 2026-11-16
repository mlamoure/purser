"""Response-based sharing evidence and stable integration identities (#525)."""

from alembic import op

revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE api_tokens ADD COLUMN integration_id UUID;
        ALTER TABLE api_tokens ADD COLUMN authorization_enc BYTEA;
        UPDATE api_tokens SET integration_id = id WHERE kind = 'api';
        CREATE INDEX api_tokens_integration ON api_tokens(integration_id);
        ALTER TABLE audit_log ADD COLUMN channel TEXT;
        ALTER TABLE audit_log ADD COLUMN actor_kind TEXT;
        ALTER TABLE audit_log ADD COLUMN integration_id UUID;
        ALTER TABLE audit_log ADD COLUMN outcome TEXT;
        ALTER TABLE audit_log ADD COLUMN data_types TEXT[];
        ALTER TABLE audit_log ADD COLUMN account_ids UUID[];
        CREATE INDEX audit_log_integration_at ON audit_log(integration_id, at, id);
        CREATE INDEX audit_log_data_types ON audit_log USING gin(data_types);
        CREATE INDEX audit_log_account_ids ON audit_log USING gin(account_ids);
        CREATE TABLE audit_receipts (
            event_id UUID PRIMARY KEY REFERENCES audit_log(id) ON DELETE CASCADE,
            at TIMESTAMPTZ NOT NULL DEFAULT now(),
            state TEXT NOT NULL CHECK (state IN ('returned', 'interrupted'))
        );
    """)


def downgrade() -> None:
    op.execute("""
        DROP TABLE audit_receipts;
        ALTER TABLE audit_log DROP COLUMN channel, DROP COLUMN actor_kind,
            DROP COLUMN integration_id, DROP COLUMN outcome,
            DROP COLUMN data_types, DROP COLUMN account_ids;
        ALTER TABLE api_tokens DROP COLUMN integration_id, DROP COLUMN authorization_enc;
    """)
