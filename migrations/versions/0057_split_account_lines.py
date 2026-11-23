"""Editable split transactions with category and [Account] lines (issue #590,
DESIGN decision 118; amends 87, generalizes 62).

- ``transactions.split_account_id``: the marker that makes a row a split
  transaction, naming its OWN account — the real leg that keeps the bank's
  exact amount. A split's lines may now be other real accounts too, so its
  postings alone no longer say what it is: one category line plus one
  ``[Account]`` line is exactly a decision-62 loan payment's shape. Every
  classifier (``transfer_shape``, ``is_transfer``, the matcher, unlink, the
  purge, the transfer pool) reads the marker first, so a split is never a
  transfer whatever its shape, and its own leg is known without guessing.
- Backfill: every existing row with exactly one real leg and two or more
  category legs — decision 87's split shape, written by the QIF import —
  gets the marker, so one test classifies old and new splits alike. The
  posted guard (``transactions_posted_immutable``, 0005) allow-lists only a
  void with lineage, so the backfill runs with it disabled, as 0010 did; the
  value written is what the row's postings already say, never new content.
- ``suggestions.kind`` gains ``split``: "split like last time?" cards.
- ``readmodel.v2_transactions_flat``: a split's ``amount``/``account_path``
  are its own leg, ``category_path`` is NULL and ``is_transfer`` false. Same
  columns, same order, same grant (the readmodel contract, DESIGN §3.1).

Downgrade drops the marker and every ``split`` card and restores 0035's view.
A split with ``[Account]`` lines written meanwhile stays in the ledger as a
multi-posting row, classified by shape again.

Revision ID: 0057
Revises: 0056
Create Date: 2026-11-21

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0057"
down_revision: str | None = "0056"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMN = """
ALTER TABLE transactions ADD COLUMN split_account_id UUID REFERENCES accounts(id);
CREATE INDEX transactions_split_account
  ON transactions (split_account_id) WHERE split_account_id IS NOT NULL;
"""

_BACKFILL = """
ALTER TABLE transactions DISABLE TRIGGER transactions_posted_immutable;
WITH shape AS (
  SELECT p.transaction_id,
         COUNT(*) FILTER (WHERE NOT a.is_virtual) AS real_legs,
         COUNT(*) FILTER (WHERE a.is_virtual) AS category_legs,
         (ARRAY_AGG(p.account_id) FILTER (WHERE NOT a.is_virtual))[1] AS own_account
    FROM postings p
    JOIN accounts a ON a.id = p.account_id
   GROUP BY p.transaction_id
)
UPDATE transactions t
   SET split_account_id = shape.own_account
  FROM shape
 WHERE shape.transaction_id = t.id
   AND shape.real_legs = 1
   AND shape.category_legs >= 2;
ALTER TABLE transactions ENABLE TRIGGER transactions_posted_immutable;
"""

_KINDS_UP = """
ALTER TABLE suggestions DROP CONSTRAINT IF EXISTS suggestions_kind_check;
ALTER TABLE suggestions ADD CONSTRAINT suggestions_kind_check
  CHECK (kind IN ('schedule', 'transfer_link', 'match', 'transfer_pair', 'split'));
"""

_KINDS_DOWN = """
DELETE FROM suggestions WHERE kind = 'split';
ALTER TABLE suggestions DROP CONSTRAINT IF EXISTS suggestions_kind_check;
ALTER TABLE suggestions ADD CONSTRAINT suggestions_kind_check
  CHECK (kind IN ('schedule', 'transfer_link', 'match', 'transfer_pair'));
"""

_VIEW_HEAD = """
CREATE OR REPLACE VIEW readmodel.v2_transactions_flat AS
SELECT
  t.id,
  t.date,
  t.status,
  t.origin,
  real_leg.amount,
  real_acct.path::text AS account_path,
"""

_VIEW_CATEGORY_0035 = """
  CASE
    WHEN (SELECT COUNT(*) FROM postings p3
            JOIN accounts a3 ON a3.id = p3.account_id
           WHERE p3.transaction_id = t.id AND a3.is_virtual) > 1 THEN NULL
    ELSE cat_acct.path::text
  END AS category_path,
"""

_VIEW_CATEGORY_0057 = """
  CASE
    WHEN t.split_account_id IS NOT NULL THEN NULL
    WHEN (SELECT COUNT(*) FROM postings p3
            JOIN accounts a3 ON a3.id = p3.account_id
           WHERE p3.transaction_id = t.id AND a3.is_virtual) > 1 THEN NULL
    ELSE cat_acct.path::text
  END AS category_path,
"""

_VIEW_TRANSFER_0035 = """
  (SELECT COUNT(*) = 2 FROM postings p2
     JOIN accounts a2 ON a2.id = p2.account_id
    WHERE p2.transaction_id = t.id AND NOT a2.is_virtual)
   AND cat_acct.id IS NULL AS is_transfer,
"""

_VIEW_TRANSFER_0057 = """
  t.split_account_id IS NULL
   AND (SELECT COUNT(*) = 2 FROM postings p2
     JOIN accounts a2 ON a2.id = p2.account_id
    WHERE p2.transaction_id = t.id AND NOT a2.is_virtual)
   AND cat_acct.id IS NULL AS is_transfer,
"""

_VIEW_FROM = """
  t.created_at
FROM transactions t
LEFT JOIN LATERAL (
  SELECT p.amount, p.account_id FROM postings p
  JOIN accounts a ON a.id = p.account_id
  WHERE p.transaction_id = t.id AND NOT a.is_virtual
"""

_VIEW_REAL_LEG_0035 = """
  ORDER BY p.amount LIMIT 1
"""

# A split's row is its own leg (the bank line); otherwise the outgoing leg.
_VIEW_REAL_LEG_0057 = """
  ORDER BY (p.account_id = t.split_account_id) IS TRUE DESC, p.amount LIMIT 1
"""

_VIEW_TAIL = """
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


def _view(category: str, transfer: str, real_leg: str) -> str:
    return _VIEW_HEAD + category + transfer + _VIEW_FROM + real_leg + _VIEW_TAIL


def upgrade() -> None:
    op.execute(_COLUMN)
    op.execute(_BACKFILL)
    op.execute(_KINDS_UP)
    op.execute(_view(_VIEW_CATEGORY_0057, _VIEW_TRANSFER_0057, _VIEW_REAL_LEG_0057))


def downgrade() -> None:
    op.execute(_view(_VIEW_CATEGORY_0035, _VIEW_TRANSFER_0035, _VIEW_REAL_LEG_0035))
    op.execute(_KINDS_DOWN)
    op.execute("DROP INDEX IF EXISTS transactions_split_account;")
    op.execute("ALTER TABLE transactions DROP COLUMN split_account_id;")
