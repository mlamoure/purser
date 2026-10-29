"""Replace the propose and commit scopes with one write scope (issue #436).

The agent connected to Purser is the user's own and acts with the user's
authority. Proposing is now a choice the agent makes per call (``activate``
on rules and memories, ``propose`` on schedules), not a ceiling enforced by
its token, so the two write scopes collapse into ``write``. ``write`` grants
everything ``commit`` granted.

Every token holding ``propose`` or ``commit`` — API tokens and sign-in
sessions alike — has both replaced by ``write``, in place, keeping the
order of its other scopes. ``read``, ``read:redacted`` and ``admin`` are
untouched. Each converted token gets one ``token.scopes_migrate`` audit row
(system actor) with its scopes before and after; the row's payload is
encrypted like every other audit payload, so the migration needs the key.

The downgrade maps ``write`` back to ``propose`` + ``commit``, because
``write`` carries commit's authority; a token that held only ``propose``
before the upgrade comes back with ``commit`` too (the upgrade's audit rows
record what each token held). The downgrade writes no audit rows.

DESIGN.md §1, §9 and §10 updated in the same PR.

Revision ID: 0037
Revises: 0036
Create Date: 2026-10-24

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from purser import crypto

revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RETIRED = ("propose", "commit")


def _converted(scopes: list[str]) -> list[str]:
    out: list[str] = []
    for scope in scopes:
        scope = "write" if scope in RETIRED else scope
        if scope not in out:
            out.append(scope)
    return out


def upgrade() -> None:
    crypto.ensure_key()  # fail before any write: the audit payload needs the key
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT id, scopes FROM api_tokens WHERE scopes && ARRAY['propose', 'commit']::text[]"
        )
    ).all()
    for token_id, scopes in rows:
        after = _converted(list(scopes))
        bind.execute(
            sa.text("UPDATE api_tokens SET scopes = :s WHERE id = :id"),
            {"s": after, "id": token_id},
        )
        bind.execute(
            sa.text(
                "INSERT INTO audit_log (actor_token_id, action, entity_type, entity_id, "
                "before_after_enc, kind) VALUES (NULL, 'token.scopes_migrate', 'api_token', "
                ":id, :ba, 'change')"
            ),
            {
                "id": token_id,
                "ba": crypto.encrypt_json(
                    {"before": {"scopes": list(scopes)}, "after": {"scopes": after}}
                ),
            },
        )


def downgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT id, scopes FROM api_tokens WHERE 'write' = ANY(scopes)")
    ).all()
    for token_id, scopes in rows:
        before: list[str] = []
        for scope in scopes:
            for restored in RETIRED if scope == "write" else (scope,):
                if restored not in before:
                    before.append(restored)
        bind.execute(
            sa.text("UPDATE api_tokens SET scopes = :s WHERE id = :id"),
            {"s": before, "id": token_id},
        )
