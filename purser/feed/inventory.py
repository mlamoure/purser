"""Read-only feed inventory for agents (#482).

Every account SimpleFIN delivers — connected to a ledger account or not —
grouped by bank login, with what an agent needs to answer "did a new account
show up at my bank?" or "which bank needs a new login?": the login's issue,
and per account whether it is connected, how old its bank balance is,
whether holdings and transactions arrive, and any account-level issue.

Read from each connection's latest completed sync (``latest_sync_fetch``),
the same source as discovery. For tokens that withhold external identifiers
(every non-admin API token, #437) the SimpleFIN ids are left out and the
masked account-number suffixes banks put in account names are removed.
"""

import re
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from purser import appsettings, clock, crypto
from purser.feed import drift, envelope
from purser.models import Account, Connection

# Masked account-number fragments banks put in display names:
# "(1234)", "( ***1234 )", "*1234", "Card-1234", "(_000)".
_MASKED_NUMBER = re.compile(r"\s*\(\s*[*xX•#_.\s-]*\d{3,}\s*\)|\s*[*xX•#]+\d{3,}|-\d{4}\b")


def redact_name(name: str | None) -> str | None:
    """A bank's account name without its masked account-number suffix."""
    if name is None:
        return None
    cleaned = " ".join(_MASKED_NUMBER.sub("", name.replace("\xa0", " ")).split())
    return cleaned or None


def feed_inventory(session: Session, *, redact: bool) -> list[dict[str, Any]]:
    """One entry per SimpleFIN connection that is not disabled: its status
    and last sync, and its bank logins with their accounts."""
    today = clock.ledger_today()
    stale_days = appsettings.effective().feed_stale_days
    return [
        _connection_entry(session, connection, redact=redact, today=today, stale_days=stale_days)
        for connection in session.execute(
            select(Connection)
            .where(Connection.status != "disabled")
            .order_by(Connection.created_at)
        ).scalars()
    ]


def _connection_entry(
    session: Session, connection: Connection, *, redact: bool, today: date, stale_days: int
) -> dict[str, Any]:
    from purser.feed.service import latest_sync_fetch

    raw = latest_sync_fetch(session, connection.id)
    payload: dict[str, Any] = crypto.decrypt_json(raw.payload_enc) if raw else {}
    mapped = {
        account.external_id: account
        for account in session.execute(
            select(Account).where(
                Account.connection_id == connection.id, Account.external_id.is_not(None)
            )
        ).scalars()
    }
    from purser.feed.service import ignored_external_ids

    ignored = ignored_external_ids(session, connection.id)
    issues = envelope.issues(payload)
    # Keyed by the login's own id where the issue names one of the listed
    # logins (the ids can differ by a provider prefix), else by the issue's.
    login_issue: dict[str, envelope.Issue] = {}
    for i in issues:
        if i.prefix == "con" and i.conn_id:
            named = envelope.login_of_issue(payload, i)
            login_issue[(named.conn_id if named else None) or i.conn_id] = i
    account_issue = {i.account_id: i for i in issues if i.prefix == "act" and i.account_id}
    ambiguous = envelope.duplicate_account_ids(payload)

    logins: dict[str | None, dict[str, Any]] = {}

    def login_for(institution: envelope.Institution | None) -> dict[str, Any]:
        key = (
            (institution.conn_id or institution.org_id or institution.name) if institution else None
        )
        if key not in logins:
            issue = login_issue.get(key) if key else None
            logins[key] = {
                "institution": institution.name if institution else None,
                "conn_id": None if redact or institution is None else institution.conn_id,
                "url": institution.org_url if institution else None,
                "issue": {"code": issue.code, "message": issue.msg} if issue else None,
                "accounts": [],
            }
        return logins[key]

    for institution in envelope.institutions(payload):
        login_for(institution)

    for sf_account in payload.get("accounts") or []:
        if not isinstance(sf_account, dict):
            continue
        login = login_for(envelope.institution_of(payload, sf_account))
        external_id = sf_account.get("id")
        account = mapped.get(external_id)
        balance_date = drift.parse_balance_date(sf_account.get("balance-date"))
        age = (today - balance_date).days if balance_date else None
        issue = account_issue.get(str(external_id)) if external_id else None
        name = sf_account.get("name")
        login["accounts"].append(
            {
                "name": redact_name(name) if redact else name,
                "external_id": None if redact else external_id,
                "account": account.path if account else None,
                "connected": account is not None and account.closed_at is None,
                "closed": account is not None and account.closed_at is not None,
                "balance_date": balance_date,
                "days_since_balance": age,
                "stale": age is not None and age > stale_days,
                "holdings": len(sf_account.get("holdings") or []),
                "transactions": len(sf_account.get("transactions") or []),
                "issue": {"code": issue.code, "message": issue.msg} if issue else None,
                "ambiguous": external_id in ambiguous,
                "ignored": account is None and external_id in ignored,
            }
        )

    return {
        "connection_id": None if redact else connection.id,
        "status": connection.status,
        "last_sync_at": connection.last_fetch_at,
        "protocol_version": envelope.version(payload) if raw else None,
        "stale_days": stale_days,
        "logins": list(logins.values()),
    }


def feed_status(session: Session) -> dict[str, Any]:
    """The agent summary (MCP ``feed_status``): one line per bank login, and
    only the accounts that need attention — not connected, stale, failing or
    ambiguous — so a healthy setup answers in a few lines."""
    inventory = feed_inventory(session, redact=True)
    logins: list[dict[str, Any]] = []
    attention: list[dict[str, Any]] = []
    last_sync = None
    for connection in inventory:
        if connection["last_sync_at"] and (
            last_sync is None or connection["last_sync_at"] > last_sync
        ):
            last_sync = connection["last_sync_at"]
        for login in connection["logins"]:
            accounts = login["accounts"]
            problem = login["issue"]
            logins.append(
                {
                    "institution": login["institution"],
                    "status": "needs_login"
                    if problem and problem["code"].startswith("con.auth")
                    else ("error" if problem else "ok"),
                    **({"issue": problem["message"]} if problem else {}),
                    "accounts": len(accounts),
                    "connected": sum(1 for a in accounts if a["connected"]),
                }
            )
            for account in accounts:
                reasons = []
                if not account["connected"] and not account["closed"] and not account["ignored"]:
                    reasons.append("not_connected")
                if account["connected"] and account["stale"]:
                    reasons.append(f"stale {account['days_since_balance']}d")
                if account["issue"]:
                    reasons.append(account["issue"]["code"])
                if account["ambiguous"]:
                    reasons.append("ambiguous_id")
                if reasons:
                    attention.append(
                        {
                            "institution": login["institution"],
                            "name": account["name"],
                            **({"account": account["account"]} if account["account"] else {}),
                            "problems": reasons,
                        }
                    )
    return {
        "last_sync_at": last_sync.isoformat() if last_sync else None,
        "logins": logins,
        "attention": attention,
    }
