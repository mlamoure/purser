from fastapi.testclient import TestClient
from sqlalchemy import Engine

from purser.api.app import create_app


def test_healthz_ok(migrated_engine: Engine) -> None:
    client = TestClient(create_app())
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "database": "ok"}
