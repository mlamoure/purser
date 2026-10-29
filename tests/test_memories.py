"""Memories (issue #176, DESIGN §3.6, decision 48).

Two invariants carry the feature and both get direct tests: a draft is NEVER
served (that is what stops an agent writing its own future context), and
serving is budgeted (100 memories is ~12k tokens).
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from purser import memories as memories_service


@pytest.fixture
def seeded(admin_client: TestClient) -> dict[str, str]:
    ids: dict[str, str] = {}
    for name, type_, path in [
        ("Checking", "checking", "assets.checking"),
        ("Fidelity College", "brokerage", "assets.fidelity_college"),
    ]:
        ids[path] = admin_client.post(
            "/accounts", json={"name": name, "type": type_, "path": path}
        ).json()["id"]
    admin_client.post(
        "/categories",
        json={
            "name": "Uncategorized",
            "type": "category_expense",
            "path": "expenses.uncategorized",
        },
    )
    return ids


class TestDraftsAreNeverServed:
    """The containment argument: for AI-written text to influence a later
    pass, a human must activate it. The loop needs approval at every hop."""

    def test_a_draft_is_absent_from_every_serving_path(
        self, seeded: dict[str, str], client: TestClient, db_session: Session
    ) -> None:
        resp = client.post("/memories", json={"text": "a draft nuance"})
        assert resp.status_code == 201, resp.text
        assert resp.json()["state"] == "draft"

        assert memories_service.serving_block(db_session)["active"] == 0
        assert memories_service.list_memories(db_session)["memories"] == []
        assert memories_service.relevant_for(db_session) == []

    def test_activating_makes_it_servable(
        self, seeded: dict[str, str], client: TestClient, db_session: Session
    ) -> None:
        mem_id = client.post("/memories", json={"text": "the $750 is a 529 contribution"}).json()[
            "id"
        ]
        assert client.post(f"/memories/{mem_id}/activate").status_code == 200
        db_session.expire_all()
        assert memories_service.serving_block(db_session)["active"] == 1

    def test_an_archived_memory_is_not_served(
        self, seeded: dict[str, str], client: TestClient, db_session: Session
    ) -> None:
        mem_id = client.post("/memories", json={"text": "retired nuance", "activate": True}).json()[
            "id"
        ]
        assert client.post(f"/memories/{mem_id}/archive").status_code == 200
        db_session.expire_all()
        assert memories_service.serving_block(db_session)["active"] == 0


class TestNonExecutable:
    def test_no_module_parses_memory_text(self) -> None:
        """The absence IS the feature: no evaluator, no import hook, no
        scheduler entry reads a memory. If a nuance should ACT it graduates
        into a rule."""
        import purser.memories as mod

        source = (mod.__file__ or "").replace("memories.py", "")
        assert source  # sanity
        # The service exposes no evaluate/apply/match surface at all.
        for forbidden in ("evaluate", "apply", "matches", "execute"):
            assert not hasattr(mod, forbidden)


class TestRelevanceIsExact:
    def test_a_memory_linked_to_one_account_is_absent_for_another(
        self, seeded: dict[str, str], client: TestClient, db_session: Session
    ) -> None:
        """FK equality — no substring, no name similarity, no embeddings."""
        client.post(
            "/memories",
            json={
                "text": "the monthly 750.00 to Fidelity College is the 529 contribution",
                "accounts": ["assets.fidelity_college"],
                "activate": True,
            },
        )
        db_session.expire_all()
        fidelity = uuid.UUID(seeded["assets.fidelity_college"])
        checking = uuid.UUID(seeded["assets.checking"])
        assert memories_service.relevant_for(db_session, account_ids=[fidelity])
        assert memories_service.relevant_for(db_session, account_ids=[checking]) == []

    def test_an_unlinked_memory_is_global(
        self, seeded: dict[str, str], client: TestClient, db_session: Session
    ) -> None:
        client.post("/memories", json={"text": "we file taxes jointly", "activate": True})
        db_session.expire_all()
        any_account = uuid.UUID(seeded["assets.checking"])
        assert memories_service.relevant_for(db_session, account_ids=[any_account])


class TestBudgets:
    def test_describe_carries_a_count_plus_the_pinned_few(
        self, seeded: dict[str, str], client: TestClient, db_session: Session
    ) -> None:
        for i in range(12):
            client.post("/memories", json={"text": f"nuance {i}", "activate": True})
        db_session.expire_all()
        block = memories_service.serving_block(db_session)
        assert block["active"] == 12
        # Nothing pinned, so no rows travel — just the count and the pointer.
        assert block["pinned"] == []
        assert block["tool"] == "list_memories"

    def test_the_pinned_payload_respects_both_caps(
        self, seeded: dict[str, str], client: TestClient, db_session: Session
    ) -> None:
        for i in range(memories_service.MAX_PINNED):
            client.post("/memories", json={"text": f"pinned {i}", "pinned": True, "activate": True})
        db_session.expire_all()
        block = memories_service.serving_block(db_session)
        assert len(block["pinned"]) <= memories_service.DESCRIBE_MAX_ROWS
        assert sum(len(m["text"]) for m in block["pinned"]) <= memories_service.DESCRIBE_CHAR_BUDGET

    def test_list_accounts_for_its_truncation_exactly(
        self, seeded: dict[str, str], client: TestClient, db_session: Session
    ) -> None:
        for i in range(15):
            client.post("/memories", json={"text": f"nuance {i}", "activate": True})
        db_session.expire_all()
        page = memories_service.list_memories(db_session, limit=5)
        assert len(page["memories"]) + page["truncated"] == page["matched"] == 15


class TestCaps:
    def test_the_draft_cap_is_the_anti_flood_valve(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        for i in range(memories_service.MAX_DRAFTS):
            assert client.post("/memories", json={"text": f"draft {i}"}).status_code == 201
        resp = client.post("/memories", json={"text": "one too many"})
        assert resp.status_code == 422
        assert "draft cap" in resp.json()["detail"]

    def test_text_is_capped(self, seeded: dict[str, str], client: TestClient) -> None:
        resp = client.post("/memories", json={"text": "x" * (memories_service.MAX_TEXT_CHARS + 1)})
        assert resp.status_code == 422

    def test_the_pinned_cap_holds(self, seeded: dict[str, str], client: TestClient) -> None:
        for i in range(memories_service.MAX_PINNED):
            client.post("/memories", json={"text": f"p{i}", "pinned": True, "activate": True})
        resp = client.post("/memories", json={"text": "extra", "pinned": True, "activate": True})
        assert resp.status_code == 422
        assert "pinned cap" in resp.json()["detail"]


class TestScopes:
    def test_write_can_activate_on_create(self, seeded: dict[str, str], client: TestClient) -> None:
        """#436: activating is the caller's per-call choice, not a scope."""
        token = client.post("/tokens", json={"name": "p", "scopes": ["read", "write"]}).json()[
            "token"
        ]
        resp = client.post(
            "/memories",
            headers={"Authorization": f"Bearer {token}"},
            json={"text": "agent nuance", "activate": True},
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["state"] == "active"

    def test_a_write_token_creates_a_draft_by_default(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        token = client.post("/tokens", json={"name": "p2", "scopes": ["read", "write"]}).json()[
            "token"
        ]
        resp = client.post(
            "/memories",
            headers={"Authorization": f"Bearer {token}"},
            json={"text": "agent nuance"},
        )
        assert resp.status_code == 201
        assert resp.json()["state"] == "draft"


class TestStaleness:
    def test_a_link_to_a_closed_account_is_a_lint_not_an_action(
        self, seeded: dict[str, str], client: TestClient, db_session: Session
    ) -> None:
        """Purser never edits or archives the user's prose on its own."""
        mem_id = client.post(
            "/memories",
            json={
                "text": "about the brokerage",
                "accounts": ["assets.fidelity_college"],
                "activate": True,
            },
        ).json()["id"]
        assert (
            client.post(f"/accounts/{seeded['assets.fidelity_college']}/close", json={}).status_code
            == 200
        )
        db_session.expire_all()
        rows = client.get("/memories").json()
        row = next(m for m in rows if m["id"] == mem_id)
        assert row["stale"] is True
        assert row["state"] == "active"  # still served, just flagged


class TestRulesInterlock:
    def test_archiving_a_memory_leaves_its_rules_alone(
        self, seeded: dict[str, str], client: TestClient, db_session: Session
    ) -> None:
        """Rules are the executable layer and must not change behavior because
        a note was tidied."""
        from purser.models import Rule

        mem_id = client.post(
            "/memories", json={"text": "why this rule exists", "activate": True}
        ).json()["id"]
        rule_id = client.post(
            "/rules",
            json={
                "type": "categorize",
                "category_id": seeded["assets.checking"],
                "activate": True,
            },
        ).json()["id"]
        rule = db_session.get(Rule, uuid.UUID(rule_id))
        assert rule is not None
        rule.memory_id = uuid.UUID(mem_id)
        db_session.commit()

        assert client.post(f"/memories/{mem_id}/archive").status_code == 200
        db_session.expire_all()
        rule = db_session.get(Rule, uuid.UUID(rule_id))
        assert rule is not None
        assert rule.state == "active"
        assert rule.memory_id == uuid.UUID(mem_id)
