"""Account row locks: the one place the account-before-watermark rule lives
(decision 88, #403, #413, #422).

Every writer that changes what an account's balance, holdings or net worth
shows locks the account row FIRST, then marks the balance watermark
(``balances.mark_dirty``): the importer, the holdings writers, a source
switch, reconciliation, close/reopen and the delete-account path. With one
order everywhere, two writers wait for each other instead of deadlocking.

FOR NO KEY UPDATE, not FOR UPDATE: two writers on one account still exclude
each other, while an import inserting postings on it (whose foreign-key
check takes KEY SHARE on the row) is never blocked. Several accounts are
locked in id order. Rows are read fresh, so a waiter sees what the previous
holder committed.
"""

import uuid
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from purser.models import Account


def lock_accounts(session: Session, account_ids: Iterable[uuid.UUID]) -> list[Account]:
    """Lock these account rows, in id order, and return them read fresh
    (missing ids are simply absent)."""
    ids = sorted(set(account_ids))
    if not ids:
        return []
    return list(
        session.execute(
            select(Account)
            .where(Account.id.in_(ids))
            .order_by(Account.id)
            .with_for_update(key_share=True)
            .execution_options(populate_existing=True)
        ).scalars()
    )


def lock_account(session: Session, account_id: uuid.UUID) -> Account | None:
    """Lock one account row and return it read fresh, or None."""
    accounts = lock_accounts(session, [account_id])
    return accounts[0] if accounts else None
