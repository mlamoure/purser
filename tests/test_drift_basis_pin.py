"""Per-account drift-basis pin (issue #307, decision 83, DESIGN §7.3).

``accounts.drift_basis_override`` pins the ledger basis the post-sync drift
check reports its delta on; the history rule (decision 64) keeps running under
the pin, so clearing it resumes exactly where the history would be. Pure rule
tests, a property, the end-to-end sync path, the admin-gated PATCH field and
the migration's CHECKs.
"""

import os
import uuid
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy import Engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import purser.config
import tests.test_feed as feed_tests
from purser import db, digest
from purser.feed import drift
from purser.models import AuditLog
from tests.conftest import TEST_ENCRYPTION_KEY
from tests.test_feed import (  # noqa: F401 (fixture re-export)
    FeedHarness,
    _account_uuid,
    _drift_block,
    _drift_events,
    _drift_payloads,
    _recent_epoch,
    feed_env,
    harness,
    payload_of,
)

D = Decimal
_MONEY = st.decimals(
    min_value=D("-5000"), max_value=D("5000"), places=2, allow_nan=False, allow_infinity=False
)
_PIN = st.sampled_from([None, drift.POSTED_ONLY, drift.PENDING_INCLUSIVE])


def _simulate(
    account_id: uuid.UUID,
    syncs: list[tuple[Decimal, Decimal, Decimal]],
    pins: list[str | None],
) -> list[drift.DriftComparison]:
    """Run ``compare_with_history`` sync by sync as the post-sync check does,
    storing each observation the way ``_observe`` records it (``pinned`` and
    ``learned`` only on pinned observations)."""
    blocks: list[dict[str, Any]] = []
    out = []
    for (reported, posted, pending), pin in zip(syncs, pins, strict=True):
        comp = drift.compare_with_history(
            blocks[: drift.BASIS_WINDOW_SYNCS - 1],
            account_id=account_id,
            reported=reported,
            posted=posted,
            pending=pending,
            balance_date=None,
            pinned_basis=pin,
        )
        out.append(comp)
        obs: dict[str, Any] = {
            "reported": str(reported),
            "posted": drift.canonical(posted),
            "pending": drift.canonical(pending),
            "basis": comp.basis,
        }
        if comp.pinned:
            obs["pinned"] = True
            obs["learned"] = comp.learned_basis
        blocks.insert(0, {str(account_id): obs})
    return out


class TestPinRule:
    """The pin as pure functions."""

    # Posted-only delta constant, pending-inclusive moving: history says posted.
    HISTORY: ClassVar[list[tuple[Decimal, Decimal, Decimal]]] = [
        (D("-520.00"), D("-500.0000"), D("-500.0000")),
        (D("-520.00"), D("-500.0000"), D("-515.0000")),
        (D("-520.00"), D("-500.0000"), D("-523.0000")),
    ]

    def test_pin_beats_history(self) -> None:
        unpinned = _simulate(uuid.uuid4(), self.HISTORY, [None] * 3)
        assert unpinned[-1].basis == drift.POSTED_ONLY
        assert unpinned[-1].pinned is False
        assert unpinned[-1].learned_basis == drift.POSTED_ONLY

        pinned = _simulate(uuid.uuid4(), self.HISTORY, [drift.PENDING_INCLUSIVE] * 3)
        last = pinned[-1]
        assert last.basis == drift.PENDING_INCLUSIVE
        assert last.pinned is True
        assert last.learned_basis == drift.POSTED_ONLY  # the rule kept running
        assert last.delta == D("3")  # -520 - (-523): the pinned basis's delta

    def test_pinned_account_is_clean_when_either_basis_matches(self) -> None:
        # Pinned pending-inclusive, but the bank's number matches posted-only.
        comp = drift.compare_with_history(
            [],
            account_id=uuid.uuid4(),
            reported=D("-500.00"),
            posted=D("-500.0000"),
            pending=D("-515.0000"),
            balance_date=None,
            pinned_basis=drift.PENDING_INCLUSIVE,
        )
        assert comp.delta == 0
        assert comp.basis == drift.PENDING_INCLUSIVE
        assert comp.pinned is True

    def test_tie_break_reads_learned_on_pinned_observations(self) -> None:
        """No evidence either way (the bases never differed): the previous
        observation decides. On a pinned observation that is its ``learned``
        basis, never the pin it reported, so clearing a pin inherits nothing."""
        account_id = uuid.uuid4()
        flat = {"reported": "-520.00", "posted": "-500.0000", "pending": "-500.0000"}
        pinned_obs = dict(flat, basis=drift.PENDING_INCLUSIVE, pinned=True, learned="posted_only")
        kwargs: dict[str, Any] = {
            "account_id": account_id,
            "reported": D("-520.00"),
            "posted": D("-500.0000"),
            "pending": D("-500.0000"),
            "balance_date": None,
        }
        cleared = drift.compare_with_history([{str(account_id): pinned_obs}], **kwargs)
        assert cleared.basis == drift.POSTED_ONLY
        assert cleared.pinned is False
        # An unpinned observation's basis is still the tie-break.
        unpinned_obs = dict(flat, basis=drift.PENDING_INCLUSIVE)
        assert (
            drift.compare_with_history([{str(account_id): unpinned_obs}], **kwargs).basis
            == drift.PENDING_INCLUSIVE
        )

    def test_unknown_pin_is_ignored(self) -> None:
        comp = drift.compare_with_history(
            [],
            account_id=uuid.uuid4(),
            reported=D("-520.00"),
            posted=D("-500.0000"),
            pending=D("-515.0000"),
            balance_date=None,
            pinned_basis="posted",
        )
        assert comp.pinned is False
        assert comp.basis == drift.PENDING_INCLUSIVE  # smaller |delta|

    @settings(max_examples=200, deadline=None)
    @given(
        syncs=st.lists(st.tuples(_MONEY, _MONEY, _MONEY), min_size=1, max_size=25),
        pins=st.lists(_PIN, min_size=25, max_size=25),
    )
    def test_pin_wins_and_the_learned_rule_is_untouched(
        self, syncs: list[tuple[Decimal, Decimal, Decimal]], pins: list[str | None]
    ) -> None:
        """For any history and any pin sequence (set, changed, cleared at any
        sync): the reported basis is the pin whenever there is one; the learned
        basis is exactly what the history rule picks with no pin ever set; and
        the account is clean exactly when either basis matches."""
        pins = pins[: len(syncs)]
        account_id = uuid.uuid4()
        with_pins = _simulate(account_id, syncs, pins)
        never_pinned = _simulate(account_id, syncs, [None] * len(syncs))
        for comp, plain, pin, (reported, posted, pending) in zip(
            with_pins, never_pinned, pins, syncs, strict=True
        ):
            assert comp.learned_basis == plain.basis
            assert comp.basis == (pin or plain.basis)
            assert comp.pinned is (pin is not None)
            delta_posted, delta_pending = drift.basis_deltas(
                reported=reported, posted=posted, pending=pending
            )
            assert (comp.delta == 0) is (delta_posted == 0 or delta_pending == 0)
            if comp.delta != 0:
                assert comp.delta == (
                    delta_posted if comp.basis == drift.POSTED_ONLY else delta_pending
                )


def _pin(client: TestClient, account_id: str, value: str | None) -> dict[str, Any]:
    resp = client.patch(f"/accounts/{account_id}", json={"drift_basis_override": value})
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


class TestPinEndToEnd:
    """The #307 acceptance: a pinned basis wins over the history rule on every
    surface; clearing the pin brings the history rule back."""

    def test_pin_and_clear(
        self,
        harness: FeedHarness,  # noqa: F811
        migrated_engine: Engine,
    ) -> None:
        # A card whose bank leaves pendings out, sitting a constant 20.00 off
        # the posted ledger: the history learns posted_only (the #283 case).
        syncs = feed_tests.TestBalanceDrift._card_lifecycle()
        balances = ["-520.00", "-520.00", "-520.00", "-535.00", "-535.00"]
        cid = feed_tests.TestBalanceDrift()._connect_card(
            harness, payload_of(syncs[0], balance=balances[0])
        )
        for transactions, balance in zip(syncs, balances, strict=True):
            harness.use_payload(payload_of(transactions, balance=balance))
            harness.sync(cid)
        card_id = _account_uuid(migrated_engine, "liabilities.card")
        (obs,) = _drift_block(cid).values()
        assert obs["basis"] == "posted_only"
        assert "pinned" not in obs and "learned" not in obs
        assert _drift_events(migrated_engine) == [f"{card_id}:-20.0000"]

        # The admin pins pending-inclusive.
        body = _pin(harness.client, card_id, "pending_inclusive")
        assert body["drift_basis_override"] == "pending_inclusive"
        assert (
            harness.client.get(f"/accounts/{card_id}").json()["drift_basis_override"]
            == "pending_inclusive"
        )

        # A new pending: posted-only delta stays -20, pending-inclusive -12.
        hold = {
            "id": "p3",
            "posted": _recent_epoch(0),
            "amount": "-8.00",
            "description": "Hold three",
            "pending": True,
        }
        harness.use_payload(payload_of([*syncs[-1], hold], balance="-535.00"))
        harness.sync(cid)  # candidate (the pin moved the delta)
        harness.sync(cid)  # same delta twice: confirmed, one new episode
        (obs,) = _drift_block(cid).values()
        assert obs["basis"] == "pending_inclusive"
        assert obs["pinned"] is True
        assert obs["learned"] == "posted_only"
        assert obs["delta"] == "-12.0000"
        assert _drift_events(migrated_engine) == [f"{card_id}:-20.0000", f"{card_id}:-12.0000"]
        learned_payload, pinned_payload = _drift_payloads()
        assert "basis_pinned" not in learned_payload
        assert pinned_payload["mode"] == "pending_inclusive"
        assert pinned_payload["basis_pinned"] is True

        (row,) = harness.client.get(f"/connections/{cid}/discovered").json()
        assert row["drift"] == "-12.0000"
        assert row["drift_basis"] == "pending_inclusive"
        assert row["drift_basis_pinned"] is True
        assert row["drift_basis_learned"] == "posted_only"

        with db.get_sessionmaker()() as session:
            lines = digest.whats_new(session)["balance_drifts"]
        by_delta = {line["delta"]: line for line in lines}
        assert by_delta["-12.0000"]["basis"] == "pending_inclusive"
        assert by_delta["-12.0000"]["pinned"] is True
        assert by_delta["-20.0000"]["basis"] == "posted_only"
        assert "pinned" not in by_delta["-20.0000"]

        # Clearing the pin: the learned basis is back, with no pinned keys.
        assert _pin(harness.client, card_id, None)["drift_basis_override"] is None
        harness.sync(cid)
        (obs,) = _drift_block(cid).values()
        assert obs["basis"] == "posted_only"
        assert obs["delta"] == "-20.0000"
        assert "pinned" not in obs and "learned" not in obs
        (row,) = harness.client.get(f"/connections/{cid}/discovered").json()
        assert row["drift"] == "-20.0000"
        assert row["drift_basis"] == "posted_only"
        assert row["drift_basis_pinned"] is False
        assert row["drift_basis_learned"] == "posted_only"


@pytest.fixture
def accounts(admin_client: TestClient) -> dict[str, str]:
    """Admin session + one account of each relevant kind; returns name -> id."""

    def create(endpoint: str, name: str, type_: str, path: str) -> str:
        resp = admin_client.post(endpoint, json={"name": name, "type": type_, "path": path})
        assert resp.status_code == 201, resp.text
        return str(resp.json()["id"])

    return {
        "card": create("/accounts", "Card", "credit_card", "liabilities.card"),
        "loan": create("/accounts", "Loan", "loan", "liabilities.loan"),
        "brokerage": create("/accounts", "Brokerage", "brokerage", "assets.brokerage"),
        "dining": create("/categories", "Dining", "category_expense", "expenses.dining"),
    }


class TestPatchField:
    def test_set_keep_and_clear_audited(
        self, accounts: dict[str, str], client: TestClient, db_session: Session
    ) -> None:
        card = accounts["card"]
        assert client.get(f"/accounts/{card}").json()["drift_basis_override"] is None
        assert _pin(client, card, "posted_only")["drift_basis_override"] == "posted_only"
        # Absent leaves it unchanged...
        resp = client.patch(f"/accounts/{card}", json={"name": "Everyday Card"})
        assert resp.status_code == 200
        assert resp.json()["drift_basis_override"] == "posted_only"
        # ...an explicit null clears it.
        assert _pin(client, card, None)["drift_basis_override"] is None

        rows = (
            db_session.execute(
                select(AuditLog)
                .where(AuditLog.action == "account.update", AuditLog.entity_id == uuid.UUID(card))
                .order_by(AuditLog.at, AuditLog.id)
            )
            .scalars()
            .all()
        )
        assert [
            (
                r.before_after["before"]["drift_basis_override"],
                r.before_after["after"]["drift_basis_override"],
            )
            for r in rows
        ] == [(None, "posted_only"), ("posted_only", "posted_only"), ("posted_only", None)]

    def test_requires_admin_scope(self, accounts: dict[str, str], client: TestClient) -> None:
        commit = client.post("/tokens", json={"name": "agent", "scopes": ["read", "write"]})
        headers = {"Authorization": f"Bearer {commit.json()['token']}"}
        for value in ("posted_only", None):
            resp = client.patch(
                f"/accounts/{accounts['card']}",
                json={"drift_basis_override": value},
                headers=headers,
            )
            assert resp.status_code == 403
            assert "admin" in resp.json()["detail"]
        # The commit surface of the same route is unchanged.
        resp = client.patch(f"/accounts/{accounts['card']}", json={"name": "X"}, headers=headers)
        assert resp.status_code == 200, resp.text

    def test_bad_value_422(self, accounts: dict[str, str], client: TestClient) -> None:
        # The issue's `posted` is reconciliation vocabulary, not the drift's.
        resp = client.patch(
            f"/accounts/{accounts['card']}", json={"drift_basis_override": "posted"}
        )
        assert resp.status_code == 422

    def test_ineligible_types_422(self, accounts: dict[str, str], client: TestClient) -> None:
        for name in ("loan", "brokerage"):
            resp = client.patch(
                f"/accounts/{accounts[name]}", json={"drift_basis_override": "posted_only"}
            )
            assert resp.status_code == 422, name
            assert "drift check" in resp.json()["detail"]
        # Clearing is always allowed (nothing to refuse).
        assert _pin(client, accounts["loan"], None)["drift_basis_override"] is None

    def test_category_refused(self, accounts: dict[str, str], client: TestClient) -> None:
        resp = client.patch(
            f"/accounts/{accounts['dining']}", json={"drift_basis_override": "posted_only"}
        )
        assert resp.status_code == 422
        assert "PATCH /categories" in resp.json()["detail"]


# --------------------------------------------------------------------------
# Migration 0033 up/down (scratch database, never the suite's)
# --------------------------------------------------------------------------


@pytest.fixture
def scratch_db(database_url: str) -> Iterator[tuple[Config, sa.Engine]]:
    os.environ["PURSER_ENCRYPTION_KEY"] = TEST_ENCRYPTION_KEY
    purser.config.get_settings.cache_clear()
    admin = sa.create_engine(database_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(sa.text("DROP DATABASE IF EXISTS purser_drift_basis_pin_mig WITH (FORCE)"))
        conn.execute(sa.text("CREATE DATABASE purser_drift_basis_pin_mig"))
    admin.dispose()
    scratch_url = database_url.rsplit("/", 1)[0] + "/purser_drift_basis_pin_mig"
    root = Path(__file__).resolve().parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    cfg.set_main_option("sqlalchemy.url", scratch_url)
    engine = sa.create_engine(scratch_url)
    yield cfg, engine
    engine.dispose()


def _account_columns(engine: sa.Engine) -> set[str]:
    with engine.connect() as conn:
        return set(
            conn.execute(
                sa.text(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_name = 'accounts'"
                )
            ).scalars()
        )


def test_migration_0033_checks_and_round_trip(scratch_db: tuple[Config, sa.Engine]) -> None:
    cfg, engine = scratch_db
    command.upgrade(cfg, "0033")
    assert "drift_basis_override" in _account_columns(engine)
    with engine.begin() as conn:
        card = conn.execute(
            sa.text(
                "INSERT INTO accounts (name, type, path, drift_basis_override)"
                " VALUES ('Card', 'credit_card', 'liabilities.card', 'posted_only') RETURNING id"
            )
        ).scalar_one()
        conn.execute(
            sa.text(
                "UPDATE accounts SET drift_basis_override = 'pending_inclusive' WHERE id = :id"
            ),
            {"id": card},
        )
    # Only the drift vocabulary...
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(
            sa.text("UPDATE accounts SET drift_basis_override = 'posted' WHERE id = :id"),
            {"id": card},
        )
    # ...and never on a category.
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO accounts (name, type, is_virtual, path, drift_basis_override)"
                " VALUES ('Dining', 'category_expense', true, 'expenses.dining', 'posted_only')"
            )
        )

    command.downgrade(cfg, "-1")
    assert "drift_basis_override" not in _account_columns(engine)
    command.upgrade(cfg, "0033")
    with engine.connect() as conn:
        assert (
            conn.execute(
                sa.text("SELECT drift_basis_override FROM accounts WHERE id = :id"), {"id": card}
            ).scalar_one()
            is None
        )
