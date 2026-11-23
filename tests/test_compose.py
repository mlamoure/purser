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
    assert s["cap_drop"] == ["ALL"]
    assert s["security_opt"] == ["no-new-privileges:true"]


@pytest.mark.parametrize("page", SETUP_PAGES)
def test_setup_pages_name_the_current_release(page: str) -> None:
    text = (REPO / page).read_text()
    tag = f"v{_version()}"
    urls = re.findall(r"raw\.githubusercontent\.com/mlamoure/purser/(v\d+\.\d+\.\d+)/", text)
    images = re.findall(re.escape(IMAGE) + r":(v\d+\.\d+\.\d+)", text)
    assert urls, f"{page} has no versioned compose download URL"
    assert set(urls) == {tag}, (page, urls)
    assert set(images) <= {tag}, (page, images)


# Pages that hand out the macOS runtime script from the release tag (#581).
SCRIPT_PAGES = [
    "docs/src/content/docs/tutorials/ai-setup.md",
    "docs/src/content/docs/how-to/macos-uvx.md",
    "scripts/install-docker-macos.sh",
]


@pytest.mark.parametrize("page", SCRIPT_PAGES)
def test_macos_script_is_downloaded_from_the_current_release(page: str) -> None:
    text = (REPO / page).read_text()
    urls = re.findall(
        r"raw\.githubusercontent\.com/mlamoure/purser/(v\d+\.\d+\.\d+)/scripts/install-docker-macos\.sh",
        text,
    )
    assert urls, f"{page} does not link the macOS script at a release tag"
    assert set(urls) == {f"v{_version()}"}, (page, urls)


def test_compose_publishes_a_configurable_local_port() -> None:
    """Purser answers on this machine only by default; PURSER_HOST_PORT moves it
    off a taken port and the default public URL follows (#581)."""
    purser = _service("docker-compose.yml", "purser")
    assert purser["ports"] == ["${PURSER_BIND_ADDRESS:-127.0.0.1}:${PURSER_HOST_PORT:-8000}:8000"]
    environment = purser["environment"]
    assert isinstance(environment, dict)
    assert (
        environment["PURSER_PUBLIC_URL"]
        == "${PURSER_PUBLIC_URL:-http://localhost:${PURSER_HOST_PORT:-8000}}"
    )


@pytest.mark.parametrize(
    ("path", "service"),
    [("docker-compose.yml", "purser"), ("docker-compose.split.yml", "worker")],
)
def test_compose_passes_the_household_time_zone(path: str, service: str) -> None:
    """Both processes read the same PURSER_TIMEZONE default (#581): the ledger
    day must not differ between the API and the worker."""
    environment = _service(path, service)["environment"]
    assert isinstance(environment, dict)
    assert environment["PURSER_TIMEZONE"] == "${PURSER_TIMEZONE:-UTC}"


def test_compose_passes_the_allowed_host_names() -> None:
    """The home-network setup lists the machine's other names (#633); unset,
    the variable reaches Purser empty, which means no extra names."""
    environment = _service("docker-compose.yml", "purser")["environment"]
    assert isinstance(environment, dict)
    assert environment["PURSER_ALLOWED_HOSTS"] == "${PURSER_ALLOWED_HOSTS:-}"


def test_compose_tells_purser_the_address_it_publishes_on() -> None:
    """Settings shows how Purser is reached (#632), but the app cannot see the
    host's port mapping from inside its container: Compose passes the same
    PURSER_BIND_ADDRESS, with the same default, as the mapping uses."""
    purser = _service("docker-compose.yml", "purser")
    environment = purser["environment"]
    assert isinstance(environment, dict)
    assert environment["PURSER_BIND_ADDRESS"] == "${PURSER_BIND_ADDRESS:-127.0.0.1}"
    ports = purser["ports"]
    assert isinstance(ports, list)
    assert ports[0].startswith(environment["PURSER_BIND_ADDRESS"] + ":")
