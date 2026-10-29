"""New-transaction review flags (issue #175, DESIGN §4, decision 47).

The load-bearing claim is that a review mark is an annotation OUTSIDE the
immutability envelope — so the posted-guard regression test here is the point,
not a formality: it proves marking touched no ledger column.
"""

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from purser import review
from purser.models import ApiToken, TransactionReview


@pytest.fixture
def seeded(admin_client: TestClient) -> None:
    admin_client.post(
        "/accounts", json={"name": "Checking", "type": "checking", "path": "assets.checking"}
    )
    for name, path in [
        ("Uncategorized", "expenses.uncategorized"),
        ("Dining", "expenses.dining"),
    ]:
        admin_client.post(
            "/categories", json={"name": name, "type": "category_expense", "path": path}
        )


def _feed_row(
    session: Session,
    *,
    status: str = "posted",
    when: date | None = None,
    category: str = "expenses.uncategorized",
) -> uuid.UUID:
    """A row that looks like it came from the feed — origin='import' WITH a
    source_fetch_id, which is what puts it in review scope (decision 45's
    feed-row definition)."""
    from purser import crypto
    from purser.models import Connection, RawFetch

    conn = Connection(provider="simplefin")
    session.add(conn)
    session.flush()
    fetch = RawFetch(
        connection_id=conn.id, content_hash=uuid.uuid4().hex, payload_enc=crypto.encrypt("{}")
    )
    session.add(fetch)
    session.flush()

    from purser import ledger
    from purser.core import ledger as core

    tx = ledger.create_flat_transaction(
        session,
        account_ref="assets.checking",
        category_ref=category,
        amount=Decimal("-25.00"),
        date=when or date.today(),
        payee_name="Feed Row",
        description="FEED ROW",
        requested_status=core.Status.POSTED if status == "posted" else core.Status.PENDING,
        origin=core.Origin.IMPORT,
        token=None,
        source_fetch_id=fetch.id,
        content_hash=uuid.uuid4().hex,
    )
    session.flush()
    return tx.id


class TestScope:
    def test_a_feed_row_is_unreviewed_and_a_manual_row_is_not(
        self, seeded: None, client: TestClient, db_session: Session
    ) -> None:
        """QIF/CSV rows are ALSO origin='import'; the source_fetch_id clause is
        what stops a 6,081-row Quicken import becoming 6,081 review items."""
        feed_id = _feed_row(db_session)
        db_session.commit()
        assert review.unreviewed_count(db_session) == 1

        # A manual (agent/human) row is out of scope entirely.
        client.post(
            "/transactions",
            json={
                "account": "assets.checking",
                "category": "expenses.dining",
                "amount": "-10.00",
                "date": date.today().isoformat(),
                "status": "posted",
            },
        )
        db_session.expire_all()
        assert review.unreviewed_count(db_session) == 1
        assert feed_id is not None

    def test_a_real_qif_import_stays_out_of_review_scope(
        self, seeded: None, client: TestClient, db_session: Session
    ) -> None:
        """#127 archives QIF uploads to raw_fetches — but must NOT stamp
        source_fetch_id on the ledger rows, or every historical manual import
        floods the review set. Exercises the real import path end to end."""
        qif = (
            "!Account\nNChecking\nTBank\n^\n!Type:Bank\n"
            "D1/15/2024\nT-42.00\nPCorner Store\nLDining\n^\n"
        )
        resp = client.post(
            "/import/qif",
            json={"content": qif, "account_map": {"Checking": "assets.checking"}},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["imported"] == 1
        db_session.expire_all()
        assert review.unreviewed_count(db_session) == 0

    def test_marking_is_idempotent_and_monotonic(self, seeded: None, db_session: Session) -> None:
        tx_id = _feed_row(db_session)
        db_session.commit()
        assert review.mark(db_session, tx_id=tx_id, token=None, via="explicit") is True
        assert review.mark(db_session, tx_id=tx_id, token=None, via="explicit") is False
        assert review.unreviewed_count(db_session) == 0


class TestImmutabilityEnvelope:
    def test_marking_touches_no_ledger_column_on_a_posted_row(
        self, seeded: None, db_session: Session, migrated_engine: Engine
    ) -> None:
        """The reason marks live in a side table: `transactions_posted_guard`
        allow-lists only status/superseded_by, so a `reviewed_at` COLUMN could
        never be set on the very rows this feature exists for. Prove the guard
        still bites, and that marking sidesteps it."""
        tx_id = _feed_row(db_session)
        db_session.commit()
        review.mark(db_session, tx_id=tx_id, token=None, via="explicit")
        db_session.commit()

        with migrated_engine.connect() as conn:
            with pytest.raises(Exception) as excinfo:
                conn.execute(
                    text("UPDATE transactions SET origin = 'human' WHERE id = :id"),
                    {"id": tx_id},
                )
                conn.commit()
            assert "immutable" in str(excinfo.value).lower()


class TestImplicitMarks:
    def test_posting_a_row_marks_it_reviewed(
        self, seeded: None, client: TestClient, db_session: Session
    ) -> None:
        tx_id = _feed_row(db_session, status="pending")
        db_session.commit()
        assert review.unreviewed_count(db_session) == 1

        assert client.post(f"/transactions/{tx_id}/post").status_code == 200
        db_session.expire_all()
        assert review.unreviewed_count(db_session) == 0

    def test_automation_never_marks_a_row_reviewed(
        self, seeded: None, client: TestClient, db_session: Session
    ) -> None:
        """The rule the whole feature rests on: automation that merely TOUCHES
        a row leaves it needing review. Only a reviewer clears it."""
        _feed_row(db_session)
        db_session.commit()
        before = review.unreviewed_count(db_session)
        client.post("/categorize/run")  # a system-actor automation pass
        db_session.expire_all()
        assert review.unreviewed_count(db_session) == before


class TestBatch:
    def test_the_batch_declares_which_context_was_available(
        self, seeded: None, db_session: Session
    ) -> None:
        """A batch that silently ships memories:[] because that feature is
        absent is a row reviewed BLIND, clearing indistinguishably from one
        reviewed properly. The context map is what turns a silent degradation
        into a declared one."""
        _feed_row(db_session)
        db_session.commit()
        payload = review.batch(db_session, limit=5)
        assert set(payload["context"]) == {
            "rules",
            "memories",
            "provenance",
            "transfer_candidates",
        }
        assert payload["context"]["rules"] is True  # migration 0020 shipped
        assert payload["unreviewed_total"] == 1
        assert payload["guidance"]

    def test_signals_ride_inline_so_there_are_no_per_row_round_trips(
        self, seeded: None, db_session: Session
    ) -> None:
        _feed_row(db_session)
        db_session.commit()
        row = review.batch(db_session, limit=5)["rows"][0]
        for key in ("category", "flags", "automation", "memories", "amount", "account"):
            assert key in row
        assert "uncategorized" in row["flags"]

    def test_guidance_can_be_omitted_on_later_pages(
        self, seeded: None, db_session: Session
    ) -> None:
        _feed_row(db_session)
        db_session.commit()
        assert "guidance" not in review.batch(db_session, limit=5, include_guidance=False)


class TestMarkReviewedApi:
    def test_bulk_and_idempotent(
        self, seeded: None, client: TestClient, db_session: Session
    ) -> None:
        tx_id = _feed_row(db_session)
        db_session.commit()
        first = client.post("/transactions/mark-reviewed", json={"transaction_ids": [str(tx_id)]})
        assert first.status_code == 200, first.text
        assert first.json() == {"marked": 1, "already_reviewed": 0}
        second = client.post("/transactions/mark-reviewed", json={"transaction_ids": [str(tx_id)]})
        assert second.json() == {"marked": 0, "already_reviewed": 1}

    def test_escalation_clears_the_row_but_flags_it(
        self, seeded: None, client: TestClient, db_session: Session
    ) -> None:
        """Escalation is an escape hatch, not a second review tier: the row
        LEAVES the unreviewed set, so no queue is recreated."""
        tx_id = _feed_row(db_session)
        db_session.commit()
        client.post(
            "/transactions/mark-reviewed",
            json={"transaction_ids": [str(tx_id)], "escalate": True, "note": "ambiguous pair"},
        )
        db_session.expire_all()
        assert review.unreviewed_count(db_session) == 0
        payload = review.batch(db_session, limit=5)
        assert [r["id"] for r in payload["escalated"]] == [str(tx_id)]

    def test_requires_write_scope(self, seeded: None, client: TestClient) -> None:
        token = client.post("/tokens", json={"name": "p", "scopes": ["read"]}).json()["token"]
        resp = client.post(
            "/transactions/mark-reviewed",
            headers={"Authorization": f"Bearer {token}"},
            json={"transaction_ids": [str(uuid.uuid4())]},
        )
        assert resp.status_code == 403


class TestRestSurface:
    """These exercise the ROUTES, not the service.

    The gap that let a real bug through: every other test here called
    `review.batch()` directly, so nothing noticed that
    GET /transactions/review-batch was shadowed by GET /transactions/{tx_id}
    and 422'd trying to parse "review-batch" as a UUID. FastAPI matches in
    registration order, so a literal path declared after a parameterized one
    is unreachable.
    """

    def test_review_batch_route_is_not_shadowed_by_the_tx_id_route(
        self, seeded: None, client: TestClient, db_session: Session
    ) -> None:
        _feed_row(db_session)
        db_session.commit()
        resp = client.get("/transactions/review-batch", params={"limit": 5})
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        assert payload["unreviewed_total"] == 1
        assert "context" in payload
        assert payload["guidance"]

    def test_mark_reviewed_route_is_reachable(self, seeded: None, client: TestClient) -> None:
        resp = client.post("/transactions/mark-reviewed", json={"transaction_ids": []})
        assert resp.status_code == 200, resp.text

    def test_a_real_transaction_id_still_resolves(
        self, seeded: None, client: TestClient, db_session: Session
    ) -> None:
        """The reordering must not break the parameterized route itself."""
        tx_id = _feed_row(db_session)
        db_session.commit()
        assert client.get(f"/transactions/{tx_id}").status_code == 200


class TestRegisterFilter:
    def test_reviewed_false_is_the_unreviewed_set(
        self, seeded: None, client: TestClient, db_session: Session
    ) -> None:
        tx_id = _feed_row(db_session)
        db_session.commit()
        page = client.get("/transactions", params={"reviewed": False}).json()
        assert [t["id"] for t in page["items"]] == [str(tx_id)]
        assert page["items"][0]["reviewed_at"] is None

        client.post("/transactions/mark-reviewed", json={"transaction_ids": [str(tx_id)]})
        after = client.get("/transactions", params={"reviewed": False}).json()
        assert after["items"] == []
        marked = client.get("/transactions", params={"reviewed": True}).json()
        assert any(t["id"] == str(tx_id) for t in marked["items"])


ALL_STATUSES = ["expected", "pending", "posted", "void"]


def _all_rows(client: TestClient, **params: object) -> list[dict[str, object]]:
    """Every row the API will show — all statuses, correction chains
    uncollapsed — paged through so nothing hides past `limit`."""
    items: list[dict[str, object]] = []
    offset = 0
    while True:
        page = client.get(
            "/transactions",
            params={
                "status": ALL_STATUSES,
                "include_collapsed": True,
                "limit": 200,
                "offset": offset,
                **params,
            },
        ).json()
        items += page["items"]
        offset += 200
        if offset >= page["total"]:
            return items


def _ids(client: TestClient) -> set[str]:
    return {str(t["id"]) for t in _all_rows(client)}


def _assert_one_predicate(client: TestClient, session: Session) -> set[str]:
    """The per-row flag, ?reviewed=false (with ?reviewed=true its exact
    complement), the count and the batch all describe ONE set (#248).
    Returns that set."""
    rows = _all_rows(client)
    flagged = {str(t["id"]) for t in rows if t["needs_review"] is True}
    unreviewed = {str(t["id"]) for t in _all_rows(client, reviewed=False)}
    complement = {str(t["id"]) for t in _all_rows(client, reviewed=True)}
    assert flagged == unreviewed
    assert unreviewed.isdisjoint(complement)
    assert unreviewed | complement == {str(t["id"]) for t in rows}
    session.expire_all()
    assert review.unreviewed_count(session) == len(flagged)
    batch = client.get(
        "/transactions/review-batch", params={"limit": 25, "include_guidance": False}
    ).json()
    assert batch["unreviewed_total"] == len(flagged)
    assert {r["id"] for r in batch["rows"]} <= flagged
    return flagged


def _schedule_instance(client: TestClient) -> str:
    """An `expected` row a schedule materialized (origin='rule'). Weekly, so
    the 60-day horizon holds instances whatever day the suite runs."""
    resp = client.post(
        "/schedules",
        json={
            "name": "Synthetic Gym",
            "account": "assets.checking",
            "rrule": "FREQ=WEEKLY",
            "amount_type": "fixed",
            "amount": "-30.00",
        },
    )
    assert resp.status_code == 201, resp.text
    instances = [
        t
        for t in _all_rows(client)
        if t["schedule_id"] == resp.json()["id"] and t["status"] == "expected"
    ]
    assert instances
    assert all(t["origin"] == "rule" for t in instances)
    return str(instances[0]["id"])


def _import(client: TestClient, path: str, body: dict[str, object]) -> str:
    """Run a real file import and return the one row it created."""
    before = _ids(client)
    resp = client.post(path, json=body)
    assert resp.status_code == 200, resp.text
    assert resp.json()["imported"] == 1
    (row_id,) = _ids(client) - before
    return row_id


# Row shapes by (origin, status, from_feed). Origins and statuses the
# predicate distinguishes, built directly so a property run stays fast.
_SHAPES = st.sampled_from(
    [
        ("import", "posted", True),  # feed row
        ("import", "pending", True),  # feed pending
        ("import", "posted", False),  # QIF/CSV-shaped: no source_fetch_id
        ("system", "posted", False),  # automation (merge, reversal)
        ("rule", "posted", False),  # rule replacement
        ("rule", "expected", False),  # schedule-instance shaped
        ("human", "posted", False),
        ("agent", "pending", False),
    ]
)


def _shaped_row(session: Session, *, origin: str, status: str, from_feed: bool) -> uuid.UUID:
    if from_feed:
        return _feed_row(session, status=status)
    from purser import ledger
    from purser.core import ledger as core

    tx = ledger.create_flat_transaction(
        session,
        account_ref="assets.checking",
        category_ref="expenses.uncategorized",
        amount=Decimal("-5.00"),
        date=date.today(),
        payee_name="Synthetic Payee",
        requested_status=core.Status(status),
        origin=core.Origin(origin),
        token=None,
    )
    session.flush()
    return tx.id


def _correct_by_automation(session: Session, tx_id: uuid.UUID) -> uuid.UUID:
    """A system-actor recategorization: reversal + replacement (origin rule),
    the replacement inheriting the original's review state."""
    from purser import ledger
    from purser.models import Transaction

    tx = session.get(Transaction, tx_id)
    assert tx is not None
    replacement = ledger.recategorize_posted(
        session,
        tx=tx,
        category_id=ledger.resolve_account(session, "expenses.dining").id,
        token=None,
        provenance={"source": "rule"},
    )
    session.flush()
    return replacement.id


class TestNeedsReviewFlag:
    """The register's "needs review" badge renders `needs_review`, which the
    server computes with review.py's one predicate (#248).

    The issue blamed the SCOPE for flagging schedule instances, but the scope
    never held them: it covers live (pending/posted) rows only. The badge was
    computed in the browser as "no review mark", and most rows never need
    one — so it also flagged manual, agent and QIF/CSV rows.
    """

    def test_each_kind_of_row_reports_its_own_membership(
        self, seeded: None, client: TestClient, db_session: Session
    ) -> None:
        instance_id = _schedule_instance(client)
        manual = client.post(
            "/transactions",
            json={
                "account": "assets.checking",
                "category": "expenses.dining",
                "amount": "-10.00",
                "date": date.today().isoformat(),
                "status": "posted",
            },
        )
        assert manual.status_code == 201, manual.text
        agent_token = client.post(
            "/tokens", json={"name": "agent", "scopes": ["read", "write"]}
        ).json()["token"]
        agent = client.post(
            "/transactions",
            headers={"Authorization": f"Bearer {agent_token}"},
            json={
                "account": "assets.checking",
                "category": "expenses.dining",
                "amount": "-11.00",
                "date": date.today().isoformat(),
            },
        )
        assert agent.status_code == 201, agent.text
        assert agent.json()["origin"] == "agent"
        qif_id = _import(
            client,
            "/import/qif",
            {
                "content": (
                    "!Account\nNChecking\nTBank\n^\n!Type:Bank\n"
                    "D1/15/2024\nT-42.00\nPSynthetic Store\nLDining\n^\n"
                ),
                "account_map": {"Checking": "assets.checking"},
            },
        )
        csv_id = _import(
            client,
            "/import/csv",
            {
                "content": "Date,Payee,Amount\n2024-07-02,Synthetic Cafe,-12.00\n",
                "account_map": {"__default__": "assets.checking"},
            },
        )
        unreviewed_id = _feed_row(db_session)
        reviewed_id = _feed_row(db_session)
        db_session.commit()
        resp = client.post(
            "/transactions/mark-reviewed", json={"transaction_ids": [str(reviewed_id)]}
        )
        assert resp.status_code == 200, resp.text

        want = {
            instance_id: False,
            str(manual.json()["id"]): False,
            str(agent.json()["id"]): False,
            qif_id: False,
            csv_id: False,
            str(unreviewed_id): True,
            str(reviewed_id): False,
        }
        rows = {str(t["id"]): t for t in _all_rows(client)}
        assert {i: rows[i]["needs_review"] for i in want} == want
        # None of the rows the browser used to flag carries a mark — which is
        # exactly why "no mark" cannot be the badge's input.
        for i, needs in want.items():
            if i != str(reviewed_id):
                assert rows[i]["reviewed_at"] is None
            assert client.get(f"/transactions/{i}").json()["needs_review"] is needs
        # The detail view carries the mark, too (it used to report null).
        assert client.get(f"/transactions/{reviewed_id}").json()["reviewed_at"] is not None
        assert _assert_one_predicate(client, db_session) == {str(unreviewed_id)}

    def test_a_correction_chain_is_asked_about_once_at_its_head(
        self, seeded: None, client: TestClient, db_session: Session
    ) -> None:
        """An automation correction leaves the original AND its reversal live
        and in scope (posted, unsuperseded, origins import/system). Only the
        chain-head test keeps them out of the set — and ?reviewed=false with
        include_collapsed used to skip that test, listing three rows where the
        count saw one."""
        original = _feed_row(db_session)
        head = _correct_by_automation(db_session, original)
        db_session.commit()
        instance_id = _schedule_instance(client)
        skipped = client.post(f"/transactions/{instance_id}/void")  # a skip
        assert skipped.status_code == 200, skipped.text
        assert skipped.json()["needs_review"] is False

        posted = {str(t["id"]) for t in _all_rows(client) if t["status"] == "posted"}
        assert len(posted) == 3 and {str(original), str(head)} <= posted  # + the reversal
        assert _assert_one_predicate(client, db_session) == {str(head)}

    @settings(
        max_examples=10, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
    )
    @given(plan=st.lists(st.tuples(_SHAPES, st.booleans(), st.booleans()), min_size=1, max_size=6))
    def test_flag_and_filter_are_one_set_for_any_ledger(
        self,
        seeded: None,
        client: TestClient,
        db_session: Session,
        plan: list[tuple[tuple[str, str, bool], bool, bool]],
    ) -> None:
        """Whatever mix of origins, statuses, marks and corrections the ledger
        holds, the per-row flag and GET ?reviewed=false never disagree (rows
        accumulate across examples, which only widens the mix)."""
        for (origin, status, from_feed), marked, corrected in plan:
            tx_id = _shaped_row(db_session, origin=origin, status=status, from_feed=from_feed)
            if marked:
                review.mark(db_session, tx_id=tx_id, token=None, via=review.VIA_EXPLICIT)
            if corrected and status == "posted":
                _correct_by_automation(db_session, tx_id)
        db_session.commit()
        _assert_one_predicate(client, db_session)


class TestAgedSweep:
    def test_stale_rows_are_marked_aged_and_distinguishable(
        self, seeded: None, db_session: Session
    ) -> None:
        """`via='aged'` is deliberately NOT 'explicit': an audit reader must
        always be able to tell which rows were actually looked at."""
        from purser.models import TransactionReview

        # Dated old at CREATION: a posted row's date is immutable (migration
        # 0005), which is the same guard this feature had to route around.
        tx_id = _feed_row(db_session, when=datetime.now(UTC).date() - timedelta(days=200))
        db_session.commit()

        assert review.aged_sweep(db_session, max_age_days=90) == 1
        db_session.commit()
        mark_row = db_session.get(TransactionReview, tx_id)
        assert mark_row is not None
        assert mark_row.via == "aged"

    def test_fresh_rows_are_untouched(self, seeded: None, db_session: Session) -> None:
        _feed_row(db_session)
        db_session.commit()
        assert review.aged_sweep(db_session, max_age_days=90) == 0


class TestBackendReviewer:
    def test_is_a_no_op_without_a_provider(self, seeded: None, db_session: Session) -> None:
        _feed_row(db_session)
        db_session.commit()
        assert review.ai_review_pass(db_session) == {
            "reviewed": 0,
            "escalated": 0,
            "skipped": 0,
        }
        assert review.unreviewed_count(db_session) == 1

    def test_is_a_no_op_with_a_provider_but_the_switch_off(
        self, seeded: None, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Configuring a provider must grant nothing by itself (DESIGN §11)."""
        import purser.config

        monkeypatch.setenv("PURSER_AI_PROVIDER", "openai")
        monkeypatch.setenv("PURSER_AI_MODEL", "test-model")
        purser.config.get_settings.cache_clear()
        try:
            _feed_row(db_session)
            db_session.commit()
            assert review.ai_review_pass(db_session)["reviewed"] == 0
            assert review.unreviewed_count(db_session) == 1
        finally:
            purser.config.get_settings.cache_clear()


# --------------------------------------------------------------------------
# A mark follows its bank line (decision 82, #342)
# --------------------------------------------------------------------------


def _reviewer(session: Session) -> ApiToken:
    from purser.models import User

    user = User(email=f"reviewer_{uuid.uuid4().hex[:8]}@example.com")
    session.add(user)
    session.flush()
    token = ApiToken(
        user_id=user.id, name="reviewer", token_hash=uuid.uuid4().hex, scopes=["read", "write"]
    )
    session.add(token)
    session.flush()
    return token


def _supersede(
    session: Session, pending_id: uuid.UUID, successor_id: uuid.UUID, **kw: bool
) -> None:
    from purser.feed import reconcile
    from purser.models import Transaction

    pending = session.get(Transaction, pending_id)
    successor = session.get(Transaction, successor_id)
    assert pending is not None and successor is not None
    reconcile.void_pending(session, pending, successor=successor, **kw)


def _review_row(session: Session, tx_id: uuid.UUID) -> TransactionReview | None:
    session.expire_all()
    return session.get(TransactionReview, tx_id)


def _reviewed_audits(session: Session, tx_id: uuid.UUID) -> list[dict[str, object]]:
    from sqlalchemy import select

    from purser.models import AuditLog

    rows = session.execute(
        select(AuditLog).where(
            AuditLog.action == "transaction.reviewed", AuditLog.entity_id == tx_id
        )
    ).scalars()
    return [
        {"actor": row.actor_token_id, **(row.before_after or {}).get("after", {})} for row in rows
    ]


def _automation_correction(session: Session, tx_id: uuid.UUID, category: str) -> uuid.UUID:
    from purser import ledger
    from purser.models import Transaction

    tx = session.get(Transaction, tx_id)
    assert tx is not None
    replacement = ledger.recategorize_posted(
        session,
        tx=tx,
        category_id=ledger.resolve_account(session, category).id,
        token=None,
        provenance={"source": "rule"},
    )
    session.flush()
    return replacement.id


@pytest.fixture
def carry_seeded(seeded: None, client: TestClient) -> None:
    resp = client.post(
        "/categories",
        json={"name": "Groceries", "type": "category_expense", "path": "expenses.groceries"},
    )
    assert resp.status_code == 201, resp.text


class TestReviewFollowsThePending:
    """The reconciler's ``void_pending`` is where both of #342's paths — the
    successor pairing and the identity move — supersede a pending by its
    posted row, so this is the seam the carry is pinned at."""

    def test_the_whole_mark_goes_to_the_successor(
        self, carry_seeded: None, db_session: Session
    ) -> None:
        token = _reviewer(db_session)
        pending = _feed_row(db_session, status="pending")
        successor = _feed_row(db_session)
        review.mark(
            db_session, tx_id=pending, token=token, via="explicit", note="checked", escalate=True
        )
        source = _review_row(db_session, pending)
        assert source is not None

        _supersede(db_session, pending, successor, identity_moved=True)

        copy = _review_row(db_session, successor)
        assert copy is not None
        assert (copy.reviewed_by, copy.via, copy.note) == (token.id, "explicit", "checked")
        assert (copy.reviewed_at, copy.escalated_at) == (source.reviewed_at, source.escalated_at)
        assert copy.carried_from == pending
        (audit,) = _reviewed_audits(db_session, successor)
        assert audit["actor"] is None
        assert (audit["reason"], audit["carried_from"], audit["escalated"]) == (
            "pending_posted",
            str(pending),
            True,
        )
        assert review.needs_review_ids(db_session, [successor]) == set()

    def test_an_unreviewed_pending_leaves_its_successor_unreviewed(
        self, carry_seeded: None, db_session: Session
    ) -> None:
        pending = _feed_row(db_session, status="pending")
        successor = _feed_row(db_session)

        _supersede(db_session, pending, successor)

        assert _review_row(db_session, successor) is None
        assert review.needs_review_ids(db_session, [successor]) == {successor}

    def test_a_successor_with_its_own_mark_keeps_it(
        self, carry_seeded: None, db_session: Session
    ) -> None:
        token = _reviewer(db_session)
        pending = _feed_row(db_session, status="pending")
        successor = _feed_row(db_session)
        review.mark(db_session, tx_id=pending, token=token, via="explicit", escalate=True)
        review.mark(db_session, tx_id=successor, token=None, via="aged")

        _supersede(db_session, pending, successor)

        kept = _review_row(db_session, successor)
        assert kept is not None
        assert (kept.via, kept.reviewed_by, kept.escalated_at, kept.carried_from) == (
            "aged",
            None,
            None,
            None,
        )
        assert [a["via"] for a in _reviewed_audits(db_session, successor)] == ["aged"]

    def test_a_category_the_person_never_saw_comes_back_for_review(
        self, carry_seeded: None, db_session: Session
    ) -> None:
        """A person recategorized the pending (which reviews it); the bank's
        posted row arrives uncategorized — payee memory reads posted rows
        only — so it says something nobody reviewed."""
        from purser import ledger

        token = _reviewer(db_session)
        pending = _feed_row(db_session, status="pending")
        successor = _feed_row(db_session)
        ledger.update_transaction(
            db_session, tx_id=pending, token=token, category_ref="expenses.dining"
        )
        mark = _review_row(db_session, pending)
        assert mark is not None and mark.via == "update"

        _supersede(db_session, pending, successor)

        assert _review_row(db_session, successor) is None
        assert review.needs_review_ids(db_session, [successor]) == {successor}

    def test_a_successor_corrected_first_takes_the_mark_on_its_replacement(
        self, carry_seeded: None, db_session: Session
    ) -> None:
        token = _reviewer(db_session)
        pending = _feed_row(db_session, status="pending", category="expenses.dining")
        successor = _feed_row(db_session)
        review.mark(db_session, tx_id=pending, token=token, via="explicit")
        replacement = _automation_correction(db_session, successor, "expenses.dining")

        _supersede(db_session, pending, successor)

        assert _review_row(db_session, successor) is None  # reversed: never asked about
        copy = _review_row(db_session, replacement)
        assert copy is not None
        assert (copy.reviewed_by, copy.carried_from) == (token.id, pending)
        assert review.needs_review_ids(db_session, [successor, replacement]) == set()

    def test_a_carried_mark_survives_the_post_sync_correction(
        self, carry_seeded: None, db_session: Session
    ) -> None:
        """Post-sync categorization corrects the posted row the same night:
        the replacement inherits the carried mark whole, not a NULL actor
        stamped now()."""
        token = _reviewer(db_session)
        pending = _feed_row(db_session, status="pending")
        successor = _feed_row(db_session)
        review.mark(db_session, tx_id=pending, token=token, via="match")
        _supersede(db_session, pending, successor)
        carried = _review_row(db_session, successor)
        assert carried is not None

        replacement = _automation_correction(db_session, successor, "expenses.dining")

        copy = _review_row(db_session, replacement)
        assert copy is not None
        assert (copy.reviewed_by, copy.reviewed_at, copy.via) == (
            token.id,
            carried.reviewed_at,
            "match",
        )
        assert copy.carried_from == successor
        (audit,) = _reviewed_audits(db_session, replacement)
        assert (audit["reason"], audit["actor"]) == ("correction", None)

    def test_unlink_copies_the_whole_mark_escalation_included(
        self, carry_seeded: None, db_session: Session
    ) -> None:
        token = _reviewer(db_session)
        void_row = _feed_row(db_session)
        clone = _feed_row(db_session)
        review.mark(db_session, tx_id=void_row, token=token, via="explicit", escalate=True)

        review.copy_to_clone(db_session, void_id=void_row, clone_id=clone)

        copy = _review_row(db_session, clone)
        assert copy is not None
        assert copy.reviewed_by == token.id
        assert copy.escalated_at is not None
        assert copy.carried_from == void_row
        (audit,) = _reviewed_audits(db_session, clone)
        assert audit["reason"] == "unlink"


class TestReviewCarryProperty:
    """Guardrail 3 for decision 82: whatever the pending's mark, the path,
    the categories and what already happened to the successor, its live head
    is reviewed exactly when it was pre-marked, or the pending was marked
    and the categories are equal — and a carried mark equals its source."""

    @settings(
        max_examples=30, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
    )
    @given(
        marked=st.booleans(),
        via=st.sampled_from(["explicit", "match", "update", "post", "ai_review"]),
        escalated=st.booleans(),
        identity_moved=st.booleans(),
        same_category=st.booleans(),
        premarked=st.booleans(),
        corrected_first=st.booleans(),
    )
    def test_the_head_is_reviewed_exactly_when_it_should_be(
        self,
        carry_seeded: None,
        marked: bool,
        via: str,
        escalated: bool,
        identity_moved: bool,
        same_category: bool,
        premarked: bool,
        corrected_first: bool,
    ) -> None:
        import purser.db
        from purser import schedules

        with purser.db.get_sessionmaker()() as session:
            token = _reviewer(session)
            pending = _feed_row(session, status="pending", category="expenses.dining")
            landed_in = (
                "expenses.uncategorized"
                if corrected_first or not same_category
                else ("expenses.dining")
            )
            successor = _feed_row(session, category=landed_in)
            if marked:
                review.mark(session, tx_id=pending, token=token, via=via, escalate=escalated)
            if premarked:
                review.mark(session, tx_id=successor, token=None, via="aged")
            if corrected_first:
                _automation_correction(
                    session,
                    successor,
                    "expenses.dining" if same_category else "expenses.groceries",
                )
            source = _review_row(session, pending)
            head = schedules.chain_heads(session, [successor])[successor]
            before = _review_row(session, head)
            before_shape = (before.via, before.reviewed_by, before.reviewed_at) if before else None

            _supersede(session, pending, successor, identity_moved=identity_moved)

            after = _review_row(session, head)
            assert (after is not None) == (premarked or (marked and same_category))
            assert review.needs_review_ids(session, [head]) == (set() if after else {head})
            if premarked:
                assert after is not None
                assert (after.via, after.reviewed_by, after.reviewed_at) == before_shape
            elif after is not None:
                assert source is not None
                assert (after.reviewed_by, after.via, after.reviewed_at, after.note) == (
                    source.reviewed_by,
                    source.via,
                    source.reviewed_at,
                    source.note,
                )
                assert after.escalated_at == source.escalated_at
                assert after.carried_from == pending
            session.rollback()


# --------------------------------------------------------------------------
# Migration 0032 up/down (scratch database, never the suite's)
# --------------------------------------------------------------------------


def test_migration_0032_round_trips(database_url: str) -> None:
    import os
    from pathlib import Path

    import sqlalchemy as sa
    from alembic import command
    from alembic.config import Config

    import purser.config
    from tests.conftest import TEST_ENCRYPTION_KEY

    os.environ["PURSER_ENCRYPTION_KEY"] = TEST_ENCRYPTION_KEY
    purser.config.get_settings.cache_clear()
    admin = sa.create_engine(database_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(sa.text("DROP DATABASE IF EXISTS purser_review_carry_mig WITH (FORCE)"))
        conn.execute(sa.text("CREATE DATABASE purser_review_carry_mig"))
    admin.dispose()
    scratch_url = database_url.rsplit("/", 1)[0] + "/purser_review_carry_mig"
    root = Path(__file__).resolve().parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    cfg.set_main_option("sqlalchemy.url", scratch_url)
    engine = sa.create_engine(scratch_url)
    on_delete = sa.text(
        "SELECT c.confdeltype FROM pg_constraint c"
        " JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)"
        " WHERE c.conrelid = 'transaction_reviews'::regclass AND c.contype = 'f'"
        " AND a.attname = 'carried_from'"
    )
    try:
        command.upgrade(cfg, "0032")
        with engine.connect() as conn:
            assert conn.execute(on_delete).scalar_one() == "n"  # ON DELETE SET NULL
        command.downgrade(cfg, "-1")  # whatever 0032 is re-chained onto
        with engine.connect() as conn:
            columns = set(
                conn.execute(
                    sa.text(
                        "SELECT column_name FROM information_schema.columns"
                        " WHERE table_name = 'transaction_reviews'"
                    )
                ).scalars()
            )
        assert "carried_from" not in columns and "escalated_at" in columns
    finally:
        engine.dispose()
