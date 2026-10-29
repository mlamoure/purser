"""Admin runtime configuration: /config API, env-default vs DB-override
precedence, write-only secret handling, audit, and effective consumption
by the code paths that read these settings."""

import json
from collections.abc import Iterator
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

import purser.config
from purser import appsettings
from purser.appsettings import SyncSchedule


@pytest.fixture(autouse=True)
def fresh_settings_cache() -> Iterator[None]:
    """Tests here monkeypatch PURSER_* env vars; never leak a cached Settings
    (or cached overrides) into neighbouring tests."""
    yield
    purser.config.get_settings.cache_clear()
    appsettings.invalidate()


def _agent_headers(client: TestClient, scopes: list[str]) -> dict[str, str]:
    resp = client.post("/tokens", json={"name": "agent", "scopes": scopes})
    assert resp.status_code == 201, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


class TestSyncScheduleModel:
    def test_daily_defaults_to_0630(self) -> None:
        assert SyncSchedule().to_cron() == {"hour": 6, "minute": 30}

    def test_daily_custom_time(self) -> None:
        cron = SyncSchedule(preset="daily", hour=21, minute=0).to_cron()
        assert cron == {"hour": 21, "minute": 0}

    def test_interval_preset_anchors(self) -> None:
        assert SyncSchedule(preset="every_12h").to_cron() == {"hour": "6-23/12", "minute": 30}
        assert SyncSchedule(preset="every_6h").to_cron() == {"hour": "0-23/6", "minute": 30}
        assert SyncSchedule(preset="hourly").to_cron() == {"minute": 30}

    def test_time_rejected_for_interval_presets(self) -> None:
        with pytest.raises(ValidationError):
            SyncSchedule(preset="hourly", hour=3)

    def test_unknown_preset_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SyncSchedule(preset="weekly")


class TestConfigAuth:
    def test_unauthenticated_gets_401(self, clean_db: None, client: TestClient) -> None:
        assert client.get("/config").status_code == 401

    def test_non_admin_scopes_get_403(self, admin_client: TestClient) -> None:
        headers = _agent_headers(admin_client, ["read", "write"])
        assert admin_client.get("/config", headers=headers).status_code == 403
        put = admin_client.put("/config", json={"session_ttl_days": 3}, headers=headers)
        assert put.status_code == 403

    def test_admin_session_allowed(self, admin_client: TestClient) -> None:
        assert admin_client.get("/config").status_code == 200


class TestConfigApi:
    def test_defaults_with_no_overrides(self, admin_client: TestClient) -> None:
        body = admin_client.get("/config").json()
        defaults = purser.config.Settings()
        assert body["overridden"] == []
        assert body["ai_api_key_set"] is False
        assert body["values"]["sync_schedule"]["preset"] == "daily"
        assert body["values"]["session_ttl_days"] == defaults.session_ttl_days
        assert body["values"]["ai_provider"] == defaults.ai_provider

    def test_put_overrides_and_get_reflects(self, admin_client: TestClient) -> None:
        resp = admin_client.put("/config", json={"session_ttl_days": 3, "ai_provider": "openai"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["values"]["session_ttl_days"] == 3
        assert body["values"]["ai_provider"] == "openai"
        assert body["overridden"] == ["ai_provider", "session_ttl_days"]
        again = admin_client.get("/config").json()
        assert again["values"]["session_ttl_days"] == 3

    def test_db_override_beats_env(
        self, admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PURSER_SESSION_TTL_DAYS", "10")
        purser.config.get_settings.cache_clear()
        assert admin_client.get("/config").json()["values"]["session_ttl_days"] == 10
        admin_client.put("/config", json={"session_ttl_days": 3})
        assert admin_client.get("/config").json()["values"]["session_ttl_days"] == 3

    def test_null_only_for_nullable_keys(self, admin_client: TestClient) -> None:
        ok = admin_client.put("/config", json={"grafana_url": None})
        assert ok.status_code == 200
        assert ok.json()["values"]["grafana_url"] is None
        assert "grafana_url" in ok.json()["overridden"]
        assert admin_client.put("/config", json={"session_ttl_days": None}).status_code == 422

    def test_validation_rejections(self, admin_client: TestClient) -> None:
        assert admin_client.put("/config", json={"nope": 1}).status_code == 422
        assert admin_client.put("/config", json={"ai_provider": "gpt"}).status_code == 422
        assert admin_client.put("/config", json={"ai_base_url": "ftp://x"}).status_code == 422
        assert admin_client.put("/config", json={"session_ttl_days": 0}).status_code == 422
        bad_sched = admin_client.put(
            "/config", json={"sync_schedule": {"preset": "hourly", "hour": 3}}
        )
        assert bad_sched.status_code == 422

    def test_delete_resets_to_env_default(self, admin_client: TestClient) -> None:
        default = purser.config.Settings().session_ttl_days
        admin_client.put("/config", json={"session_ttl_days": 3})
        resp = admin_client.delete("/config/session_ttl_days")
        assert resp.status_code == 200
        assert resp.json()["values"]["session_ttl_days"] == default
        assert resp.json()["overridden"] == []

    def test_transfer_auto_link_round_trip(self, admin_client: TestClient) -> None:
        assert admin_client.get("/config").json()["values"]["transfer_auto_link"] is True
        resp = admin_client.put("/config", json={"transfer_auto_link": False})
        assert resp.status_code == 200, resp.text
        assert resp.json()["values"]["transfer_auto_link"] is False
        assert "transfer_auto_link" in resp.json()["overridden"]
        # Non-nullable: null means "use DELETE to reset".
        assert admin_client.put("/config", json={"transfer_auto_link": None}).status_code == 422
        reset = admin_client.delete("/config/transfer_auto_link")
        assert reset.json()["values"]["transfer_auto_link"] is True

    def test_delete_unknown_key_404(self, admin_client: TestClient) -> None:
        assert admin_client.delete("/config/database_url").status_code == 404

    def test_empty_put_is_a_noop(self, admin_client: TestClient, db_session: Session) -> None:
        resp = admin_client.put("/config", json={})
        assert resp.status_code == 200
        count = db_session.execute(
            text("SELECT count(*) FROM audit_log WHERE entity_type = 'config' AND kind = 'change'")
        ).scalar_one()
        assert count == 0

    def test_sync_schedule_round_trip(self, admin_client: TestClient) -> None:
        resp = admin_client.put("/config", json={"sync_schedule": {"preset": "every_6h"}})
        assert resp.status_code == 200, resp.text
        sched = resp.json()["values"]["sync_schedule"]
        assert sched["preset"] == "every_6h"
        # TestClient runs no scheduler: the change is persisted, and the API
        # honestly reports that no live reschedule happened here.
        assert resp.json()["sync_reschedule_live"] is False
        daily = admin_client.put(
            "/config", json={"sync_schedule": {"preset": "daily", "hour": 21, "minute": 0}}
        )
        assert daily.json()["values"]["sync_schedule"] == {
            "preset": "daily",
            "hour": 21,
            "minute": 0,
        }

    def test_oidc_enabled_once_trio_complete(self, admin_client: TestClient) -> None:
        body = admin_client.get("/config").json()
        assert body["values"]["oidc_enabled"] is False
        partial = admin_client.put(
            "/config",
            json={"oidc_issuer": "https://op.example.com", "oidc_client_id": "purser"},
        )
        assert partial.status_code == 200, partial.text
        assert partial.json()["values"]["oidc_issuer"] == "https://op.example.com"
        assert partial.json()["values"]["oidc_enabled"] is False  # secret still missing
        full = admin_client.put("/config", json={"oidc_client_secret": "s3cret"})
        assert full.json()["values"]["oidc_enabled"] is True
        assert full.json()["oidc_client_secret_set"] is True

    def test_oidc_null_override_disables_env_config(
        self, admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PURSER_OIDC_ISSUER", "https://env-op.example.com")
        monkeypatch.setenv("PURSER_OIDC_CLIENT_ID", "env-client")
        monkeypatch.setenv("PURSER_OIDC_CLIENT_SECRET", "env-secret")
        purser.config.get_settings.cache_clear()
        assert admin_client.get("/config").json()["values"]["oidc_enabled"] is True
        resp = admin_client.put("/config", json={"oidc_issuer": None})
        assert resp.json()["values"]["oidc_enabled"] is False
        # The disabled route agrees with the derived flag.
        assert admin_client.get("/auth/oidc/login", follow_redirects=False).status_code == 404


class TestSecretHandling:
    def test_api_key_never_returned(
        self, admin_client: TestClient, migrated_engine: Engine
    ) -> None:
        from purser import crypto

        secret = "sk-test-not-a-real-key-1234"
        resp = admin_client.put("/config", json={"ai_api_key": secret})
        assert resp.status_code == 200
        assert secret not in resp.text
        assert resp.json()["ai_api_key_set"] is True
        get = admin_client.get("/config")
        assert secret not in get.text

        with migrated_engine.connect() as conn:
            row = conn.execute(
                text("SELECT value, value_enc FROM app_settings WHERE key = 'ai_api_key'")
            ).one()
        assert row.value is None
        assert json.loads(crypto.decrypt(bytes(row.value_enc))) == secret

        with migrated_engine.connect() as conn:
            audits = conn.execute(
                text(
                    "SELECT before_after_enc FROM audit_log"
                    " WHERE entity_type = 'config' AND kind = 'change'"
                )
            ).scalars()
            # before_after is encrypted at rest now (#137); decrypt to assert the
            # secret is masked to "set" in the payload (not merely hidden by the
            # ciphertext).
            for enc in audits:
                blob = crypto.decrypt(bytes(enc))
                assert secret not in blob
                assert '"set"' in blob

    def test_blank_api_key_rejected(self, admin_client: TestClient) -> None:
        assert admin_client.put("/config", json={"ai_api_key": "  "}).status_code == 422

    def test_oidc_client_secret_write_only(
        self, admin_client: TestClient, migrated_engine: Engine
    ) -> None:
        from purser import crypto

        secret = "oidc-secret-not-real-5678"
        resp = admin_client.put("/config", json={"oidc_client_secret": secret})
        assert resp.status_code == 200
        assert secret not in resp.text
        assert resp.json()["oidc_client_secret_set"] is True
        assert secret not in admin_client.get("/config").text
        with migrated_engine.connect() as conn:
            row = conn.execute(
                text("SELECT value, value_enc FROM app_settings WHERE key = 'oidc_client_secret'")
            ).one()
        assert row.value is None
        assert json.loads(crypto.decrypt(bytes(row.value_enc))) == secret
        # Null is not a valid secret write; resets go through DELETE.
        assert admin_client.put("/config", json={"oidc_client_secret": None}).status_code == 422
        reset = admin_client.delete("/config/oidc_client_secret")
        assert reset.status_code == 200
        assert reset.json()["oidc_client_secret_set"] is False


class TestEffectiveConsumption:
    def test_categorize_status_sees_override(self, admin_client: TestClient) -> None:
        admin_client.put("/config", json={"ai_provider": "openai", "ai_model": "gpt-test"})
        status = admin_client.get("/categorize/status").json()
        assert status == {"provider": "openai", "model": "gpt-test", "configured": True}

    def test_session_ttl_override_reaches_sessions(self, admin_client: TestClient) -> None:
        from purser.admin import service

        admin_client.put("/config", json={"session_ttl_days": 1})
        assert service.session_ttl() == timedelta(days=1)

    def test_grafana_override_reaches_links(self, admin_client: TestClient) -> None:
        from purser.mcp import presentation

        admin_client.put("/config", json={"grafana_url": "https://grafana.example.com/"})
        links = presentation.grafana_links("net-worth")
        assert links == {"grafana": "https://grafana.example.com/d/purser-net-worth"}


class TestConfigAudit:
    def test_one_audit_row_per_put_and_reset(
        self, admin_client: TestClient, migrated_engine: Engine
    ) -> None:
        from purser import crypto

        admin_client.put("/config", json={"session_ttl_days": 2, "grafana_url": "https://g.ex"})
        admin_client.delete("/config/grafana_url")
        with migrated_engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT action, before_after_enc FROM audit_log"
                    " WHERE entity_type = 'config' AND kind = 'change' ORDER BY at"
                )
            ).all()
        assert [r.action for r in rows] == ["config.update", "config.reset"]
        # before_after is encrypted now (#137).
        update = json.loads(crypto.decrypt(bytes(rows[0].before_after_enc)))
        assert set(update["after"]) == {"session_ttl_days", "grafana_url"}
        assert update["after"]["session_ttl_days"] == 2
        reset = json.loads(crypto.decrypt(bytes(rows[1].before_after_enc)))
        assert reset["after"] == {"grafana_url": None}


def test_audit_retention_round_trip(admin_client: TestClient) -> None:
    """audit_retention_days: default, override, floor validation, reset."""
    assert admin_client.get("/config").json()["values"]["audit_retention_days"] == 90

    resp = admin_client.put("/config", json={"audit_retention_days": 30})
    assert resp.status_code == 200, resp.text
    assert resp.json()["values"]["audit_retention_days"] == 30
    assert "audit_retention_days" in resp.json()["overridden"]

    # floor: below 7 days would erase the trail of the change itself
    assert admin_client.put("/config", json={"audit_retention_days": 6}).status_code == 422

    reset = admin_client.delete("/config/audit_retention_days")
    assert reset.json()["values"]["audit_retention_days"] == 90


def test_encryption_status_block(admin_client: TestClient, db_session: Session) -> None:
    """The read-only posture block (issue #123): canary verified, fingerprint,
    acknowledgment state, encrypted scopes."""
    from purser import crypto

    enc = admin_client.get("/config").json()["encryption"]
    assert enc["field_encryption_active"] is True
    assert enc["canary_ok"] is True
    assert enc["key_fingerprint"] == crypto.key_fingerprint()
    assert enc["key_acknowledged_at"] is None
    # 4 pre-#137 scopes + 6 narrative/keyed-hash entries.
    assert len(enc["encrypted_scopes"]) == 10

    assert admin_client.post("/auth/encryption-key/ack").status_code == 200
    try:
        enc = admin_client.get("/config").json()["encryption"]
        assert enc["key_acknowledged_at"] is not None
    finally:
        # the canary row is session-scoped shared state — leave it pristine
        db_session.execute(text("UPDATE encryption_canary SET key_acknowledged_at = NULL"))
        db_session.commit()
