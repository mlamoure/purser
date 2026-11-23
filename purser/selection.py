"""The `present` link: transactions shown to the signed-in person in full
detail, without their values passing through an agent (#437, #613).

One function serves the MCP ``present`` tool and ``POST
/transactions/present``. The link (``PURSER_PUBLIC_URL/selection?id=…``)
carries only transaction ids and an optional note; the web app renders it
from ``GET /transactions/selection``, a signed-in-only route.
"""

import uuid
from typing import Any
from urllib.parse import urlencode

from sqlalchemy import select
from sqlalchemy.orm import Session

from purser.config import get_settings
from purser.ledger import LedgerError
from purser.models import Transaction

PRESENT_MAX = 50
NOTE_MAX = 200


def selection_link(
    session: Session, transaction_ids: list[str], note: str | None
) -> dict[str, Any]:
    """``{"url", "count"}`` for these rows, in the order given (duplicates
    dropped). Raises ``LedgerError`` for an empty or oversized list, a long
    note, or an id that is not a transaction."""
    if not transaction_ids:
        raise LedgerError("present needs at least one transaction id")
    if len(transaction_ids) > PRESENT_MAX:
        raise LedgerError(f"present takes at most {PRESENT_MAX} transaction ids")
    if note is not None and len(note) > NOTE_MAX:
        raise LedgerError(f"note must be {NOTE_MAX} characters or fewer")
    try:
        ids = list(dict.fromkeys(uuid.UUID(str(t)) for t in transaction_ids))
    except ValueError as exc:
        raise LedgerError("transaction_ids must be transaction UUIDs") from exc
    found = set(session.execute(select(Transaction.id).where(Transaction.id.in_(ids))).scalars())
    missing = [str(i) for i in ids if i not in found]
    if missing:
        raise LedgerError(f"unknown transaction id(s): {', '.join(missing)}")
    query = [("id", str(i)) for i in ids] + ([("note", note)] if note else [])
    base = get_settings().public_url.rstrip("/")
    return {"url": f"{base}/selection?{urlencode(query)}", "count": len(ids)}
