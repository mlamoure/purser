"""Split transactions in the flat read view (issue #274, decision 87).

A split transaction is one real leg plus two or more category legs (DESIGN
§3.3). ``readmodel.v2_transactions_flat`` picked its ``category_path`` with an
unordered ``LIMIT 1``, so a split row showed an arbitrary one of its
categories. The view now reports ``category_path`` NULL when a row has more
than one category leg, as the REST flat view does. Same columns, same order,
same grant: the readmodel contract (DESIGN §3.1) holds. ``is_transfer`` keeps
reading the first category leg, so it is unchanged.

The downgrade restores 0016's definition.

Revision ID: 0035
Revises: 0034
Create Date: 2026-10-13

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_HEAD = """
CREATE OR REPLACE VIEW readmodel.v2_transactions_flat AS
SELECT
  t.id,
  t.date,
  t.status,
  t.origin,
  real_leg.amount,
  real_acct.path::text AS account_path,
"""

_SPLIT_AWARE_CATEGORY = """
  CASE
    WHEN (SELECT COUNT(*) FROM postings p3
            JOIN accounts a3 ON a3.id = p3.account_id
           WHERE p3.transaction_id = t.id AND a3.is_virtual) > 1 THEN NULL
    ELSE cat_acct.path::text
  END AS category_path,
"""

_ANY_CATEGORY = """
  cat_acct.path::text AS category_path,
"""

_TAIL = """
  (SELECT COUNT(*) = 2 FROM postings p2
     JOIN accounts a2 ON a2.id = p2.account_id
    WHERE p2.transaction_id = t.id AND NOT a2.is_virtual)
   AND cat_acct.id IS NULL AS is_transfer,
  t.created_at
FROM transactions t
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
"""


def upgrade() -> None:
    op.execute(_HEAD + _SPLIT_AWARE_CATEGORY + _TAIL)


def downgrade() -> None:
    op.execute(_HEAD + _ANY_CATEGORY + _TAIL)
