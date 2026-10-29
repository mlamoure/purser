"""A manual transfer confirm proposes a payee-scoped transfer_link rule in the
suggestions inbox (issue #271, DESIGN decision 67).

Replaces the hidden payee memory #253 proposed. The review of #253 found four
problems with that memory — it learned from one confirm, applied
retroactively without decision 46's projected-count gate, was never unlearned
on unlink, and broke when a rename aliased the payee. Each has a test here:

- a confirm proposes exactly one suggestion, and repeat confirms refresh it;
- accepting creates the rule as a DRAFT, and only the activation confirm
  makes it link — past pairs inside its scope, then future ones;
- a rejection is remembered;
- the payee scope follows rename aliases;
- unlinking leaves nothing that links by itself.

Synthetic data only. Every seeded date is a fixed day (offsets from DAY0), so
nothing here depends on today or on where it falls in the month (#262).
"""

import os
import uuid
from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy import Engine, text

import purser.config
import purser.db
from purser import crypto
from purser import rules as rules_service
from purser import suggestions as service
from purser.core import ledger as core_ledger
from tests.conftest import TEST_ENCRYPTION_KEY
from tests.test_mcp import (  # noqa: F401 (fixture re-export)
    LiveServer,
    _mcp_call,
    seeded_tokens,
)

DAY0 = date(2026, 5, 4)
CHECKING, SAVINGS = "assets.checking", "assets.savings"
# Descriptors with no name signal: no transfer keyword, and no token naming
# the counterparty account ("Checking" / "Reserve").
OUT_TEXT = "ZEPHYR BKG SVC SENT"
IN_TEXT = "ZEPHYR FUNDS RECEIVED"
OTHER_OUT, OTHER_IN = "NORTHWIND DEBIT", "NORTHWIND CREDIT"


def _day(offset: int) -> date:
    return DAY0 + timedelta(days=offset)


@pytest.fixture
def seeded(admin_client: TestClient) -> dict[str, str]:
    ids: dict[str, str] = {}
    for name, type_, path in (("Checking", "checking", CHECKING), ("Reserve", "savings", SAVINGS)):
        resp = admin_client.post("/accounts", json={"name": name, "type": type_, "path": path})
        assert resp.status_code == 201, resp.text
        ids[path] = resp.json()["id"]
    return ids


def _bank_row(engine: Engine, account_path: str, amount: str, on: date, payee: str | None) -> str:
    """A posted one-sided import row, its payee resolved by blind index the
    way the importer resolves it — so two rows with the same text share one
    payee, exactly as in production."""
    with engine.begin() as conn:
        account_id = conn.execute(
            text("SELECT id FROM accounts WHERE path = :p"), {"p": account_path}
        ).scalar_one()
        category = conn.execute(
            text("SELECT id FROM accounts WHERE path = 'expenses.uncategorized'")
        ).scalar_one_or_none()
        if category is None:
            category = conn.execute(
                text(
                    "INSERT INTO accounts (name, type, is_virtual, path) VALUES "
                    "('Uncategorized', 'category_expense', true, 'expenses.uncategorized')"
                    " RETURNING id"
                )
            ).scalar_one()
        payee_id = None
        if payee is not None:
            bidx = crypto.blind_index(core_ledger.normalize_payee(payee))
            payee_id = conn.execute(
                text("SELECT id FROM payees WHERE name_bidx = :b"), {"b": bidx}
            ).scalar_one_or_none()
            if payee_id is None:
                payee_id = conn.execute(
                    text("INSERT INTO payees (name_enc, name_bidx) VALUES (:n, :b) RETURNING id"),
                    {"n": crypto.encrypt(payee), "b": bidx},
                ).scalar_one()
        tx_id = conn.execute(
            text(
                "INSERT INTO transactions (date, payee_id, description_enc, status, origin)"
                " VALUES (:d, :p, :desc, 'posted', 'import') RETURNING id"
            ),
            {"d": on, "p": payee_id, "desc": crypto.encrypt(payee or "BANK LINE")},
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO postings (transaction_id, account_id, amount) VALUES "
                "(:tx, :acct, :amt), (:tx, :cat, :neg)"
            ),
            {
                "tx": tx_id,
                "acct": account_id,
                "amt": Decimal(amount),
                "cat": category,
                "neg": -Decimal(amount),
            },
        )
    return str(tx_id)


def _pair(
    engine: Engine,
    amount: str,
    out_day: int,
    in_day: int,
    *,
    out_text: str | None = OUT_TEXT,
    in_text: str | None = IN_TEXT,
    source: str = CHECKING,
    to: str = SAVINGS,
) -> tuple[str, str]:
    """``amount`` leaves ``source`` on ``out_day`` and lands on ``to``."""
    out_id = _bank_row(engine, source, f"-{amount}", _day(out_day), out_text)
    in_id = _bank_row(engine, to, amount, _day(in_day), in_text)
    return out_id, in_id


def _confirm(client: TestClient, out_id: str, in_id: str) -> dict[str, Any]:
    resp = client.post(
        "/matching/confirm-transfer", json={"outgoing_id": out_id, "incoming_id": in_id}
    )
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


def _links(client: TestClient, status: str = "open") -> list[dict[str, Any]]:
    resp = client.get("/suggestions", params={"kind": "transfer_link", "status": status})
    assert resp.status_code == 200, resp.text
    return list(resp.json())


def _decide(client: TestClient, sid: str, **body: Any) -> Any:
    return client.post(f"/suggestions/{sid}/decide", json=body)


def _sweep() -> dict[str, int]:
    """The nightly auto-link pass, committed."""
    from purser import transfers

    with purser.db.get_sessionmaker()() as session:
        stats = transfers.auto_link_pass(session)
        session.commit()
    return stats


def _merged_into(engine: Engine, tx_id: str) -> str | None:
    """The transfer a bank row was voided into, or None while it is live."""
    with engine.connect() as conn:
        status, superseded_by = conn.execute(
            text("SELECT status, superseded_by::text FROM transactions WHERE id = :id"),
            {"id": tx_id},
        ).one()
    return str(superseded_by) if status == "void" else None


def _projected(client: TestClient, rule_id: str) -> int:
    """The activation confirm's number, read off its refusal."""
    refused = client.post(f"/rules/{rule_id}/state", json={"state": "active"})
    assert refused.status_code == 422, refused.text
    return int(refused.json()["detail"].split("would link ")[1].split(" ")[0])


def _count(engine: Engine, sql: str, **params: Any) -> int:
    with engine.connect() as conn:
        return int(conn.execute(text(sql), params).scalar_one())


# --------------------------------------------------------------------------
# A confirm proposes; repeats refresh
# --------------------------------------------------------------------------


class TestConfirmProposes:
    def test_a_confirm_proposes_exactly_one_payee_scoped_rule(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        out_id, in_id = _pair(migrated_engine, "250.00", 0, 2)
        result = _confirm(client, out_id, in_id)

        (row,) = _links(client)
        assert result["suggestion_id"] == row["id"]
        assert (row["kind"], row["source"], row["status"]) == ("transfer_link", "confirm", "open")
        assert row["confidence"] is None
        draft = row["draft"]
        assert (draft["from_account"], draft["to_account"]) == (CHECKING, SAVINGS)
        # The outgoing row's payee is the scope; nothing narrows it by default.
        assert draft["payee"] == OUT_TEXT
        assert (draft["amount_exact"], draft["window_days"], draft["effective_from"]) == (
            None,
            None,
            None,
        )
        assert row["headline"] == (
            f"Link transfers between {CHECKING} and {SAVINGS} "
            f"when the payee is {OUT_TEXT} (or an alias)"
        )
        assert row["evidence_summary"]["count"] == 1
        assert row["evidence_summary"]["amount_range"] == {"low": "250.00", "high": "250.00"}
        evidence = client.get(f"/suggestions/{row['id']}").json()["evidence"]
        (pair,) = evidence["occurrences"]
        assert pair["transaction_id"] == result["merged_transaction_id"]
        assert (pair["outgoing_id"], pair["incoming_id"]) == (out_id, in_id)
        assert (pair["date"], pair["incoming_date"]) == (_day(0).isoformat(), _day(2).isoformat())

        # Proposed, audited, evented — and nothing else: no rule exists, so
        # nothing can link because of this confirm.
        assert client.get("/rules").json() == []
        assert (
            _count(migrated_engine, "SELECT COUNT(*) FROM events WHERE type = 'suggestion.created'")
            == 1
        )
        assert (
            _count(
                migrated_engine,
                "SELECT COUNT(*) FROM audit_log WHERE action = 'suggestion.create'"
                " AND entity_id = :id",
                id=row["id"],
            )
            == 1
        )
        with migrated_engine.connect() as conn:
            fingerprint = conn.execute(
                text("SELECT fingerprint FROM suggestions WHERE id = :id"), {"id": row["id"]}
            ).scalar_one()
            dedup = conn.execute(
                text("SELECT dedup_key FROM events WHERE type = 'suggestion.created'")
            ).scalar_one()
        assert dedup == fingerprint

    def test_repeat_confirms_keep_one_fingerprint(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        first = _confirm(client, *_pair(migrated_engine, "275.00", 30, 31))
        # An older pair confirmed later still reads as the first.
        second = _confirm(client, *_pair(migrated_engine, "250.00", 0, 2))
        # The other direction proposes the same (unordered) rule when its
        # outgoing row carries the same payee.
        third = _confirm(
            client, *_pair(migrated_engine, "90.00", 60, 60, source=SAVINGS, to=CHECKING)
        )
        assert first["suggestion_id"] == second["suggestion_id"] == third["suggestion_id"]
        (row,) = _links(client)
        assert row["seen_count"] == 3
        summary = row["evidence_summary"]
        assert summary["count"] == 3
        assert (summary["first"], summary["last"]) == (_day(0).isoformat(), _day(60).isoformat())
        assert summary["amount_range"] == {"low": "90.00", "high": "275.00"}
        assert row["draft"]["from_account"] == CHECKING  # as first seen
        assert _count(migrated_engine, "SELECT COUNT(*) FROM suggestions") == 1
        assert (
            _count(migrated_engine, "SELECT COUNT(*) FROM events WHERE type = 'suggestion.created'")
            == 1
        )
        assert (
            _count(
                migrated_engine,
                "SELECT COUNT(*) FROM audit_log WHERE action = 'suggestion.refresh'",
            )
            == 2
        )

        # Another payee on the same pair is another rule: a second suggestion.
        other = _confirm(
            client, *_pair(migrated_engine, "40.00", 70, 70, out_text=OTHER_OUT, in_text=OTHER_IN)
        )
        assert other["suggestion_id"] != first["suggestion_id"]
        assert {r["draft"]["payee"] for r in _links(client)} == {OUT_TEXT, OTHER_OUT}

    def test_the_incoming_payee_scopes_when_the_outgoing_row_has_none(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        _confirm(client, *_pair(migrated_engine, "60.00", 0, 0, out_text=None))
        (row,) = _links(client)
        assert row["draft"]["payee"] == IN_TEXT

    def test_nothing_to_scope_on_proposes_nothing(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        """A whole-pair rule is a broader claim than one confirm justifies."""
        result = _confirm(
            client, *_pair(migrated_engine, "60.00", 0, 0, out_text=None, in_text=None)
        )
        assert "suggestion_id" not in result
        assert _links(client, "all") == []

    def test_an_existing_rule_for_the_pair_suppresses_the_proposal(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        created = client.post(
            "/rules",
            json={
                "type": "transfer_link",
                "pair_account_a": seeded[SAVINGS],
                "pair_account_b": seeded[CHECKING],
            },
        )
        assert created.status_code == 201, created.text
        result = _confirm(client, *_pair(migrated_engine, "250.00", 0, 2))
        assert "suggestion_id" not in result
        assert _links(client, "all") == []

        # A disabled rule is still the user's call about the pair...
        rule_id = created.json()["id"]
        assert client.post(f"/rules/{rule_id}/state", json={"state": "disabled"}).status_code == 200
        assert "suggestion_id" not in _confirm(client, *_pair(migrated_engine, "251.00", 10, 10))
        # ...an archived (deleted) one is not.
        assert client.delete(f"/rules/{rule_id}").status_code == 200
        proposed = _confirm(client, *_pair(migrated_engine, "252.00", 20, 20))
        assert [r["id"] for r in _links(client)] == [proposed["suggestion_id"]]


# --------------------------------------------------------------------------
# Accept: a draft rule, then the activation gate
# --------------------------------------------------------------------------


class TestAccept:
    def test_accept_creates_a_draft_that_only_the_gate_activates(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        sid = _confirm(client, *_pair(migrated_engine, "250.00", 0, 2))["suggestion_id"]
        # History the rule could reach: one pair from the same payee, and one
        # on the same accounts from someone else. Neither carries a signal,
        # so the sweep leaves both alone while there is no rule.
        same_out, same_in = _pair(migrated_engine, "310.00", 30, 31)
        other_out, other_in = _pair(
            migrated_engine, "45.00", 40, 40, out_text=OTHER_OUT, in_text=OTHER_IN
        )
        assert _sweep()["transfers_auto_linked"] == 0

        accepted = _decide(client, sid, decision="accept")
        assert accepted.status_code == 200, accepted.text
        body = accepted.json()
        assert body["schedule"] is None
        rule = body["rule"]
        assert rule["state"] == "draft"
        assert rule["type"] == "transfer_link"
        assert rule["payee_scope"] == OUT_TEXT
        assert f'a leg\'s payee is "{OUT_TEXT}" or an alias' in rule["predicate"]
        assert rule["action"] == f"link {CHECKING} <-> {SAVINGS}"
        assert rule["window_days"] == 3
        # Inside the scope: the same payee's pair. Not the other payee's.
        assert body["projected_links"] == 1
        suggestion = body["suggestion"]
        assert suggestion["status"] == "accepted"
        assert (suggestion["result_entity_type"], suggestion["result_entity_id"]) == (
            "rule",
            rule["id"],
        )

        # A draft never runs.
        assert _sweep()["transfers_auto_linked"] == 0
        assert _merged_into(migrated_engine, same_out) is None

        # The existing activation path, with decision 46's confirm.
        assert _projected(client, rule["id"]) == body["projected_links"]
        active = client.post(
            f"/rules/{rule['id']}/state", json={"state": "active", "confirm_links": 1}
        )
        assert active.status_code == 200, active.text
        assert _sweep()["transfers_auto_linked"] == 1
        merged = _merged_into(migrated_engine, same_out)
        assert merged is not None and merged == _merged_into(migrated_engine, same_in)
        assert _merged_into(migrated_engine, other_out) is None
        assert _merged_into(migrated_engine, other_in) is None

        # Future pairs from the payee link as they post; the other payee's
        # pairs still need a confirm.
        next_out, next_in = _pair(migrated_engine, "260.00", 61, 62)
        stray_out, stray_in = _pair(
            migrated_engine, "70.00", 61, 61, out_text=OTHER_OUT, in_text=OTHER_IN
        )
        assert _sweep()["transfers_auto_linked"] == 1
        assert _merged_into(migrated_engine, next_out) is not None
        assert _merged_into(migrated_engine, next_out) == _merged_into(migrated_engine, next_in)
        assert _merged_into(migrated_engine, stray_out) is None
        assert _merged_into(migrated_engine, stray_in) is None

        # Accepted means decided: further confirms propose nothing new.
        again = _confirm(client, *_pair(migrated_engine, "15.00", 90, 90))
        assert "suggestion_id" not in again
        assert len(_links(client, "all")) == 1

    def test_the_rule_audit_carries_the_scope(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        sid = _confirm(client, *_pair(migrated_engine, "250.00", 0, 2))["suggestion_id"]
        rule_id = _decide(client, sid, decision="accept").json()["rule"]["id"]
        with migrated_engine.connect() as conn:
            payee_scope_id, created_by = conn.execute(
                text("SELECT payee_scope_id::text, created_by FROM rules WHERE id = :id"),
                {"id": rule_id},
            ).one()
            audit_enc = conn.execute(
                text("SELECT before_after_enc FROM audit_log WHERE action = 'rule.create'")
            ).scalar_one()
        assert created_by is not None
        after = crypto.decrypt_json(bytes(audit_enc))["after"]
        assert after["state"] == "draft"
        assert after["payee_scope_id"] == payee_scope_id

    def test_edit_then_accept_narrows_and_refuses_identity_edits(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        sid = _confirm(client, *_pair(migrated_engine, "250.00", 0, 2))["suggestion_id"]
        for edits, message in (
            ({"payee": "SOMEONE ELSE"}, "unknown draft fields: payee"),
            ({"to_account": CHECKING}, "unknown draft fields: to_account"),
            ({"window_days": 15}, "window_days"),
            ({"window_days": "5"}, "window_days"),
            ({"window_days": True}, "window_days"),
            ({"amount_exact": 250.5}, "decimal string"),
            ({"effective_from": "not-a-date"}, "effective_from"),
        ):
            refused = _decide(client, sid, decision="edit_then_accept", edits=edits)
            assert refused.status_code == 422, (edits, refused.text)
            assert message in refused.json()["detail"], (edits, refused.text)
        assert client.get("/rules").json() == []
        assert _links(client)[0]["status"] == "open"

        good = _decide(
            client,
            sid,
            decision="edit_then_accept",
            edits={"window_days": 5, "amount_exact": "250.00", "effective_from": "2026-05-01"},
        )
        assert good.status_code == 200, good.text
        rule = good.json()["rule"]
        assert rule["window_days"] == 5
        assert "amount = 250.00" in rule["predicate"]
        assert "on or after 2026-05-01" in rule["predicate"]
        assert rule["state"] == "draft"
        applied = good.json()["suggestion"]["draft"]
        assert (applied["window_days"], applied["amount_exact"]) == (5, "250.00")

    def test_write_scope_decides(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        sid = _confirm(client, *_pair(migrated_engine, "250.00", 0, 2))["suggestion_id"]
        agent = client.post("/tokens", json={"name": "agent", "scopes": ["read"]})
        headers = {"Authorization": f"Bearer {agent.json()['token']}"}
        denied = client.post(
            f"/suggestions/{sid}/decide", json={"decision": "accept"}, headers=headers
        )
        assert denied.status_code == 403
        assert client.get("/rules").json() == []


# --------------------------------------------------------------------------
# Reject and snooze
# --------------------------------------------------------------------------


class TestRejectAndSnooze:
    def test_reject_is_remembered(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        sid = _confirm(client, *_pair(migrated_engine, "250.00", 0, 2))["suggestion_id"]
        rejected = _decide(client, sid, decision="reject", note="not a standing transfer")
        assert rejected.status_code == 200, rejected.text
        assert rejected.json()["suggestion"]["status"] == "rejected"
        assert rejected.json()["rule"] is None

        # The same pair and payee, in either direction: never again.
        for source, to in ((CHECKING, SAVINGS), (SAVINGS, CHECKING)):
            again = _confirm(client, *_pair(migrated_engine, "80.00", 30, 30, source=source, to=to))
            assert "suggestion_id" not in again
        assert _links(client) == []
        (row,) = _links(client, "all")
        assert (row["id"], row["status"], row["seen_count"]) == (sid, "rejected", 1)
        assert (
            _count(migrated_engine, "SELECT COUNT(*) FROM events WHERE type = 'suggestion.created'")
            == 1
        )

        # exact scope rejects this payee only; counterparty scope the pair.
        other = _confirm(
            client, *_pair(migrated_engine, "40.00", 40, 40, out_text=OTHER_OUT, in_text=OTHER_IN)
        )["suggestion_id"]
        assert (
            _decide(client, other, decision="reject", reject_scope="counterparty").status_code
            == 200
        )
        third = _confirm(
            client,
            *_pair(
                migrated_engine, "41.00", 50, 50, out_text="QUILLON DEBIT", in_text="QUILLON IN"
            ),
        )
        assert "suggestion_id" not in third
        assert len(_links(client, "all")) == 2

    def test_snooze_works_as_for_schedules(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        sid = _confirm(client, *_pair(migrated_engine, "250.00", 0, 2))["suggestion_id"]
        snoozed = _decide(client, sid, decision="snooze")
        assert snoozed.status_code == 200, snoozed.text
        assert snoozed.json()["suggestion"]["status"] == "open"
        until = date.fromisoformat(snoozed.json()["suggestion"]["snooze_until"])
        assert until == date.today() + timedelta(days=service.DEFAULT_SNOOZE_DAYS)
        assert _links(client) == []
        listed = client.get(
            "/suggestions", params={"kind": "transfer_link", "include_snoozed": True}
        ).json()
        assert [r["id"] for r in listed] == [sid]
        # A repeat confirm refreshes the snoozed row; it stays snoozed, and
        # nothing ever auto-accepts.
        assert _confirm(client, *_pair(migrated_engine, "90.00", 30, 30))["suggestion_id"] == sid
        assert _links(client) == []
        assert client.get(f"/suggestions/{sid}").json()["seen_count"] == 2
        assert client.get("/rules").json() == []


# --------------------------------------------------------------------------
# The scope follows aliases; unlink leaves nothing behind
# --------------------------------------------------------------------------


class TestAliasesAndUnlink:
    def test_the_scope_follows_a_rename_alias(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        """#253's memory keyed on the exact payee_id, so a rename that merged
        payees silently disabled it. The scope resolves the alias group."""
        sid = _confirm(client, *_pair(migrated_engine, "250.00", 0, 2))["suggestion_id"]
        # A live row still carrying the raw payee, so a rename's
        # apply-history has something to alias.
        _bank_row(migrated_engine, CHECKING, "-12.34", _day(5), OUT_TEXT)
        rule = _decide(client, sid, decision="accept").json()["rule"]
        assert _projected(client, rule["id"]) == 0
        assert (
            client.post(
                f"/rules/{rule['id']}/state", json={"state": "active", "confirm_links": 0}
            ).status_code
            == 200
        )

        rename = client.post(
            "/rules",
            json={
                "type": "rename",
                "match_field": "payee",
                "match_mode": "prefix",
                "match_text": "zephyr bkg",
                "payee_name": "Zephyr Brokerage",
                "activate": True,
            },
        )
        assert rename.status_code == 201, rename.text
        aliased = client.post(
            f"/rules/{rename.json()['id']}/apply-history", json={"dry_run": False}
        )
        assert aliased.status_code == 200, aliased.text
        assert aliased.json()["payees_aliased"] == 1

        # Rows imported from now on carry the friendly payee. The pair rule
        # still covers them, and now reads under the payee's current name.
        out_id, in_id = _pair(migrated_engine, "300.00", 30, 31, out_text="Zephyr Brokerage")
        assert _sweep()["transfers_auto_linked"] == 1
        assert _merged_into(migrated_engine, out_id) is not None
        assert _merged_into(migrated_engine, out_id) == _merged_into(migrated_engine, in_id)
        (listed,) = [r for r in client.get("/rules").json() if r["type"] == "transfer_link"]
        assert listed["payee_scope"] == "Zephyr Brokerage"

    def test_alias_drift_keeps_one_suggestion_and_its_rejection(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        sid = _confirm(client, *_pair(migrated_engine, "250.00", 0, 2))["suggestion_id"]
        friendly = _bank_row(migrated_engine, CHECKING, "-1.00", _day(3), "Zephyr Brokerage")
        # What a rename's apply-history does: alias the raw payee onto the
        # friendly one. The suggestion's fingerprint was taken before it.
        with purser.db.get_sessionmaker()() as session:
            raw = session.execute(
                text("SELECT subject_payee_id FROM suggestions WHERE id = :id"), {"id": sid}
            ).scalar_one()
            target = session.execute(
                text("SELECT payee_id FROM transactions WHERE id = :id"), {"id": friendly}
            ).scalar_one()
            rules_service.alias_payee(session, source_id=raw, target_id=target, token=None)
            session.commit()

        # A confirm under the friendly payee is the same suggestion.
        repeat = _confirm(
            client, *_pair(migrated_engine, "75.00", 30, 30, out_text="Zephyr Brokerage")
        )
        assert repeat["suggestion_id"] == sid
        (row,) = _links(client)
        assert row["seen_count"] == 2
        assert row["draft"]["payee"] == "Zephyr Brokerage"

        # ...and rejecting it holds for every payee in the group.
        assert _decide(client, sid, decision="reject").status_code == 200
        for text_ in (OUT_TEXT, "Zephyr Brokerage"):
            again = _confirm(client, *_pair(migrated_engine, "76.00", 40, 40, out_text=text_))
            assert "suggestion_id" not in again
        assert len(_links(client, "all")) == 1

    def test_unlink_leaves_no_hidden_auto_link(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        """#253's reproduction: confirm, unlink, and the next pair from the
        same payee still auto-linked. Now a confirm leaves only a visible
        suggestion — nothing that links by itself."""
        out_id, in_id = _pair(migrated_engine, "250.00", 0, 2)
        confirmed = _confirm(client, out_id, in_id)
        unlinked = client.post(
            f"/transactions/{confirmed['merged_transaction_id']}/unlink-transfer"
        )
        assert unlinked.status_code == 200, unlinked.text

        next_out, next_in = _pair(migrated_engine, "250.00", 30, 31)
        assert _sweep() == {"transfers_absorbed": 0, "transfers_auto_linked": 0}
        assert _merged_into(migrated_engine, next_out) is None
        assert _merged_into(migrated_engine, next_in) is None
        assert client.get("/rules").json() == []
        # The proposal is still there to see and reject — never applied.
        (row,) = _links(client)
        assert row["id"] == confirmed["suggestion_id"]


# --------------------------------------------------------------------------
# Kind-aware surfaces: REST filters, retired schedule routes, MCP
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def live_server(migrated_engine: Engine) -> Iterator[LiveServer]:
    server = LiveServer()
    server.start()
    yield server
    server.stop()


class TestSurfaces:
    def test_rest_kind_filter_and_retired_schedule_routes(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        sid = _confirm(client, *_pair(migrated_engine, "250.00", 0, 2))["suggestion_id"]
        assert client.get("/suggestions", params={"kind": "schedule"}).json() == []
        assert [r["id"] for r in client.get("/suggestions").json()] == [sid]
        assert client.get("/suggestions", params={"kind": "bogus"}).status_code == 422
        # The schedule wrappers are retired (#347): nothing decides a link there.
        assert client.post(f"/schedules/{sid}/reject").status_code == 404
        assert client.post(f"/schedules/{sid}/activate").status_code == 404
        assert _links(client)[0]["status"] == "open"
        assert client.get("/rules").json() == []

    def test_mcp_confirm_list_and_decide(
        self,
        seeded_tokens: dict[str, str],  # noqa: F811
        live_server: LiveServer,
        client: TestClient,
        migrated_engine: Engine,
    ) -> None:
        resp = client.post(
            "/accounts", json={"name": "Reserve", "type": "savings", "path": SAVINGS}
        )
        assert resp.status_code == 201, resp.text
        out_id, in_id = _pair(migrated_engine, "250.00", 0, 2)
        same_out, _ = _pair(migrated_engine, "125.00", 30, 30)

        confirmed, listed, schedules_only, bogus = _mcp_call(
            live_server.url,
            seeded_tokens["operator"],
            [
                ("confirm_transfer", {"outgoing_id": out_id, "incoming_id": in_id}),
                ("list_suggestions", {"kind": "transfer_link"}),
                ("list_suggestions", {"kind": "schedule"}),
                ("list_suggestions", {"kind": "bogus"}),
            ],
        )
        sid = confirmed["suggestion_id"]
        assert listed["returned"] == 1
        (item,) = listed["items"]
        assert item["id"] == sid and item["kind"] == "transfer_link"
        assert item["draft"]["payee"] == OUT_TEXT
        assert schedules_only["returned"] == 0
        assert "kind must be one of" in bogus["error"]

        # A read-only token cannot decide; a write token can, and gets a
        # DRAFT rule plus the count its activation will ask for.
        (denied,) = _mcp_call(
            live_server.url,
            seeded_tokens["reader"],
            [("decide_suggestion", {"suggestion_id": sid, "decision": "accept"})],
        )
        assert "error" in denied
        decided, rules = _mcp_call(
            live_server.url,
            seeded_tokens["operator"],
            [
                ("decide_suggestion", {"suggestion_id": sid, "decision": "accept"}),
                ("list_rules", {"type": "transfer_link"}),
            ],
        )
        assert decided["status"] == "accepted" and decided["kind"] == "transfer_link"
        rule = decided["rule"]
        assert rule["state"] == "draft"
        assert rule["projected_links"] == 1
        assert rule["then"] == f"link {CHECKING} <-> {SAVINGS}"
        (listed_rule,) = rules["rules"]
        assert listed_rule["id"] == rule["id"]
        assert listed_rule["payee_scope"] == OUT_TEXT
        assert listed_rule["state"] == "draft"
        assert _merged_into(migrated_engine, same_out) is None  # a draft links nothing


# --------------------------------------------------------------------------
# Pure: the scope only ever narrows a declared pair
# --------------------------------------------------------------------------

PAYEES = [uuid.UUID(int=n) for n in range(1, 6)]
A, B = uuid.UUID(int=101), uuid.UUID(int=102)


class TestScopeProperty:
    @given(
        group=st.frozensets(st.sampled_from(PAYEES), min_size=1),
        legs=st.tuples(
            st.one_of(st.none(), st.sampled_from(PAYEES)),
            st.one_of(st.none(), st.sampled_from(PAYEES)),
        ),
        gap=st.integers(min_value=0, max_value=6),
        window=st.integers(min_value=0, max_value=14),
    )
    @settings(max_examples=300, deadline=None)
    def test_a_payee_scope_narrows_and_never_widens(
        self,
        group: frozenset[uuid.UUID],
        legs: tuple[uuid.UUID | None, uuid.UUID | None],
        gap: int,
        window: int,
    ) -> None:
        def declared(payees: frozenset[uuid.UUID] | None) -> list[rules_service.DeclaredPair]:
            return [
                rules_service.DeclaredPair(
                    rule_id=uuid.UUID(int=7),
                    accounts=frozenset({A, B}),
                    amount_exact=None,
                    effective_from=None,
                    window_days=window,
                    payees=payees,
                )
            ]

        def covered(payees: frozenset[uuid.UUID] | None) -> bool:
            return (
                rules_service.pair_declared(
                    declared(payees), A, B, Decimal("10.00"), DAY0, gap_days=gap, payee_ids=legs
                )
                is not None
            )

        whole_pair = covered(None)
        scoped = covered(group)
        assert not scoped or whole_pair  # a scope never adds a link
        assert scoped == (whole_pair and any(leg in group for leg in legs if leg is not None))
        # The same holds for the widened stage's window lookup.
        widest = rules_service.declared_window(
            declared(group), A, B, Decimal("10.00"), DAY0, payee_ids=legs
        )
        assert (widest is not None) == any(leg in group for leg in legs if leg is not None)


# --------------------------------------------------------------------------
# Migration 0030 up/down (scratch database, never the suite's)
# --------------------------------------------------------------------------


@pytest.fixture
def scratch_db(database_url: str) -> Iterator[tuple[Config, sa.Engine]]:
    os.environ["PURSER_ENCRYPTION_KEY"] = TEST_ENCRYPTION_KEY
    purser.config.get_settings.cache_clear()
    admin = sa.create_engine(database_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(sa.text("DROP DATABASE IF EXISTS purser_transfer_link_sugg_mig WITH (FORCE)"))
        conn.execute(sa.text("CREATE DATABASE purser_transfer_link_sugg_mig"))
    admin.dispose()
    scratch_url = database_url.rsplit("/", 1)[0] + "/purser_transfer_link_sugg_mig"
    root = Path(__file__).resolve().parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    cfg.set_main_option("sqlalchemy.url", scratch_url)
    engine = sa.create_engine(scratch_url)
    yield cfg, engine
    engine.dispose()


def _rule_columns(engine: sa.Engine) -> set[str]:
    with engine.connect() as conn:
        return set(
            conn.execute(
                sa.text(
                    "SELECT column_name FROM information_schema.columns WHERE table_name = 'rules'"
                )
            ).scalars()
        )


def _insert_link_suggestion(conn: sa.Connection, a: Any, b: Any, payee: Any) -> Any:
    return conn.execute(
        sa.text(
            "INSERT INTO suggestions (kind, fingerprint, source, subject_account_id,"
            " subject_payee_id, counterparty_account_id, draft_enc)"
            " VALUES ('transfer_link', :fp, 'confirm', :a, :p, :b, :d) RETURNING id"
        ),
        {"fp": uuid.uuid4().hex, "a": a, "b": b, "p": payee, "d": crypto.encrypt_json({})},
    ).scalar_one()


def test_migration_0030_round_trips(scratch_db: tuple[Config, sa.Engine]) -> None:
    from sqlalchemy.exc import IntegrityError

    cfg, engine = scratch_db
    command.upgrade(cfg, "0029")
    with engine.begin() as conn:
        a, b = (
            conn.execute(
                sa.text(
                    "INSERT INTO accounts (name, type, path) VALUES (:n, 'checking', :p)"
                    " RETURNING id"
                ),
                {"n": name, "p": path},
            ).scalar_one()
            for name, path in (("One", "assets.one"), ("Two", "assets.two"))
        )
        payee = conn.execute(
            sa.text("INSERT INTO payees (name_enc, name_bidx) VALUES (:n, 'x') RETURNING id"),
            {"n": crypto.encrypt("SYNTHETIC PAYEE")},
        ).scalar_one()
        existing = conn.execute(
            sa.text(
                "INSERT INTO rules (type, state, pair_account_a, pair_account_b)"
                " VALUES ('transfer_link', 'active', :a, :b) RETURNING id"
            ),
            {"a": a, "b": b},
        ).scalar_one()
    # Before 0030 the kind CHECK refuses the new kind.
    with pytest.raises(IntegrityError), engine.begin() as conn:
        _insert_link_suggestion(conn, a, b, payee)

    command.upgrade(cfg, "0030")
    assert "payee_scope_id" in _rule_columns(engine)
    with engine.begin() as conn:
        # No backfill: an existing rule keeps declaring the whole pair.
        assert (
            conn.execute(
                sa.text("SELECT payee_scope_id FROM rules WHERE id = :id"), {"id": existing}
            ).scalar_one()
            is None
        )
        scoped = conn.execute(
            sa.text(
                "INSERT INTO rules (type, state, pair_account_a, pair_account_b, payee_scope_id)"
                " VALUES ('transfer_link', 'active', :a, :b, :p) RETURNING id"
            ),
            {"a": a, "b": b, "p": payee},
        ).scalar_one()
        _insert_link_suggestion(conn, a, b, payee)
    # The scope is transfer_link only.
    with pytest.raises(IntegrityError), engine.begin() as conn:
        category = conn.execute(
            sa.text(
                "INSERT INTO accounts (name, type, is_virtual, path)"
                " VALUES ('Dining', 'category_expense', true, 'expenses.dining') RETURNING id"
            )
        ).scalar_one()
        conn.execute(
            sa.text(
                "INSERT INTO rules (type, category_id, payee_scope_id)"
                " VALUES ('categorize', :c, :p)"
            ),
            {"c": category, "p": payee},
        )

    command.downgrade(cfg, "0029")
    assert "payee_scope_id" not in _rule_columns(engine)
    with engine.connect() as conn:
        states = dict(conn.execute(sa.text("SELECT id, state FROM rules")).all())
        # Dropping the scope would widen the rule to its whole pair without
        # the activation confirm, so the scoped rule is disabled instead.
        assert states == {existing: "active", scoped: "disabled"}
        assert conn.execute(sa.text("SELECT COUNT(*) FROM suggestions")).scalar_one() == 0
    with pytest.raises(IntegrityError), engine.begin() as conn:
        _insert_link_suggestion(conn, a, b, payee)

    command.upgrade(cfg, "0030")
    assert "payee_scope_id" in _rule_columns(engine)
