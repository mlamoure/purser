"""Narrative-layer encryption (issue #137, decision 36).

Decision 22 kept ledger + readmodel plaintext. This supersedes it for the
*narrative* layer — the identifying words — while the numeric skeleton
(amounts, dates, statuses, ltree paths, ids) stays plaintext because every SQL
invariant/aggregate computes on it.

Encrypted onto ``*_enc`` BYTEA (Fernet, transparent via the crypto
TypeDecorators): ``payees.name``, ``transactions.description``,
``postings.memo``, ``schedules.name``, ``audit_log.before_after``,
``audit_log.detail``, ``events.payload``.

Equality/dedup survives ciphertext via three plaintext skeleton columns:
- ``payees.name_bidx`` — blind index (HMAC over normalize_payee(name));
  REPLACES ``payees.normalized_name`` for resolve_payee / register payee filter
  / schedule detection.
- ``events.dedup_key`` — the once-only emit guards' structural id (transfer
  pair_key, forecast shortfall key), lifted out of the encrypted payload.
- ``audit_log.replacement_id`` — the register's category-provenance target,
  lifted out of the encrypted before_after.

``transactions.content_hash`` is rehashed into keyed HMAC form
(``keyed_hash(sha256_identity)``): a pure injective value map that preserves the
partial-unique index and — provably — idempotency, while hardening the dedup
identity against a dictionary attack on a DB dump.

Both HMAC keys are random 32-byte DEKs stored Fernet-wrapped in the new
``encryption_keys`` table (decision 36: wrapped DEKs keep every blind index and
content hash byte-identical across Fernet rotations; HKDF-from-primary would
force a global rehash on every rotation).

Ordering (one transaction — Postgres transactional DDL): seed keys → add
columns → DISABLE the three posted-immutability guards by name + flush deferred
zero-sum events (``SET CONSTRAINTS ALL IMMEDIATE``, since the posted-postings
memo backfill fires the deferred constraint trigger) → batched backfill →
content_hash rehash → re-ENABLE guards → swap ``readmodel.v1_transactions_flat``
for ``v2`` (minus payee/description) → drop plaintext columns. No trigger
RECREATION: the live guard (0005) is a column-name-free ``to_jsonb`` allow-list
that already covers the renamed columns; re-authoring it from 0001's deny-list
body would silently regress it.

**Downgrade is unsupported** past the content_hash rehash: a keyed HMAC cannot
be un-HMAC'd. downgrade() raises — restore from the pre-migration ``pg_dump``.

DESIGN.md §3.1 / §9 / §12 (decision 36) updated in the same PR (schema rule).

Revision ID: 0016
Revises: 0015
Create Date: 2026-07-12

"""

from collections.abc import Callable, Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

from purser import crypto

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BATCH = 200  # keyset-paginated backfill, bounded working set (0007 precedent)

# Disabled for the whole rewrite window (steps 3-6). The live posted-immutable
# guards reject ANY column rewrite on posted rows (memo_enc / description_enc
# backfill and the content_hash rehash all hit posted rows).
_POSTED_GUARD_TRIGGERS = (
    ("transactions", "transactions_posted_immutable"),
    ("transactions", "transactions_posted_undeletable"),
    ("postings", "postings_posted_immutable"),
)

# v1 minus payee / description (the encrypted narrative columns); otherwise
# byte-identical to migration 0004's flat view. The other four v1_* views touch
# no narrative column and keep their names.
V2_VIEW_DDL = """
CREATE VIEW readmodel.v2_transactions_flat AS
SELECT
  t.id,
  t.date,
  t.status,
  t.origin,
  real_leg.amount,
  real_acct.path::text AS account_path,
  cat_acct.path::text AS category_path,
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
GRANT SELECT ON readmodel.v2_transactions_flat TO purser_read;
"""


def _backfill(
    bind: sa.Connection,
    *,
    table: str,
    columns: str,
    where: str,
    compute: Callable[[Any], dict[str, Any]],
    update: str,
) -> None:
    """Keyset-paginate `table` in id order, applying `compute(row) -> params`
    then `update` per row. Key material rides only in bind params, never SQL
    text (0007 precedent)."""
    last = None
    while True:
        clauses = []
        if where:
            clauses.append(f"({where})")  # parenthesize: `where` may contain OR
        if last is not None:
            clauses.append("id > :last")
        where_sql = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params: dict[str, Any] = {"batch": BATCH}
        if last is not None:
            params["last"] = last
        rows = bind.execute(
            sa.text(f"SELECT id, {columns} FROM {table}{where_sql} ORDER BY id LIMIT :batch"),
            params,
        ).all()
        if not rows:
            break
        for row in rows:
            bind.execute(sa.text(update), {"id": row.id, **compute(row)})
        last = rows[-1].id


def upgrade() -> None:
    crypto.ensure_key()  # fail before any DDL: the backfill needs the key
    bind = op.get_bind()

    # 1. Key table + two wrapped DEKs. The plaintext keys stay in hand for the
    #    backfill (they are not yet loadable via crypto._load_dek — the rows are
    #    uncommitted and that path uses a separate engine).
    op.execute(
        """
        CREATE TABLE encryption_keys (
          purpose    TEXT PRIMARY KEY,
          key_enc    BYTEA NOT NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    blind_index_key = crypto.generate_dek()
    content_hash_key = crypto.generate_dek()
    for purpose, key in (
        (crypto.BLIND_INDEX_PURPOSE, blind_index_key),
        (crypto.CONTENT_HASH_PURPOSE, content_hash_key),
    ):
        bind.execute(
            sa.text("INSERT INTO encryption_keys (purpose, key_enc) VALUES (:p, :k)"),
            {"p": purpose, "k": crypto.wrap_key(key)},
        )

    # 2. Encrypted columns + plaintext skeleton columns + their indexes.
    op.execute("ALTER TABLE payees ADD COLUMN name_enc BYTEA")
    op.execute("ALTER TABLE payees ADD COLUMN name_bidx TEXT")
    op.execute("ALTER TABLE transactions ADD COLUMN description_enc BYTEA")
    op.execute("ALTER TABLE postings ADD COLUMN memo_enc BYTEA")
    op.execute("ALTER TABLE schedules ADD COLUMN name_enc BYTEA")
    op.execute("ALTER TABLE audit_log ADD COLUMN before_after_enc BYTEA")
    op.execute("ALTER TABLE audit_log ADD COLUMN detail_enc BYTEA")
    op.execute("ALTER TABLE audit_log ADD COLUMN replacement_id UUID")
    op.execute("ALTER TABLE events ADD COLUMN payload_enc BYTEA")
    op.execute("ALTER TABLE events ADD COLUMN dedup_key TEXT")
    op.execute("CREATE INDEX payees_name_bidx ON payees (name_bidx)")
    op.execute("CREATE INDEX events_dedup_key ON events (dedup_key)")
    op.execute(
        "CREATE INDEX audit_log_replacement_id ON audit_log (replacement_id) "
        "WHERE replacement_id IS NOT NULL"
    )

    # 3. Disable the posted-immutability guards for the whole rewrite window, and
    #    flush deferred zero-sum events immediately (the posted-postings memo
    #    backfill fires the deferred constraint trigger; leaving events pending
    #    would make the step-6 ENABLE TRIGGER refuse — account_delete precedent).
    for table, trigger in _POSTED_GUARD_TRIGGERS:
        op.execute(f"ALTER TABLE {table} DISABLE TRIGGER {trigger}")
    op.execute("SET CONSTRAINTS ALL IMMEDIATE")

    # 4. Backfill. Plaintext skeleton ids first (single UPDATEs over the
    #    still-plaintext JSONB — no key needed), then the batched encryptions.
    op.execute(
        "UPDATE audit_log SET replacement_id = (before_after->'after'->>'replacement_id')::uuid "
        "WHERE before_after->'after'->>'replacement_id' IS NOT NULL"
    )
    op.execute(
        "UPDATE events SET dedup_key = COALESCE(payload->>'pair_key', payload->>'key') "
        "WHERE payload ? 'pair_key' OR payload ? 'key'"
    )

    _backfill(
        bind,
        table="payees",
        columns="name, normalized_name",
        where="",
        compute=lambda r: {
            "name_enc": crypto.encrypt(r.name),
            "name_bidx": crypto._hmac_hex(blind_index_key, r.normalized_name),
        },
        update="UPDATE payees SET name_enc = :name_enc, name_bidx = :name_bidx WHERE id = :id",
    )
    _backfill(
        bind,
        table="transactions",
        columns="description",
        where="description IS NOT NULL",
        compute=lambda r: {"description_enc": crypto.encrypt(r.description)},
        update="UPDATE transactions SET description_enc = :description_enc WHERE id = :id",
    )
    _backfill(
        bind,
        table="postings",
        columns="memo",
        where="memo IS NOT NULL",
        compute=lambda r: {"memo_enc": crypto.encrypt(r.memo)},
        update="UPDATE postings SET memo_enc = :memo_enc WHERE id = :id",
    )
    _backfill(
        bind,
        table="schedules",
        columns="name",
        where="",
        compute=lambda r: {"name_enc": crypto.encrypt(r.name)},
        update="UPDATE schedules SET name_enc = :name_enc WHERE id = :id",
    )
    _backfill(
        bind,
        table="audit_log",
        columns="before_after, detail",
        where="before_after IS NOT NULL OR detail IS NOT NULL",
        compute=lambda r: {
            "ba": crypto.encrypt_json(r.before_after) if r.before_after is not None else None,
            "d": crypto.encrypt_json(r.detail) if r.detail is not None else None,
        },
        update="UPDATE audit_log SET before_after_enc = :ba, detail_enc = :d WHERE id = :id",
    )
    _backfill(
        bind,
        table="events",
        columns="payload",
        where="",
        compute=lambda r: {"payload_enc": crypto.encrypt_json(r.payload)},
        update="UPDATE events SET payload_enc = :payload_enc WHERE id = :id",
    )

    # NOT NULL where the plaintext source was NOT NULL.
    op.execute("ALTER TABLE payees ALTER COLUMN name_enc SET NOT NULL")
    op.execute("ALTER TABLE payees ALTER COLUMN name_bidx SET NOT NULL")
    op.execute("ALTER TABLE schedules ALTER COLUMN name_enc SET NOT NULL")
    op.execute("ALTER TABLE events ALTER COLUMN payload_enc SET NOT NULL")

    # 5. Rehash content_hash: keyed_hash(sha256_identity). Batched Python (not a
    #    single SQL UPDATE via pgcrypto) so the HMAC recipe is byte-identical to
    #    the runtime crypto.keyed_hash — the composition property depends on it.
    #    Injective map: the partial-unique index holds, dedup is preserved.
    _backfill(
        bind,
        table="transactions",
        columns="content_hash",
        where="content_hash IS NOT NULL",
        compute=lambda r: {"content_hash": crypto._hmac_hex(content_hash_key, r.content_hash)},
        update="UPDATE transactions SET content_hash = :content_hash WHERE id = :id",
    )

    # 6. Re-enable the posted-immutability guards.
    for table, trigger in _POSTED_GUARD_TRIGGERS:
        op.execute(f"ALTER TABLE {table} ENABLE TRIGGER {trigger}")

    # 7. Swap the flat readmodel view BEFORE the column drops (it depends on
    #    payees.name / transactions.description).
    op.execute("DROP VIEW readmodel.v1_transactions_flat")
    op.execute(V2_VIEW_DDL)

    # 8. Drop the plaintext narrative columns (+ the old blind-index precursor).
    op.execute("DROP INDEX payees_norm")
    op.execute("ALTER TABLE payees DROP COLUMN name")
    op.execute("ALTER TABLE payees DROP COLUMN normalized_name")
    op.execute("ALTER TABLE transactions DROP COLUMN description")
    op.execute("ALTER TABLE postings DROP COLUMN memo")
    op.execute("ALTER TABLE schedules DROP COLUMN name")
    op.execute("ALTER TABLE audit_log DROP COLUMN before_after")
    op.execute("ALTER TABLE audit_log DROP COLUMN detail")
    op.execute("ALTER TABLE events DROP COLUMN payload")


def downgrade() -> None:
    """Irreversible past the content_hash rehash.

    Decrypting the narrative columns back is mechanically possible, but
    ``transactions.content_hash`` was rewritten into a one-way keyed HMAC — the
    original sha256 identities cannot be reconstructed (feed rows' inputs are
    gone), so a faithful downgrade is impossible. Fail loudly rather than leave
    the ledger with unusable dedup identities. Restore from the pre-migration
    pg_dump instead (the WS1 merge runbook takes one immediately before deploy).
    """
    raise RuntimeError(
        "migration 0016 (narrative encryption) is irreversible: content_hash was "
        "rewritten to a one-way keyed HMAC and cannot be un-hashed. Restore from "
        "the pre-migration pg_dump."
    )
