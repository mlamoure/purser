"""Disposable Docker onboarding and logical-restore drill (#427/#428).

Run via: uv run --group e2e python tests/e2e/run_docker.py --image purser:dev
Set PURSER_E2E_CDP_URL to drive an existing browser over CDP; otherwise install the
Playwright Chromium runtime for isolated CI. Only uniquely named test resources
are removed. Never use production volumes, credentials or databases here.
"""

import argparse
import http.cookiejar
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path


def docker(*args: str, data: bytes | None = None) -> bytes:
    result = subprocess.run(["docker", *args], input=data, capture_output=True)
    if result.returncode:
        # Command/env-file contents may contain credentials: do not echo argv.
        raise RuntimeError(f"Docker {args[0]} failed: {result.stderr.decode()[-1000:]}")
    return result.stdout


def wait_database(name: str) -> None:
    for _ in range(90):
        r = subprocess.run(
            ["docker", "exec", name, "pg_isready", "-h", "127.0.0.1", "-U", "purser"],
            capture_output=True,
        )
        if r.returncode == 0:
            return
        time.sleep(1)
    raise RuntimeError("Test PostgreSQL did not become ready")


def wait_app(url: str) -> None:
    for _ in range(90):
        try:
            with urllib.request.urlopen(url + "/healthz", timeout=2) as r:
                if json.load(r) == {"status": "ok", "database": "ok"}:
                    return
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(1)
    raise RuntimeError("Test Purser did not become healthy")


def snapshot(db: str) -> dict[str, str]:
    def sql(query: str) -> str:
        return docker("exec", db, "psql", "-U", "purser", "-At", "-c", query).decode().strip()

    tables = sql(
        "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY 1"
    ).splitlines()
    result = {}
    for table in tables:
        if not table.replace("_", "").isalnum():
            raise ValueError("Unexpected table identifier")
        result[table] = sql(
            "SELECT count(*)::text || ':' || md5(coalesce("
            "string_agg(to_jsonb(t)::text, '' ORDER BY to_jsonb(t)::text), '')) "
            f'FROM public."{table}" t'
        )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--url-host", help="Reachable host/tunnel when Docker runs remotely")
    parser.add_argument("--postgres-image", default="postgres:16")
    parser.add_argument(
        "--port", type=int, default=0, help="Explicit port for a remote Docker host"
    )
    parser.add_argument(
        "--bind", default="127.0.0.1", help="LAN address only for shared-browser tests"
    )
    args = parser.parse_args()
    prefix = "purser-e2e-" + uuid.uuid4().hex[:10]
    network = prefix + "-net"
    db, restored, app = prefix + "-db", prefix + "-restored", prefix + "-app"
    containers: list[str] = []
    port = args.port
    if not port:
        with socket.socket() as reservation:
            reservation.bind((args.bind, 0))
            port = reservation.getsockname()[1]
    url = f"http://{args.url_host or args.bind}:{port}"
    label = "purser.hardening.disposable=true"
    with tempfile.TemporaryDirectory(prefix="purser-e2e-") as temp:
        key = docker("run", "--rm", args.image, "keygen").decode().strip()
        db_password = uuid.uuid4().hex
        db_env = Path(temp) / "database.env"
        db_env.write_text(
            f"POSTGRES_USER=purser\nPOSTGRES_DB=purser\nPOSTGRES_PASSWORD={db_password}\n"
        )
        db_env.chmod(0o600)
        app_env = Path(temp) / "app.env"

        def configure_app(database: str) -> None:
            app_env.write_text(
                f"PURSER_DATABASE_URL=postgresql+psycopg://purser:{db_password}@{database}:5432/purser\n"
                f"PURSER_ENCRYPTION_KEY={key}\nPURSER_PUBLIC_URL={url}\n"
                "PURSER_DISABLE_SCHEDULER=true\n"
            )
            app_env.chmod(0o600)

        def start_database(name: str) -> None:
            containers.append(name)
            docker(
                "run",
                "-d",
                "--name",
                name,
                "--label",
                label,
                "--network",
                network,
                "--env-file",
                str(db_env),
                args.postgres_image,
            )
            wait_database(name)

        def start_app() -> None:
            containers.append(app)
            docker(
                "run",
                "-d",
                "--name",
                app,
                "--label",
                label,
                "--network",
                network,
                "--env-file",
                str(app_env),
                "-p",
                f"{args.bind}:{port}:8000",
                args.image,
            )
            wait_app(url)

        docker("network", "create", "--label", label, network)
        try:
            start_database(db)
            configure_app(db)
            start_app()
            env = {**os.environ, "PURSER_E2E_URL": url}
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "tests/e2e/test_onboarding.py",
                    "-q",
                    "--tb=short",
                ],
                env=env,
            )
            if result.returncode:
                return result.returncode
            docker("stop", app)
            docker("rm", app)
            containers.remove(app)
            before = snapshot(db)
            dump = docker("exec", db, "pg_dump", "-U", "purser", "-Fc", "purser")
            start_database(restored)
            # pg_dump excludes cluster roles; the read-model grants require this one.
            docker(
                "exec",
                restored,
                "psql",
                "-U",
                "purser",
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
                "CREATE ROLE purser_read NOLOGIN",
            )
            docker(
                "exec",
                "-i",
                restored,
                "pg_restore",
                "--exit-on-error",
                "-U",
                "purser",
                "-d",
                "purser",
                data=dump,
            )
            after = snapshot(restored)
            assert before == after, "Restored row counts/content hashes differ"
            configure_app(restored)
            start_app()
            docker("restart", app)
            wait_app(url)
            # A running process alone is not restore proof: authenticate and decrypt.
            opener = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
            )
            login = urllib.request.Request(
                url + "/auth/login",
                data=json.dumps(
                    {
                        "email": "onboarding@example.com",
                        "password": "synthetic-onboarding-password",
                    }
                ).encode(),
                headers={"Content-Type": "application/json"},
            )
            with opener.open(login, timeout=10) as response:
                assert response.status == 200
            with opener.open(url + "/transactions", timeout=10) as response:
                payload = json.load(response)
                assert "E2E encrypted narrative" in json.dumps(payload), (
                    "Restored narrative did not decrypt"
                )
            # Exercise the documented maintenance command on the restored copy,
            # then prove the old key is no longer required by a fresh process.
            docker("stop", app)
            docker("rm", app)
            containers.remove(app)
            new_key = docker("run", "--rm", args.image, "keygen").decode().strip()
            key = new_key + "," + key
            configure_app(restored)
            docker(
                "run",
                "--rm",
                "--network",
                network,
                "--env-file",
                str(app_env),
                args.image,
                "admin",
                "rotate-key",
            )
            key = new_key
            configure_app(restored)
            start_app()
            with opener.open(login, timeout=10) as response:
                assert response.status == 200
            with opener.open(url + "/transactions", timeout=10) as response:
                assert "E2E encrypted narrative" in json.dumps(json.load(response)), (
                    "Rotated narrative did not decrypt with the new key alone"
                )
            print(
                json.dumps(
                    {
                        "image": args.image,
                        "tables_restored": len(before),
                        "onboarding": "passed",
                        "logical_restore": "exact row hashes match",
                        "restart": "passed",
                        "key_rotation": "passed with new key only",
                    }
                )
            )
            return 0
        finally:
            for name in reversed(containers):
                subprocess.run(["docker", "rm", "-f", "-v", name], capture_output=True)
            subprocess.run(["docker", "network", "rm", network], capture_output=True)


if __name__ == "__main__":
    raise SystemExit(main())
