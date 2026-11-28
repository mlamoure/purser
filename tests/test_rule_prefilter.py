"""Rule preview, apply-history and the trade-rule projection pre-filtered by
the search text cache (#141 phase 1c): the same results as the full walk,
over all of history, with only the candidate rows loaded. Synthetic data."""

import uuid
from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from purser import ledger as ledger_service
from purser import rules as rules_service
from purser.core import ledger as core
from purser.core import rules as core_rules
from purser.db import get_sessionmaker
from purser.models import Transaction
from purser.search import textcache
from tests.conftest import reset_db, today

_PAYEES = [None, "Blue Cup Coffee", "Whole Foods", "Café Rosa", "7-Eleven", "東京 Ramen"]
_DESCRIPTIONS = [
    None,
    "POS PURCHASE BLUE CUP #12",
    "WHOLE FOODS MARKET 7",
    "CHECKCARD CAFE ROSA",
    "7-ELEVEN 1234",
    "Purchase return",
    "東京Cafe order",
    "ACH DEBIT ROSA",
]
_TEXTS = st.sampled_from(
    [
        "blue cup",
        "cup",
        "whole foods",
        "cafe",
        "café rosa",
        "rosa",
        "7 eleven",
        "7-Eleven",
        "chase",  # never inside "purchase": word boundaries
        "purchase",
        "東京",
        "",
        "#",
        "order",
    ]
)


@pytest.fixture
def cache(migrated_engine: Engine) -> Iterator[None]:
    textcache.reset()
    yield
    textcache.reset()


def _world(session: Session) -> tuple[str, str]:
    account = ledger_service.create_account(
        session, name="Checking", type_="checking", path="assets.checking", actor_token_id=None
    )
    ledger_service.ensure_category_path(session, "expenses.uncategorized", actor_token_id=None)
    cat = ledger_service.ensure_category_path(session, "expenses.dining", actor_token_id=None)
    return str(account.id), str(cat.id)


def _both(session: Session, run: Any) -> tuple[Any, Any]:
    """``run`` with the cache ready, then with none (the full walk)."""
    store = textcache.warm()
    try:
        with_cache = run()
    finally:
        textcache.reset()
    without = run()
    del store
    return with_cache, without


class TestParity:
    @settings(
        max_examples=40,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
    )
    @given(
        rows=st.lists(
            st.tuples(
                st.sampled_from(_PAYEES),
                st.sampled_from(_DESCRIPTIONS),
                st.booleans(),
                st.integers(0, 6),
            ),
            max_size=25,
        ),
        field=st.sampled_from(list(core_rules.MatchField)),
        mode=st.sampled_from(list(core_rules.MatchMode)),
        text=_TEXTS,
    )
    @example(
        rows=[
            ("Blue Cup Coffee", "Purchase return", False, 0),
            (None, "POS PURCHASE BLUE CUP #12", True, 1),
            ("東京 Ramen", "東京Cafe order", False, 2),
        ],
        field=core_rules.MatchField.PAYEE_OR_DESCRIPTION,
        mode=core_rules.MatchMode.CONTAINS,
        text="cup",
    )
    def test_preview_and_apply_history_equal_the_full_walk(
        self,
        migrated_engine: Engine,
        cache: None,
        monkeypatch: pytest.MonkeyPatch,
        rows: list[tuple[str | None, str | None, bool, int]],
        field: core_rules.MatchField,
        mode: core_rules.MatchMode,
        text: str,
    ) -> None:
        # The sample window bigger than the ledger: preview's full walk then
        # covers everything too, so the two answers must be identical.
        monkeypatch.setattr(rules_service, "_SCAN_CAP", 10_000)
        reset_db(migrated_engine)
        with get_sessionmaker()() as session:
            account, dining = _world(session)
            for payee, description, pending, days in rows:
                ledger_service.create_flat_transaction(
                    session,
                    account_ref=account,
                    category_ref="expenses.uncategorized",
                    amount=Decimal("-9.50"),
                    date=date(2026, 3, 10) - timedelta(days=days),
                    payee_name=payee,
                    description=description,
                    requested_status=core.Status.PENDING if pending else core.Status.POSTED,
                    origin=core.Origin.IMPORT,
                    token=None,
                )
            rule = rules_service.create(
                session,
                fields={
                    "type": "categorize",
                    "match_field": field.value,
                    "match_mode": mode.value,
                    "match_text": text or "x",
                    "category_id": uuid.UUID(dining),
                },
                token=None,
                activate=True,
            )
            if not text:  # an empty needle: stored text that normalizes to nothing
                rule.match_text = "#"
            session.commit()
            predicate = rules_service._predicate_of(rule)

            def preview() -> dict[str, object]:
                return rules_service.preview(session, predicate=predicate, limit=50)

            def dry_run() -> dict[str, Any]:
                return rules_service.apply_history(
                    session,
                    rule_id=rule.id,
                    token=None,
                    dry_run=True,
                    posted="correct",
                    include_user_categorized=True,
                )

            cached, walked = _both(session, preview)
            assert cached == walked
            cached, walked = _both(session, dry_run)
            assert cached == walked
            session.rollback()


def _row(client: TestClient, payee: str, day: date, status: str = "pending") -> str:
    resp = client.post(
        "/transactions",
        json={
            "account": "assets.checking",
            "category": "expenses.uncategorized",
            "amount": "-25.00",
            "date": day.isoformat(),
            "payee": payee,
            "status": status,
        },
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


@pytest.fixture
def seeded(admin_client: TestClient) -> dict[str, str]:
    ids = {}
    assert (
        admin_client.post(
            "/accounts",
            json={"name": "Checking", "type": "checking", "path": "assets.checking"},
        ).status_code
        == 201
    )
    for name, path in (("Uncategorized", "expenses.uncategorized"), ("Dining", "expenses.dining")):
        resp = admin_client.post(
            "/categories", json={"name": name, "type": "category_expense", "path": path}
        )
        if resp.status_code == 201:
            ids[path] = resp.json()["id"]
    return ids


class TestPreviewCoversAllHistory:
    def test_a_text_predicate_has_no_window_with_the_cache(
        self,
        seeded: dict[str, str],
        client: TestClient,
        cache: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """With the window shrunk to 4 rows, the old preview saw none of the
        older matches; from the cache it counts all of them and says it
        scanned every live row. A predicate with no text keeps the window."""
        monkeypatch.setattr(rules_service, "_SCAN_CAP", 4)
        day = today()
        for days_ago in range(5):
            _row(client, "Example Hardware", day - timedelta(days=days_ago))
        for i in range(3):
            _row(client, "Example Taqueria", day - timedelta(days=10 + i), status="posted")
        textcache.warm()
        predicate = {"match_field": "payee", "match_mode": "contains", "match_text": "taqueria"}
        found = client.post("/rules/preview", json=predicate).json()
        assert (found["scanned"], found["scan_capped"], found["matched"]) == (8, False, 3)
        assert [s["payee"] for s in found["sample"]] == ["Example Taqueria"] * 3
        amount_only = client.post("/rules/preview", json={"amount_exact": "-25.00"}).json()
        assert (amount_only["scanned"], amount_only["scan_capped"]) == (4, True)

    def test_only_the_candidates_are_loaded(
        self,
        seeded: dict[str, str],
        client: TestClient,
        cache: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        day = today()
        for days_ago in range(30):
            _row(client, "Example Hardware", day - timedelta(days=days_ago))
        taqueria = _row(client, "Example Taqueria", day - timedelta(days=40))
        textcache.warm()
        predicate = core_rules.Predicate(
            match_field=core_rules.MatchField.PAYEE,
            match_mode=core_rules.MatchMode.CONTAINS,
            match_text="taqueria",
        )
        examined: list[str] = []
        real = rules_service._candidate_of

        def spy(tx: Transaction) -> core_rules.Candidate | None:
            examined.append(str(tx.id))
            return real(tx)

        monkeypatch.setattr(rules_service, "_candidate_of", spy)
        with get_sessionmaker()() as session:
            result = rules_service.preview(session, predicate=predicate)
            assert examined == [taqueria]
            assert (result["matched"], result["scanned"]) == (1, 31)
            session.rollback()


class TestProjectMatches:
    def test_the_trade_rule_projection_equals_the_full_walk(
        self, seeded: dict[str, str], client: TestClient, cache: None
    ) -> None:
        day = today()
        for i in range(6):
            _row(client, "Example Broker", day - timedelta(days=i), status="posted")
            _row(client, "Example Grocer", day - timedelta(days=i))
        rule = client.post(
            "/rules",
            json={
                "type": "categorize",
                "match_field": "payee_or_description",
                "match_mode": "contains",
                "match_text": "example broker",
                "category_id": seeded["expenses.dining"],
            },
        ).json()
        with get_sessionmaker()() as session:
            row = session.get(rules_service.Rule, uuid.UUID(rule["id"]))
            assert row is not None
            cached, walked = _both(session, lambda: rules_service.project_matches(session, row))
            assert cached == walked == 6
            session.rollback()
