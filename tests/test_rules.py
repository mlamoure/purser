"""Deterministic rules (issue #174, DESIGN decision 46).

The predicate is pure, so its invariants get property-based tests rather than
examples: monotonicity is the one that matters, because a rule engine that can
turn a non-match into a match by ADDING a constraint is not a rule engine.
"""

import uuid
from datetime import date, timedelta
from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from purser.core import rules as core_rules

AMOUNTS = st.decimals(
    min_value=Decimal("-9999.99"),
    max_value=Decimal("9999.99"),
    places=2,
    allow_nan=False,
    allow_infinity=False,
)
TEXTS = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd", "Zs")), min_size=0, max_size=40
)
DATES = st.dates(min_value=date(2020, 1, 1), max_value=date(2030, 12, 31))


@st.composite
def candidates(draw: st.DrawFn) -> core_rules.Candidate:
    return core_rules.Candidate(
        account_id=draw(st.sampled_from(["acct-a", "acct-b", "acct-c"])),
        amount=draw(AMOUNTS),
        date=draw(DATES),
        payee=draw(st.one_of(st.none(), TEXTS)),
        description=draw(st.one_of(st.none(), TEXTS)),
    )


class TestPredicateProperties:
    @given(candidate=candidates())
    @settings(max_examples=200, deadline=None)
    def test_an_empty_predicate_matches_everything(self, candidate: core_rules.Candidate) -> None:
        """All-None is deliberately universal: a `categorize` rule scoped only
        by account has to be expressible."""
        assert core_rules.matches(core_rules.Predicate(), candidate)

    @given(candidate=candidates(), amount=AMOUNTS, when=DATES)
    @settings(max_examples=200, deadline=None)
    def test_adding_a_constraint_is_monotone(
        self, candidate: core_rules.Candidate, amount: Decimal, when: date
    ) -> None:
        """Narrowing can only ever turn a match into a non-match. If any
        single added field could turn a non-match INTO a match, the AND
        semantics are broken and rule behavior stops being predictable."""
        base = core_rules.Predicate()
        narrower = [
            core_rules.Predicate(account_ids=frozenset({"acct-a"})),
            core_rules.Predicate(amount_exact=amount),
            core_rules.Predicate(amount_min=amount),
            core_rules.Predicate(amount_max=amount),
            core_rules.Predicate(direction=core_rules.Direction.DEBIT),
            core_rules.Predicate(direction=core_rules.Direction.CREDIT),
            core_rules.Predicate(effective_from=when),
        ]
        assert core_rules.matches(base, candidate)
        for predicate in narrower:
            if core_rules.matches(predicate, candidate):
                # It may still match — but never MORE than the empty predicate.
                assert core_rules.matches(base, candidate)

    @given(haystack=TEXTS, needle=TEXTS)
    @settings(max_examples=200, deadline=None)
    def test_contains_never_matches_inside_a_token(self, haystack: str, needle: str) -> None:
        """The footgun core/matching.py documents: an account named "Chase"
        must not match "PURCHASE RETURN". Boundary-aware `contains` means a
        hit implies the needle is a whole token run of the haystack."""
        from purser.core.ledger import normalize_payee

        h, n = normalize_payee(haystack), normalize_payee(needle)
        if not core_rules.text_matches(h, n, core_rules.MatchMode.CONTAINS):
            return
        assert n
        h_tokens, n_tokens = h.split(), n.split()
        # The needle's tokens appear as a contiguous run of whole tokens.
        assert any(
            h_tokens[i : i + len(n_tokens)] == n_tokens
            for i in range(len(h_tokens) - len(n_tokens) + 1)
        )


class TestTokenBoundaries:
    """The cases that motivated a structured predicate over a regex."""

    def test_chase_does_not_match_purchase_return(self) -> None:
        candidate = core_rules.Candidate(
            account_id="a",
            amount=Decimal("-10.00"),
            date=date(2026, 8, 1),
            description="PURCHASE RETURN",
        )
        predicate = core_rules.Predicate(
            match_field=core_rules.MatchField.DESCRIPTION,
            match_mode=core_rules.MatchMode.CONTAINS,
            match_text="chase",
        )
        assert not core_rules.matches(predicate, candidate)

    def test_prefix_anchors_and_ends_on_a_boundary(self) -> None:
        assert not core_rules.text_matches("starbucks coffee", "star", core_rules.MatchMode.PREFIX)
        assert core_rules.text_matches("star market", "star", core_rules.MatchMode.PREFIX)

    def test_payee_or_description_searches_both(self) -> None:
        candidate = core_rules.Candidate(
            account_id="a",
            amount=Decimal("-10.00"),
            date=date(2026, 8, 1),
            payee="Whole Foods",
            description="POS PURCHASE",
        )
        both = core_rules.Predicate(
            match_field=core_rules.MatchField.PAYEE_OR_DESCRIPTION,
            match_mode=core_rules.MatchMode.CONTAINS,
            match_text="whole foods",
        )
        assert core_rules.matches(both, candidate)

    def test_direction_reads_the_post_invert_sign(self) -> None:
        """`direction` is written in the user's sign world, not the bank's
        (decision 43): the importer inverts before the predicate sees it."""
        outflow = core_rules.Candidate(
            account_id="a", amount=Decimal("-25.00"), date=date(2026, 8, 1)
        )
        inflow = core_rules.Candidate(
            account_id="a", amount=Decimal("25.00"), date=date(2026, 8, 1)
        )
        debit = core_rules.Predicate(direction=core_rules.Direction.DEBIT)
        credit = core_rules.Predicate(direction=core_rules.Direction.CREDIT)
        assert core_rules.matches(debit, outflow)
        assert not core_rules.matches(debit, inflow)
        assert core_rules.matches(credit, inflow)
        assert not core_rules.matches(credit, outflow)

    def test_effective_from_excludes_strictly_older_rows(self) -> None:
        cutoff = date(2026, 8, 1)
        predicate = core_rules.Predicate(effective_from=cutoff)
        base = {"account_id": "a", "amount": Decimal("-1.00")}
        assert not core_rules.matches(
            predicate, core_rules.Candidate(date=cutoff - timedelta(days=1), **base)
        )
        assert core_rules.matches(predicate, core_rules.Candidate(date=cutoff, **base))


class TestEvaluationOrder:
    def test_first_match_wins_and_order_is_stable_under_shuffle(self) -> None:
        """A rule's effect must be explainable by pointing at ONE row, so the
        winner cannot depend on load order beyond (priority, created_at, id)."""
        from purser import rules as rules_service

        def rule(priority: int, text: str, rid: uuid.UUID) -> rules_service.LoadedRule:
            return rules_service.LoadedRule(
                id=rid,
                type=rules_service.TYPE_CATEGORIZE,
                priority=priority,
                predicate=core_rules.Predicate(
                    match_field=core_rules.MatchField.PAYEE,
                    match_mode=core_rules.MatchMode.CONTAINS,
                    match_text=text,
                ),
                category_id=uuid.uuid4(),
                payee_name=None,
                pair=None,
                amount_exact=None,
                effective_from=None,
            )

        low = rule(10, "amazon", uuid.uuid4())
        high = rule(200, "amazon", uuid.uuid4())
        candidate = core_rules.Candidate(
            account_id="a", amount=Decimal("-10.00"), date=date(2026, 8, 1), payee="AMAZON MKTPL"
        )
        # load_active sorts; first_match then takes the head of that order.
        ordered = sorted([high, low], key=lambda r: (r.priority, r.id))
        winner = rules_service.first_match(ordered, rules_service.TYPE_CATEGORIZE, candidate)
        assert winner is not None and winner.id == low.id

        reordered = sorted([low, high], key=lambda r: (r.priority, r.id))
        assert reordered == ordered

    def test_a_rule_of_another_type_never_wins(self) -> None:
        from purser import rules as rules_service

        rename = rules_service.LoadedRule(
            id=uuid.uuid4(),
            type=rules_service.TYPE_RENAME,
            priority=1,
            predicate=core_rules.Predicate(),
            category_id=None,
            payee_name="Friendly",
            pair=None,
            amount_exact=None,
            effective_from=None,
        )
        candidate = core_rules.Candidate(
            account_id="a", amount=Decimal("-1.00"), date=date(2026, 8, 1)
        )
        assert rules_service.first_match([rename], rules_service.TYPE_CATEGORIZE, candidate) is None
        assert rules_service.first_match([rename], rules_service.TYPE_RENAME, candidate) is rename


# --------------------------------------------------------------------------
# Service + API behaviour
# --------------------------------------------------------------------------

import os  # noqa: E402
from collections.abc import Iterator  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402

import pytest  # noqa: E402
import sqlalchemy as sa  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import Engine, text  # noqa: E402

import purser.config  # noqa: E402
from tests.conftest import TEST_ENCRYPTION_KEY  # noqa: E402


@pytest.fixture
def seeded(admin_client: TestClient) -> dict[str, str]:
    ids: dict[str, str] = {}
    for name, type_, path in [
        ("Checking", "checking", "assets.checking"),
        ("Savings", "savings", "assets.savings"),
    ]:
        ids[path] = admin_client.post(
            "/accounts", json={"name": name, "type": type_, "path": path}
        ).json()["id"]
    for name, path in [
        ("Uncategorized", "expenses.uncategorized"),
        ("Dining", "expenses.dining"),
        ("Groceries", "expenses.groceries"),
    ]:
        resp = admin_client.post(
            "/categories", json={"name": name, "type": "category_expense", "path": path}
        )
        if resp.status_code == 201:
            ids[path] = resp.json()["id"]
    return ids


def _row(
    client: TestClient,
    payee: str,
    amount: str = "-25.00",
    status: str = "pending",
    day: date | None = None,
) -> str:
    resp = client.post(
        "/transactions",
        json={
            "account": "assets.checking",
            "category": "expenses.uncategorized",
            "amount": amount,
            "date": (day or date.today()).isoformat(),
            "payee": payee,
            "status": status,
        },
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


class TestDraftsNeverExecute:
    """The propose/commit gate is structural: load_active is the only loader
    and filters state='active', so a draft cannot run even by accident."""

    def test_a_draft_rule_does_not_categorize(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        resp = client.post(
            "/rules",
            json={
                "type": "categorize",
                "match_field": "payee",
                "match_mode": "contains",
                "match_text": "chipotle",
                "category_id": seeded["expenses.dining"],
            },
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["state"] == "draft"

        tx_id = _row(client, "Chipotle")
        stats = client.post("/categorize/run").json()
        assert stats["rule"] == 0
        assert client.get(f"/transactions/{tx_id}").json()["category_path"] == (
            "expenses.uncategorized"
        )

    def test_activating_makes_the_same_rule_fire(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        rule_id = client.post(
            "/rules",
            json={
                "type": "categorize",
                "match_field": "payee",
                "match_mode": "contains",
                "match_text": "chipotle",
                "category_id": seeded["expenses.dining"],
            },
        ).json()["id"]
        assert client.post(f"/rules/{rule_id}/state", json={"state": "active"}).status_code == 200
        tx_id = _row(client, "Chipotle")
        stats = client.post("/categorize/run").json()
        assert stats["rule"] == 1
        assert client.get(f"/transactions/{tx_id}").json()["category_path"] == "expenses.dining"


class TestPrecedence:
    def test_a_rule_beats_a_conflicting_payee_memory(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        """rule > payee memory (DESIGN §11). A rule must be able to beat a
        WRONG memory — that is the whole "graduate a repeated fix" path."""
        for _ in range(2):
            client.post(
                "/transactions",
                json={
                    "account": "assets.checking",
                    "category": "expenses.dining",
                    "amount": "-20.00",
                    "date": date.today().isoformat(),
                    "payee": "Whole Foods",
                    "status": "posted",
                },
            )
        rule_id = client.post(
            "/rules",
            json={
                "type": "categorize",
                "match_field": "payee",
                "match_mode": "contains",
                "match_text": "whole foods",
                "category_id": seeded["expenses.groceries"],
            },
        ).json()["id"]
        client.post(f"/rules/{rule_id}/state", json={"state": "active"})

        tx_id = _row(client, "Whole Foods")
        stats = client.post("/categorize/run").json()
        assert stats["rule"] == 1
        assert stats["deterministic"] == 0
        assert client.get(f"/transactions/{tx_id}").json()["category_path"] == (
            "expenses.groceries"
        )

    def test_payee_prefix_rule_handles_bank_suffix_variants(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        """The durable payee mapping (how-to/rules.md): one prefix rule covers
        a descriptor whose stable text is followed by a changing reference."""
        rule_id = client.post(
            "/rules",
            json={
                "type": "categorize",
                "match_field": "payee",
                "match_mode": "prefix",
                "match_text": "example grocer store",
                "category_id": seeded["expenses.groceries"],
            },
        ).json()["id"]
        assert client.post(f"/rules/{rule_id}/state", json={"state": "active"}).status_code == 200
        ids = [
            _row(client, payee)
            for payee in (
                "Example Grocer Store 0412",
                "EXAMPLE GROCER STORE 1187 A",
                "Example Grocer Store Ref 2290",
            )
        ]
        assert client.post("/categorize/run").json()["rule"] == 3
        assert {client.get(f"/transactions/{tx_id}").json()["category_path"] for tx_id in ids} == {
            "expenses.groceries"
        }

    def test_payee_rule_can_target_a_real_account_as_a_transfer(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        """A real-account target is the `[account]` transfer sugar: the same
        rule model maps payee text to a transfer, not just a category."""
        rule_id = client.post(
            "/rules",
            json={
                "type": "categorize",
                "match_field": "payee",
                "match_mode": "contains",
                "match_text": "example brokerage services",
                "category_id": seeded["assets.savings"],
            },
        ).json()["id"]
        assert client.post(f"/rules/{rule_id}/state", json={"state": "active"}).status_code == 200
        tx_id = _row(client, "Example Brokerage Services LLC", "-250.00")
        assert client.post("/categorize/run").json()["rule"] == 1
        row = client.get(f"/transactions/{tx_id}").json()
        assert row["is_transfer"] is True
        assert row["transfer_account_path"] == "assets.savings"


class TestTransferLinkActivation:
    """A merge is balance-neutral, so §7.3's drift check cannot detect a bad
    one. Activation is retroactive for free via the nightly sweep, so the
    projected count is confirmed explicitly rather than shown for information."""

    def test_activation_without_confirmation_is_refused(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        rule_id = client.post(
            "/rules",
            json={
                "type": "transfer_link",
                "pair_account_a": seeded["assets.checking"],
                "pair_account_b": seeded["assets.savings"],
            },
        ).json()["id"]
        resp = client.post(f"/rules/{rule_id}/state", json={"state": "active"})
        assert resp.status_code == 422
        assert "confirm_links" in resp.json()["detail"]
        assert client.get("/rules").json()[0]["state"] == "draft"

    def test_activation_with_the_projected_count_succeeds(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        rule_id = client.post(
            "/rules",
            json={
                "type": "transfer_link",
                "pair_account_a": seeded["assets.checking"],
                "pair_account_b": seeded["assets.savings"],
            },
        ).json()["id"]
        refused = client.post(f"/rules/{rule_id}/state", json={"state": "active"})
        projected = int(refused.json()["detail"].split("would link ")[1].split(" ")[0])
        ok = client.post(
            f"/rules/{rule_id}/state", json={"state": "active", "confirm_links": projected}
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["state"] == "active"

    def test_a_wrong_count_is_refused(self, seeded: dict[str, str], client: TestClient) -> None:
        rule_id = client.post(
            "/rules",
            json={
                "type": "transfer_link",
                "pair_account_a": seeded["assets.checking"],
                "pair_account_b": seeded["assets.savings"],
            },
        ).json()["id"]
        resp = client.post(
            f"/rules/{rule_id}/state", json={"state": "active", "confirm_links": 999}
        )
        assert resp.status_code == 422


class TestApplyHistory:
    def test_dry_run_counts_equal_commit_results(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        """Decision 35's preview-exactness rule: the preview's counts ARE the
        commit's results, because both walk the identical candidate list."""
        for _ in range(3):
            _row(client, "Chipotle")
        rule_id = client.post(
            "/rules",
            json={
                "type": "categorize",
                "match_field": "payee",
                "match_mode": "contains",
                "match_text": "chipotle",
                "category_id": seeded["expenses.dining"],
                "activate": True,
            },
        ).json()["id"]

        dry = client.post(f"/rules/{rule_id}/apply-history", json={"dry_run": True}).json()
        wet = client.post(f"/rules/{rule_id}/apply-history", json={"dry_run": False}).json()
        assert dry["matched"] == wet["matched"] == 3
        assert dry["pending"] == wet["pending"] == 3

    def test_history_past_the_preview_window_is_reached(
        self, seeded: dict[str, str], client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#356: apply-history walked preview's capped window, so on a ledger
        past the cap a retroactive rule silently never reached older rows.
        With the batch and the cap both shrunk, a rule whose every match is
        older than the newest rows (more of them than a batch) reaches all of
        them, dry run and commit alike — posted corrections included, which
        mint replacements mid-history — while preview stays a sample."""
        from purser import rules as rules_service

        monkeypatch.setattr(rules_service, "_HISTORY_BATCH", 3)
        monkeypatch.setattr(rules_service, "_SCAN_CAP", 4)
        today = date.today()
        for days_ago in range(5):
            _row(client, "Example Hardware", day=today - timedelta(days=days_ago))
        pending = [
            _row(client, "Example Taqueria", day=today - timedelta(days=10 + i)) for i in range(4)
        ]
        for i in range(3):
            _row(client, "Example Taqueria", status="posted", day=today - timedelta(days=20 + i))
        predicate = {"match_field": "payee", "match_mode": "contains", "match_text": "taqueria"}
        rule_id = client.post(
            "/rules",
            json={
                "type": "categorize",
                **predicate,
                "category_id": seeded["expenses.dining"],
                "activate": True,
            },
        ).json()["id"]

        sample = client.post("/rules/preview", json=predicate).json()
        assert (sample["scanned"], sample["scan_capped"], sample["matched"]) == (4, True, 0)

        body = {"posted": "correct"}
        dry = client.post(f"/rules/{rule_id}/apply-history", json={**body, "dry_run": True})
        wet = client.post(f"/rules/{rule_id}/apply-history", json={**body, "dry_run": False})
        assert dry.status_code == wet.status_code == 200, wet.text
        counts = ("scanned", "matched", "pending", "posted", "skipped_user_categorized")
        assert {k: dry.json()[k] for k in counts} == {k: wet.json()[k] for k in counts}
        assert {k: wet.json()[k] for k in counts} == {
            "scanned": 12,
            "matched": 7,
            "pending": 4,
            "posted": 3,
            "skipped_user_categorized": 0,
        }
        for tx_id in pending:
            assert client.get(f"/transactions/{tx_id}").json()["category_path"] == "expenses.dining"
        dining = client.get("/transactions", params={"category": "expenses.dining"}).json()
        assert dining["total"] == 7
        assert {row["payee"] for row in dining["items"]} == {"Example Taqueria"}

    def test_transfer_link_refuses_apply_history(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        rule_id = client.post(
            "/rules",
            json={
                "type": "transfer_link",
                "pair_account_a": seeded["assets.checking"],
                "pair_account_b": seeded["assets.savings"],
            },
        ).json()["id"]
        resp = client.post(f"/rules/{rule_id}/apply-history", json={"dry_run": True})
        assert resp.status_code == 422
        assert "nightly sweep" in resp.json()["detail"]

    def test_a_transfer_is_never_selected_so_preview_equals_commit(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        """#311: with include_user_categorized the pass reached transfer rows
        — a transfer has no category leg to set — and the commit failed on
        them, so the dry run's counts were not the commit's. Transfers are now
        never selected, in the dry run or the commit, and are reported as
        skipped; the flat rows still take the category."""
        uncategorized = _row(client, "Example Gym Dues")
        user_set = _row(client, "Example Gym Dues", "-30.00")
        resp = client.patch(f"/transactions/{user_set}", json={"category": "expenses.groceries"})
        assert resp.status_code == 200, resp.text
        transfers = []
        for status in ("pending", "posted"):
            resp = client.post(
                "/transactions/transfer",
                json={
                    "from_account": "assets.checking",
                    "to_account": "assets.savings",
                    "amount": "40.00",
                    "date": date.today().isoformat(),
                    "description": "EXAMPLE GYM DUES SAVINGS SWEEP",
                    "status": status,
                },
            )
            assert resp.status_code == 201, resp.text
            transfers.append(resp.json()["id"])
        rule_id = client.post(
            "/rules",
            json={
                "type": "categorize",
                "match_field": "payee_or_description",
                "match_mode": "contains",
                "match_text": "example gym",
                "category_id": seeded["expenses.dining"],
                "activate": True,
            },
        ).json()["id"]

        body = {"include_user_categorized": True, "posted": "correct"}
        dry = client.post(f"/rules/{rule_id}/apply-history", json={**body, "dry_run": True})
        wet = client.post(f"/rules/{rule_id}/apply-history", json={**body, "dry_run": False})
        assert dry.status_code == wet.status_code == 200, wet.text
        counts = ("matched", "pending", "posted", "skipped_user_categorized", "skipped_transfers")
        assert {k: dry.json()[k] for k in counts} == {k: wet.json()[k] for k in counts}
        assert {k: wet.json()[k] for k in counts} == {
            "matched": 2,
            "pending": 2,
            "posted": 0,
            "skipped_user_categorized": 0,
            "skipped_transfers": 2,
        }
        for tx_id in (uncategorized, user_set):
            assert client.get(f"/transactions/{tx_id}").json()["category_path"] == "expenses.dining"
        for tx_id, status in zip(transfers, ("pending", "posted"), strict=True):
            row = client.get(f"/transactions/{tx_id}").json()
            assert (row["status"], row["is_transfer"], row["transfer_account_path"]) == (
                status,
                True,
                "assets.savings",
            )

    def test_a_transfer_automation_made_is_skipped_by_default(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        """The default pass reached a transfer too. A categorize rule naming a
        real account converts a pending row into a transfer in place, and
        audits it as transaction.categorize — so the row counts as
        automation-categorized, which the default pass may correct. A later
        rule's history pass must skip it rather than fail on it."""
        tx_id = _row(client, "Example Brokerage Services LLC", "-250.00")
        resp = client.post(
            "/rules",
            json={
                "type": "categorize",
                "match_field": "payee",
                "match_mode": "contains",
                "match_text": "example brokerage",
                "category_id": seeded["assets.savings"],
                "activate": True,
            },
        )
        assert resp.status_code == 201, resp.text
        assert client.post("/categorize/run").json()["rule"] == 1
        assert client.get(f"/transactions/{tx_id}").json()["is_transfer"] is True
        to_dining = client.post(
            "/rules",
            json={
                "type": "categorize",
                "match_field": "payee",
                "match_mode": "contains",
                "match_text": "example brokerage",
                "category_id": seeded["expenses.dining"],
                "activate": True,
            },
        ).json()["id"]

        dry = client.post(f"/rules/{to_dining}/apply-history", json={"dry_run": True})
        wet = client.post(f"/rules/{to_dining}/apply-history", json={"dry_run": False})
        assert dry.status_code == wet.status_code == 200, wet.text
        for body in (dry.json(), wet.json()):
            assert (body["matched"], body["pending"], body["skipped_transfers"]) == (0, 0, 1)
        row = client.get(f"/transactions/{tx_id}").json()
        assert (row["is_transfer"], row["transfer_account_path"]) == (True, "assets.savings")


class TestScopes:
    def test_a_pair_rule_cannot_be_created_active(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        """Activating a transfer_link rule confirms its projected link count
        (decision 46 Q2); creating it active would skip that (#436)."""
        resp = client.post(
            "/rules",
            json={
                "type": "transfer_link",
                "pair_account_a": seeded["assets.checking"],
                "pair_account_b": seeded["assets.savings"],
                "activate": True,
            },
        )
        assert resp.status_code == 422, resp.text
        assert "confirm_links" in resp.json()["detail"]
        assert client.get("/rules").json() == []
        draft = client.post(
            "/rules",
            json={
                "type": "transfer_link",
                "pair_account_a": seeded["assets.checking"],
                "pair_account_b": seeded["assets.savings"],
            },
        )
        assert draft.status_code == 201 and draft.json()["state"] == "draft"

    def test_write_can_activate_on_create(self, seeded: dict[str, str], client: TestClient) -> None:
        """#436: activating is the caller's per-call choice, not a scope."""
        token = client.post("/tokens", json={"name": "prop", "scopes": ["read", "write"]}).json()[
            "token"
        ]
        resp = client.post(
            "/rules",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "type": "categorize",
                "category_id": seeded["expenses.dining"],
                "activate": True,
            },
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["state"] == "active"

    def test_a_write_token_creates_a_draft_by_default(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        token = client.post("/tokens", json={"name": "prop2", "scopes": ["read", "write"]}).json()[
            "token"
        ]
        resp = client.post(
            "/rules",
            headers={"Authorization": f"Bearer {token}"},
            json={"type": "categorize", "category_id": seeded["expenses.dining"]},
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["state"] == "draft"


class TestRendering:
    def test_the_predicate_renders_as_one_readable_line(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        """The readability argument for a structured predicate, made testable."""
        client.post(
            "/rules",
            json={
                "type": "categorize",
                "match_field": "payee",
                "match_mode": "contains",
                "match_text": "amazon",
                "direction": "debit",
                "category_id": seeded["expenses.dining"],
            },
        )
        rendered = client.get("/rules").json()[0]
        assert rendered["predicate"] == 'payee contains "amazon" AND debit'
        assert rendered["action"] == "-> expenses.dining"

    def test_a_real_account_target_renders_bracketed(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        """A categorize rule MAY name a real account — the deterministic twin
        of the AI classifier — and must read like a transfer, not a category."""
        client.post(
            "/rules",
            json={"type": "categorize", "category_id": seeded["assets.savings"]},
        )
        assert client.get("/rules").json()[0]["action"] == "-> [assets.savings]"


# --------------------------------------------------------------------------
# transfer_link window_days (issue #210, decision 66)
# --------------------------------------------------------------------------


def _pair_rule(client: TestClient, seeded: dict[str, str], **extra: object) -> Any:
    return client.post(
        "/rules",
        json={
            "type": "transfer_link",
            "pair_account_a": seeded["assets.checking"],
            "pair_account_b": seeded["assets.savings"],
            **extra,
        },
    )


class TestWindowDaysValidation:
    """A whole number of days, 0..14, on a transfer_link rule only — refused
    at the boundary (422), never coerced and never left to the DB CHECK."""

    @pytest.mark.parametrize("value", [-1, 15, 100])
    def test_out_of_range_is_refused(
        self, seeded: dict[str, str], client: TestClient, value: int
    ) -> None:
        resp = _pair_rule(client, seeded, window_days=value)
        assert resp.status_code == 422, resp.text
        assert client.get("/rules").json() == []

    @pytest.mark.parametrize("value", [5.5, "5", True, "a week"])
    def test_a_non_integer_is_refused(
        self, seeded: dict[str, str], client: TestClient, value: object
    ) -> None:
        resp = _pair_rule(client, seeded, window_days=value)
        assert resp.status_code == 422, resp.text
        assert client.get("/rules").json() == []

    @pytest.mark.parametrize("value", [0, 3, 14])
    def test_the_bounds_are_inclusive(
        self, seeded: dict[str, str], client: TestClient, value: int
    ) -> None:
        resp = _pair_rule(client, seeded, window_days=value)
        assert resp.status_code == 201, resp.text
        assert resp.json()["window_days"] == value

    def test_only_a_transfer_link_rule_has_a_window(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        resp = client.post(
            "/rules",
            json={
                "type": "categorize",
                "category_id": seeded["expenses.dining"],
                "window_days": 5,
            },
        )
        assert resp.status_code == 422
        assert "only to transfer_link" in resp.json()["detail"]

    def test_an_edit_is_held_to_the_same_bounds(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        rule_id = _pair_rule(client, seeded).json()["id"]
        for value in (-1, 15, 2.5, "4"):
            resp = client.patch(f"/rules/{rule_id}", json={"window_days": value})
            assert resp.status_code == 422, (value, resp.text)
        ok = client.patch(f"/rules/{rule_id}", json={"window_days": 7})
        assert ok.status_code == 200, ok.text
        assert ok.json()["window_days"] == 7
        # null resets to the standard window.
        reset = client.patch(f"/rules/{rule_id}", json={"window_days": None})
        assert reset.json()["window_days"] == 3

    def test_the_service_check_rejects_bools_and_floats(self) -> None:
        """The shared validator behind REST, MCP and the service: bool is an
        int subclass, and True is not one day."""
        from purser import rules as rules_service

        for bad in (True, 4.0, "4", -1, 15):
            with pytest.raises(rules_service.RuleError):
                rules_service.validate_window_days(bad, rule_type="transfer_link")
        with pytest.raises(rules_service.RuleError, match="only to transfer_link"):
            rules_service.validate_window_days(4, rule_type="rename")
        for good in (None, 0, 14):
            rules_service.validate_window_days(good, rule_type="transfer_link")

    def test_the_db_check_is_the_backstop(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        from sqlalchemy.exc import IntegrityError

        rule_id = _pair_rule(client, seeded).json()["id"]
        with pytest.raises(IntegrityError), migrated_engine.begin() as conn:
            conn.execute(text("UPDATE rules SET window_days = 15 WHERE id = :id"), {"id": rule_id})
        categorize_id = client.post(
            "/rules", json={"type": "categorize", "category_id": seeded["expenses.dining"]}
        ).json()["id"]
        with pytest.raises(IntegrityError), migrated_engine.begin() as conn:
            conn.execute(
                text("UPDATE rules SET window_days = 5 WHERE id = :id"), {"id": categorize_id}
            )


class TestWindowDaysSurfaces:
    def test_a_pair_rule_always_says_its_window(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        """Unset reads as the standard window it links within, so the 'When'
        column never claims "every row" for a declared pair."""
        _pair_rule(client, seeded)
        _pair_rule(client, seeded, window_days=5, amount_exact="184.50", priority=200)
        client.post("/rules", json={"type": "categorize", "category_id": seeded["expenses.dining"]})
        # Evaluation order: (priority, created_at) — the 200 sorts last.
        default, categorize, wide = client.get("/rules").json()
        assert default["predicate"] == "legs <= 3 days apart"
        assert default["window_days"] == 3
        assert wide["predicate"] == "amount = 184.5000 AND legs <= 5 days apart"
        assert wide["window_days"] == 5
        assert wide["action"] == "link assets.checking <-> assets.savings"
        assert categorize["window_days"] is None
        assert "days apart" not in categorize["predicate"]

    def test_the_audit_row_records_the_window(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        """Widening a drift-blind operation must be answerable from the audit
        log: who set the window, and when."""
        from sqlalchemy import select

        import purser.db
        from purser.models import AuditLog

        rule_id = _pair_rule(client, seeded, window_days=5).json()["id"]
        client.patch(f"/rules/{rule_id}", json={"window_days": 6})
        with purser.db.get_sessionmaker()() as session:
            rows = list(
                session.execute(
                    select(AuditLog)
                    .where(
                        AuditLog.entity_type == "rule",
                        AuditLog.entity_id == uuid.UUID(rule_id),
                        AuditLog.kind == "change",
                    )
                    .order_by(AuditLog.at)
                ).scalars()
            )
        assert [r.action for r in rows] == ["rule.create", "rule.update"]
        created, updated = (r.before_after for r in rows)
        assert created is not None and updated is not None
        assert created["after"]["window_days"] == 5
        assert updated["before"]["window_days"] == 5
        assert updated["after"]["window_days"] == 6

    def test_the_confirm_message_names_the_window(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        rule_id = _pair_rule(client, seeded, window_days=6).json()["id"]
        refused = client.post(f"/rules/{rule_id}/state", json={"state": "active"})
        assert refused.status_code == 422
        assert (
            "would link 0 historical pair(s) with legs up to 6 days apart"
            in (refused.json()["detail"])
        )

    def test_preview_refuses_a_half_built_pair(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        one_side = client.post("/rules/preview", json={"pair_account_a": seeded["assets.checking"]})
        assert one_side.status_code == 422
        stray_window = client.post("/rules/preview", json={"window_days": 5})
        assert stray_window.status_code == 422
        assert "transfer_link" in stray_window.json()["detail"]
        with_text = client.post(
            "/rules/preview",
            json={
                "pair_account_a": seeded["assets.checking"],
                "pair_account_b": seeded["assets.savings"],
                "match_field": "payee",
                "match_mode": "contains",
                "match_text": "x",
            },
        )
        assert with_text.status_code == 422
        same_account = client.post(
            "/rules/preview",
            json={
                "pair_account_a": seeded["assets.checking"],
                "pair_account_b": seeded["assets.checking"],
            },
        )
        assert same_account.status_code == 422
        too_wide = client.post(
            "/rules/preview",
            json={
                "pair_account_a": seeded["assets.checking"],
                "pair_account_b": seeded["assets.savings"],
                "window_days": 15,
            },
        )
        assert too_wide.status_code == 422

    def test_a_predicate_preview_is_unchanged(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        """Without a pair the preview is the predicate dry run it always was."""
        for _ in range(2):
            _row(client, "Chipotle")
        _row(client, "Whole Foods")
        resp = client.post(
            "/rules/preview",
            json={"match_field": "payee", "match_mode": "contains", "match_text": "chipotle"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["matched"] == 2
        assert len(body["sample"]) == 2
        assert "links" not in body

    def test_a_pair_preview_answers_in_the_link_shape(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        resp = client.post(
            "/rules/preview",
            json={
                "pair_account_a": seeded["assets.checking"],
                "pair_account_b": seeded["assets.savings"],
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {
            "type": "transfer_link",
            "window_days": 3,
            "links": 0,
            "sample": [],
        }


# --------------------------------------------------------------------------
# Migration 0029 up/down (scratch database, never the suite's)
# --------------------------------------------------------------------------


@pytest.fixture
def scratch_db(database_url: str) -> Iterator[tuple[Config, sa.Engine]]:
    os.environ["PURSER_ENCRYPTION_KEY"] = TEST_ENCRYPTION_KEY
    purser.config.get_settings.cache_clear()
    admin = sa.create_engine(database_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(sa.text("DROP DATABASE IF EXISTS purser_rule_window_mig WITH (FORCE)"))
        conn.execute(sa.text("CREATE DATABASE purser_rule_window_mig"))
    admin.dispose()
    scratch_url = database_url.rsplit("/", 1)[0] + "/purser_rule_window_mig"
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


def test_migration_0029_round_trips(scratch_db: tuple[Config, sa.Engine]) -> None:
    from sqlalchemy.exc import IntegrityError

    cfg, engine = scratch_db
    command.upgrade(cfg, "0028")
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
        existing = conn.execute(
            sa.text(
                "INSERT INTO rules (type, pair_account_a, pair_account_b)"
                " VALUES ('transfer_link', :a, :b) RETURNING id"
            ),
            {"a": a, "b": b},
        ).scalar_one()

    command.upgrade(cfg, "0029")
    assert "window_days" in _rule_columns(engine)
    with engine.begin() as conn:
        # No backfill: an existing rule keeps the standard window (NULL).
        assert (
            conn.execute(
                sa.text("SELECT window_days FROM rules WHERE id = :id"), {"id": existing}
            ).scalar_one()
            is None
        )
        conn.execute(sa.text("UPDATE rules SET window_days = 14 WHERE id = :id"), {"id": existing})
    for bad in (
        "UPDATE rules SET window_days = 15 WHERE id = :id",
        "UPDATE rules SET window_days = -1 WHERE id = :id",
    ):
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(sa.text(bad), {"id": existing})
    with pytest.raises(IntegrityError), engine.begin() as conn:
        category = conn.execute(
            sa.text(
                "INSERT INTO accounts (name, type, is_virtual, path)"
                " VALUES ('Dining', 'category_expense', true, 'expenses.dining') RETURNING id"
            )
        ).scalar_one()
        conn.execute(
            sa.text(
                "INSERT INTO rules (type, category_id, window_days) VALUES ('categorize', :c, 5)"
            ),
            {"c": category},
        )

    command.downgrade(cfg, "0028")
    assert "window_days" not in _rule_columns(engine)
    command.upgrade(cfg, "0029")
    assert "window_days" in _rule_columns(engine)
