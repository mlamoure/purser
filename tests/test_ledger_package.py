"""The purser.ledger package (#91): the split into submodules keeps every
name callers reach, defines each one exactly once, and has no import cycle."""

import ast
import importlib
import inspect
import pkgutil
import subprocess
import sys
from pathlib import Path

import pytest

import purser.ledger as ledger

PUBLIC = {
    "CATEGORY_TYPES",
    "LedgerError",
    "NotFoundError",
    "PathConflictError",
    "REAL_TYPES",
    "SPENDING_BASELINES",
    "SPLIT_REMAINDER_MEMO",
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
}
# Private helpers purser.transfers reaches through the package.
PRIVATES = {"_flat_legs", "_persist_transaction", "_specs_of", "_tx_audit_shape"}
EXPECTED_SURFACE = PUBLIC | PRIVATES

# Dependency order: each submodule may import only the ones before it.
SUBMODULES = [
    "errors",
    "payees",
    "accounts",
    "reporting",
    "tx_create",
    "lifecycle",
    "tx_read",
    "tx_update",
]
PACKAGE_DIR = Path(ledger.__file__).parent
REPO_ROOT = Path(__file__).resolve().parents[1]


def _top_level_definitions(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def test_surface_is_all_plus_the_private_reexports() -> None:
    assert len(ledger.__all__) == len(set(ledger.__all__))
    assert set(ledger.__all__) == PUBLIC
    assert set(ledger.__all__) | PRIVATES == EXPECTED_SURFACE
    assert [n for n in sorted(EXPECTED_SURFACE) if not hasattr(ledger, n)] == []


def test_submodules_are_the_documented_set() -> None:
    assert {m.name for m in pkgutil.iter_modules(ledger.__path__)} == set(SUBMODULES)


CALLABLES = sorted(
    n
    for n in EXPECTED_SURFACE
    if inspect.isfunction(getattr(ledger, n)) or inspect.isclass(getattr(ledger, n))
)


@pytest.mark.parametrize("name", CALLABLES)
def test_functions_and_classes_come_from_their_submodule(name: str) -> None:
    obj = getattr(ledger, name)
    assert obj.__module__.startswith("purser.ledger."), obj.__module__
    assert getattr(importlib.import_module(obj.__module__), name) is obj


def test_every_name_is_defined_once_and_the_package_only_reexports() -> None:
    assert _top_level_definitions(PACKAGE_DIR / "__init__.py") == {"__all__"}
    owner: dict[str, str] = {}
    for sub in SUBMODULES:
        for name in _top_level_definitions(PACKAGE_DIR / f"{sub}.py"):
            assert name not in owner, f"{name} defined in both {owner[name]} and {sub}"
            owner[name] = sub
    for name in EXPECTED_SURFACE:
        assert name in owner, f"{name} is not defined in any submodule"
        submodule = importlib.import_module(f"purser.ledger.{owner[name]}")
        assert getattr(ledger, name) is getattr(submodule, name)


@pytest.mark.parametrize("sub", SUBMODULES)
def test_submodule_imports_only_earlier_siblings_by_absolute_path(sub: str) -> None:
    """Never through the half-initialised package, and never a later sibling."""
    earlier = set(SUBMODULES[: SUBMODULES.index(sub)])
    tree = ast.parse((PACKAGE_DIR / f"{sub}.py").read_text())
    for node in tree.body:  # top level only; function-local imports may go anywhere
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            assert not (node.module == "purser" and any(a.name == "ledger" for a in node.names))
            assert node.module != "purser.ledger", f"{sub} imports from the package"
            if node.module.startswith("purser.ledger."):
                assert node.module.removeprefix("purser.ledger.") in earlier, node.module
        elif isinstance(node, ast.Import):
            assert all(not a.name.startswith("purser.ledger") for a in node.names)


@pytest.mark.parametrize("sub", SUBMODULES)
def test_submodule_imports_cleanly_in_a_fresh_interpreter(sub: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", f"import purser.ledger.{sub}"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
