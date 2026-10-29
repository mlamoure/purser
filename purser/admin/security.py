"""Token generation/hashing, password hashing, and scope semantics."""

import functools
import hashlib
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

TOKEN_PREFIX = "purser_"

SCOPE_READ = "read"
SCOPE_WRITE = "write"
SCOPE_ADMIN = "admin"
ALL_SCOPES = [SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN]
# Retired scopes and what replaced them. #436: an agent acts with the user's
# authority, so writes are one scope (migration 0037). #437: read:redacted's
# effect — external account identifiers omitted — is the default for every
# API token now (migration 0038).
RETIRED_SCOPES = {"propose": SCOPE_WRITE, "commit": SCOPE_WRITE, "read:redacted": SCOPE_READ}

_hasher = PasswordHasher()


def generate_token() -> tuple[str, str]:
    """Return (raw_token, token_hash). The raw token is shown exactly once."""
    raw = TOKEN_PREFIX + secrets.token_urlsafe(32)
    return raw, hash_token(raw)


def hash_token(raw: str) -> str:
    # Tokens are high-entropy random strings, so a fast digest is appropriate;
    # the hash column also serves as the constant-time lookup key.
    return hashlib.sha256(raw.encode()).hexdigest()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False


@functools.cache
def dummy_password_hash() -> str:
    """A fixed argon2 hash that no password matches (issue #304).

    Sign-in verifies against it when there is no real hash to check — an
    unknown email, or an account without a password — so every attempt costs
    one argon2 verification and the response time does not reveal whether
    the account exists. It is the hash of a random secret that is discarded,
    made once per process by the same hasher as real passwords, so it carries
    the same cost parameters."""
    return _hasher.hash(secrets.token_urlsafe(32))


def can_read(scopes: list[str]) -> bool:
    return bool({SCOPE_READ, SCOPE_ADMIN} & set(scopes))


def withholds_external_ids(kind: str, scopes: list[str]) -> bool:
    """External account identifiers (``external_id``, ``connection_id``) are
    omitted for every non-admin API token (#437): no agent task needs them.
    A person's sign-in session and an admin token (feed configuration) see
    them; over MCP any agent can ask with ``include``."""
    return kind != "session" and SCOPE_ADMIN not in scopes


def can_write(scopes: list[str]) -> bool:
    return bool({SCOPE_WRITE, SCOPE_ADMIN} & set(scopes))


def is_admin(scopes: list[str]) -> bool:
    return SCOPE_ADMIN in scopes
