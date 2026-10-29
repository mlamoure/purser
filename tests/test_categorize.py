"""AI categorization: deterministic memory, BYO providers, provenance,
corrections feeding back — and full function with AI off."""

import json
from datetime import date

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from purser import crypto


@pytest.fixture
def seeded(admin_client: TestClient) -> None:
    admin_client.post(
        "/accounts", json={"name": "Checking", "type": "checking", "path": "assets.checking"}
    )
    for name, path in [
        ("Uncategorized", "expenses.uncategorized"),
        ("Dining", "expenses.dining"),
        ("Groceries", "expenses.groceries"),
    ]:
        admin_client.post(
            "/categories", json={"name": name, "type": "category_expense", "path": path}
        )


def _pending(client: TestClient, payee: str, category: str = "expenses.uncategorized") -> str:
    resp = client.post(
        "/transactions",
        json={
            "account": "assets.checking",
            "category": category,
            "amount": "-25.00",
            "date": date.today().isoformat(),
            "payee": payee,
            "status": "pending",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _posted(client: TestClient, payee: str, category: str) -> None:
    resp = client.post(
        "/transactions",
        json={
            "account": "assets.checking",
            "category": category,
            "amount": "-20.00",
            "date": date.today().isoformat(),
            "payee": payee,
            "status": "posted",
        },
    )
    assert resp.status_code == 201


def _ai_env(monkeypatch: pytest.MonkeyPatch, provider: str, model: str = "test-model") -> None:
    import purser.config

    monkeypatch.setenv("PURSER_AI_PROVIDER", provider)
    monkeypatch.setenv("PURSER_AI_MODEL", model)
    monkeypatch.setenv("PURSER_AI_API_KEY", "test-key")
    purser.config.get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_settings():
    import purser.config

    yield
    purser.config.get_settings.cache_clear()


class TestAiOff:
    def test_fully_usable_with_no_provider(self, seeded: None, client: TestClient) -> None:
        assert client.get("/categorize/status").json() == {
            "provider": "none",
            "model": "",
            "configured": False,
        }
        _pending(client, "Mystery Shop")
        stats = client.post("/categorize/run").json()
        assert stats == {
            "rule": 0,
            "deterministic": 0,
            "ai": 0,
            "unresolved": 1,
        }


class TestDeterministicMemory:
    def test_accepted_history_categorizes_without_a_model(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        # Two accepted Chipotle transactions in dining = payee memory.
        _posted(client, "Chipotle", "expenses.dining")
        _posted(client, "Chipotle", "expenses.dining")
        tx_id = _pending(client, "Chipotle")

        stats = client.post("/categorize/run").json()
        assert stats["deterministic"] == 1
        detail = client.get(f"/transactions/{tx_id}").json()
        assert detail["category_path"] == "expenses.dining"

        with migrated_engine.connect() as conn:
            enc = conn.execute(
                text(
                    "SELECT before_after_enc FROM audit_log "
                    "WHERE action = 'transaction.categorize' AND entity_id = :id"
                ),
                {"id": tx_id},
            ).scalar_one()
            row = json.loads(crypto.decrypt(bytes(enc)))
        assert row["after"]["source"] == "deterministic"

    def test_single_occurrence_is_not_enough(self, seeded: None, client: TestClient) -> None:
        _posted(client, "New Cafe", "expenses.dining")  # only once
        _pending(client, "New Cafe")
        stats = client.post("/categorize/run").json()
        assert stats["unresolved"] == 1


def _mock_ai_transport(reply: str, seen: list[dict]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        if request.url.host == "api.anthropic.com":
            return httpx.Response(200, json={"content": [{"text": reply}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": reply}}]})

    return httpx.MockTransport(handler)


def _patch_http(monkeypatch: pytest.MonkeyPatch, transport: httpx.MockTransport) -> None:
    real_client = httpx.Client

    def patched(*args: object, **kwargs: object) -> httpx.Client:
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx, "Client", patched)


class TestProviders:
    @pytest.mark.parametrize("provider", ["openai", "anthropic"])
    def test_model_suggestion_lands_with_provenance(
        self,
        seeded: None,
        client: TestClient,
        migrated_engine: Engine,
        monkeypatch: pytest.MonkeyPatch,
        provider: str,
    ) -> None:
        _ai_env(monkeypatch, provider)
        seen: list[dict] = []
        _patch_http(
            monkeypatch,
            _mock_ai_transport('{"category": "expenses.groceries", "confidence": 0.87}', seen),
        )
        tx_id = _pending(client, "Trader Joes")
        stats = client.post("/categorize/run").json()
        assert stats["ai"] == 1

        detail = client.get(f"/transactions/{tx_id}").json()
        assert detail["category_path"] == "expenses.groceries"
        assert detail["status"] == "pending"  # normal review rules still apply

        with migrated_engine.connect() as conn:
            enc = conn.execute(
                text(
                    "SELECT before_after_enc FROM audit_log "
                    "WHERE action = 'transaction.categorize' AND entity_id = :id"
                ),
                {"id": tx_id},
            ).scalar_one()
            row = json.loads(crypto.decrypt(bytes(enc)))
        provenance = row["after"]
        assert provenance["source"] == "ai"
        assert provenance["model"] == "test-model"
        assert provenance["provider"] == provider
        assert provenance["confidence"] == 0.87
        # The model only ever saw the category list + one transaction.
        assert seen and "expenses.groceries" in json.dumps(seen[0])

    def test_out_of_list_reply_is_rejected(
        self, seeded: None, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _ai_env(monkeypatch, "openai")
        _patch_http(
            monkeypatch,
            _mock_ai_transport('{"category": "expenses.made_up", "confidence": 0.9}', []),
        )
        _pending(client, "Trader Joes")
        stats = client.post("/categorize/run").json()
        assert stats["unresolved"] == 1


class TestCorrectionsFeedback:
    def test_correction_improves_deterministic_hit_rate(
        self, seeded: None, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _ai_env(monkeypatch, "openai")
        _patch_http(
            monkeypatch,
            _mock_ai_transport('{"category": "expenses.dining", "confidence": 0.6}', []),
        )
        # Model (wrongly) says dining; the human corrects both to groceries
        # and accepts them (posts). Only posted history seeds payee memory.
        first = _pending(client, "Corner Market")
        second = _pending(client, "Corner Market")
        client.post("/categorize/run")
        for tx in (first, second):
            client.patch(f"/transactions/{tx}", json={"category": "expenses.groceries"})
            assert client.post(f"/transactions/{tx}/post").status_code == 200

        # Next time the same payee shows up, the accepted history wins —
        # deterministic, no model call needed.
        third = _pending(client, "Corner Market")
        stats = client.post("/categorize/run").json()
        assert stats["deterministic"] == 1
        assert stats["ai"] == 0
        assert client.get(f"/transactions/{third}").json()["category_path"] == "expenses.groceries"

    def test_pending_ai_guesses_do_not_seed_deterministic_memory(
        self, seeded: None, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two unreviewed AI suggestions must not launder into 'deterministic'
        for the third transaction — payee memory is posted-only."""
        _ai_env(monkeypatch, "openai")
        _patch_http(
            monkeypatch,
            _mock_ai_transport('{"category": "expenses.dining", "confidence": 0.9}', []),
        )
        _pending(client, "Laundry Co")
        _pending(client, "Laundry Co")
        stats = client.post("/categorize/run").json()
        assert stats["ai"] == 2  # both suggestions land, still pending

        # Provider off: with no accepted history, the third stays unresolved.
        import purser.config

        monkeypatch.setenv("PURSER_AI_PROVIDER", "none")
        purser.config.get_settings.cache_clear()
        third = _pending(client, "Laundry Co")
        stats = client.post("/categorize/run").json()
        assert stats == {
            "rule": 0,
            "deterministic": 0,
            "ai": 0,
            "unresolved": 1,
        }
        detail = client.get(f"/transactions/{third}").json()
        assert detail["category_path"] == "expenses.uncategorized"


class TestProviderFailures:
    """A failing provider never breaks the run: deterministic results
    survive and are audit-recorded; affected rows count as unresolved."""

    def _seed_mixed_work(self, client: TestClient) -> str:
        # Accepted history for Chipotle (deterministic hit); none for Trader Joes.
        _posted(client, "Chipotle", "expenses.dining")
        _posted(client, "Chipotle", "expenses.dining")
        deterministic_tx = _pending(client, "Chipotle")
        _pending(client, "Trader Joes")
        return deterministic_tx

    def _assert_deterministic_survived(
        self, client: TestClient, migrated_engine: Engine, tx_id: str
    ) -> None:
        detail = client.get(f"/transactions/{tx_id}").json()
        assert detail["category_path"] == "expenses.dining"
        with migrated_engine.connect() as conn:
            enc = conn.execute(
                text(
                    "SELECT before_after_enc FROM audit_log "
                    "WHERE action = 'transaction.categorize' AND entity_id = :id"
                ),
                {"id": tx_id},
            ).scalar_one()
            row = json.loads(crypto.decrypt(bytes(enc)))
        assert row["after"]["source"] == "deterministic"

    def test_http_500_counts_unresolved_and_keeps_deterministic(
        self,
        seeded: None,
        client: TestClient,
        migrated_engine: Engine,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _ai_env(monkeypatch, "openai")
        _patch_http(monkeypatch, httpx.MockTransport(lambda request: httpx.Response(500)))
        deterministic_tx = self._seed_mixed_work(client)

        resp = client.post("/categorize/run")
        assert resp.status_code == 200  # provider failure never surfaces as a 500
        assert resp.json() == {
            "rule": 0,
            "deterministic": 1,
            "ai": 0,
            "unresolved": 1,
        }
        self._assert_deterministic_survived(client, migrated_engine, deterministic_tx)

    def test_connection_error_stops_after_first_failure(
        self,
        seeded: None,
        client: TestClient,
        migrated_engine: Engine,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _ai_env(monkeypatch, "openai")
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            raise httpx.ConnectError("connection refused")

        _patch_http(monkeypatch, httpx.MockTransport(handler))
        deterministic_tx = self._seed_mixed_work(client)
        _pending(client, "Corner Market")  # a second AI candidate

        resp = client.post("/categorize/run")
        assert resp.status_code == 200
        assert resp.json() == {
            "rule": 0,
            "deterministic": 1,
            "ai": 0,
            "unresolved": 2,
        }
        # A down provider costs ONE failed call, not one per transaction.
        assert len(calls) == 1
        self._assert_deterministic_survived(client, migrated_engine, deterministic_tx)

    def test_malformed_envelope_counts_unresolved(
        self,
        seeded: None,
        client: TestClient,
        migrated_engine: Engine,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _ai_env(monkeypatch, "openai")
        _patch_http(
            monkeypatch,
            httpx.MockTransport(lambda request: httpx.Response(200, json={"weird": True})),
        )
        deterministic_tx = self._seed_mixed_work(client)

        resp = client.post("/categorize/run")
        assert resp.status_code == 200
        assert resp.json() == {
            "rule": 0,
            "deterministic": 1,
            "ai": 0,
            "unresolved": 1,
        }
        self._assert_deterministic_survived(client, migrated_engine, deterministic_tx)


class TestBornCategorizedImports:
    """Issue #111: the deterministic pass runs at import — rows are born
    categorized when payee memory knows the payee."""

    def _import_row(self, migrated_engine: Engine, payee: str, *, tx_hash: str) -> str:
        """Run the importer's creation path directly (no feed harness needed)."""
        import purser.db
        from purser import ledger
        from purser.core import ledger as core
        from purser.feed.importer import category_for_new_row

        with purser.db.get_sessionmaker()() as session:
            account = ledger.account_by_path(session, "assets.checking")
            category, from_memory = category_for_new_row(session, payee_name=payee, income=False)
            tx = ledger.create_flat_transaction(
                session,
                account_ref=str(account.id),
                category_ref=str(category.id),
                amount=core.quantize(__import__("decimal").Decimal("-25.00")),
                date=date.today(),
                payee_name=payee,
                requested_status=core.Status.POSTED,
                origin=core.Origin.IMPORT,
                token=None,
                content_hash=tx_hash,
            )
            session.commit()
            assert from_memory in (True, False)
            return str(tx.id)

    def test_known_payee_imports_born_categorized(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        _posted(client, "Chipotle", "expenses.dining")
        _posted(client, "Chipotle", "expenses.dining")
        tx_id = self._import_row(migrated_engine, "Chipotle", tx_hash="h-born-1")
        detail = client.get(f"/transactions/{tx_id}").json()
        assert detail["category_path"] == "expenses.dining"
        assert detail["status"] == "posted"  # born categorized — no correction chain
        assert detail["reverses_id"] is None

    def test_single_occurrence_stays_uncategorized_at_import(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        _posted(client, "One Timer", "expenses.dining")  # only once — below threshold
        tx_id = self._import_row(migrated_engine, "One Timer", tx_hash="h-born-2")
        assert (
            client.get(f"/transactions/{tx_id}").json()["category_path"] == "expenses.uncategorized"
        )

    def test_pending_history_never_seeds_import_categorization(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        """The laundering guard extends to import time: unreviewed pendings
        must not become 'deterministic' answers."""
        _pending(client, "Fresh Payee")
        _pending(client, "Fresh Payee")
        tx_id = self._import_row(migrated_engine, "Fresh Payee", tx_hash="h-born-3")
        assert (
            client.get(f"/transactions/{tx_id}").json()["category_path"] == "expenses.uncategorized"
        )


class TestPostedCoverage:
    """Issue #111: the categorization pass covers posted rows via the
    standard reversal+replacement correction."""

    def test_posted_row_corrected_with_rule_origin(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        _posted(client, "Chipotle", "expenses.dining")
        _posted(client, "Chipotle", "expenses.dining")
        stale_id = _pending(client, "Chipotle")
        # Post it while still uncategorized — the pre-0.5.0 gap.
        assert client.post(f"/transactions/{stale_id}/post").status_code == 200

        stats = client.post("/categorize/run").json()
        assert stats["deterministic"] == 1

        # The original is corrected exactly once; the chain head carries the
        # category and origin=rule; balances stay exact.
        page = client.get(
            "/transactions", params={"account": "assets.checking", "status": "posted"}
        ).json()
        head = next(
            t
            for t in page["items"]
            if t["category_path"] == "expenses.dining" and t["origin"] == "rule"
        )
        assert head["id"] != stale_id
        with migrated_engine.connect() as conn:
            balance = conn.execute(
                text(
                    "SELECT COALESCE(SUM(p.amount),0) FROM postings p"
                    " JOIN accounts a ON a.id = p.account_id"
                    " JOIN transactions t ON t.id = p.transaction_id"
                    " WHERE a.path = 'assets.checking' AND t.status IN ('pending','posted')"
                )
            ).scalar_one()
        assert str(balance) == "-65.0000"  # 2x -20 posted + corrected -25

        # A second run never re-corrects.
        again = client.post("/categorize/run").json()
        assert again["deterministic"] == 0

    def test_a_write_token_corrects_posted_rows_too(self, seeded: None, client: TestClient) -> None:
        """#436: an agent's run acts with the user's authority — a posted row
        is corrected like the scheduled pass corrects it."""
        _posted(client, "Chipotle", "expenses.dining")
        _posted(client, "Chipotle", "expenses.dining")
        stale_id = _pending(client, "Chipotle")
        assert client.post(f"/transactions/{stale_id}/post").status_code == 200

        token = client.post("/tokens", json={"name": "agent", "scopes": ["read", "write"]}).json()[
            "token"
        ]
        stats = client.post("/categorize/run", headers={"Authorization": f"Bearer {token}"}).json()
        assert "skipped_posted" not in stats
        assert stats["deterministic"] == 1

    def test_category_source_surfaces_with_provenance_param(
        self, seeded: None, client: TestClient
    ) -> None:
        _posted(client, "Chipotle", "expenses.dining")
        _posted(client, "Chipotle", "expenses.dining")
        pending_id = _pending(client, "Chipotle")
        client.post("/categorize/run")

        plain = client.get("/transactions", params={"account": "assets.checking"}).json()
        assert all(t["category_source"] is None for t in plain["items"])

        page = client.get(
            "/transactions",
            params={"account": "assets.checking", "with_category_provenance": "true"},
        ).json()
        row = next(t for t in page["items"] if t["id"] == pending_id)
        assert row["category_source"] == "deterministic"


# --------------------------------------------------------------------------
# Transfer categorization (issue #183, DESIGN §11.1 / decision 49)
# --------------------------------------------------------------------------


def _real_account(client: TestClient, name: str, path: str, type_: str = "savings") -> str:
    resp = client.post("/accounts", json={"name": name, "type": type_, "path": path})
    assert resp.status_code == 201, resp.text
    account_id: str = resp.json()["id"]
    return account_id


class TestTransferCategorization:
    """Before #183 a transfer row had NO correct answer available: §3.2 omits
    transfer categories because the category FIELD carries them, and the
    option list was virtual-accounts-only. The classifier could only mislabel
    the row or leave it uncategorized."""

    def test_option_list_offers_real_accounts_and_omits_the_rows_own(
        self, seeded: None, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _real_account(client, "Savings", "assets.savings")
        hidden = _real_account(client, "Old Card", "liabilities.oldcard", type_="credit_card")
        assert client.patch(f"/accounts/{hidden}", json={"hidden": True}).status_code == 200

        _ai_env(monkeypatch, "openai")
        seen: list[dict] = []
        _patch_http(
            monkeypatch,
            _mock_ai_transport('{"category": "expenses.groceries", "confidence": 0.9}', seen),
        )
        _pending(client, "Trader Joes")
        client.post("/categorize/run")

        prompt = json.dumps(seen[0])
        assert "[assets.savings]" in prompt
        # A transfer to itself is not a legal answer.
        assert "[assets.checking]" not in prompt
        # Hidden accounts stay out of the option list.
        assert "[liabilities.oldcard]" not in prompt

    def test_bracket_reply_converts_the_row_to_a_transfer(
        self, seeded: None, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _real_account(client, "Savings", "assets.savings")
        _ai_env(monkeypatch, "openai")
        _patch_http(
            monkeypatch,
            _mock_ai_transport('{"category": "[assets.savings]", "confidence": 0.95}', []),
        )
        tx_id = _pending(client, "Online Transfer")
        stats = client.post("/categorize/run").json()
        assert stats["ai"] == 1

        detail = client.get(f"/transactions/{tx_id}").json()
        assert detail["is_transfer"] is True
        assert detail["transfer_account_path"] == "assets.savings"
        # Two REAL legs, no virtual one — this is the shape recategorize_posted
        # cannot produce, which is why _apply_category has to delegate.
        paths = sorted(p["account_path"] for p in detail["postings"])
        assert paths == ["assets.checking", "assets.savings"]

    def test_virtual_reply_still_takes_the_leg_swap_path(
        self, seeded: None, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _real_account(client, "Savings", "assets.savings")
        _ai_env(monkeypatch, "openai")
        _patch_http(
            monkeypatch,
            _mock_ai_transport('{"category": "expenses.groceries", "confidence": 0.9}', []),
        )
        tx_id = _pending(client, "Trader Joes")
        client.post("/categorize/run")

        detail = client.get(f"/transactions/{tx_id}").json()
        assert detail["category_path"] == "expenses.groceries"
        assert detail["is_transfer"] is False

    def test_bracket_below_the_confidence_bar_leaves_the_row_uncategorized(
        self, seeded: None, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bracket answer restructures the row and moves a second account's
        balance, and the merge it may trigger is balance-neutral so §7.3
        cannot detect a bad one. The bar stands in for that missing check."""
        from purser.categorize import BRACKET_MIN_CONFIDENCE

        _real_account(client, "Savings", "assets.savings")
        _ai_env(monkeypatch, "openai")
        low = BRACKET_MIN_CONFIDENCE - 0.1
        _patch_http(
            monkeypatch,
            _mock_ai_transport(json.dumps({"category": "[assets.savings]", "confidence": low}), []),
        )
        tx_id = _pending(client, "Online Transfer")
        stats = client.post("/categorize/run").json()
        assert stats["unresolved"] == 1
        assert stats["ai"] == 0

        detail = client.get(f"/transactions/{tx_id}").json()
        assert detail["is_transfer"] is False
        assert detail["category_path"] == "expenses.uncategorized"

    def test_an_ordinary_category_is_not_held_to_the_bracket_bar(
        self, seeded: None, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The higher bar applies to bracket answers ONLY — an ordinary
        category at the same confidence must still land."""
        from purser.categorize import BRACKET_MIN_CONFIDENCE

        _real_account(client, "Savings", "assets.savings")
        _ai_env(monkeypatch, "openai")
        low = BRACKET_MIN_CONFIDENCE - 0.1
        _patch_http(
            monkeypatch,
            _mock_ai_transport(
                json.dumps({"category": "expenses.groceries", "confidence": low}), []
            ),
        )
        _pending(client, "Trader Joes")
        stats = client.post("/categorize/run").json()
        assert stats["ai"] == 1

    def test_a_bracket_naming_no_real_account_is_rejected(
        self, seeded: None, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _real_account(client, "Savings", "assets.savings")
        _ai_env(monkeypatch, "openai")
        _patch_http(
            monkeypatch,
            _mock_ai_transport('{"category": "[assets.imaginary]", "confidence": 0.99}', []),
        )
        _pending(client, "Online Transfer")
        stats = client.post("/categorize/run").json()
        assert stats["unresolved"] == 1

    def test_the_system_path_will_not_merge_a_pending_counterpart(
        self, seeded: None, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DESIGN §11 rule 5: the model names the ACCOUNT; which rows pair
        stays deterministic, and `auto_link_pass` is posted-only on purpose —
        a bank-pending row can come back posted under a different description,
        and consuming it in a merge first turns that rehash into a duplicated
        leg. The explicit user conversion may still merge a pending row."""
        _real_account(client, "Savings", "assets.savings")
        counterpart = client.post(
            "/transactions",
            json={
                "account": "assets.savings",
                "category": "expenses.uncategorized",
                "amount": "25.00",
                "date": date.today().isoformat(),
                "payee": "Online Transfer",
                "status": "pending",
            },
        )
        assert counterpart.status_code == 201, counterpart.text
        counterpart_id = counterpart.json()["id"]

        _ai_env(monkeypatch, "openai")
        _patch_http(
            monkeypatch,
            _mock_ai_transport('{"category": "[assets.savings]", "confidence": 0.99}', []),
        )
        tx_id = _pending(client, "Online Transfer")
        assert client.post(f"/transactions/{tx_id}/post").status_code == 200
        client.post("/categorize/run")

        # The row became a one-sided transfer of its own...
        detail = client.get(f"/transactions/{tx_id}").json()
        assert detail["status"] in {"posted", "void"}
        # ...and the pending counterpart was NOT consumed.
        other = client.get(f"/transactions/{counterpart_id}").json()
        assert other["status"] == "pending"
        assert other["is_transfer"] is False

    def test_the_system_path_respects_unlink_suppression(
        self, seeded: None, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`_absorb_target_for_convert` documents "explicit user intent: no
        unlink suppression" — true for a human typing [account], false for an
        automated pass, which would otherwise re-merge a pair the user just
        unlinked on every sync."""
        _real_account(client, "Savings", "assets.savings")
        out_id = _pending(client, "Online Transfer")
        assert client.post(f"/transactions/{out_id}/post").status_code == 200
        incoming = client.post(
            "/transactions",
            json={
                "account": "assets.savings",
                "category": "expenses.uncategorized",
                "amount": "25.00",
                "date": date.today().isoformat(),
                "payee": "Online Transfer",
                "status": "posted",
            },
        )
        assert incoming.status_code == 201, incoming.text
        in_id = incoming.json()["id"]

        merged = client.post(
            "/matching/confirm-transfer", json={"outgoing_id": out_id, "incoming_id": in_id}
        )
        assert merged.status_code == 200, merged.text
        merged_id = merged.json()["merged_transaction_id"]
        assert client.post(f"/transactions/{merged_id}/unlink-transfer").status_code == 200

        # Unlink resurrects each voided original as a fresh CLONE with a new
        # id (decision 29 — the content_hash moves onto it), so capture the
        # live rows rather than reusing the pre-merge ids.
        def live_savings() -> set[str]:
            page = client.get(
                "/transactions", params={"account": "assets.savings", "status": ["posted"]}
            ).json()
            return {t["id"] for t in page["items"]}

        before = live_savings()
        assert len(before) == 1

        _ai_env(monkeypatch, "openai")
        _patch_http(
            monkeypatch,
            _mock_ai_transport('{"category": "[assets.savings]", "confidence": 0.99}', []),
        )
        client.post("/categorize/run")

        # A merge would VOID the savings clone, dropping it out of the live
        # set. It survives: the checking clone took the ordinary posted
        # conversion (reversal + transfer-shaped replacement) instead, which
        # is why the set grows by that replacement's savings leg rather than
        # staying equal.
        assert before <= live_savings(), (
            "the automated pass re-consumed a pair the user explicitly unlinked"
        )
