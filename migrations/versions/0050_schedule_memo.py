"""Scheduled transactions get a payee and a memo (#534).

Adds ``schedules.memo_enc`` (Fernet, like every narrative field) and splits
each stored ``name`` with the rules ``schedules.resolve_labels`` applies to an
agent that still sends ``name``:

- a transfer (counterparty set): the name becomes its memo;
- a flat schedule without a payee: the name becomes its payee (found by blind
  index, else created);
- a flat schedule with a payee: the name becomes its memo, unless it is the
  payee's name (normalized).

``name_enc`` itself is untouched: the derived label (memo, else payee, else
the counterparty account) equals the old name in every case, so no instance
needs relabelling. The blind-index key is read through this migration's own
connection, not ``crypto._load_dek`` (which opens the app engine).
"""

import sqlalchemy as sa
from alembic import op

from purser import crypto
from purser.core import ledger as core

revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    op.execute("ALTER TABLE schedules ADD COLUMN memo_enc BYTEA")
    rows = bind.execute(
        sa.text(
            "SELECT s.id, s.name_enc, s.payee_id, s.counterparty_account_id,"
            " p.name_enc AS payee_enc"
            " FROM schedules s LEFT JOIN payees p ON p.id = s.payee_id"
        )
    ).all()
    if not rows:
        return
    crypto.ensure_key()  # only an install with schedules needs to decrypt
    key_row = bind.execute(
        sa.text("SELECT key_enc FROM encryption_keys WHERE purpose = :p"),
        {"p": crypto.BLIND_INDEX_PURPOSE},
    ).first()
    if key_row is None:
        raise RuntimeError("blind-index key missing: run migrations from 0016 onward")
    bidx_key = crypto.unwrap_key(key_row.key_enc)

    for r in rows:
        name = crypto.decrypt(r.name_enc).strip()
        if not name:
            continue
        if r.counterparty_account_id is not None:
            bind.execute(
                sa.text("UPDATE schedules SET memo_enc = :m WHERE id = :id"),
                {"m": crypto.encrypt(name), "id": r.id},
            )
        elif r.payee_id is None:
            bidx = crypto._hmac_hex(bidx_key, core.normalize_payee(name))
            payee_id = bind.execute(
                sa.text("SELECT id FROM payees WHERE name_bidx = :b ORDER BY id LIMIT 1"),
                {"b": bidx},
            ).scalar()
            if payee_id is None:
                payee_id = bind.execute(
                    sa.text(
                        "INSERT INTO payees (name_enc, name_bidx) VALUES (:n, :b) RETURNING id"
                    ),
                    {"n": crypto.encrypt(name), "b": bidx},
                ).scalar_one()
            bind.execute(
                sa.text("UPDATE schedules SET payee_id = :p WHERE id = :id"),
                {"p": payee_id, "id": r.id},
            )
        else:
            payee_name = crypto.decrypt(r.payee_enc) if r.payee_enc is not None else ""
            if core.normalize_payee(name) != core.normalize_payee(payee_name):
                bind.execute(
                    sa.text("UPDATE schedules SET memo_enc = :m WHERE id = :id"),
                    {"m": crypto.encrypt(name), "id": r.id},
                )


def downgrade() -> None:
    # Payees the upgrade attached stay attached: harmless to the old code,
    # which already preferred a schedule's payee over its name.
    op.execute("ALTER TABLE schedules DROP COLUMN memo_enc")
