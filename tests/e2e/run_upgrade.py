"""Disposable cross-version upgrade and rollback drill (#428).

Run via:
  uv run --group e2e python tests/e2e/run_upgrade.py \
      --old-image ghcr.io/mlamoure/purser:v2026.10.0 --new-image purser:dev

1. The OLD image takes first-run setup and real writes over REST: accounts, a
   category, posted and pending transactions (encrypted narrative), a schedule,
   and an agent token with the old scopes.
2. A pre-upgrade logical backup is taken (the documented rollback point).
3. The NEW image's API and worker start at the same moment against that
   database: startup migrations must run once, and both must come up healthy.
4. The upgraded data must answer exactly as before (balances, rows, decrypted
   narrative); the old agent token keeps working with its converted scopes.
5. Rollback rehearsal: the pre-upgrade backup restores into a fresh database
   and the OLD image serves it, with the same key, exactly as before.

Only uniquely named, labelled test resources are created and removed. Never
point this at production volumes, credentials or databases.
"""

import argparse
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

from run_docker import docker, snapshot, wait_app, wait_database

ADMIN = {"email": "upgrade@example.com", "password": "synthetic-upgrade-password"}
NARRATIVE = "Upgrade drill encrypted narrative"


class Client:
    def __init__(self, url: str, token: str | None = None) -> None:
        self.url = url
        self.token = token
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )

    def call(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.url + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=20) as response:
                return response.status, _body(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, _body(exc.read())

    def ok(self, method: str, path: str, body: Any = None) -> Any:
        status, payload = self.call(method, path, body)
        if status not in (200, 201):
            raise RuntimeError(f"{method} {path} -> {status}: {payload}")
        return payload


def _body(raw: bytes) -> Any:
    """JSON when the server sent JSON; an error page (a 500 during a database
    outage, say) comes back as text."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw.decode(errors="replace")


def seed(url: str) -> str:
    """First-run setup and writes on the OLD version; returns the agent token."""
    admin = Client(url)
    admin.ok("POST", "/auth/setup", ADMIN)
    for account in (
        {"name": "Upgrade Checking", "type": "checking", "path": "assets.upgrade_checking"},
        {"name": "Upgrade Card", "type": "credit_card", "path": "liabilities.upgrade_card"},
    ):
        admin.ok("POST", "/accounts", account)
    admin.ok(
        "POST",
        "/categories",
        {"name": "Upgrade Groceries", "type": "category_expense", "path": "expenses.upgrade_food"},
    )
    for amount, status, account in (
        ("-41.27", "posted", "assets.upgrade_checking"),
        ("-12.09", "pending", "assets.upgrade_checking"),
        ("-88.40", "posted", "liabilities.upgrade_card"),
    ):
        admin.ok(
            "POST",
            "/transactions",
            {
                "account": account,
                "category": "expenses.upgrade_food",
                "amount": amount,
                "date": "2026-07-15",
                "status": status,
                "payee": "Example Grocer",
                "description": NARRATIVE,
            },
        )
    admin.ok(
        "POST",
        "/schedules",
        {
            "name": "Upgrade rent",
            "account": "assets.upgrade_checking",
            "rrule": "FREQ=MONTHLY;BYMONTHDAY=1",
            "amount": "-1200.00",
            "category": "expenses.upgrade_food",
        },
    )
    minted = admin.ok("POST", "/tokens", {"name": "upgrade-agent", "scopes": ["read", "propose"]})
    return str(minted["token"])


def observe(url: str) -> dict[str, Any]:
    """What a user sees: balances, rows (with decrypted narrative), schedules."""
    admin = Client(url)
    admin.ok("POST", "/auth/login", ADMIN)
    accounts = admin.ok("GET", "/accounts?include_balances=true")
    page = admin.ok("GET", "/transactions?limit=200&include_collapsed=true")
    schedules = admin.ok("GET", "/schedules")
    return {
        "balances": sorted((a["path"], a.get("balance")) for a in accounts),
        "rows": sorted(
            (t["date"], t["amount"], t["status"], t["description"], t["payee"])
            for t in page["items"]
        ),
        "schedules": sorted((s["name"], s["status"]) for s in schedules),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old-image", required=True)
    parser.add_argument("--new-image", required=True)
    parser.add_argument("--postgres-image", default="postgres:16")
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="Fixed port when Docker runs remotely (tunnel it to this host's 127.0.0.1)",
    )
    args = parser.parse_args()
    prefix = "purser-upgrade-" + uuid.uuid4().hex[:10]
    network = prefix + "-net"
    db, rollback_db = prefix + "-db", prefix + "-rollback"
    label = "purser.hardening.disposable=true"
    containers: list[str] = []
    port = args.port
    if not port:
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    with tempfile.TemporaryDirectory(prefix="purser-upgrade-") as temp:
        key = docker("run", "--rm", args.new_image, "keygen").decode().strip()
        db_password = uuid.uuid4().hex
        db_env = Path(temp) / "database.env"
        db_env.write_text(
            f"POSTGRES_USER=purser\nPOSTGRES_DB=purser\nPOSTGRES_PASSWORD={db_password}\n"
        )
        db_env.chmod(0o600)
        app_env = Path(temp) / "app.env"

        def configure(database: str) -> None:
            app_env.write_text(
                f"PURSER_DATABASE_URL=postgresql+psycopg://purser:{db_password}@{database}:5432/purser\n"
                f"PURSER_ENCRYPTION_KEY={key}\nPURSER_PUBLIC_URL={url}\n"
                "PURSER_DISABLE_SCHEDULER=true\n"
            )
            app_env.chmod(0o600)

        def start_database(name: str) -> None:
            containers.append(name)
            docker(
                "run", "-d", "--name", name, "--label", label, "--network", network,
                "--env-file", str(db_env), args.postgres_image,
            )  # fmt: skip
            wait_database(name)

        def start(name: str, image: str, *command: str, publish: bool) -> None:
            containers.append(name)
            ports = ["-p", f"127.0.0.1:{port}:8000"] if publish else []
            docker(
                "run", "-d", "--name", name, "--label", label, "--network", network,
                "--env-file", str(app_env), *ports, image, *command,
            )  # fmt: skip

        def stop(name: str) -> None:
            docker("rm", "-f", name)
            containers.remove(name)

        def sql(database: str, query: str) -> str:
            out = docker("exec", database, "psql", "-U", "purser", "-At", "-c", query)
            return out.decode().strip()

        docker("network", "create", "--label", label, network)
        try:
            # 1. The old version, with real writes.
            start_database(db)
            configure(db)
            start(prefix + "-old", args.old_image, publish=True)
            wait_app(url)
            agent_token = seed(url)
            before = observe(url)
            old_revision = sql(db, "SELECT version_num FROM alembic_version")
            stop(prefix + "-old")

            # 2. The pre-upgrade backup: the rollback point.
            backup = docker("exec", db, "pg_dump", "-U", "purser", "-Fc", "purser")
            backup_snapshot = snapshot(db)

            # 3. New API and worker at the same moment: migrations must run once.
            start(prefix + "-api", args.new_image, publish=True)
            start(prefix + "-worker", args.new_image, "worker", publish=False)
            wait_app(url)
            time.sleep(5)  # let the worker finish its own startup
            states = {
                name: docker("inspect", "-f", "{{.State.Status}}", name).decode().strip()
                for name in (prefix + "-api", prefix + "-worker")
            }
            assert all(state == "running" for state in states.values()), states
            new_revision = sql(db, "SELECT version_num FROM alembic_version")
            revisions = sql(db, "SELECT count(*) FROM alembic_version")
            assert revisions == "1", f"alembic_version has {revisions} rows"
            assert new_revision != old_revision, "the new image did not migrate"
            logs = "".join(
                subprocess.run(
                    ["docker", "logs", name], capture_output=True, check=False, text=True
                ).stdout
                + subprocess.run(
                    ["docker", "logs", name], capture_output=True, check=False, text=True
                ).stderr
                for name in (prefix + "-api", prefix + "-worker")
            )
            assert "Traceback" not in logs, "a startup traceback in the API or worker logs"

            # 4. The same answers, and the old agent token still works.
            after = observe(url)
            assert after == before, f"upgraded data differs:\n{before}\n{after}"
            agent = Client(url, agent_token)
            me = agent.ok("GET", "/auth/me")
            assert sorted(me["scopes"]) == ["read", "write"], me["scopes"]
            written = agent.ok(
                "POST",
                "/transactions",
                {
                    "account": "assets.upgrade_checking",
                    "category": "expenses.upgrade_food",
                    "amount": "-3.21",
                    "date": "2026-07-16",
                    "status": "posted",
                    "description": "post-upgrade agent write",
                },
            )
            assert written["status"] == "posted" and written["origin"] == "agent", written
            migrated = sql(
                db, "SELECT count(*) FROM audit_log WHERE action = 'token.scopes_migrate'"
            )
            assert int(migrated) >= 2, "token conversions were not audited"
            stop(prefix + "-worker")
            stop(prefix + "-api")

            # 5. Rollback: the pre-upgrade backup with the old image and the same key.
            start_database(rollback_db)
            docker(
                "exec", rollback_db, "psql", "-U", "purser", "-v", "ON_ERROR_STOP=1",
                "-c", "CREATE ROLE purser_read NOLOGIN",
            )  # fmt: skip
            docker(
                "exec", "-i", rollback_db, "pg_restore", "--exit-on-error", "-U", "purser",
                "-d", "purser", data=backup,
            )  # fmt: skip
            assert snapshot(rollback_db) == backup_snapshot, "the backup did not restore exactly"
            configure(rollback_db)
            start(prefix + "-rolled-back", args.old_image, publish=True)
            wait_app(url)
            assert observe(url) == before, "the rolled-back instance answers differently"
            print(
                json.dumps(
                    {
                        "old_image": args.old_image,
                        "new_image": args.new_image,
                        "migrated": f"{old_revision} -> {new_revision}",
                        "concurrent_api_and_worker": "both running, one alembic revision",
                        "data_after_upgrade": "identical (balances, rows, narrative, schedules)",
                        "old_agent_token": "converted to read+write, posts a row",
                        "rollback": "pre-upgrade backup + old image + same key: identical",
                    }
                )
            )
            return 0
        finally:
            for name in reversed(containers):
                subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)
            subprocess.run(["docker", "network", "rm", network], capture_output=True, check=False)


if __name__ == "__main__":
    sys.exit(main())
