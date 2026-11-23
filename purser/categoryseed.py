"""Idempotent seeding of the shipped default category set (DESIGN §3.2).

Called by migration 0012 (so every install — fresh or existing — gets the
defaults exactly once; later user deletions stick) and by tests. A category
added to the shipped set later is seeded by its own migration through
``seed_categories`` (0058: two income categories), so the rest of the tree is
never re-seeded. Existing
paths are never touched: installs with their own tree merge additively.
"""

from collections.abc import Iterable

from sqlalchemy import text
from sqlalchemy.engine import Connection

from purser.core.categories import DEFAULT_CATEGORIES

_INSERT = text(
    """
    INSERT INTO accounts (name, type, is_virtual, path, description, parent_id)
    SELECT :name, :type, true, CAST(:path AS ltree), :description,
           (SELECT id FROM accounts WHERE path = CAST(:parent AS ltree))
    WHERE NOT EXISTS (SELECT 1 FROM accounts WHERE path = CAST(:path AS ltree))
    """
)


def seed_default_categories(bind: Connection) -> int:
    """Insert missing default categories (parents first); returns how many
    rows were created. Safe to run repeatedly."""
    created = 0
    for spec in DEFAULT_CATEGORIES:
        parent = spec.path.rsplit(".", 1)[0] if "." in spec.path else None
        result = bind.execute(
            _INSERT,
            {
                "name": spec.name,
                "type": spec.type,
                "path": spec.path,
                "description": spec.description,
                "parent": parent,
            },
        )
        created += result.rowcount or 0
    return created


_INSERT_UNDER_PARENT = text(
    """
    INSERT INTO accounts (name, type, is_virtual, path, description, parent_id)
    SELECT :name, :type, true, CAST(:path AS ltree), :description, parent.id
      FROM accounts parent
     WHERE parent.path = CAST(:parent AS ltree)
       AND NOT EXISTS (SELECT 1 FROM accounts WHERE path = CAST(:path AS ltree))
    """
)


def seed_categories(bind: Connection, paths: Iterable[str]) -> int:
    """Insert only the named default categories, each once (a later
    migration adding to the shipped set, #591). Never the whole tree: a
    default category a person deleted stays deleted. A child whose parent is
    missing (the person deleted the parent) is skipped rather than seeded as
    an orphan. Returns how many rows were created."""
    wanted = set(paths)
    unknown = wanted - {spec.path for spec in DEFAULT_CATEGORIES}
    if unknown:
        raise ValueError(f"not a default category: {sorted(unknown)}")
    created = 0
    for spec in DEFAULT_CATEGORIES:  # parents first
        if spec.path not in wanted:
            continue
        params = {
            "name": spec.name,
            "type": spec.type,
            "path": spec.path,
            "description": spec.description,
        }
        if "." in spec.path:
            result = bind.execute(
                _INSERT_UNDER_PARENT, {**params, "parent": spec.path.rsplit(".", 1)[0]}
            )
        else:
            result = bind.execute(_INSERT, {**params, "parent": None})
        created += result.rowcount or 0
    return created
