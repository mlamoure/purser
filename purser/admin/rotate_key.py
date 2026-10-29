"""Key rotation (#137, decision 36): ``purser admin rotate-key``.

Re-encrypt every Fernet ciphertext column under the new primary key WITHOUT
exposing plaintext (``MultiFernet.rotate``), in one transaction. Run with both
keys configured::

    PURSER_ENCRYPTION_KEY="<new>,<old>" purser admin rotate-key

The two wrapped DEK rows (``encryption_keys``) rotate the same way, so their
unwrapped plaintext — and therefore every ``payees.name_bidx`` and
``transactions.content_hash`` derived from them — is byte-identical before and
after. That is the payoff of storing DEKs wrapped rather than deriving them from
the primary key (HKDF-from-primary would force a global rehash on every
rotation). After a clean rotation, drop the old key from ``PURSER_ENCRYPTION_KEY``.

The posted-immutability guards and the raw_fetches append-only guard are
disabled for the rewrite window (re-encryption is an UPDATE on posted /
append-only rows) and re-enabled before commit — the account_delete / migration
precedent. Deferred zero-sum events are flushed immediately first, since the
posted-postings memo rotation fires the deferred constraint trigger.
"""

from typing import Any

import typer
from sqlalchemy import text
from sqlalchemy.orm import Session

from purser import crypto

BATCH = 200

# BYTEA ciphertext the code encrypts and decrypts itself (the ORM maps it as
# raw bytes, so it cannot be found by type): listed by hand. Every
# EncryptedText / EncryptedJSON column is read from the ORM's metadata
# instead (#414), so a new encrypted column is rotated without an edit here.
_RAW_CIPHERTEXT: tuple[tuple[str, str], ...] = (
    ("raw_fetches", "payload_enc"),
    ("connections", "access_url_enc"),
    ("webhook_subscriptions", "secret_enc"),
    ("app_settings", "value_enc"),
    ("encryption_keys", "key_enc"),
)


def fernet_columns() -> tuple[tuple[str, str, str], ...]:
    """(table, column, primary-key column) of every column holding Fernet
    ciphertext: each ``EncryptedText`` / ``EncryptedJSON`` column of the ORM,
    keyed by its table's primary key, plus ``_RAW_CIPHERTEXT``. EXCEPT
    encryption_canary (handled explicitly: its fingerprint moves too). The
    wrapped DEK rows (encryption_keys) come last."""
    from purser.models import Base

    raw = set(_RAW_CIPHERTEXT)
    found: list[tuple[str, str, str]] = []
    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            encrypted = isinstance(column.type, (crypto.EncryptedText, crypto.EncryptedJSON))
            if not encrypted and (table.name, column.name) not in raw:
                continue
            keys = [key.name for key in table.primary_key.columns]
            if len(keys) != 1:
                raise RuntimeError(f"{table.name} needs a one-column primary key to rotate")
            found.append((table.name, column.name, keys[0]))
    missing = raw - {(table, column) for table, column, _pk in found}
    if missing:
        raise RuntimeError(f"raw ciphertext columns not in the ORM: {sorted(missing)}")
    return tuple(sorted(found, key=lambda c: (c[0] == "encryption_keys", c[0], c[1])))


_FERNET_COLUMNS: tuple[tuple[str, str, str], ...] = fernet_columns()

# Disabled for the rewrite window: re-encryption is an UPDATE on posted /
# append-only rows.
_GUARD_TRIGGERS: tuple[tuple[str, str], ...] = (
    ("raw_fetches", "raw_fetches_no_update_delete"),
    ("transactions", "transactions_posted_immutable"),
    ("postings", "postings_posted_immutable"),
    ("position_events", "position_events_immutable"),  # append-only (#390)
)


def _rotate_column(session: Session, table: str, column: str, pk: str) -> int:
    """Keyset-paginate `table` by `pk`, re-wrapping non-null `column` values
    under the new primary key. Ciphertext never leaves the DB as plaintext."""
    last: Any = None
    total = 0
    while True:
        clauses = [f"{column} IS NOT NULL"]
        params: dict[str, Any] = {"batch": BATCH}
        if last is not None:
            clauses.append(f"{pk} > :last")
            params["last"] = last
        rows = session.execute(
            text(
                f"SELECT {pk} AS pk, {column} AS ct FROM {table} "
                f"WHERE {' AND '.join(clauses)} ORDER BY {pk} LIMIT :batch"
            ),
            params,
        ).all()
        if not rows:
            break
        for row in rows:
            session.execute(
                text(f"UPDATE {table} SET {column} = :ct WHERE {pk} = :pk"),
                {"ct": crypto.rotate_ciphertext(row.ct), "pk": row.pk},
            )
        last = rows[-1].pk
        total += len(rows)
    return total


def rotate(session: Session) -> dict[str, int]:
    """Rotate every ciphertext column in one transaction (caller commits)."""
    session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    for tbl, trig in _GUARD_TRIGGERS:
        session.execute(text(f"ALTER TABLE {tbl} DISABLE TRIGGER {trig}"))
    try:
        counts = {
            f"{table}.{column}": _rotate_column(session, table, column, pk)
            for table, column, pk in _FERNET_COLUMNS
        }
        # Canary: re-encrypt the sentinel fresh under the new primary and move
        # the stored fingerprint, so a later wrong-key start reports correctly.
        session.execute(
            text("UPDATE encryption_canary SET sentinel_enc = :s, key_fingerprint = :fp"),
            {"s": crypto.encrypt(crypto.CANARY_SENTINEL), "fp": crypto.key_fingerprint()},
        )
    finally:
        for tbl, trig in _GUARD_TRIGGERS:
            session.execute(text(f"ALTER TABLE {tbl} ENABLE TRIGGER {trig}"))
    return counts


def run_rotate() -> None:
    """Entry point for `purser admin rotate-key`."""
    from purser.db import get_sessionmaker

    keys = crypto._keys()
    if len(keys) < 2:
        typer.echo(
            "warning: only one key in PURSER_ENCRYPTION_KEY — rotation will re-wrap "
            "under the SAME key. Configure PURSER_ENCRYPTION_KEY='<new>,<old>' to "
            "actually rotate.",
            err=True,
        )
    typer.echo(f"Rotating ciphertext to primary key fingerprint {crypto.key_fingerprint()} …")
    with get_sessionmaker()() as session:
        counts = rotate(session)
        session.commit()
    # DEK plaintext is unchanged by rotation, but drop the cache so the next load
    # unwraps the freshly re-wrapped rows.
    crypto.reset_dek_cache()
    for name, n in counts.items():
        typer.echo(f"  {name}: {n} row(s)")
    typer.echo(
        "Done. Verify the app starts, then drop the old key from "
        "PURSER_ENCRYPTION_KEY (leave only the new primary)."
    )
