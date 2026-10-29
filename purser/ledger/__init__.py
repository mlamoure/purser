"""Ledger service layer: persists what purser.core.ledger computes.

Structural rules enforced here (not in routes) so every caller — REST, MCP,
importer — gets identical semantics:
- writes declare a scope; a write principal's requested status is honoured (#436)
- posted rows never change: updates become atomic reversal+replacement pairs
- every mutation writes audit_log; posted transactions emit events

This module only re-exports (issue #91). The code lives in submodules, each
importing only the ones listed before it:
- errors: LedgerError and its subclasses, check_path, ltree_input_refusal
- payees: resolve_payee
- accounts: account and category lookup, create, update, move, delete, and
  the closed-account guard
- reporting: account_balances, category_rollup, spending summary and baseline
- tx_create: the create forms and the _persist_transaction funnel
- lifecycle: close_account and reopen_account
- tx_read: get_transaction and search_transactions
- tx_update: edits, correction pairs, recategorize_posted, transition_status

Two rules keep the split invisible:
- Import from ``purser.ledger`` only (``from purser import ledger`` or
  ``from purser.ledger import LedgerError``), never from a submodule. Inside
  the package, submodules import siblings by absolute path
  (``from purser.ledger.accounts import resolve_account``) and never through
  ``purser.ledger``, which is half-initialised while they load.
- Tests that monkeypatch must patch the defining submodule, never
  ``purser.ledger``: ``purser.ledger.tx_create._persist_transaction``, not
  ``purser.ledger._persist_transaction``. Patching the package changes
  nothing a submodule calls. A sibling that imported the name holds its own
  binding, so a call made from there is patched there
  (``purser.ledger.tx_update._persist_transaction``).

``__all__`` lists the public names. The four private helpers purser.transfers
uses (_persist_transaction, _tx_audit_shape, _specs_of, _flat_legs) are
re-exported with the redundant-alias form; no other private name is.
"""

from purser.ledger.accounts import (
    CATEGORY_TYPES,
    REAL_TYPES,
    account_by_path,
    create_account,
    delete_category,
    ensure_category_path,
    get_account,
    move_account,
    resolve_account,
    update_account,
    update_category,
)
from purser.ledger.errors import (
    LedgerError,
    NotFoundError,
    PathConflictError,
    check_path,
    ltree_input_refusal,
)
from purser.ledger.lifecycle import close_account, reopen_account
from purser.ledger.payees import resolve_payee
from purser.ledger.reporting import (
    SPENDING_BASELINES,
    account_balances,
    category_rollup,
    spending_baseline,
    spending_summary,
)
from purser.ledger.tx_create import (
    SPLIT_REMAINDER_MEMO,
    check_category_currency,
    create_flat_transaction,
    create_split_transaction,
    create_split_transfer,
    create_transfer,
)
from purser.ledger.tx_create import _persist_transaction as _persist_transaction
from purser.ledger.tx_create import _tx_audit_shape as _tx_audit_shape
from purser.ledger.tx_read import get_transaction, search_transactions
from purser.ledger.tx_update import _flat_legs as _flat_legs
from purser.ledger.tx_update import _specs_of as _specs_of
from purser.ledger.tx_update import (
    recategorize_posted,
    reverse_and_replace_with_specs,
    transition_status,
    update_transaction,
)

__all__ = [
    "CATEGORY_TYPES",
    "REAL_TYPES",
    "SPENDING_BASELINES",
    "SPLIT_REMAINDER_MEMO",
    "LedgerError",
    "NotFoundError",
    "PathConflictError",
    "account_balances",
    "account_by_path",
    "category_rollup",
    "check_category_currency",
    "check_path",
    "close_account",
    "create_account",
    "create_flat_transaction",
    "create_split_transaction",
    "create_split_transfer",
    "create_transfer",
    "delete_category",
    "ensure_category_path",
    "get_account",
    "get_transaction",
    "ltree_input_refusal",
    "move_account",
    "recategorize_posted",
    "reopen_account",
    "resolve_account",
    "resolve_payee",
    "reverse_and_replace_with_specs",
    "search_transactions",
    "spending_baseline",
    "spending_summary",
    "transition_status",
    "update_account",
    "update_category",
    "update_transaction",
]
