# ruff: noqa: F811  (pytest fixtures re-exported from sibling modules)
"""The write scope (issue #436): the agent acts with the user's authority.

`propose` and `commit` are one `write` scope; a write principal's requested
status is honoured; proposing is a per-call choice (`activate` on rules and
memories, `propose` on schedules), with drafts as the default.
"""

from fastapi.testclient import TestClient

from tests.test_mcp import (  # noqa: F401 (fixture re-export)
    TODAY,
    LiveServer,
    _mcp_call,
    live_server,
    seeded_tokens,
)


class TestScopes:
    def test_a_retired_scope_is_refused_naming_write(self, admin_client: TestClient) -> None:
        for retired in ("propose", "commit"):
            resp = admin_client.post("/tokens", json={"name": "old", "scopes": ["read", retired]})
            assert resp.status_code == 422, resp.text
            assert "use 'write'" in resp.text

    def test_the_default_agent_token_is_read_plus_write(self, admin_client: TestClient) -> None:
        resp = admin_client.post("/tokens", json={"name": "agent"})
        assert resp.status_code == 201, resp.text
        assert resp.json()["scopes"] == ["read", "write"]

    def test_a_read_only_token_cannot_write_over_rest(self, admin_client: TestClient) -> None:
        raw = admin_client.post("/tokens", json={"name": "r", "scopes": ["read"]}).json()["token"]
        resp = admin_client.post(
            "/rules",
            json={"type": "categorize", "match_text": "x"},
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "requires scope: write"


class TestAgentWrites:
    def test_a_write_principals_requested_status_is_honoured(
        self, seeded_tokens: dict[str, str], live_server: LiveServer, client: TestClient
    ) -> None:
        """effective_status is gone: an agent asking for posted gets posted,
        and the default stays pending (the bank has not posted it)."""
        row = {
            "account": "assets.checking",
            "category": "expenses.dining",
            "amount": "-12.50",
            "date_": TODAY.isoformat(),
        }
        posted, pending = _mcp_call(
            live_server.url,
            seeded_tokens["agent"],
            [("create_transaction", {**row, "status": "posted"}), ("create_transaction", row)],
        )
        assert posted["status"] == "posted", posted
        assert pending["status"] == "pending", pending
        detail = client.get(f"/transactions/{posted['id']}").json()
        assert detail["origin"] == "agent"

    def test_propose_rule_is_a_draft_unless_activated(
        self, seeded_tokens: dict[str, str], live_server: LiveServer
    ) -> None:
        rule = {
            "type": "categorize",
            "category": "expenses.dining",
            "match_field": "payee",
            "match_mode": "contains",
            "match_text": "CAFE",
        }
        draft, active = _mcp_call(
            live_server.url,
            seeded_tokens["agent"],
            [
                ("propose_rule", rule),
                ("propose_rule", {**rule, "match_text": "BISTRO", "activate": True}),
            ],
        )
        assert draft["state"] == "draft", draft
        assert draft["note"].startswith("draft")
        assert active["state"] == "active", active
        assert active["note"].startswith("active")

    def test_propose_memory_is_a_draft_unless_activated(
        self, seeded_tokens: dict[str, str], live_server: LiveServer
    ) -> None:
        draft, active, listed = _mcp_call(
            live_server.url,
            seeded_tokens["agent"],
            [
                ("propose_memory", {"text": "The checking account is the household float."}),
                (
                    "propose_memory",
                    {"text": "Dining over 100.00 is a client dinner.", "activate": True},
                ),
                ("list_memories", {}),
            ],
        )
        assert draft["state"] == "draft", draft
        assert active["state"] == "active", active
        assert active["note"].startswith("active")
        served = {m["id"] for m in listed["memories"]}
        assert active["id"] in served and draft["id"] not in served

    def test_create_schedule_is_active_unless_proposed(
        self, seeded_tokens: dict[str, str], live_server: LiveServer, client: TestClient
    ) -> None:
        base = {
            "account": "assets.checking",
            "rrule": "FREQ=MONTHLY;BYMONTHDAY=9",
            "amount": "-40.00",
            "category": "expenses.dining",
        }
        direct, proposed = _mcp_call(
            live_server.url,
            seeded_tokens["agent"],
            [
                ("create_schedule", {**base, "name": "Streaming", "payee": "Example Stream"}),
                (
                    "create_schedule",
                    {**base, "name": "Gym", "payee": "Example Gym", "propose": True},
                ),
            ],
        )
        assert direct["status"] == "active", direct
        assert proposed["status"] == "proposed", proposed
        (card,) = client.get("/suggestions").json()
        assert card["id"] == proposed["suggestion_id"]
