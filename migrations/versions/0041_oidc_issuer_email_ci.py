"""SSO links are scoped by issuer; emails are unique case-insensitively (issue #429).

D1 — ``users.oidc_issuer`` stores the provider's issuer next to
``oidc_subject``. A subject is only unique at its provider, so the single
``UNIQUE (oidc_subject)`` becomes uniqueness on (issuer, subject); a legacy
link with no issuer keeps the old subject-only uniqueness (partial index).
A CHECK keeps an issuer from existing without a subject.

Existing links carry over. The migration reads the issuer the instance is
configured with — the ``oidc_issuer`` row in ``app_settings`` when one exists
(a JSON null there is the SSO kill switch: no issuer), else
``PURSER_OIDC_ISSUER`` from the environment — and records it, without a
trailing slash, on every user that has a subject, one ``user.oidc_issuer_backfill``
audit row (system actor) each. Every link today was made through that
issuer: a sign-in is accepted only from the configured provider, and a move
to another provider relinked by email. When no issuer can be determined the
links stay with a NULL issuer, and the next sign-in from the configured
provider with that subject claims the link and records the issuer
(``service.login_with_oidc``). Either way no sign-in changes for the user.

D4 — email uniqueness becomes case-insensitive: ``UNIQUE (email)`` is
replaced by a unique index on ``lower(email)``, which sign-in, linking and
provisioning look up through. Emails stay stored as entered. Before any
change the migration looks for accounts whose emails differ only by case and
stops with an error naming them; it never merges accounts.

The downgrade restores ``UNIQUE (email)`` (implied by the case-insensitive
index, so it cannot fail) and ``UNIQUE (oidc_subject)`` — refusing, with an
error, when two providers' links share a subject — and drops the issuer.

DESIGN.md §3.1, §9 and decision 95 updated in the same PR.

Revision ID: 0041
Revises: 0040
Create Date: 2026-10-27

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from purser import crypto
from purser.config import get_settings

revision: str = "0041"
down_revision: str | None = "0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _configured_issuer(bind: sa.Connection) -> str | None:
    """The issuer this instance signs in with, normalized like
    ``oidc.issuer_key`` (no trailing slash; copied, so this revision never
    changes with app code): the app_settings override wins over the env."""
    row = bind.execute(
        sa.text("SELECT value FROM app_settings WHERE key = 'oidc_issuer'")
    ).one_or_none()
    value = row[0] if row is not None else get_settings().oidc_issuer
    if not isinstance(value, str) or not value.rstrip("/"):
        return None
    return value.rstrip("/")


def _refuse_case_duplicates(bind: sa.Connection) -> None:
    dupes = bind.execute(
        sa.text(
            "SELECT array_agg(email ORDER BY created_at) FROM users "
            "GROUP BY lower(email) HAVING count(*) > 1 ORDER BY min(created_at)"
        )
    ).all()
    if dupes:
        groups = "; ".join(", ".join(emails) for (emails,) in dupes)
        raise RuntimeError(
            "migration 0041 stopped: these accounts have emails that differ only by "
            f"letter case ({groups}). Emails become unique regardless of case, and "
            "Purser never merges accounts. Give the extra account of each group a "
            "different email address in the database, then run `purser migrate` again."
        )


def upgrade() -> None:
    bind = op.get_bind()
    _refuse_case_duplicates(bind)

    op.execute(
        """
        ALTER TABLE users
          DROP CONSTRAINT users_email_key,
          DROP CONSTRAINT users_oidc_subject_key,
          ADD COLUMN oidc_issuer TEXT,
          ADD CONSTRAINT users_oidc_issuer_needs_subject
            CHECK (oidc_issuer IS NULL OR oidc_subject IS NOT NULL);
        CREATE UNIQUE INDEX users_email_lower_key ON users (lower(email));
        CREATE UNIQUE INDEX users_oidc_identity_key ON users (oidc_issuer, oidc_subject)
          WHERE oidc_issuer IS NOT NULL;
        CREATE UNIQUE INDEX users_oidc_legacy_subject_key ON users (oidc_subject)
          WHERE oidc_issuer IS NULL AND oidc_subject IS NOT NULL;
        """
    )

    issuer = _configured_issuer(bind)
    if issuer is None:
        return
    linked = bind.execute(
        sa.text("SELECT id FROM users WHERE oidc_subject IS NOT NULL ORDER BY created_at")
    ).all()
    if not linked:
        return
    crypto.ensure_key()  # fail before any write: the audit payload needs the key
    for (user_id,) in linked:
        bind.execute(
            sa.text("UPDATE users SET oidc_issuer = :iss WHERE id = :id"),
            {"iss": issuer, "id": user_id},
        )
        bind.execute(
            sa.text(
                "INSERT INTO audit_log (actor_token_id, action, entity_type, entity_id, "
                "before_after_enc, kind) VALUES (NULL, 'user.oidc_issuer_backfill', 'user', "
                ":id, :ba, 'change')"
            ),
            {
                "id": user_id,
                "ba": crypto.encrypt_json(
                    {"before": {"oidc_issuer": None}, "after": {"oidc_issuer": issuer}}
                ),
            },
        )


def downgrade() -> None:
    bind = op.get_bind()
    shared = bind.execute(
        sa.text(
            "SELECT count(*) FROM (SELECT oidc_subject FROM users "
            "WHERE oidc_subject IS NOT NULL GROUP BY oidc_subject HAVING count(*) > 1) s"
        )
    ).scalar_one()
    if shared:
        raise RuntimeError(
            f"downgrade below 0041 stopped: {shared} SSO subject(s) are linked to more "
            "than one account through different providers, which the older schema "
            "cannot hold. Unlink one account of each pair first."
        )
    op.execute(
        """
        DROP INDEX users_oidc_legacy_subject_key;
        DROP INDEX users_oidc_identity_key;
        DROP INDEX users_email_lower_key;
        ALTER TABLE users
          DROP CONSTRAINT users_oidc_issuer_needs_subject,
          DROP COLUMN oidc_issuer,
          ADD CONSTRAINT users_oidc_subject_key UNIQUE (oidc_subject),
          ADD CONSTRAINT users_email_key UNIQUE (email);
        """
    )
