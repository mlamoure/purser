"""Disposable failure-recovery drill (#428).

Run via:
  uv run --group e2e python tests/e2e/run_failures.py \
      --old-image ghcr.io/mlamoure/purser:v2026.10.0 --new-image purser:dev

1. Interrupted migration: the database is migrated by the OLD image, then the
   NEW image's `migrate` is SIGKILLed while provably mid-transaction (a lock
   holds it at its first alembic_version update, after that step's DDL ran):
   the revision and the schema must be exactly the old ones. Then five timed
   SIGKILLs: each leaves the old revision or the new one (Postgres DDL is
   transactional), never in between, and a final `migrate` reaches head.
2. Database outage while serving: the API reports 503 on /healthz and refuses
   writes while PostgreSQL is stopped, and recovers on its own once it is back,
   with no partial rows.
3. Hard termination: the API is SIGKILLed and started again; every committed
   row is still there and decrypts.

Only uniquely named, labelled test resources are created and removed.
"""

import argparse
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
from run_upgrade import ADMIN, NARRATIVE, Client


def health(url: str) -> int:
    try:
        with urllib.request.urlopen(url + "/healthz", timeout=3) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except OSError:
        return 0


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
    prefix = "purser-failure-" + uuid.uuid4().hex[:10]
    network, db = prefix + "-net", prefix + "-db"
    label = "purser.hardening.disposable=true"
    containers: list[str] = []
    port = args.port
    if not port:
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    report: dict[str, Any] = {"old_image": args.old_image, "new_image": args.new_image}
    with tempfile.TemporaryDirectory(prefix="purser-failure-") as temp:
        key = docker("run", "--rm", args.new_image, "keygen").decode().strip()
        db_password = uuid.uuid4().hex
        db_env = Path(temp) / "database.env"
        db_env.write_text(
            f"POSTGRES_USER=purser\nPOSTGRES_DB=purser\nPOSTGRES_PASSWORD={db_password}\n"
        )
        db_env.chmod(0o600)
        app_env = Path(temp) / "app.env"
        app_env.write_text(
            f"PURSER_DATABASE_URL=postgresql+psycopg://purser:{db_password}@{db}:5432/purser\n"
            f"PURSER_ENCRYPTION_KEY={key}\nPURSER_PUBLIC_URL={url}\n"
            "PURSER_DISABLE_SCHEDULER=true\n"
        )
        app_env.chmod(0o600)

        def sql(query: str) -> str:
            return docker("exec", db, "psql", "-U", "purser", "-At", "-c", query).decode().strip()

        def run(name: str, image: str, *command: str, publish: bool = False) -> None:
            containers.append(name)
            ports = ["-p", f"127.0.0.1:{port}:8000"] if publish else []
            docker(
                "run", "-d", "--name", name, "--label", label, "--network", network,
                "--env-file", str(app_env), *ports, image, *command,
            )  # fmt: skip

        def remove(name: str) -> None:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)
            if name in containers:
                containers.remove(name)

        def wait_exit(name: str, timeout: int = 180) -> int:
            for _ in range(timeout):
                state = docker("inspect", "-f", "{{.State.Status}} {{.State.ExitCode}}", name)
                status, code = state.decode().split()
                if status == "exited":
                    return int(code)
                time.sleep(1)
            raise RuntimeError(f"{name} did not exit")

        docker("network", "create", "--label", label, network)
        try:
            containers.append(db)
            docker(
                "run", "-d", "--name", db, "--label", label, "--network", network,
                "--env-file", str(db_env), args.postgres_image,
            )  # fmt: skip
            wait_database(db)

            # 1. Interrupted migration.
            run(prefix + "-old-migrate", args.old_image, "migrate")
            assert wait_exit(prefix + "-old-migrate") == 0, "the old image did not migrate"
            remove(prefix + "-old-migrate")
            old_revision = sql("SELECT version_num FROM alembic_version")
            fingerprint = (
                "SELECT md5(string_agg(table_name || '.' || column_name || ':' || data_type,"
                " ',' ORDER BY table_name, column_name)) FROM information_schema.columns"
                " WHERE table_schema = 'public'"
            )
            old_schema = sql(fingerprint)

            # Deterministic: kill `migrate` while it is provably inside its
            # transaction with DDL already run. Alembic runs every pending
            # step in one transaction and updates alembic_version after each;
            # a SHARE lock on that table lets it read the version and run the
            # first step's DDL, then blocks it on the version UPDATE.
            docker(
                "exec", "-d", "-e", "PGAPPNAME=purser-drill-lock", db, "psql", "-U", "purser",
                "-c", "BEGIN; LOCK TABLE alembic_version IN SHARE MODE; SELECT pg_sleep(600);",
            )  # fmt: skip
            name = prefix + "-migrate-blocked"
            run(name, args.new_image, "migrate")
            blocked = ""
            for _ in range(180):
                blocked = sql(
                    "SELECT pid FROM pg_stat_activity WHERE wait_event_type = 'Lock'"
                    " AND application_name <> 'purser-drill-lock'"
                    " AND query ILIKE '%alembic_version%' LIMIT 1"
                )
                if blocked:
                    break
                time.sleep(1)
            assert blocked, "migrate never reached the alembic_version update"
            ddl_locks = sql(
                "SELECT count(*) FROM pg_locks WHERE pid = "
                + blocked
                + " AND mode = 'AccessExclusiveLock' AND granted"
            )
            assert int(ddl_locks) > 0, "the blocked migrate had not run any DDL yet"
            subprocess.run(["docker", "kill", name], capture_output=True, check=False)
            remove(name)
            sql(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                " WHERE application_name = 'purser-drill-lock'"
            )
            # The orphaned backend gets its lock, then reads EOF from the dead
            # client and aborts; wait for it to go.
            for _ in range(60):
                if not sql(f"SELECT 1 FROM pg_stat_activity WHERE pid = {blocked}"):
                    break
                time.sleep(1)
            else:
                raise AssertionError("the killed migrate's backend never went away")
            assert sql("SELECT version_num FROM alembic_version") == old_revision
            assert sql(fingerprint) == old_schema, "DDL from a killed migrate survived"
            seen = set()
            for attempt, delay in enumerate((0.5, 1.0, 1.5, 2.0, 3.0)):
                name = f"{prefix}-migrate-{attempt}"
                run(name, args.new_image, "migrate")
                time.sleep(delay)
                subprocess.run(["docker", "kill", name], capture_output=True, check=False)
                remove(name)
                revision = sql("SELECT version_num FROM alembic_version")
                rows = sql("SELECT count(*) FROM alembic_version")
                assert rows == "1", f"alembic_version has {rows} rows after a kill"
                seen.add(revision)
            run(prefix + "-migrate-final", args.new_image, "migrate")
            assert wait_exit(prefix + "-migrate-final") == 0, "the final migrate failed"
            remove(prefix + "-migrate-final")
            head = sql("SELECT version_num FROM alembic_version")
            assert head != old_revision
            assert seen <= {old_revision, head} | _between(seen, old_revision, head), seen
            report["interrupted_migration"] = (
                f"SIGKILL mid-transaction ({ddl_locks} DDL locks held, blocked on the version "
                "update): "
                f"revision stayed {old_revision}, schema unchanged; 5 timed SIGKILLs during "
                f"{old_revision} -> {head} saw {sorted(seen)}; final migrate reached head"
            )

            # A real instance with data on the new image.
            api = prefix + "-api"
            run(api, args.new_image, publish=True)
            wait_app(url)
            admin = Client(url)
            admin.ok("POST", "/auth/setup", ADMIN)
            admin.ok(
                "POST",
                "/accounts",
                {"name": "Failure Checking", "type": "checking", "path": "assets.fail_checking"},
            )
            admin.ok(
                "POST",
                "/categories",
                {"name": "Failure Food", "type": "category_expense", "path": "expenses.fail_food"},
            )

            def write(amount: str) -> tuple[int, Any]:
                return admin.call(
                    "POST",
                    "/transactions",
                    {
                        "account": "assets.fail_checking",
                        "category": "expenses.fail_food",
                        "amount": amount,
                        "date": "2026-07-15",
                        "status": "posted",
                        "description": NARRATIVE,
                    },
                )

            assert write("-10.00")[0] == 201

            # 2. Database outage while serving.
            docker("stop", db)
            assert health(url) == 503, "healthz did not report the outage"
            status, _ = write("-20.00")
            assert status >= 500, f"a write during the outage answered {status}"
            docker("start", db)
            wait_database(db)
            recovered = False
            for _ in range(60):
                if health(url) == 200:
                    recovered = True
                    break
                time.sleep(1)
            assert recovered, "the API did not recover after the database came back"
            admin.ok("POST", "/auth/login", ADMIN)
            assert write("-30.00")[0] == 201, "no writes after the database came back"
            amounts = sorted(
                t["amount"] for t in admin.ok("GET", "/transactions?limit=50")["items"]
            )
            assert amounts == ["-10.0000", "-30.0000"], f"partial or lost rows: {amounts}"
            report["database_outage"] = (
                "healthz 503 and writes refused while down; recovered without a restart; "
                "no partial rows"
            )

            # 3. Hard termination.
            docker("kill", api)
            remove(api)
            run(api, args.new_image, publish=True)
            wait_app(url)
            admin = Client(url)
            admin.ok("POST", "/auth/login", ADMIN)
            items = admin.ok("GET", "/transactions?limit=50")["items"]
            assert sorted(t["amount"] for t in items) == ["-10.0000", "-30.0000"]
            assert all(t["description"] == NARRATIVE for t in items), "narrative lost"
            report["sigkill_restart"] = "every committed row survived and decrypts"
            print(json.dumps(report))
            return 0
        finally:
            for name in reversed(containers):
                subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)
            subprocess.run(["docker", "network", "rm", network], capture_output=True, check=False)


def _between(seen: set[str], old: str, head: str) -> set[str]:
    """Revisions strictly between old and head: an intermediate revision is a
    COMPLETED earlier migration (each Alembic step commits on its own), which
    is a consistent state too — never a half-applied one."""
    try:
        low, high = int(old), int(head)
    except ValueError:
        return set()
    return {r for r in seen if r.isdigit() and low < int(r) < high}


if __name__ == "__main__":
    sys.exit(main())
