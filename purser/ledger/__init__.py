"""Ledger service layer: persists what purser.core.ledger computes.

Structural rules enforced here (not in routes) so every caller — REST, MCP,
importer — gets identical semantics:
- writes declare a scope; a write principal's requested status is honoured (#436)
- posted rows never change: updates become atomic reversal+replacement pairs
- every mutation writes audit_log; posted transactions emit events

This module only re-exports (issue #91). The code lives in submodules, each
importing only the ones listed before it:
- errors: LedgerError and its subclasses (RowChanged among them), check_path,
  ltree_input_refusal
- payees: resolve_payee
- accounts: account and category lookup, create, update, move, delete, and
  the closed-account guard
- reporting: account_balances, category_rollup, spending summary and baseline
- tx_create: the create forms and the _persist_transaction funnel
- lifecycle: close_account and reopen_account
- tx_read: get_transaction and search_transactions
- tx_update: edits, correction pairs, recategorize_posted, transition_status,
  and the row lock and corrected-row refusal every merge and correction runs

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
    ASSET_ENTRY_PATHS,
    CAPITAL_PATH,
    CATEGORY_TYPES,
    REAL_TYPES,
    REVALUATION_PATH,
    SECURITIES_PATH,
    SYSTEM_ROOT,
    SYSTEM_TYPES,
    account_by_path,
    capital_account,
    create_account,
    delete_category,
    ensure_category_path,
    get_account,
    has_system_leg,
    in_system_subtree,
    is_capital,
    is_category,
    is_securities_trade,
    is_system_account,
    is_trade_cash_lineage,
    is_value_change,
    lock_transaction,
    move_account,
    refuse_system_account,
    refuse_system_path,
    refuse_trade_cash_lineage,
    resolve_account,
    revaluation_account,
    securities_account,
    trade_cash_lineage_ids,
    trade_cash_lineage_set,
    update_account,
    update_category,
)
from purser.ledger.errors import (
    LedgerError,
    NotFoundError,
    PathConflictError,
    RowBusy,
    RowChanged,
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
    lock_rows,
    recategorize_posted,
    refuse_corrected,
    reverse_and_replace_with_specs,
    transition_status,
    update_transaction,
)

__all__ = [
    "ASSET_ENTRY_PATHS",
    "CAPITAL_PATH",
    "CATEGORY_TYPES",
    "REAL_TYPES",
    "REVALUATION_PATH",
    "SECURITIES_PATH",
    "SPENDING_BASELINES",
    "SPLIT_REMAINDER_MEMO",
    "SYSTEM_ROOT",
    "SYSTEM_TYPES",
    "LedgerError",
    "NotFoundError",
    "PathConflictError",
    "RowBusy",
    "RowChanged",
    "account_balances",
    "account_by_path",
    "capital_account",
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
    "has_system_leg",
    "in_system_subtree",
    "is_capital",
    "is_category",
    "is_securities_trade",
    "is_system_account",
    "is_trade_cash_lineage",
    "is_value_change",
    "lock_rows",
    "lock_transaction",
    "ltree_input_refusal",
    "move_account",
    "recategorize_posted",
    "refuse_corrected",
    "refuse_system_account",
    "refuse_system_path",
    "refuse_trade_cash_lineage",
    "reopen_account",
    "resolve_account",
    "resolve_payee",
    "revaluation_account",
    "reverse_and_replace_with_specs",
    "search_transactions",
    "securities_account",
    "spending_baseline",
    "spending_summary",
    "trade_cash_lineage_ids",
    "trade_cash_lineage_set",
    "transition_status",
    "update_account",
    "update_category",
    "update_transaction",
]
