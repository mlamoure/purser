"""Payee resolution through the blind index (payee names are encrypted, #137)."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from purser import crypto
from purser.core import ledger as core
from purser.models import Payee

# ---------------------------------------------------------------------------
# Payees
# ---------------------------------------------------------------------------


def resolve_payee(session: Session, name: str) -> Payee:
    # payees.name is encrypted (#137); identity resolves via the blind index
    # (HMAC over the normalized name) instead of the dropped normalized_name.
    normalized = core.normalize_payee(name)
    bidx = crypto.blind_index(normalized)
    payee = session.execute(select(Payee).where(Payee.name_bidx == bidx)).scalars().first()
    if payee is None:
        payee = Payee(name=name, name_bidx=bidx)
        session.add(payee)
        session.flush()
    return payee
