"""SimpleFIN AccountSet shapes, v1 and v2, read through one interface (#479).

Protocol v2 (2.0.0, 2026-03-19) replaced the AccountSet's plain-text
``errors`` with structured ``errlist`` entries (``code``, ``msg``, optional
``conn_id`` / ``account_id``), replaced each account's ``org`` object with a
top-level ``connections`` list (one entry per bank login) and put a
``conn_id`` on every account. A v2 reply carries neither ``errors`` nor
``org``.

``raw_fetches`` keeps every payload verbatim, and the archive holds v1
fetches from before Purser asked for v2, so no reader touches ``errors``,
``errlist``, ``org`` or ``connections`` directly: it asks this module, which
answers the same questions for either shape. Pure functions over the decoded
payload; no I/O.
"""

from collections import Counter
from dataclasses import asdict, dataclass
from typing import Any

PROTOCOL_VERSION = 2  # what fetch_accounts asks the server for

# Error code prefixes the spec defines: General, Connection, Account. A code
# with any other prefix is treated as a naked "gen." (the spec tells
# consumers to fall back to the prefix for unknown subcodes).
_PREFIXES = ("gen", "con", "act")
# Codes that mean a bank or the server needs the user, so the connection is
# flagged. gen.api is aimed at the developer (the Bridge files its usage
# warnings, such as an over-long date range, under it) and a naked gen. says
# nothing actionable, so both are listed but leave the status alone.
_ATTENTION_PREFIXES = ("con", "act")
_ATTENTION_CODES = ("gen.auth",)


@dataclass(frozen=True)
class Issue:
    """One error the server reported with a fetch."""

    code: str
    msg: str
    conn_id: str | None = None
    account_id: str | None = None

    @property
    def prefix(self) -> str:
        head = self.code.split(".", 1)[0]
        return head if head in _PREFIXES else "gen"

    def as_dict(self) -> dict[str, str | None]:
        return asdict(self)


@dataclass(frozen=True)
class Institution:
    """One bank login the payload's accounts belong to. v1 payloads carry no
    login identity, so ``conn_id`` is None there and two logins at one bank
    read as one institution."""

    conn_id: str | None
    name: str | None
    org_id: str | None
    org_name: str | None
    org_url: str | None


def version(payload: dict[str, Any]) -> int:
    """2 when the payload has the v2 AccountSet keys, else 1."""
    return 2 if "errlist" in payload or "connections" in payload else 1


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def issues(payload: dict[str, Any]) -> list[Issue]:
    """The fetch's errors as structured issues. A v1 ``errors`` string has no
    code, so it becomes a naked ``gen.`` issue carrying the message."""
    if version(payload) == 2:
        out: list[Issue] = []
        for entry in payload.get("errlist") or []:
            if not isinstance(entry, dict):
                out.append(Issue(code="gen.", msg=str(entry)))
                continue
            out.append(
                Issue(
                    code=_text(entry.get("code")) or "gen.",
                    msg=str(entry.get("msg") or ""),
                    conn_id=_text(entry.get("conn_id")),
                    account_id=_text(entry.get("account_id")),
                )
            )
        return out
    return [Issue(code="gen.", msg=str(message)) for message in payload.get("errors") or []]


def messages(payload: dict[str, Any]) -> list[str]:
    """The issues' messages, the shape the ``errors`` fields have always had."""
    return [issue.msg for issue in issues(payload)]


def needs_attention(payload: dict[str, Any]) -> bool:
    """Whether the fetch flags the connection (status ``warning``).

    v2: a connection- or account-level issue, or ``gen.auth``. v1 strings
    cannot be told apart, so any of them flags it, as before v2."""
    if version(payload) == 1:
        return bool(payload.get("errors"))
    return any(
        issue.prefix in _ATTENTION_PREFIXES or issue.code in _ATTENTION_CODES
        for issue in issues(payload)
    )


def api_messages(payload: dict[str, Any]) -> list[str]:
    """Free-text notes the Bridge sends to the developer (``x-api-message``,
    outside the spec)."""
    notes = payload.get("x-api-message")
    if isinstance(notes, str):
        return [notes]
    return [str(note) for note in notes or []]


def institutions(payload: dict[str, Any]) -> list[Institution]:
    """Every bank login in the payload, in payload order. v1 derives them
    from the accounts' ``org`` objects, one per distinct org."""
    if version(payload) == 2:
        return [
            Institution(
                conn_id=_text(entry.get("conn_id")),
                name=_text(entry.get("name")),
                org_id=_text(entry.get("org_id")),
                org_name=_text(entry.get("org_name")) or _text(entry.get("name")),
                org_url=_text(entry.get("org_url")),
            )
            for entry in payload.get("connections") or []
            if isinstance(entry, dict)
        ]
    seen: dict[tuple[str | None, str | None], Institution] = {}
    for sf_account in payload.get("accounts") or []:
        org = _v1_org(sf_account)
        if org is not None:
            seen.setdefault((org.org_id, org.name), org)
    return list(seen.values())


def _v1_org(sf_account: dict[str, Any]) -> Institution | None:
    org = sf_account.get("org")
    if not isinstance(org, dict):
        return None
    name = _text(org.get("name"))
    return Institution(
        conn_id=None,
        name=name,
        org_id=_text(org.get("id")),
        org_name=name,
        org_url=_text(org.get("url")) or _text(org.get("domain")),
    )


def institution_of(payload: dict[str, Any], sf_account: dict[str, Any]) -> Institution | None:
    """The bank login an account belongs to: v2 by its ``conn_id``, v1 by
    its ``org``. A v2 account whose ``conn_id`` names no listed connection
    still reports the id."""
    if version(payload) == 1:
        return _v1_org(sf_account)
    conn_id = _text(sf_account.get("conn_id"))
    if conn_id is None:
        return None
    for institution in institutions(payload):
        if institution.conn_id == conn_id:
            return institution
    return Institution(conn_id=conn_id, name=None, org_id=None, org_name=None, org_url=None)


def duplicate_account_ids(payload: dict[str, Any]) -> set[str]:
    """Account ids that appear more than once in one payload.

    v2 promises an account id unique only within its connection, and Purser
    maps a feed account by its id alone, so two accounts sharing an id (two
    logins) cannot be told apart. The importer and the drift check skip
    them rather than merge two banks' rows onto one ledger account."""
    counts = Counter(
        sf_account.get("id")
        for sf_account in payload.get("accounts") or []
        if isinstance(sf_account, dict)
    )
    return {str(account_id) for account_id, n in counts.items() if n > 1 and account_id}
