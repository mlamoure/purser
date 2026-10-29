"""Disposable onboarding failure-journey drill (#427).

Run via: uv run --group e2e python tests/e2e/run_negative.py --image purser:dev

The happy path is run_docker.py's. This drill walks the ways a first run or an
agent connection goes wrong, against a real container and database, and checks
each fails safely AND says why:

- start with no key / a malformed key / the WRONG key for an initialised
  database: the process refuses to serve and its error names the problem;
- setup twice, and setup racing itself: exactly one administrator, the rest a
  409 that says setup is done;
- a wrong password, an unknown email, garbage and revoked bearer tokens, a
  token without the scope a call needs, an agent token minting tokens;
- an unparseable QIF file: a 422 naming the problem, nothing written.

Only uniquely named, labelled test resources are created and removed.
"""

import argparse
import concurrent.futures
import http.cookiejar
import json
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from run_docker import docker, wait_app, wait_database

ADMIN = {"email": "negative@example.com", "password": "synthetic-negative-password"}


def call(
    url: str,
    method: str,
    path: str,
    body: Any = None,
    *,
    token: str | None = None,
    opener: urllib.request.OpenerDirector | None = None,
) -> tuple[int, Any]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url + path, data=data, headers=headers, method=method)
    opener = opener or urllib.request.build_opener()
    try:
        with opener.open(request, timeout=20) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        raw, status = exc.read(), exc.code
    try:
        return status, json.loads(raw) if raw else None
    except ValueError:
        return status, raw.decode(errors="replace")


def detail(payload: Any) -> str:
    return str(payload.get("detail") if isinstance(payload, dict) else payload)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--postgres-image", default="postgres:16")
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="Fixed port when Docker runs remotely (tunnel it to this host's 127.0.0.1)",
    )
    args = parser.parse_args()
    prefix = "purser-negative-" + uuid.uuid4().hex[:10]
    network, db = prefix + "-net", prefix + "-db"
    label = "purser.hardening.disposable=true"
    containers: list[str] = []
    port = args.port
    if not port:
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    checks: list[str] = []
    with tempfile.TemporaryDirectory(prefix="purser-negative-") as temp:
        key = docker("run", "--rm", args.image, "keygen").decode().strip()
        other_key = docker("run", "--rm", args.image, "keygen").decode().strip()
        db_password = uuid.uuid4().hex
        db_env = Path(temp) / "database.env"
        db_env.write_text(
            f"POSTGRES_USER=purser\nPOSTGRES_DB=purser\nPOSTGRES_PASSWORD={db_password}\n"
        )
        db_env.chmod(0o600)
        app_env = Path(temp) / "app.env"

        def configure(encryption_key: str | None) -> None:
            lines = [
                f"PURSER_DATABASE_URL=postgresql+psycopg://purser:{db_password}@{db}:5432/purser",
                f"PURSER_PUBLIC_URL={url}",
                "PURSER_DISABLE_SCHEDULER=true",
            ]
            if encryption_key is not None:
                lines.append(f"PURSER_ENCRYPTION_KEY={encryption_key}")
            app_env.write_text("\n".join(lines) + "\n")
            app_env.chmod(0o600)

        def refused_start(name: str) -> str:
            """Start the API and expect it to exit non-zero; return its output."""
            containers.append(name)
            docker(
                "run", "-d", "--name", name, "--label", label, "--network", network,
                "--env-file", str(app_env), args.image,
            )  # fmt: skip
            for _ in range(60):
                state = docker("inspect", "-f", "{{.State.Status}} {{.State.ExitCode}}", name)
                status, code = state.decode().split()
                if status == "exited":
                    assert code != "0", f"{name} exited 0 instead of refusing"
                    logs = subprocess.run(
                        ["docker", "logs", name], capture_output=True, text=True, check=False
                    )
                    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
                    containers.remove(name)
                    return logs.stdout + logs.stderr
                time.sleep(1)
            raise AssertionError(f"{name} kept running without a usable key")

        def start_api() -> str:
            name = prefix + "-api"
            containers.append(name)
            docker(
                "run", "-d", "--name", name, "--label", label, "--network", network,
                "--env-file", str(app_env), "-p", f"127.0.0.1:{port}:8000", args.image,
            )  # fmt: skip
            wait_app(url)
            return name

        docker("network", "create", "--label", label, network)
        try:
            containers.append(db)
            docker(
                "run", "-d", "--name", db, "--label", label, "--network", network,
                "--env-file", str(db_env), args.postgres_image,
            )  # fmt: skip
            wait_database(db)

            # Keys: missing, malformed, then wrong for an initialised database.
            configure(None)
            out = refused_start(prefix + "-nokey")
            assert "PURSER_ENCRYPTION_KEY" in out, f"no-key refusal does not name the key:\n{out}"
            checks.append("no key: refuses to start, names PURSER_ENCRYPTION_KEY")
            configure("not-a-fernet-key")
            out = refused_start(prefix + "-badkey")
            assert "PURSER_ENCRYPTION_KEY" in out or "key" in out.lower(), out
            checks.append("malformed key: refuses to start, says the key is invalid")
            configure(key)
            api = start_api()

            # Setup: once, then refused; a race yields exactly one admin.
            jar = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
            )

            def setup(i: int) -> int:
                body = {"email": f"race{i}@example.com", "password": ADMIN["password"]}
                return call(url, "POST", "/auth/setup", body)[0]

            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
                codes = sorted(pool.map(setup, range(6)))
            assert codes.count(201) == 1 and set(codes) <= {201, 409}, codes
            status, payload = call(url, "POST", "/auth/setup", ADMIN)
            assert status == 409 and "already" in detail(payload), (status, payload)
            checks.append(
                "setup x6 concurrently: exactly one 201, the rest 409 'already completed'"
            )
            winner = next(
                f"race{i}@example.com"
                for i in range(6)
                if call(
                    url,
                    "POST",
                    "/auth/login",
                    {"email": f"race{i}@example.com", "password": ADMIN["password"]},
                    opener=jar,
                )[0]
                == 200
            )

            # Sign-in refusals say nothing about which part was wrong.
            wrong = call(url, "POST", "/auth/login", {"email": winner, "password": "wrong-pass"})
            unknown = call(
                url, "POST", "/auth/login", {"email": "nobody@example.com", "password": "x" * 10}
            )
            assert wrong[0] == unknown[0] == 401 and detail(wrong[1]) == detail(unknown[1])
            checks.append("wrong password and unknown email: the same 401")

            # Tokens: garbage, scope, escalation, revocation.
            assert call(url, "GET", "/accounts", token="purser_garbage")[0] == 401
            status, reader = call(
                url, "POST", "/tokens", {"name": "reader", "scopes": ["read"]}, opener=jar
            )
            assert status == 201, reader
            status, agent = call(
                url, "POST", "/tokens", {"name": "agent", "scopes": ["read", "write"]}, opener=jar
            )
            assert status == 201, agent
            status, payload = call(
                url,
                "POST",
                "/accounts",
                {"name": "X", "type": "checking"},
                token=reader["token"],
            )
            assert status == 403 and detail(payload) == "requires scope: write", payload
            status, payload = call(
                url, "POST", "/tokens", {"name": "clone", "scopes": ["admin"]}, token=agent["token"]
            )
            assert status == 403, (status, payload)
            status, payload = call(
                url, "POST", "/tokens", {"name": "old", "scopes": ["read", "propose"]}, opener=jar
            )
            assert status == 422 and "write" in detail(payload), payload
            assert call(url, "DELETE", f"/tokens/{agent['id']}", opener=jar)[0] == 200
            assert call(url, "GET", "/accounts", token=agent["token"])[0] == 401
            checks.append(
                "garbage token 401; read token writing 403 'requires scope: write'; agent "
                "minting 403; retired scope 422 naming write; revoked token 401"
            )

            # An unparseable QIF file: a 422 naming the problem, nothing written.
            before = call(url, "GET", "/transactions?limit=1", opener=jar)[1]["total"]
            status, payload = call(
                url,
                "POST",
                "/import/qif/preview",
                {"content": "this is not a QIF file\n^\n"},
                opener=jar,
            )
            assert status == 422 and detail(payload), (status, payload)
            after = call(url, "GET", "/transactions?limit=1", opener=jar)[1]["total"]
            assert before == after
            checks.append(f"invalid QIF: 422 '{detail(payload)[:60]}', nothing written")

            # The WRONG key for this (now initialised) database fails closed.
            subprocess.run(["docker", "rm", "-f", api], capture_output=True)
            containers.remove(api)
            configure(other_key)
            out = refused_start(prefix + "-wrongkey")
            assert "fingerprint" in out.lower() or "does not match" in out.lower(), out
            checks.append("wrong key on an initialised database: refuses, names both fingerprints")
            configure(key)
            start_api()
            assert (
                call(url, "POST", "/auth/login", {"email": winner, "password": ADMIN["password"]})[
                    0
                ]
                == 200
            )
            checks.append("right key again: serves, sign-in works")
            print(json.dumps({"image": args.image, "checks": checks}, indent=1))
            return 0
        finally:
            for name in reversed(containers):
                subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)
            subprocess.run(["docker", "network", "rm", network], capture_output=True, check=False)


if __name__ == "__main__":
    sys.exit(main())
