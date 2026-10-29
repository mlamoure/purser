"""Licence obligations (#429).

- THIRD_PARTY_NOTICES.md lists exactly what the lockfiles ship: a dependency
  added, bumped or dropped without regenerating it fails here. Offline and
  fast — it reads only uv.lock, the npm lockfiles and the committed file.
- Every listed licence is one the generator accepts next to AGPL-3.0-only.
- The image ships the licence and the notices; the SPA build writes its
  bundled licences beside the bundle.
- AGPL-3.0 section 13: a running instance offers its source to every network
  user — GET /source redirects to PURSER_SOURCE_URL, and /llms.txt and the
  OpenAPI metadata say so.
"""

import importlib.util
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

REPO = Path(__file__).resolve().parent.parent
NOTICES = REPO / "THIRD_PARTY_NOTICES.md"


@pytest.fixture(scope="module")
def generator() -> ModuleType:
    path = REPO / "scripts" / "generate_third_party_notices.py"
    spec = importlib.util.spec_from_file_location("generate_third_party_notices", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations through it
    spec.loader.exec_module(module)
    return module


def test_notices_match_the_lockfiles(generator: ModuleType) -> None:
    problems = generator.drift(NOTICES.read_text())
    assert not problems, (
        "THIRD_PARTY_NOTICES.md is out of date — run "
        "`uv run python scripts/generate_third_party_notices.py`:\n  " + "\n  ".join(problems)
    )


def test_drift_is_detected(generator: ModuleType) -> None:
    text = NOTICES.read_text()
    assert generator.drift(text.replace("| fastapi | ", "| fastapi-gone | ", 1))
    bumped = re.sub(r"(\| react \| )[^|]+", r"\g<1>0.0.1 ", text, count=1)
    assert any("react is" in p for p in generator.drift(bumped))


def test_inventory_covers_the_runtime_and_the_bundles(generator: ModuleType) -> None:
    inventory = generator.parse_inventory(NOTICES.read_text())
    python = inventory[generator.SECTION_PYTHON]
    frontend = inventory[generator.SECTION_FRONTEND]
    # Runtime dependencies only: dev tools never reach the image.
    assert {"fastapi", "sqlalchemy", "psycopg", "fastmcp"} <= python.keys()
    assert not {"pytest", "ruff", "mypy", "hypothesis", "testcontainers"} & python.keys()
    # Windows-only markers stay out of a Linux image.
    assert not {"pywin32", "colorama", "tzdata"} & python.keys()
    assert {
        "react",
        "react-dom",
        "@modelcontextprotocol/ext-apps",
        "tailwindcss",
    } <= frontend.keys()
    assert not {"vite", "vitest", "typescript"} & frontend.keys()
    assert inventory[generator.SECTION_DOCS]


def test_every_listed_licence_is_agpl_compatible(generator: ModuleType) -> None:
    licences = generator.parse_licenses(NOTICES.read_text())
    assert licences
    bad = {k: v for k, v in licences.items() if not generator.is_compatible(v)}
    assert not bad


@pytest.mark.parametrize(
    ("expression", "compatible"),
    [
        ("MIT", True),
        ("MIT OR Apache-2.0", True),
        ("Apache-2.0 AND BSD-3-Clause", True),
        ("LGPL-3.0-only", True),
        ("GPL-2.0-only", False),
        ("SSPL-1.0", False),
        ("CC-BY-NC-4.0", False),
        ("MIT AND GPL-2.0-only", False),
        ("GPL-2.0-only OR MIT", True),
        ("UNLICENSED", False),
    ],
)
def test_compatibility_rule(generator: ModuleType, expression: str, compatible: bool) -> None:
    assert generator.is_compatible(expression) is compatible


def test_image_ships_licence_and_notices() -> None:
    dockerfile = (REPO / "Dockerfile").read_text()
    assert "COPY LICENSE THIRD_PARTY_NOTICES.md /usr/share/doc/purser/" in dockerfile
    assert 'license = "AGPL-3.0-only"' in (REPO / "pyproject.toml").read_text()


def test_spa_build_writes_its_bundled_licences() -> None:
    config = (REPO / "frontend" / "vite.config.ts").read_text()
    assert 'license: { fileName: "third-party-licenses.txt" }' in config


@pytest.fixture
def source_client(migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    from purser.api.app import create_app
    from purser.config import get_settings

    monkeypatch.setenv("PURSER_SOURCE_URL", "https://example.com/fork/purser")
    get_settings.cache_clear()
    try:
        with TestClient(create_app()) as c:
            yield c
    finally:
        monkeypatch.delenv("PURSER_SOURCE_URL")
        get_settings.cache_clear()


def test_source_defaults_to_the_upstream_repository(client: TestClient) -> None:
    resp = client.get("/source", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == "https://github.com/mlamoure/purser"


def test_source_follows_the_operator_setting(source_client: TestClient) -> None:
    resp = source_client.get("/source", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == "https://example.com/fork/purser"
    llms = source_client.get("/llms.txt").text
    assert "AGPL-3.0-only" in llms
    assert "https://example.com/fork/purser" in llms


def test_openapi_names_the_licence(client: TestClient) -> None:
    info = client.get("/openapi.json").json()["info"]
    assert info["license"] == {"name": "AGPL-3.0-only", "identifier": "AGPL-3.0-only"}
