"""Ledger errors and the caller-path check every path boundary runs (issue #214)."""

import psycopg
from sqlalchemy.exc import DBAPIError

from purser.core import ledger as core


class LedgerError(Exception):
    """Domain error -> 422 at the API layer."""


class NotFoundError(LedgerError):
    """Missing entity -> 404 at the API layer."""


class PathConflictError(LedgerError):
    """An account move's destination is taken -> 409 at the API layer."""


# ---------------------------------------------------------------------------
# Path input (issue #214)
# ---------------------------------------------------------------------------


def check_path(path: str) -> str:
    """``core.validate_path`` as a LedgerError (422 at the API, an error result
    over MCP). Every caller-supplied path passes here before SQL casts it to
    ltree: account_by_path (so every "path or id" reference, bracket inner
    refs included), explicit create paths, and the subtree filters."""
    try:
        return core.validate_path(path)
    except core.InvalidPathError as exc:
        raise LedgerError(str(exc)) from exc


# SQLSTATE -> the primary-message prefix Postgres 16's ltree input parser
# uses. Matching both keeps the backstop to ltree refusing caller text: an
# unrelated syntax error (42601) in our own SQL must still surface as a 500.
_LTREE_REFUSALS = {
    "42601": "ltree syntax error",  # bad character, empty segment
    "42622": "label string is too long",  # a label over 1000 characters
    "54000": "number of ltree labels",  # more than 65535 labels
}


def ltree_input_refusal(exc: DBAPIError) -> str | None:
    """The 422 message when ``exc`` is Postgres refusing ltree input text,
    else None.

    BACKSTOP ONLY (issue #214): ``check_path`` runs at every path boundary,
    so this fires only for text the validator admits but the database's
    locale refuses (a non-ASCII label under a C-locale database, a
    non-alphabetic combining mark) — or a boundary that forgot to validate.
    Wired into the REST app as an exception handler and into the MCP
    session runner, so neither surface answers that with a 500/raw SQL.
    """
    orig = exc.orig
    if not isinstance(orig, psycopg.Error):
        return None
    prefix = _LTREE_REFUSALS.get(orig.sqlstate or "")
    primary = orig.diag.message_primary or ""
    if prefix is None or not primary.startswith(prefix):
        return None
    detail = orig.diag.message_detail
    reason = f"{primary}; {detail}" if detail else primary
    return f"invalid path: the database refused it ({reason})"
