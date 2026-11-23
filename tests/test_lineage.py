"""Correction chains and bank-line lineages (``purser.lineage``,
DESIGN §4/§5, #386 review).

A posted row is corrected by a reversal + replacement pair; the bank line
lives on in the chain head. A pending bank row the bank posted lives on in its
posted successor. The walks are driven here through the production paths that
build both: the categorize pass's recategorization, a register edit, and the
importer's reconcile step.

Synthetic data only.
"""

import uuid
from datetime import date, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import Engine, select

import purser.db
from purser import lineage
from purser.models import Transaction
from tests.conftest import today
from tests.test_matching import _imported
from tests.test_transfer_pair_suggestions import (  # noqa: F401 (fixture re-export)
    CHECKING,
    OUT_TEXT,
    _post,
    seeded,
)


def _ago(days: int) -> date:
    return today() - timedelta(days=days)


def _recategorize(tx_id: str) -> str:
    """The categorize pass on a posted row: reversal + origin=rule replacement."""
    from purser import ledger as ledger_service
    from purser.models import Account

    with purser.db.get_sessionmaker()() as session:
        category = session.execute(
            select(Account).where(Account.path == "expenses.utilities")
        ).scalar_one()
        replacement = ledger_service.recategorize_posted(
            session,
            tx=ledger_service.get_transaction(session, uuid.UUID(tx_id)),
            category_id=category.id,
            token=None,
            provenance={"source": "test"},
        )
        session.commit()
        return str(replacement.id)


def _edit(client: TestClient, tx_id: str, payee: str) -> str:
    """A register edit of a posted row: reversal + replacement (origin human)."""
    resp = client.patch(f"/transactions/{tx_id}", json={"payee": payee})
    assert resp.status_code == 200, resp.text
    return str(resp.json()["id"])


def test_a_correction_chain_and_a_posted_succession(
    seeded: dict[str, str],  # noqa: F811 (the fixture)
    client: TestClient,
    migrated_engine: Engine,
) -> None:
    pending = _imported(
        client, migrated_engine, CHECKING, "-75.00", _ago(4), payee=OUT_TEXT, status="pending"
    )
    posted = _post(client, migrated_engine, pending)
    first = _recategorize(posted)
    second = _edit(client, first, "EXAMPLE BANK WEB DEBIT 2")
    plain = _imported(client, migrated_engine, CHECKING, "-11.00", _ago(1), payee=OUT_TEXT)
    ids = [uuid.UUID(x) for x in (pending, posted, first, second, plain)]
    pending_id, posted_id, first_id, second_id, plain_id = ids

    with purser.db.get_sessionmaker()() as session:
        ancestry = lineage.correction_ancestry(session, [second_id, first_id, plain_id])
        correction_roots = lineage.correction_roots(session, [second_id, first_id, plain_id])
        roots = lineage.lineage_roots(session, [second_id, first_id, posted_id, plain_id])
        members = lineage.lineage_members(session, [pending_id, plain_id])

    # Backward through corrections, nearest first; a correction never moves
    # the chain's identity.
    assert ancestry == {second_id: [first_id, posted_id], first_id: [posted_id], plain_id: []}
    assert correction_roots == {second_id: posted_id, first_id: posted_id, plain_id: plain_id}
    # And on through the posting: the bank line was first seen pending.
    assert roots == {
        second_id: pending_id,
        first_id: pending_id,
        posted_id: pending_id,
        plain_id: plain_id,
    }
    # Forward from the roots: every row of each lineage, the reversals left out.
    assert members == {
        pending_id: pending_id,
        posted_id: pending_id,
        first_id: pending_id,
        second_id: pending_id,
        plain_id: plain_id,
    }


def test_chain_head_clause_selects_only_the_current_row(
    seeded: dict[str, str],  # noqa: F811 (the fixture)
    client: TestClient,
    migrated_engine: Engine,
) -> None:
    original = _imported(client, migrated_engine, CHECKING, "-20.00", _ago(2), payee=OUT_TEXT)
    replacement = _recategorize(original)

    with purser.db.get_sessionmaker()() as session:
        current = set(
            session.execute(
                select(Transaction.id).where(
                    lineage.chain_head_clause(), Transaction.date == _ago(2)
                )
            ).scalars()
        )

    # The original and its reversal are pointed at; the replacement is not.
    assert current == {uuid.UUID(replacement)}


def test_a_merge_ends_a_lineage(
    seeded: dict[str, str],  # noqa: F811 (the fixture)
    client: TestClient,
    migrated_engine: Engine,
) -> None:
    """#386 review round 3, item 10: the transfer a row was merged into is
    not a member of the row's lineage, and walking forward stops at the
    merged row — a merge is not a later sighting of the bank line."""
    out_id = _imported(client, migrated_engine, CHECKING, "-40.00", _ago(3), payee=OUT_TEXT)
    in_id = _imported(client, migrated_engine, "assets.savings", "40.00", _ago(2), payee=OUT_TEXT)
    confirmed = client.post(
        "/matching/confirm-transfer", json={"outgoing_id": out_id, "incoming_id": in_id}
    )
    assert confirmed.status_code == 200, confirmed.text
    merged = uuid.UUID(confirmed.json()["merged_transaction_id"])
    out = uuid.UUID(out_id)

    with purser.db.get_sessionmaker()() as session:
        assert lineage.lineage_members(session, [out]) == {out: out}
        assert lineage.ancestry(session, [merged]) == {merged: []}
        assert lineage.current_rows(session, [out]) == {out: out}


def test_an_open_pendings_successor_in_waiting(
    seeded: dict[str, str],  # noqa: F811 (the fixture)
    client: TestClient,
    migrated_engine: Engine,
) -> None:
    """#407: the posted row a gap-window import brought in for an open
    pending, before any reconcile step linked them — the reconcile step's
    rule (same account and amount, within the window, closest date first),
    never a row another bank line already lives on in, and one row per
    pending, the older pending choosing first."""

    def row(amount: str, days_ago: int, status: str = "posted") -> uuid.UUID:
        return uuid.UUID(
            _imported(
                client,
                migrated_engine,
                CHECKING,
                amount,
                _ago(days_ago),
                payee=OUT_TEXT,
                status=status,
            )
        )

    older = row("-40.00", 6, "pending")
    newer = row("-40.00", 5, "pending")
    lonely = row("-9.00", 5, "pending")
    near = row("-40.00", 5)
    far = row("-40.00", 2)
    row("-40.00", 20)  # outside the window of either pending
    claimed_pending = row("-40.00", 6, "pending")
    claimed = uuid.UUID(_post(client, migrated_engine, str(claimed_pending)))

    with purser.db.get_sessionmaker()() as session:
        found = lineage.open_pending_successors(session, [older, newer, lonely, near])

    # The older pending takes the closest row; the newer one the next free
    # row; the other pending's posted row is never offered.
    assert found == {older: near, newer: far}
    assert claimed not in found.values()
