"""Idempotent seeding of the shipped default category set (DESIGN §3.2).

Called by migration 0012 (so every install — fresh or existing — gets the
defaults exactly once; later user deletions stick) and by tests. Existing
paths are never touched: installs with their own tree merge additively.
"""

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
