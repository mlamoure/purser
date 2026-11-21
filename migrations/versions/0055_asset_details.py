"""Asset kinds, the loan link and money put in (#567).

- ``asset_details``: one row per property account — its kind (a label and an
  icon), when it was acquired, and an encrypted description. Existing
  property accounts get kind ``other``.
- ``accounts.secured_by_id``: a loan secured by a property account; the
  asset's equity is its value plus those loans (ledger sign).
- ``equity.capital``: the system account a "money put in" entry balances
  against — the purchase of an asset whose payment Purser has no record of,
  or an improvement paid from an account Purser does not track — so it never
  reads as a gain in value (which is ``equity.revaluation``).
"""

from alembic import op

revision = "0055"
down_revision = "0054"
branch_labels = None
depends_on = None

KINDS = ("house", "land", "car", "boat", "rv", "jewelry", "art", "equipment", "other")


def upgrade() -> None:
    kinds = ", ".join(f"'{k}'" for k in KINDS)
    op.execute(f"""
        CREATE TABLE asset_details (
            account_id UUID PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
            kind TEXT NOT NULL DEFAULT 'other' CHECK (kind IN ({kinds})),
            acquired_on DATE,
            description_enc BYTEA,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        INSERT INTO asset_details (account_id)
        SELECT id FROM accounts WHERE type = 'property' AND NOT is_virtual;
        ALTER TABLE accounts ADD COLUMN secured_by_id UUID REFERENCES accounts(id);
        INSERT INTO accounts (name, type, is_virtual, path, description)
        SELECT 'Capital', 'equity', true, CAST('equity.capital' AS ltree),
               'System account: the other side of money put into a hand-valued asset '
               || 'that no tracked account paid. Not a category.'
        WHERE NOT EXISTS (
            SELECT 1 FROM accounts WHERE path = CAST('equity.capital' AS ltree));
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE accounts DROP COLUMN secured_by_id;
        DROP TABLE asset_details;
        DELETE FROM accounts a
        WHERE a.path = CAST('equity.capital' AS ltree) AND a.type = 'equity'
          AND NOT EXISTS (SELECT 1 FROM postings p WHERE p.account_id = a.id);
    """)
