"""scripts/install-docker-macos.sh against stand-in commands (#581).

The script gives a Mac a Docker runtime (Colima) so a person's agent can
install Purser there. It cannot run on a Linux CI host for real, so these
tests replace `uname`, `brew`, `colima` and `docker` with small recording
programs and check what the script decides: do nothing when Docker works,
never install beside Docker Desktop, stop with the Homebrew command when
Homebrew is missing, and otherwise install, link Compose and wait for Docker.
The live procedure on a Mac is `tests/e2e/run_setup_drill.py --macos-script`.
"""

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "install-docker-macos.sh"

STUBS = {
    "uname": 'echo "${STUB_UNAME:-Darwin}"',
    # docker "works" once the state file exists (Colima started), or always
    # when STUB_DOCKER_READY is set (Docker was already there).
    "docker": (
        'echo "docker $*" >> "$STUB_LOG"\n'
        '[ -n "${STUB_DOCKER_READY:-}" ] || [ -e "$STUB_STATE/colima-running" ] || exit 1\n'
        'case "$1" in\n'
        '  version) echo "linux/arm64" ;;\n'
        '  compose) [ -n "${STUB_DOCKER_READY:-}" ]'
        ' || [ -L "$HOME/.docker/cli-plugins/docker-compose" ] || exit 1\n'
        '           echo "Docker Compose version v5.8.0" ;;\n'
        "esac"
    ),
    "brew": (
        'echo "brew $*" >> "$STUB_LOG"\n'
        'case "$1" in\n'
        "  shellenv) echo ':' ;;\n"
        '  --prefix) echo "$STUB_STATE/brew" ;;\n'
        '  services) [ -n "${STUB_SERVICE_FAILS:-}" ] || touch "$STUB_STATE/colima-running" ;;\n'
        "esac"
    ),
    "colima": 'echo "colima $*" >> "$STUB_LOG"\ntouch "$STUB_STATE/colima-running"',
    "sleep": ":",
}


@pytest.fixture
def mac(tmp_path: Path) -> dict[str, Path]:
    bin_dir, state = tmp_path / "bin", tmp_path / "state"
    for directory in (bin_dir, state, tmp_path / "home", tmp_path / "apps"):
        directory.mkdir()
    for name, body in STUBS.items():
        stub = bin_dir / name
        stub.write_text(f"#!/bin/bash\n{body}\n")
        stub.chmod(0o755)
    return {
        "bin": bin_dir,
        "state": state,
        "home": tmp_path / "home",
        "apps": tmp_path / "apps",
        "log": tmp_path / "calls.log",
    }


def run(mac: dict[str, Path], *, without: tuple[str, ...] = (), **env: str) -> tuple[int, str, str]:
    for name in without:
        (mac["bin"] / name).unlink()
    result = subprocess.run(
        ["/bin/bash", str(SCRIPT)],
        capture_output=True,
        text=True,
        env={
            # The stubs first; /usr/bin and /bin only for mkdir, ln and printf.
            "PATH": f"{mac['bin']}:/usr/bin:/bin",
            "HOME": str(mac["home"]),
            "STUB_LOG": str(mac["log"]),
            "STUB_STATE": str(mac["state"]),
            "PURSER_MAC_APPS_DIR": str(mac["apps"]),
            "PURSER_MAC_WAIT_SECONDS": "10",
            **env,
        },
        timeout=60,
    )
    calls = mac["log"].read_text() if mac["log"].exists() else ""
    return result.returncode, result.stdout + result.stderr, calls


def test_does_nothing_when_docker_already_works(mac: dict[str, Path]) -> None:
    code, output, calls = run(mac, STUB_DOCKER_READY="1")
    assert code == 0
    assert "already works" in output and "Nothing was installed" in output
    assert "brew" not in calls


def test_never_installs_beside_docker_desktop(mac: dict[str, Path]) -> None:
    (mac["apps"] / "Docker.app").mkdir()
    code, output, calls = run(mac)
    assert code == 1
    assert "STOP: Docker is installed but not running" in output
    assert "brew install" not in calls


def test_stops_with_the_homebrew_command_when_homebrew_is_missing(mac: dict[str, Path]) -> None:
    code, output, calls = run(mac, without=("brew",))
    if any(Path(p).exists() for p in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew")):
        pytest.skip("this machine has a real Homebrew at a path the script checks")
    assert code == 1
    assert "STOP: Homebrew is not installed" in output
    assert "Homebrew/install/HEAD/install.sh" in output
    assert "colima" not in calls


def test_installs_colima_links_compose_and_waits_for_docker(mac: dict[str, Path]) -> None:
    code, output, calls = run(mac)
    assert code == 0, output
    assert "brew install colima docker docker-compose" in calls
    assert "brew services start colima" in calls
    plugin = mac["home"] / ".docker/cli-plugins/docker-compose"
    assert plugin.is_symlink()
    assert os.readlink(plugin) == f"{mac['state']}/brew/opt/docker-compose/bin/docker-compose"
    assert "OK: Docker is ready on this Mac (linux/arm64, Docker Compose version v5.8.0)" in output
    # Docker's own config file is the person's: the script never writes it.
    assert not (mac["home"] / ".docker/config.json").exists()


def test_starts_colima_directly_when_the_service_does_not_come_up(mac: dict[str, Path]) -> None:
    code, output, calls = run(mac, STUB_SERVICE_FAILS="1")
    assert code == 0, output
    assert "colima start" in calls


def test_refuses_to_run_off_macos(mac: dict[str, Path]) -> None:
    code, output, calls = run(mac, STUB_UNAME="Linux")
    assert code == 1
    assert "this script is for macOS" in output
    assert calls == ""
