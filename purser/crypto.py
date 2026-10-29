"""Encryption at rest — required for every install (issue #93, #137).

Fernet keyed by PURSER_ENCRYPTION_KEY. The variable accepts comma-separated
keys (MultiFernet): the FIRST key encrypts, any key decrypts — so a future
rotation is config + re-encrypt, never schema work. Values are decrypted only
at the moment of use and must never appear in logs or API responses.

The ``encryption_canary`` row binds the key to the database: it is seeded by
the migration that introduced it and decrypted at every startup, so a wrong
key fails closed before anything is served.

Since #137 the *narrative* layer (payee names, transaction descriptions,
posting memos, schedule names, audit/event payloads) is encrypted too, via the
``EncryptedText`` / ``EncryptedJSON`` SQLAlchemy TypeDecorators (transparent
bind=encrypt / result=decrypt on ``*_enc`` BYTEA columns). Two derived
capabilities keep equality lookups and dedup working over ciphertext:

- ``blind_index(value)`` — a deterministic HMAC over normalized payee text,
  stored plaintext in ``payees.name_bidx`` (a non-unique index), so payee
  identity resolves without decrypting every row.
- ``keyed_hash(v1_hex)`` — HMAC-composed transaction ``content_hash`` (harden
  the dedup identity against dictionary attack on a DB dump).

Both HMAC keys are *wrapped data-encryption keys* (DEKs): migration 0016
generates two random 32-byte keys, stores them Fernet-wrapped in the
``encryption_keys`` table, and loads them lazily (cached, fail-closed). Wrapped
DEKs make key rotation re-encrypt ciphertext + re-wrap two rows while leaving
every blind index and content hash **byte-identical** — HKDF-from-primary would
instead force a global rehash on every rotation (decision 36).
"""

import base64
import hashlib
import hmac
import json
import secrets
import uuid
from typing import Any

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from sqlalchemy import LargeBinary, text
from sqlalchemy.engine import Connection
from sqlalchemy.types import TypeDecorator

from purser.config import get_settings

CANARY_SENTINEL = "purser-canary-v1"

# encryption_keys.purpose values for the two wrapped DEKs (#137).
BLIND_INDEX_PURPOSE = "blind_index"
CONTENT_HASH_PURPOSE = "content_hash"
# Payee references handed to agents (#437): HMAC over the payee id, so a
# reference is stable across Fernet rotations and agents' memories of it stay
# valid.
PAYEE_REF_PURPOSE = "payee_ref"
DEK_PURPOSES = (BLIND_INDEX_PURPOSE, CONTENT_HASH_PURPOSE, PAYEE_REF_PURPOSE)

KEYGEN_HINT = (
    'generate one with `purser keygen` (or python -c "from cryptography.fernet '
    'import Fernet; print(Fernet.generate_key().decode())") and record it '
    "somewhere safe — encrypted-at-rest data (raw bank envelopes AND the "
    "narrative layer: payees, descriptions, memos, schedule names, audit and "
    "event payloads) is unrecoverable without it"
)


class EncryptionKeyMissing(Exception):
    def __init__(self) -> None:
        super().__init__(f"PURSER_ENCRYPTION_KEY is not set; {KEYGEN_HINT}")


class EncryptionKeyInvalid(Exception):
    def __init__(self) -> None:
        super().__init__(f"PURSER_ENCRYPTION_KEY is not a valid Fernet key; {KEYGEN_HINT}")


class EncryptionKeyMismatch(Exception):
    def __init__(self, *, stored: str, configured: str) -> None:
        super().__init__(
            "PURSER_ENCRYPTION_KEY does not match this database (database key "
            f"fingerprint {stored}, configured key fingerprint {configured}); "
            "restore the key this database was initialized with"
        )


def _keys() -> list[str]:
    raw = get_settings().encryption_key
    if not raw:
        raise EncryptionKeyMissing()
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    if not keys:
        raise EncryptionKeyMissing()
    return keys


def _fernet() -> MultiFernet:
    try:
        return MultiFernet([Fernet(k.encode()) for k in _keys()])
    except ValueError as exc:
        raise EncryptionKeyInvalid() from exc


def ensure_key() -> None:
    """Fail fast when the key is missing or malformed — called at every
    startup, and by callers that spend one-time credentials (a SimpleFIN
    setup token is consumed by its claim)."""
    _fernet()


def key_fingerprint(key: str | None = None) -> str:
    """Short identifier of the primary (encrypting) key — safe for logs and
    error messages; reveals nothing about the key material."""
    primary = key if key is not None else _keys()[0]
    return hashlib.sha256(primary.encode()).hexdigest()[:12]


def encrypt(value: str) -> bytes:
    return _fernet().encrypt(value.encode())


def rotate_ciphertext(token: bytes) -> bytes:
    """Re-encrypt a Fernet token under the current PRIMARY key without exposing
    plaintext (``MultiFernet.rotate``). Run with ``PURSER_ENCRYPTION_KEY=new,old``
    this re-wraps old-key ciphertext under the new primary — the key-rotation
    primitive (#137). Wrapped DEK rows rotate the same way, so their unwrapped
    plaintext (and every blind index / content hash) stays byte-identical."""
    return _fernet().rotate(bytes(token))


def decrypt(token: bytes) -> str:
    return _fernet().decrypt(bytes(token)).decode()


def encrypt_json(obj: dict[str, Any]) -> bytes:
    """Encrypt the canonical (sort_keys) JSON form — the same string
    content-hashing uses, so hash-then-encrypt callers stay consistent."""
    return encrypt(json.dumps(obj, sort_keys=True))


def decrypt_json(token: bytes) -> dict[str, Any]:
    obj: dict[str, Any] = json.loads(decrypt(token))
    return obj


def verify_canary(connection: Connection) -> None:
    """Decrypt the canary sentinel; raise ``EncryptionKeyMismatch`` when the
    configured key(s) cannot. No row (pre-0007 database) passes — the
    migration that creates the canary also seeds it."""
    row = connection.execute(
        text("SELECT sentinel_enc, key_fingerprint FROM encryption_canary")
    ).first()
    if row is None:
        return
    try:
        decrypt(row.sentinel_enc)
    except InvalidToken:
        raise EncryptionKeyMismatch(
            stored=row.key_fingerprint, configured=key_fingerprint()
        ) from None


# ---------------------------------------------------------------------------
# Wrapped data-encryption keys (DEKs) for blind index + keyed content hash
# ---------------------------------------------------------------------------


class DekMissing(Exception):
    """A required ``encryption_keys`` row is absent — fail closed rather than
    silently fall back to an unkeyed hash (that would diverge every blind index
    and content hash on this database)."""

    def __init__(self, purpose: str) -> None:
        super().__init__(
            f"encryption_keys row for purpose {purpose!r} is missing; the "
            "database has not been migrated to 0016 (narrative encryption). "
            "Run `purser migrate`."
        )


def generate_dek() -> bytes:
    """A fresh 32-byte HMAC key (random, not derived from the Fernet key — see
    the module docstring / decision 36)."""
    return secrets.token_bytes(32)


def wrap_key(raw: bytes) -> bytes:
    """Fernet-wrap a raw DEK for storage in ``encryption_keys.key_enc``."""
    return _fernet().encrypt(raw)


def unwrap_key(token: bytes) -> bytes:
    return _fernet().decrypt(bytes(token))


# DEK plaintext is stable across Fernet rotations (only the wrapping changes),
# so a process-wide cache is safe. Tests that swap databases call
# ``reset_dek_cache`` (the DEKs are per-database random material).
_dek_cache: dict[str, bytes] = {}


def reset_dek_cache() -> None:
    _dek_cache.clear()


def _load_dek(purpose: str) -> bytes:
    """Lazily load + cache a DEK from ``encryption_keys`` (fail-closed)."""
    cached = _dek_cache.get(purpose)
    if cached is not None:
        return cached
    from purser.db import get_engine

    with get_engine().connect() as conn:
        row = conn.execute(
            text("SELECT key_enc FROM encryption_keys WHERE purpose = :p"),
            {"p": purpose},
        ).first()
    if row is None:
        raise DekMissing(purpose)
    dek = unwrap_key(row.key_enc)
    _dek_cache[purpose] = dek
    return dek


def _hmac_hex(key: bytes, value: str) -> str:
    """HMAC-SHA256 of ``value`` under ``key``, hex-encoded. The single recipe
    behind both ``blind_index`` and ``keyed_hash`` and the migration backfill
    (which passes freshly generated keys directly)."""
    return hmac.new(key, value.encode(), hashlib.sha256).hexdigest()


def blind_index(value: str) -> str:
    """Deterministic HMAC over an ALREADY-NORMALIZED payee string (callers pass
    ``core.normalize_payee(name)``). Stored in ``payees.name_bidx`` so equality
    lookups survive encryption of ``payees.name`` (#137)."""
    return _hmac_hex(_load_dek(BLIND_INDEX_PURPOSE), value)


def keyed_hash(v1_hex: str) -> str:
    """HMAC-compose an existing SHA-256 content-hash hex digest into the keyed
    form stored in ``transactions.content_hash``. The migration is then a pure
    injective value map (no historical hash inputs need reconstructing) and
    idempotency is provably preserved (#137, decision 36)."""
    return _hmac_hex(_load_dek(CONTENT_HASH_PURPOSE), v1_hex)


PAYEE_REF_PREFIX = "py_"
PAYEE_REF_LENGTH = 10  # base32 characters: 50 bits, no realistic collision


def payee_ref(payee_id: uuid.UUID) -> str:
    """A payee's stable reference for privatized agent output (#437):
    ``py_`` plus the first characters of HMAC(payee_ref DEK, payee id) in
    lowercase base32. It names the payee without disclosing its name, and is
    byte-identical across ``purser admin rotate-key``."""
    digest = hmac.new(_load_dek(PAYEE_REF_PURPOSE), payee_id.bytes, hashlib.sha256).digest()
    return PAYEE_REF_PREFIX + base64.b32encode(digest).decode().lower()[:PAYEE_REF_LENGTH]


# ---------------------------------------------------------------------------
# Transparent ORM encryption (narrative layer, #137)
# ---------------------------------------------------------------------------


class EncryptedText(TypeDecorator[str]):
    """Fernet-encrypt a ``str`` onto a BYTEA (``*_enc``) column, transparently.

    ORM attributes keep their logical name mapped onto the ``*_enc`` column
    (matching the existing ``raw_fetches.payload_enc`` convention):
    ``description = mapped_column("description_enc", EncryptedText)``.
    """

    impl = LargeBinary
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect: Any) -> bytes | None:
        if value is None:
            return None
        return encrypt(value)

    def process_result_value(self, value: Any, dialect: Any) -> str | None:
        if value is None:
            return None
        return decrypt(bytes(value))


class EncryptedJSON(TypeDecorator[dict[str, Any]]):
    """Like ``EncryptedText`` but for JSON payloads, using the canonical
    (sort_keys) form ``encrypt_json`` / ``decrypt_json`` already establish."""

    impl = LargeBinary
    cache_ok = True

    def process_bind_param(self, value: dict[str, Any] | None, dialect: Any) -> bytes | None:
        if value is None:
            return None
        return encrypt_json(value)

    def process_result_value(self, value: Any, dialect: Any) -> dict[str, Any] | None:
        if value is None:
            return None
        return decrypt_json(bytes(value))
