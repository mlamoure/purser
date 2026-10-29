"""Guard against version drift between pyproject.toml and __version__.

The drift has shipped twice (0.4.0 fixed it, 0.5.0 reintroduced it): release
PRs bump pyproject.toml but forget purser/__init__.py, so prod reports a stale
version on /openapi.json. Comparing the two source files directly (not
installed metadata, which goes stale in editable envs) makes the bump atomic.
"""

import re
import tomllib
from pathlib import Path

from packaging.version import Version

import purser


def test_version_matches_pyproject() -> None:
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with pyproject.open("rb") as f:
        expected = tomllib.load(f)["project"]["version"]
    assert purser.__version__ == expected


def test_version_is_calver() -> None:
    """Releases use CalVer ``YYYY.M.PATCH`` with no zero padding (from
    2026.10.0), which is also a valid semver and a normalized PEP 440
    version, so the image tag ``v<version>`` and the chart's appVersion
    stay in step with pyproject."""
    version = purser.__version__
    assert re.fullmatch(r"20\d\d\.(?:[1-9]|1[0-2])\.(?:0|[1-9]\d*)", version), version
    assert str(Version(version)) == version
