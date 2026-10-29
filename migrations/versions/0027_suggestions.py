"""Suggestions inbox (issue #233, DESIGN decision 57).

One entity for "Purser thinks you should do X": today's `status='proposed'`
schedule rows carried no evidence, no confidence, no snooze, and rejection
memory was a leftover rejected row that happened to suppress re-detection.
A suggestion carries that state — a typed draft of the target object, the
evidence behind it, a stable fingerprint (one row per fingerprint, ever:
re-detection upserts the open row, a rejected row is durable suppression),
snooze, a scoped rejection, and what accepting it produced.

Data migration: every legacy `proposed` schedule becomes an open suggestion
and every `rejected` one a rejected suggestion (exact scope), then those
schedule rows are deleted — they never materialized an instance, so nothing
references them but audit rows, which are not FKs. `schedules.status` is
then constrained to active | paused | ended.

Revision ID: 0027
Revises: 0026
Create Date: 2026-09-13

"""

import hashlib
from collections.abc import Sequence
from decimal import Decimal

import sqlalchemy as sa
from alembic import op

from purser import crypto

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE_DDL = """
CREATE TABLE suggestions (
  id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  kind                    TEXT NOT NULL CHECK (kind IN ('schedule')),
  status                  TEXT NOT NULL DEFAULT 'open'
                          CHECK (status IN ('open', 'accepted', 'rejected')),
  fingerprint             TEXT NOT NULL UNIQUE,
  source                  TEXT NOT NULL
                          CHECK (source IN ('detected', 'agent', 'account_added')),
  detector                TEXT,
  detector_version        INTEGER,
  confidence              NUMERIC(4,3),
  subject_account_id      UUID REFERENCES accounts(id),
  subject_payee_id        UUID REFERENCES payees(id),
  counterparty_account_id UUID REFERENCES accounts(id),
  draft_enc               BYTEA NOT NULL,
  evidence_enc            BYTEA,
  snooze_until            DATE,
  seen_count              INTEGER NOT NULL DEFAULT 1,
  last_seen_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  decided_at              TIMESTAMPTZ,
  decided_by              UUID REFERENCES api_tokens(id),
  decision_note_enc       BYTEA,
  reject_scope            TEXT CHECK (reject_scope IN ('exact', 'counterparty')),
  result_entity_type      TEXT,
  result_entity_id        UUID,
  created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX suggestions_open ON suggestions (kind, created_at) WHERE status = 'open';
CREATE INDEX suggestions_subject ON suggestions (subject_account_id)
  WHERE subject_account_id IS NOT NULL;
"""

DOWNGRADE_DDL = """
ALTER TABLE schedules DROP CONSTRAINT IF EXISTS schedules_status_check;
DROP TABLE IF EXISTS suggestions;
"""

_LEGACY = sa.text(
    "SELECT s.id, s.name_enc, s.account_id, s.payee_id, s.category_path,"
    " s.counterparty_account_id, s.rrule, s.dtstart, s.amount_type, s.amount,"
    " s.estimate_basis, s.autopost_policy, s.source, s.status, s.created_at,"
    " a.path AS account_path, c.path AS counterparty_path, p.name_bidx, p.name_enc AS payee_enc"
    " FROM schedules s"
    " JOIN accounts a ON a.id = s.account_id"
    " LEFT JOIN accounts c ON c.id = s.counterparty_account_id"
    " LEFT JOIN payees p ON p.id = s.payee_id"
    " WHERE s.status IN ('proposed', 'rejected')"
)


def _fingerprint(account_id: str, counterparty_key: str) -> str:
    return hashlib.sha256(f"schedule:{account_id}:{counterparty_key}".encode()).hexdigest()


def upgrade() -> None:
    crypto.ensure_key()  # the drafts are encrypted; fail before any DDL
    op.execute(UPGRADE_DDL)
    bind = op.get_bind()
    rows = bind.execute(_LEGACY).mappings().all()
    for r in rows:
        name = crypto.decrypt(bytes(r["name_enc"]))
        payee = crypto.decrypt(bytes(r["payee_enc"])) if r["payee_enc"] is not None else None
        if r["counterparty_account_id"] is not None:
            counterparty_key = f"acct:{r['counterparty_account_id']}"
        elif r["name_bidx"] is not None:
            counterparty_key = f"payee:{r['name_bidx']}"
        else:
            counterparty_key = f"name:{hashlib.sha256(name.lower().encode()).hexdigest()}"
        draft = {
            "version": 1,
            "name": name.removesuffix(" (detected)"),
            "account": r["account_path"],
            "counterparty_account": r["counterparty_path"],
            "rrule": r["rrule"],
            "dtstart": r["dtstart"].isoformat(),
            "amount_type": r["amount_type"],
            "amount": str(Decimal(r["amount"])) if r["amount"] is not None else None,
            "estimate_basis": r["estimate_basis"],
            "payee": payee,
            "category": str(r["category_path"]) if r["category_path"] is not None else None,
            "autopost_policy": r["autopost_policy"],
        }
        rejected = r["status"] == "rejected"
        bind.execute(
            sa.text(
                "INSERT INTO suggestions (kind, status, fingerprint, source, detector,"
                " detector_version, confidence, subject_account_id, subject_payee_id,"
                " counterparty_account_id, draft_enc, evidence_enc, decided_at, reject_scope,"
                " created_at, updated_at)"
                " VALUES ('schedule', :status, :fp, :source, :detector, 1, NULL, :acct, :payee,"
                " :cp, :draft, :evidence, :decided_at, :scope, :created_at, now())"
                " ON CONFLICT (fingerprint) DO NOTHING"
            ),
            {
                "status": "rejected" if rejected else "open",
                "fp": _fingerprint(str(r["account_id"]), counterparty_key),
                "source": "detected" if r["source"] == "detected" else "agent",
                "detector": "legacy_schedule_row",
                "acct": r["account_id"],
                "payee": r["payee_id"],
                "cp": r["counterparty_account_id"],
                "draft": crypto.encrypt_json(draft),
                "evidence": crypto.encrypt_json({"legacy_schedule_id": str(r["id"])}),
                "decided_at": r["created_at"] if rejected else None,
                "scope": "exact" if rejected else None,
                "created_at": r["created_at"],
            },
        )
    if rows:
        ids = [r["id"] for r in rows]
        # Proposed/rejected schedules never materialized: no postings, no
        # matches reference them. Audit rows keep the id as history (no FK).
        bind.execute(
            sa.text("DELETE FROM transactions WHERE schedule_id = ANY(:ids)"), {"ids": ids}
        )
        bind.execute(sa.text("DELETE FROM schedules WHERE id = ANY(:ids)"), {"ids": ids})
    op.execute(
        "ALTER TABLE schedules ADD CONSTRAINT schedules_status_check"
        " CHECK (status IN ('active', 'paused', 'ended'))"
    )


def downgrade() -> None:
    op.execute(DOWNGRADE_DDL)
