"""Hand-valued assets become ledgers (#566, decision 113).

- ``equity.revaluation``: the system virtual account a change in an asset's
  value balances against (like ``equity.securities``, 0043).
- ``asset_value_changes``: the optional retry reference of a change recorded
  through the value-change path; UNIQUE (account_id, reference).
- Every ``property`` account's stored valuations become entries, so its value
  on each valuation date is exactly what that valuation said: walking its
  valuation dates in order (the newest-recorded row per date), one posted
  entry for the difference between the valuation and the account's balance on
  that date, against ``equity.revaluation``. A closed account's valuations
  from its close date on were never read and are skipped, and one more entry
  on the close date takes the entries back out, so the account still reads 0
  from its close date on, as its closing adjustment left it. The valuation
  rows stay where they are; nothing reads them any more.

The entries carry no payee or description (those columns are encrypted and a
migration holds no key) and reference ``migration-0054:<valuation id>`` (or
``migration-0054:close:<account id>``), which is how the downgrade finds them.
Running balances and net worth are re-derived from the earliest entry.
"""

from datetime import date
from decimal import Decimal

import sqlalchemy as sa
from alembic import op
from sqlalchemy.orm import Session

revision = "0054"
down_revision = "0053"
branch_labels = None
depends_on = None

REFERENCE_PREFIX = "migration-0054:"

_ZONE = """COALESCE(
    (SELECT s.value #>> '{}' FROM app_settings s
      WHERE s.key = 'timezone' AND jsonb_typeof(s.value) = 'string'),
    (SELECT c.env_timezone FROM ledger_clock c),
    'UTC')"""

# The balance at the end of a date over live rows (chain heads), pending and
# posted: reconciliation.ledger_balances' pending-inclusive basis.
_BALANCE = sa.text(
    """
SELECT COALESCE(SUM(p.amount), 0)
FROM postings p
JOIN transactions t ON t.id = p.transaction_id
WHERE p.account_id = :a AND t.date <= :d
  AND t.status IN ('pending', 'posted')
  AND NOT EXISTS (SELECT 1 FROM transactions x WHERE x.reverses_id = t.id)
"""
)


def _entry(
    conn: sa.Connection,
    *,
    account: object,
    reval: object,
    currency: str,
    on: date,
    amount: Decimal,
    reference: str,
) -> None:
    tx = conn.execute(
        sa.text(
            "INSERT INTO transactions (date, status, origin) "
            "VALUES (:d, 'posted', 'system') RETURNING id"
        ),
        {"d": on},
    ).scalar_one()
    conn.execute(
        sa.text(
            "INSERT INTO postings (transaction_id, account_id, amount, currency) "
            "VALUES (:t, :a, :x, :c), (:t, :r, :y, :c)"
        ),
        {"t": tx, "a": account, "r": reval, "x": amount, "y": -amount, "c": currency},
    )
    conn.execute(
        sa.text(
            "INSERT INTO asset_value_changes (transaction_id, account_id, reference) "
            "VALUES (:t, :a, :ref)"
        ),
        {"t": tx, "a": account, "ref": reference},
    )


def upgrade() -> None:
    op.execute("""
        INSERT INTO accounts (name, type, is_virtual, path, description)
        SELECT 'Revaluation', 'equity', true, CAST('equity.revaluation' AS ltree),
               'System account: the other side of a change in a hand-valued asset''s value. '
               || 'Not a category.'
        WHERE NOT EXISTS (
            SELECT 1 FROM accounts WHERE path = CAST('equity.revaluation' AS ltree));
    """)
    op.execute("""
        CREATE TABLE asset_value_changes (
            transaction_id UUID PRIMARY KEY REFERENCES transactions(id) ON DELETE CASCADE,
            account_id UUID NOT NULL REFERENCES accounts(id),
            reference TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (account_id, reference)
        );
    """)

    conn = op.get_bind()
    reval = conn.execute(
        sa.text("SELECT id FROM accounts WHERE path = CAST('equity.revaluation' AS ltree)")
    ).scalar_one()
    accounts = conn.execute(
        sa.text(
            f"SELECT id, currency, closed_at, (closed_at AT TIME ZONE {_ZONE})::date "
            "FROM accounts WHERE type = 'property' AND NOT is_virtual ORDER BY id"
        )
    ).all()
    earliest: date | None = None
    for account, currency, closed_at, closed_on in accounts:
        currency = currency.strip()
        # A closed account takes no new posting (0015's guard). These entries
        # are its history before the close, so the stamp is lifted while they
        # are written and put back, unchanged, right after.
        if closed_at is not None:
            conn.execute(
                sa.text("UPDATE accounts SET closed_at = NULL WHERE id = :a"), {"a": account}
            )
        valuations = conn.execute(
            sa.text(
                "SELECT DISTINCT ON (as_of) id, as_of, value FROM valuations "
                "WHERE account_id = :a ORDER BY as_of, created_at DESC, id DESC"
            ),
            {"a": account},
        ).all()
        put_in = Decimal(0)
        for valuation_id, as_of, value in valuations:
            if closed_on is not None and as_of >= closed_on:
                break
            held = Decimal(conn.execute(_BALANCE, {"a": account, "d": as_of}).scalar_one())
            difference = Decimal(value) - held
            if difference == 0:
                continue
            _entry(
                conn,
                account=account,
                reval=reval,
                currency=currency,
                on=as_of,
                amount=difference,
                reference=f"{REFERENCE_PREFIX}{valuation_id}",
            )
            put_in += difference
            earliest = as_of if earliest is None else min(earliest, as_of)
        if closed_on is not None and put_in != 0:
            _entry(
                conn,
                account=account,
                reval=reval,
                currency=currency,
                on=closed_on,
                amount=-put_in,
                reference=f"{REFERENCE_PREFIX}close:{account}",
            )
        if closed_at is not None:
            conn.execute(
                sa.text("UPDATE accounts SET closed_at = :c WHERE id = :a"),
                {"c": closed_at, "a": account},
            )

    if earliest is not None:
        _rederive(conn, earliest)


def _rederive(conn: sa.Connection, since: date) -> None:
    """Mark the watermark from ``since`` and re-derive running balances with
    the canonical recompute (0017's way); net worth heals on its next consume."""
    from purser import balances

    conn.execute(
        sa.text(
            "UPDATE balance_watermark SET"
            " running_dirty_from = LEAST(COALESCE(running_dirty_from, :d), :d),"
            " networth_dirty_from = LEAST(COALESCE(networth_dirty_from, :d), :d),"
            " updated_at = now()"
        ),
        {"d": since},
    )
    session = Session(bind=conn)
    balances.recompute_running(session)
    session.flush()


def downgrade() -> None:
    conn = op.get_bind()
    earliest = conn.execute(
        sa.text(
            "SELECT min(t.date) FROM asset_value_changes v "
            "JOIN transactions t ON t.id = v.transaction_id WHERE v.reference LIKE :p"
        ),
        {"p": REFERENCE_PREFIX + "%"},
    ).scalar_one()
    # Posted rows are undeletable by trigger; the guards are lifted inside
    # this transaction only, as the guarded delete-account CLI does (deferred
    # constraint events are flushed first: Postgres refuses ALTER TABLE while
    # any are pending).
    guards = (
        ("transactions", "transactions_posted_immutable"),
        ("transactions", "transactions_posted_undeletable"),
        ("postings", "postings_posted_immutable"),
    )
    doomed = "SELECT transaction_id FROM asset_value_changes WHERE reference LIKE :p"
    conn.execute(sa.text("SET CONSTRAINTS ALL IMMEDIATE"))
    for table, trigger in guards:
        conn.execute(sa.text(f"ALTER TABLE {table} DISABLE TRIGGER {trigger}"))
    params = {"p": REFERENCE_PREFIX + "%"}
    conn.execute(
        sa.text(f"UPDATE transactions SET reverses_id = NULL WHERE reverses_id IN ({doomed})"),
        params,
    )
    conn.execute(sa.text(f"DELETE FROM postings WHERE transaction_id IN ({doomed})"), params)
    conn.execute(sa.text(f"DELETE FROM transactions WHERE id IN ({doomed})"), params)
    for table, trigger in guards:
        conn.execute(sa.text(f"ALTER TABLE {table} ENABLE TRIGGER {trigger}"))
    conn.execute(sa.text("SET CONSTRAINTS ALL DEFERRED"))
    op.execute("DROP TABLE asset_value_changes;")
    op.execute("""
        DELETE FROM accounts a
        WHERE a.path = CAST('equity.revaluation' AS ltree) AND a.type = 'equity'
          AND NOT EXISTS (SELECT 1 FROM postings p WHERE p.account_id = a.id);
    """)
    if earliest is not None:
        _rederive(conn, earliest)
