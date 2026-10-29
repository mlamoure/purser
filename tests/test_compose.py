"""The compose files and the setup pages name the current release (#427).

Setup downloads `docker-compose.yml` from the release tag and runs the
published image it names, so a release bump that forgets the compose file
would install the previous release, and one that forgets the setup pages
would hand out the previous release's compose file. Like the chart's
appVersion check (tests/test_helm_chart.py), these compare against
pyproject.toml.
"""

import re
import tomllib
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
IMAGE = "ghcr.io/mlamoure/purser"

# Pages whose install steps download a versioned compose file or name the
# release image. Other pages may cite an older release as an example.
SETUP_PAGES = [
    "docs/src/content/docs/tutorials/ai-setup.md",
    "docs/src/content/docs/tutorials/quickstart.md",
    "skills/purser/SKILL.md",
    "docker-compose.yml",
]


def _version() -> str:
    with (REPO / "pyproject.toml").open("rb") as f:
        version: str = tomllib.load(f)["project"]["version"]
    return version


def _service(path: str, name: str) -> dict[str, object]:
    service: dict[str, object] = yaml.safe_load((REPO / path).read_text())["services"][name]
    return service


@pytest.mark.parametrize(
    ("path", "service"),
    [("docker-compose.yml", "purser"), ("docker-compose.split.yml", "worker")],
)
def test_compose_image_is_the_current_release(path: str, service: str) -> None:
    s = _service(path, service)
    assert s["image"] == f"{IMAGE}:v{_version()}"
    # Contributors still build from a clone with `docker compose build`.
    assert s["build"] == "."


@pytest.mark.parametrize("page", SETUP_PAGES)
def test_setup_pages_name_the_current_release(page: str) -> None:
    text = (REPO / page).read_text()
    tag = f"v{_version()}"
    urls = re.findall(r"raw\.githubusercontent\.com/mlamoure/purser/(v\d+\.\d+\.\d+)/", text)
    images = re.findall(re.escape(IMAGE) + r":(v\d+\.\d+\.\d+)", text)
    assert urls, f"{page} has no versioned compose download URL"
    assert set(urls) == {tag}, (page, urls)
    assert set(images) <= {tag}, (page, images)
