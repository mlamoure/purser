"""Packaging (issue #15): the wheel is self-sufficient — an installed
`purser` can migrate a fresh database with no repo checkout (the uvx path,
validated here on x86_64)."""

import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import Engine

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("dist")
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(out)],
        cwd=REPO,
        check=True,
        capture_output=True,
    )
    (wheel_path,) = out.glob("purser-*.whl")
    return wheel_path


def test_wheel_contains_migrations(wheel: Path) -> None:
    import zipfile

    names = zipfile.ZipFile(wheel).namelist()
    assert any(n == "purser/_migrations/alembic.ini" for n in names)
    assert any("purser/_migrations/migrations/versions/0001_initial_schema" in n for n in names)


def test_installed_wheel_migrates_a_fresh_database(
    wheel: Path, database_url: str, tmp_path: Path
) -> None:
    """Wheel-install equivalent (`uvx --from git+<repo-url> purser
    migrate` — not on PyPI): install the wheel into a clean venv and run
    migrations against a brand-new database."""
    from sqlalchemy import create_engine, text

    admin_engine: Engine = create_engine(database_url, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        conn.execute(text("DROP DATABASE IF EXISTS wheeltest"))
        conn.execute(text("CREATE DATABASE wheeltest"))
    wheel_db_url = database_url.rsplit("/", 1)[0] + "/wheeltest"

    venv = tmp_path / "venv"
    subprocess.run(
        ["uv", "venv", str(venv), "--python", sys.version.split()[0]],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["uv", "pip", "install", "--python", str(venv / "bin" / "python"), str(wheel)],
        check=True,
        capture_output=True,
    )
    from tests.conftest import TEST_ENCRYPTION_KEY

    result = subprocess.run(
        [str(venv / "bin" / "purser"), "migrate"],
        env={
            "PURSER_DATABASE_URL": wheel_db_url,
            "PURSER_ENCRYPTION_KEY": TEST_ENCRYPTION_KEY,  # required at startup (#93)
            "PATH": "/usr/bin:/bin",
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    check_engine = create_engine(wheel_db_url)
    with check_engine.connect() as conn:
        tables = set(
            conn.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            ).scalars()
        )
    assert {"transactions", "postings", "raw_fetches", "net_worth_daily"} <= tables
    check_engine.dispose()
