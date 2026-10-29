"""Minimal disclosure to agents (issue #437).

- ``encryption_keys`` gains a third wrapped DEK, purpose ``payee_ref``: the
  HMAC key behind the stable payee references (``py_...``) agents receive
  in place of payee names when the purchase-privacy setting is on. Like the
  blind-index and content-hash keys it is random material wrapped by the
  primary key, so ``purser admin rotate-key`` re-wraps it and every reference
  stays byte-identical.
- ``audit_log.disclosed TEXT[]``: on an MCP call row, the fields the agent
  asked for with ``include`` beyond the user's privacy defaults. Field names
  only, never values, so it is a plaintext skeleton column (the call detail
  itself is encrypted) and the audit view can filter on it: "what did my AI
  choose to see". NULL on every other row.
- The ``read:redacted`` scope is retired. Its only effect — external account
  identifiers omitted — is now the default for every API token, so each
  token holding it has it replaced by ``read`` (one ``token.scopes_migrate``
  audit row each, system actor, payload encrypted like every audit payload).

The downgrade drops the column but keeps the DEK row (a re-upgrade must
not change every reference agents hold); it does not restore
``read:redacted`` (``read`` is a superset of it, so nothing is lost by
keeping the converted scope).

DESIGN.md §3.1, §3.6, §9 and §10 updated in the same PR.

Revision ID: 0038
Revises: 0037
Create Date: 2026-10-24

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from purser import crypto

revision: str = "0038"
down_revision: str | None = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    crypto.ensure_key()  # fail before any DDL: the DEK and audit rows need the key
    bind = op.get_bind()
    # ON CONFLICT: a downgrade keeps the key (references must not change
    # under a re-upgrade), so a re-run finds it already there.
    bind.execute(
        sa.text(
            "INSERT INTO encryption_keys (purpose, key_enc) VALUES (:p, :k) "
            "ON CONFLICT (purpose) DO NOTHING"
        ),
        {"p": crypto.PAYEE_REF_PURPOSE, "k": crypto.wrap_key(crypto.generate_dek())},
    )
    op.execute("ALTER TABLE audit_log ADD COLUMN disclosed TEXT[]")
    op.execute("CREATE INDEX audit_log_disclosed ON audit_log (at) WHERE disclosed IS NOT NULL")

    rows = bind.execute(
        sa.text("SELECT id, scopes FROM api_tokens WHERE 'read:redacted' = ANY(scopes)")
    ).all()
    for token_id, scopes in rows:
        after: list[str] = []
        for scope in scopes:
            scope = "read" if scope == "read:redacted" else scope
            if scope not in after:
                after.append(scope)
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
    # The payee_ref key stays: dropping it would change every reference
    # agents hold once the migration ran again.
    op.execute("DROP INDEX IF EXISTS audit_log_disclosed")
    op.execute("ALTER TABLE audit_log DROP COLUMN IF EXISTS disclosed")
