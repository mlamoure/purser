"""Setup wizard support + SPA serving (issue #12)."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.conftest import ADMIN_CREDENTIALS


class TestSetupStatus:
    def test_public_probe_flips_after_setup(self, clean_db: None, client: TestClient) -> None:
        # Public (the SPA calls it before any auth exists) but says nothing
        # beyond the boolean.
        resp = client.get("/auth/setup/status")
        assert resp.status_code == 200
        assert resp.json() == {"setup_needed": True}

        client.post("/auth/setup", json=ADMIN_CREDENTIALS)
        assert client.get("/auth/setup/status").json() == {"setup_needed": False}


class TestSpaServing:
    """The full app serves the SPA shell at / and for unknown paths, while
    API routes still win. A fake bundle keeps it hermetic: the CI checks job
    never builds the frontend."""

    @pytest.fixture(autouse=True)
    def fake_bundle(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Autouse, so it runs before ``client`` builds the app — _mount_spa
        # reads PURSER_STATIC_DIR at create_app().
        static = tmp_path / "static"
        (static / "assets").mkdir(parents=True)  # StaticFiles mount needs it to exist
        (static / "index.html").write_text("<!doctype html><title>Purser</title>")
        monkeypatch.setenv("PURSER_STATIC_DIR", str(static))

    def test_root_serves_spa(self, clean_db: None, client: TestClient) -> None:
        resp = client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "Purser" in resp.text

    def test_unknown_paths_fall_back_to_index(self, clean_db: None, client: TestClient) -> None:
        # SPA routes are hash-based so they never collide with API paths;
        # any other unknown path still lands on the app shell.
        resp = client.get("/some-unknown-page")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_api_routes_still_win(self, clean_db: None, client: TestClient) -> None:
        assert client.get("/healthz").json()["status"] == "ok"
        assert client.get("/auth/me").status_code == 401


class TestSpaTraversal:
    """The SPA catch-all must never serve a file outside the static bundle.

    Hermetic: builds its own static dir and app so it runs without Docker or a
    frontend build. `resolve()`
    dereferences symlinks, so an in-bundle symlink pointing outside is refused
    too.
    """

    SENTINEL = "TOP-SECRET-OUTSIDE-BUNDLE"
    SHELL = "<html>Purser app shell</html>"

    @pytest.fixture
    def spa_client(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
        static = tmp_path / "static"
        (static / "assets").mkdir(parents=True)  # StaticFiles mount needs it to exist
        (static / "index.html").write_text(self.SHELL)
        (static / "robots.txt").write_text("User-agent: *")
        (tmp_path / "secret.txt").write_text(self.SENTINEL)

        monkeypatch.setenv("PURSER_STATIC_DIR", str(static))
        from purser.api.app import _mount_spa

        app = FastAPI()
        _mount_spa(app)
        with TestClient(app) as c:
            yield c

    @pytest.mark.parametrize(
        "path",
        [
            "/%2e%2e/secret.txt",
            "/%2e%2e%2f%2e%2e%2fsecret.txt",
            "/%2e%2e/%2e%2e/etc/passwd",
            "/..%2fsecret.txt",
        ],
    )
    def test_encoded_traversal_falls_back_to_shell(self, spa_client: TestClient, path: str) -> None:
        resp = spa_client.get(path)
        assert resp.status_code == 200
        assert self.SENTINEL not in resp.text
        assert "Purser app shell" in resp.text

    def test_in_bundle_file_still_served(self, spa_client: TestClient) -> None:
        resp = spa_client.get("/robots.txt")
        assert resp.status_code == 200
        assert "User-agent" in resp.text

    def test_unknown_client_route_falls_back(self, spa_client: TestClient) -> None:
        resp = spa_client.get("/accounts/123")
        assert resp.status_code == 200
        assert "Purser app shell" in resp.text

    def test_symlink_escape_refused(self, tmp_path: Path, spa_client: TestClient) -> None:
        link = tmp_path / "static" / "leak"
        try:
            link.symlink_to(tmp_path / "secret.txt")
        except OSError:
            pytest.skip("filesystem does not support symlinks")
        resp = spa_client.get("/leak")
        assert resp.status_code == 200
        assert self.SENTINEL not in resp.text
