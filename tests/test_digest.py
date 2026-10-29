"""whats_new digest (issue #77): cursor exactly-once property over generated
event streams (same-timestamp batches + truncated-resume), plus section
shaping."""

import uuid
from decimal import Decimal

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import text
from sqlalchemy.orm import Session

from purser import digest, events

# Batches of events; every event in one batch shares a flush (and therefore
# a transaction timestamp) — the exact same-timestamp case the (at, id)
# keyset exists for.
_batches = st.lists(
    st.lists(st.sampled_from(["transaction.posted", "sync.failed"]), min_size=1, max_size=6),
    min_size=1,
    max_size=5,
)


def _insert_batch(session: Session, types: list[str]) -> int:
    for type_ in types:
        events.emit(session, type_=type_, entity_type="test", entity_id=uuid.uuid4(), payload={})
    session.commit()  # one transaction -> identical `at` for the whole batch
    return len(types)


@settings(
    max_examples=15, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(batches=_batches, scan_limit=st.integers(min_value=1, max_value=4))
def test_cursor_chaining_never_repeats_nor_drops(
    clean_db: None, db_session: Session, batches: list[list[str]], scan_limit: int
) -> None:
    """Chained whats_new calls partition the event stream exactly: summing
    the per-call counts over a tiny scan_limit (forcing truncated resumes)
    reproduces the inserted totals — no event counted twice, none skipped."""
    # Hypothesis examples share the test-scoped DB — isolate each example
    # (clean_db truncates per test function, not per example).
    db_session.execute(text("DELETE FROM events"))
    db_session.commit()

    inserted_tx = 0
    inserted_sync = 0
    # A first chunk exists before the first call; more lands mid-chain below.
    for batch in batches[: max(1, len(batches) // 2)]:
        inserted_tx += sum(1 for t in batch if t == "transaction.posted")
        inserted_sync += sum(1 for t in batch if t == "sync.failed")
        _insert_batch(db_session, batch)

    seen_tx = 0
    seen_sync = 0
    cursor: str | None = None
    interleaved = False
    for _ in range(200):  # hard stop; the loop exits when the stream runs dry
        result = digest.whats_new(db_session, since_cursor=cursor, scan_limit=scan_limit)
        seen_tx += result["transactions"]["count"]
        seen_sync += result["sync"]["failures"]
        if not interleaved:
            # New events arriving between calls must be picked up next call.
            for batch in batches[max(1, len(batches) // 2) :]:
                inserted_tx += sum(1 for t in batch if t == "transaction.posted")
                inserted_sync += sum(1 for t in batch if t == "sync.failed")
                _insert_batch(db_session, batch)
            interleaved = True
        if result["cursor"] == cursor and not result["events_truncated"]:
            break
        cursor = result["cursor"]
    else:
        raise AssertionError("cursor chain did not converge")

    assert seen_tx == inserted_tx
    assert seen_sync == inserted_sync


class TestDigestShaping:
    def test_quiet_day_is_quiet(self, clean_db: None, db_session: Session) -> None:
        result = digest.whats_new(db_session)
        assert result["transactions"]["count"] == 0
        assert result["matches"] == []
        assert result["cursor"] is None
        assert result["events_truncated"] is False

    def test_price_change_and_shortfall_sections(self, clean_db: None, db_session: Session) -> None:
        events.emit(
            db_session,
            type_="recurring.price_changed",
            entity_type="schedule",
            entity_id=uuid.uuid4(),
            payload={"payee": "YouTube Premium", "delta": "2.00", "schedule_id": "s-1"},
        )
        events.emit(
            db_session,
            type_="forecast.shortfall",
            entity_type="account",
            entity_id=uuid.uuid4(),
            payload={"account": "assets.checking", "date": "2026-07-19", "low": "-142.00"},
        )
        db_session.commit()
        result = digest.whats_new(db_session)
        assert result["price_changes"] == [
            {"payee": "YouTube Premium", "delta": "2.00", "schedule_id": "s-1"}
        ]
        assert result["forecast_flags"][0]["account"] == "assets.checking"
        assert result["forecast_flags"][0]["low"] == "-142.00"
        assert result["cursor"] is not None

    def test_caps_and_materiality(self, clean_db: None, db_session: Session) -> None:
        for i in range(digest.PRICE_CHANGES_CAP + 2):
            events.emit(
                db_session,
                type_="recurring.price_changed",
                entity_type="schedule",
                entity_id=uuid.uuid4(),
                payload={"payee": f"Svc {i}", "delta": "1.00", "schedule_id": str(i)},
            )
        db_session.commit()
        result = digest.whats_new(db_session, materiality=Decimal("50"))
        assert len(result["price_changes"]) == digest.PRICE_CHANGES_CAP
        assert result["price_changes_truncated"] is True

    def test_balance_drift_section(self, clean_db: None, db_session: Session) -> None:
        for i in range(digest.BALANCE_DRIFTS_CAP + 1):
            events.emit(
                db_session,
                type_="balance.drift",
                entity_type="account",
                entity_id=uuid.uuid4(),
                payload={
                    "account": f"assets.acct_{i}",
                    "delta": "-25.0000",
                    "balance_date": "2026-07-13",
                },
                dedup_key=f"{i}:-25.0000",
            )
        db_session.commit()
        result = digest.whats_new(db_session)
        # One shared commit timestamp: which entries fill the cap is (at, id)-
        # keyset order, not insertion order — assert shape, not identity.
        assert len(result["balance_drifts"]) == digest.BALANCE_DRIFTS_CAP
        for line in result["balance_drifts"]:
            assert line["account"].startswith("assets.acct_")
            assert line["delta"] == "-25.0000"
            assert line["balance_date"] == "2026-07-13"
            assert "basis" not in line  # the events recorded no basis
        assert result["balance_drifts_truncated"] is True

    def test_balance_drift_basis_present_only_when_recorded(
        self, clean_db: None, db_session: Session
    ) -> None:
        """Each drift line names the ledger basis its delta is measured on
        (the event's ``mode``, issue #283); a line whose event recorded none
        omits the key rather than sending null (guardrail 6)."""
        for path, mode in [
            ("liabilities.card", "posted_only"),
            ("assets.checking", "pending_inclusive"),
            ("assets.savings", None),
        ]:
            events.emit(
                db_session,
                type_="balance.drift",
                entity_type="account",
                entity_id=uuid.uuid4(),
                payload={
                    "account": path,
                    "delta": "-20.0000",
                    "balance_date": None,
                    **({"mode": mode} if mode else {}),
                },
                dedup_key=f"{path}:-20.0000",
            )
        db_session.commit()
        lines = {line["account"]: line for line in digest.whats_new(db_session)["balance_drifts"]}
        assert lines["liabilities.card"]["basis"] == "posted_only"
        assert lines["assets.checking"]["basis"] == "pending_inclusive"
        assert "basis" not in lines["assets.savings"]
        assert all("pinned" not in line for line in lines.values())

    def test_balance_drift_pinned_only_when_the_event_says_so(
        self, clean_db: None, db_session: Session
    ) -> None:
        """Issue #307: a line whose basis is the account's admin pin carries
        ``pinned: true`` (the event's ``basis_pinned``); learned lines omit it."""
        for path, pinned in [("liabilities.card", True), ("assets.checking", False)]:
            events.emit(
                db_session,
                type_="balance.drift",
                entity_type="account",
                entity_id=uuid.uuid4(),
                payload={
                    "account": path,
                    "delta": "-20.0000",
                    "mode": "posted_only",
                    "balance_date": None,
                    **({"basis_pinned": True} if pinned else {}),
                },
                dedup_key=f"{path}:-20.0000",
            )
        db_session.commit()
        lines = {line["account"]: line for line in digest.whats_new(db_session)["balance_drifts"]}
        assert lines["liabilities.card"]["pinned"] is True
        assert lines["liabilities.card"]["basis"] == "posted_only"
        assert "pinned" not in lines["assets.checking"]

    def test_pendings_vanished_section(self, clean_db: None, db_session: Session) -> None:
        """#282: every pending the bank dropped without a posted successor is
        counted; those above the materiality bar are itemized, largest first,
        and capped with a truncated flag."""
        from datetime import date

        from purser import ledger
        from purser.core import ledger as core
        from purser.feed import importer

        account = ledger.create_account(
            db_session,
            name="Card",
            type_="credit_card",
            path="liabilities.card",
            actor_token_id=None,
        )
        category = importer._uncategorized(db_session, income=False)
        for i, amount in enumerate(["-12.00", "-180.00", "95.50", "-60.00", "-75.25"]):
            tx = ledger.create_flat_transaction(
                db_session,
                account_ref=str(account.id),
                category_ref=str(category.id),
                amount=Decimal(amount),
                date=date(2026, 9, 1),
                payee_name=f"Shop {i}",
                requested_status=core.Status.PENDING,
                origin=core.Origin.IMPORT,
                token=None,
            )
            tx.status = "void"
            events.emit(
                db_session,
                type_="pending.vanished",
                entity_type="transaction",
                entity_id=tx.id,
                payload={"date": "2026-09-01", "description": None},
            )
        db_session.commit()

        block = digest.whats_new(db_session, materiality=Decimal("50"))["pendings_vanished"]
        assert block["count"] == 5
        assert [(line["payee"], line["amount"]) for line in block["notable"]] == [
            ("Shop 1", "-180.0000"),
            ("Shop 2", "95.5000"),
            ("Shop 4", "-75.2500"),
        ]
        assert block["truncated"] is True  # -60.00 is notable too, beyond the cap
        quiet = digest.whats_new(db_session, materiality=Decimal("500"))["pendings_vanished"]
        assert quiet == {"count": 5, "notable": [], "truncated": False}


class TestUpcoming:
    """The week's bills (``upcoming``)."""

    def test_an_instance_a_live_proposal_settles_is_not_listed(
        self, clean_db: None, db_session: Session
    ) -> None:
        """#352 (decision 76): an open proposal naming a live bank row means
        the bill is paid, pending confirmation — the same rule the forecast
        applies — so it is not still to come. A rejected one is listed."""
        from datetime import UTC, datetime, timedelta

        from purser import ledger
        from purser.core import ledger as core
        from purser.feed import importer
        from purser.models import Match

        today = datetime.now(UTC).date()
        account = ledger.create_account(
            db_session,
            name="Checking",
            type_="checking",
            path="assets.checking",
            actor_token_id=None,
        )
        category = importer._uncategorized(db_session, income=False)

        def row(amount: str, status: core.Status, origin: core.Origin) -> uuid.UUID:
            return ledger.create_flat_transaction(
                db_session,
                account_ref=str(account.id),
                category_ref=str(category.id),
                amount=Decimal(amount),
                date=today + timedelta(days=2),
                payee_name="Eversource Gas",
                requested_status=status,
                origin=origin,
                token=None,
            ).id

        settled = row("-32.44", core.Status.EXPECTED, core.Origin.HUMAN)
        still_due = row("-139.95", core.Status.EXPECTED, core.Origin.HUMAN)
        pending = row("-32.44", core.Status.PENDING, core.Origin.IMPORT)
        rejected_bank = row("-139.95", core.Status.PENDING, core.Origin.IMPORT)
        db_session.add(
            Match(expected_id=settled, posted_id=pending, score=0.6, variance=0, state="proposed")
        )
        db_session.add(
            Match(
                expected_id=still_due,
                posted_id=rejected_bank,
                score=0.6,
                variance=0,
                state="rejected",
            )
        )
        db_session.commit()

        upcoming = digest.whats_new(db_session)["upcoming"]
        assert [item["transaction_id"] for item in upcoming["items"]] == [str(still_due)]
        assert upcoming["count"] == 1
        assert upcoming["total"] == "139.95"

    def test_transfer_instances_are_bills_from_the_paying_leg(
        self, clean_db: None, db_session: Session
    ) -> None:
        """#357: a card payment is a two-leg instance — the week's biggest bill
        used to vanish because no single real leg read as its amount. It is
        listed from the leg on the schedule's own account (the paying side). A
        transfer schedule whose own leg is an inflow (money moving INTO the
        schedule's account) is still not a bill, though its other leg is
        negative."""
        from datetime import UTC, datetime, timedelta

        from purser import ledger
        from purser.core import ledger as core
        from purser.models import Schedule

        today = datetime.now(UTC).date()
        due = today + timedelta(days=3)
        checking, card, savings = (
            ledger.create_account(
                db_session, name=name, type_=type_, path=path, actor_token_id=None
            )
            for name, type_, path in [
                ("Checking", "checking", "assets.checking"),
                ("Citi Card", "credit_card", "liabilities.citicard"),
                ("Savings", "savings", "assets.savings"),
            ]
        )

        def instance(
            name: str,
            account_id: uuid.UUID,
            counterparty_id: uuid.UUID,
            amount: str,
            src: str,
            dst: str,
        ) -> uuid.UUID:
            schedule = Schedule(
                name=name,
                account_id=account_id,
                counterparty_account_id=counterparty_id,
                rrule="FREQ=MONTHLY",
                dtstart=due,
                amount_type="fixed",
                amount=Decimal(amount),
                source="manual",
            )
            db_session.add(schedule)
            db_session.flush()
            return ledger.create_transfer(
                db_session,
                from_account_ref=src,
                to_account_ref=dst,
                amount=abs(Decimal(amount)),
                date=due,
                description=name,
                requested_status=core.Status.EXPECTED,
                token=None,
                schedule_id=schedule.id,
                occurrence_date=due,
            ).id

        # Checking pays the card: the schedule's own (checking) leg is -500.
        payment = instance(
            "Citi payment",
            checking.id,
            card.id,
            "-500.00",
            "assets.checking",
            "liabilities.citicard",
        )
        # A sweep INTO checking from savings: checking's leg is +200, an inflow.
        instance(
            "Savings sweep", checking.id, savings.id, "200.00", "assets.savings", "assets.checking"
        )
        db_session.commit()

        upcoming = digest.whats_new(db_session)["upcoming"]
        assert upcoming["items"] == [
            {
                "date": due.isoformat(),
                "name": "Citi payment",
                "amount": "-500.0000",
                "transaction_id": str(payment),
            }
        ]
        assert upcoming["count"] == 1
        assert upcoming["total"] == "500.00"
