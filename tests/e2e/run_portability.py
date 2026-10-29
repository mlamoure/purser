"""Cross-architecture backup portability drill, with durations (#428).

Run via (the target Docker daemon reached through a tunnel, as in #426):
  uv run --group e2e python tests/e2e/run_portability.py \
      --source-image purser:dev --target-image purser:arm64 \
      --target-docker unix:///path/to/remote.sock --target-port 18431

1. A source instance is seeded through REST with a representative synthetic
   dataset: four accounts with several years of QIF history (payees and
   memos are encrypted narrative), a manually tracked position with a
   recorded close, and an agent token.
2. Its database is dumped (pg_dump -Fc) and restored into a fresh database
   on the TARGET Docker host, which may be another architecture; both are
   timed.
3. Every public table's row count and content hash must match, and the
   target app, given only the same key, must sign in, report the same
   transaction count and balances, and decrypt a narrative.

Only uniquely named, labelled test resources are created and removed, on
both hosts.
"""

import argparse
import asyncio
import http.cookiejar
import json
import os
import random
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

LABEL = "purser.hardening.disposable=true"
ADMIN = {"email": "portability@example.com", "password": "synthetic-portability-password"}
NARRATIVE = "Synthetic portability narrative"
ACCOUNTS = (
    ("Port Checking", "checking", "assets.port_checking"),
    ("Port Savings", "savings", "assets.port_savings"),
    ("Port Card", "credit_card", "liabilities.port_card"),
    ("Port Joint", "checking", "assets.port_joint"),
)


class Host:
    """One Docker daemon (DOCKER_HOST) and the port its app publishes."""

    def __init__(self, docker_host: str | None, port: int, url_host: str) -> None:
        self.env = {**os.environ}
        if docker_host:
            self.env["DOCKER_HOST"] = docker_host
        self.port = port or _free_port()
        self.url = f"http://{url_host}:{self.port}"

    def docker(self, *args: str, data: bytes | None = None) -> bytes:
        result = subprocess.run(["docker", *args], input=data, capture_output=True, env=self.env)
        if result.returncode:
            # Command/env-file contents may contain credentials: do not echo argv.
            raise RuntimeError(f"docker {args[0]} failed: {result.stderr.decode()[-1000:]}")
        return result.stdout

    def arch(self, image: str) -> str:
        return self.docker("image", "inspect", "-f", "{{.Architecture}}", image).decode().strip()

    def wait_database(self, name: str) -> None:
        for _ in range(90):
            ok = subprocess.run(
                ["docker", "exec", name, "pg_isready", "-h", "127.0.0.1", "-U", "purser"],
                capture_output=True,
                env=self.env,
            )
            if ok.returncode == 0:
                return
            time.sleep(1)
        raise RuntimeError("PostgreSQL did not become ready")

    def sql(self, db: str, query: str) -> str:
        return self.docker("exec", db, "psql", "-U", "purser", "-At", "-c", query).decode().strip()

    def snapshot(self, db: str) -> dict[str, str]:
        tables = self.sql(
            db, "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY 1"
        ).splitlines()
        out = {}
        for table in tables:
            if not table.replace("_", "").isalnum():
                raise ValueError("unexpected table identifier")
            out[table] = self.sql(
                db,
                "SELECT count(*)::text || ':' || md5(coalesce("
                "string_agg(to_jsonb(t)::text, '' ORDER BY to_jsonb(t)::text), '')) "
                f'FROM public."{table}" t',
            )
        return out


def _free_port() -> int:
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        return int(reservation.getsockname()[1])


def wait_app(url: str) -> None:
    for _ in range(120):
        try:
            with urllib.request.urlopen(url + "/healthz", timeout=3) as r:
                if json.load(r) == {"status": "ok", "database": "ok"}:
                    return
        except (OSError, urllib.error.URLError, ValueError):
            pass
        time.sleep(1)
    raise RuntimeError(f"Purser at {url} did not become healthy")


class Client:
    def __init__(self, url: str) -> None:
        self.url = url
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )

    def ok(self, method: str, path: str, body: Any = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            self.url + path, data=data, method=method, headers={"Content-Type": "application/json"}
        )
        try:
            with self.opener.open(request, timeout=300) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"{method} {path}: {exc.code} {exc.read()[:500]!r}") from exc
        return json.loads(raw) if raw else None


def synthetic_qif(name: str, rows: int, seed: int) -> str:
    """Several years of one account's history: synthetic payees, memos and
    categories, amounts as decimal strings."""
    rng = random.Random(seed)
    start = date(2022, 1, 3)
    lines = ["!Account", f"N{name}", "TBank", "^", "!Type:Bank"]
    for i in range(rows):
        day = start + timedelta(days=i * 1000 // max(rows, 1))
        cents = rng.randint(100, 25000) * (1 if rng.random() < 0.15 else -1)
        amount = Decimal(cents) / 100
        lines += [
            f"D{day.month}/{day.day}/{day.year}",
            f"T{amount:.2f}",
            f"PSynthetic Store {rng.randint(1, 400)}",
            f"M{NARRATIVE} {seed}-{i}",
            f"L{rng.choice(['Groceries', 'Utilities', 'Dining', 'Travel', 'Salary'])}",
            "^",
        ]
    return "\n".join(lines) + "\n"


def seed(client: Client, per_account: int) -> dict[str, Any]:
    client.ok("POST", "/auth/setup", ADMIN)
    account_map = {}
    for i, (name, type_, path) in enumerate(ACCOUNTS):
        client.ok("POST", "/accounts", {"name": name, "type": type_, "path": path})
        account_map[name] = path
        client.ok(
            "POST",
            "/import/qif",
            {"content": synthetic_qif(name, per_account, i), "account_map": {name: path}},
        )
    broker = client.ok(
        "POST", "/accounts", {"name": "Port Broker", "type": "brokerage", "path": "assets.port_brk"}
    )
    client.ok("PATCH", f"/accounts/{broker['id']}", {"holdings_source": "manual"})
    client.ok(
        "POST",
        f"/accounts/{broker['id']}/positions",
        {"symbol": "PORTX", "quantity": "12.5", "as_of": "2024-06-03"},
    )
    client.ok("POST", "/prices", {"symbol": "PORTX", "date": "2024-06-03", "close": "41.25"})
    token = client.ok(
        "POST", "/tokens", {"name": "portability-agent", "scopes": ["read", "write"]}
    )["token"]
    return {**facts(client), "_agent_token": token}


def facts(client: Client) -> dict[str, Any]:
    """What the app reports, to compare across the restore."""
    total = client.ok("GET", "/transactions?limit=1")["total"]
    balances = sorted(
        (a["path"], a.get("balance"))
        for a in client.ok("GET", "/accounts")
        if not a.get("is_virtual")
    )
    sample = client.ok("GET", "/transactions?limit=5&account=assets.port_checking")["items"]
    narratives = sorted(
        f"{t.get('description')}|{t.get('payee')}|{t.get('amount')}" for t in sample
    )
    return {"transactions": total, "balances": balances, "sample": narratives}


async def mcp_accounts(url: str, token: str) -> list[str]:
    """The agent's view after the restore: the token minted before the dump
    still authenticates over MCP and reads the restored accounts."""
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async with (
        httpx2.AsyncClient(headers={"Authorization": "Bearer " + token}) as http,
        streamable_http_client(url + "/mcp", http_client=http) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        result = await session.call_tool("list_accounts", {})
        assert not result.is_error, result
        listed = (result.structured_content or {}).get("result", [])  # a list tool's wrapper
        return sorted(a["path"] for a in listed)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-image", required=True)
    parser.add_argument("--target-image", required=True)
    parser.add_argument("--source-docker", default=None, help="DOCKER_HOST for the source side")
    parser.add_argument("--target-docker", default=None, help="DOCKER_HOST for the target side")
    parser.add_argument("--source-port", type=int, default=0)
    parser.add_argument("--target-port", type=int, default=0)
    parser.add_argument("--postgres-image", default="postgres:16")
    parser.add_argument("--per-account", type=int, default=5000, help="QIF rows per account")
    args = parser.parse_args()
    source = Host(args.source_docker, args.source_port, "127.0.0.1")
    target = Host(args.target_docker, args.target_port, "127.0.0.1")
    prefix = "purser-port-" + uuid.uuid4().hex[:10]
    report: dict[str, Any] = {
        "source": f"{args.source_image} ({source.arch(args.source_image)})",
        "target": f"{args.target_image} ({target.arch(args.target_image)})",
    }
    created: list[tuple[Host, str, str]] = []  # (host, kind, name)
    with tempfile.TemporaryDirectory(prefix="purser-port-") as temp:
        key = source.docker("run", "--rm", args.source_image, "keygen").decode().strip()
        password = uuid.uuid4().hex

        def env_files(side: str, db: str, url: str) -> tuple[Path, Path]:
            db_env = Path(temp) / f"{side}-db.env"
            db_env.write_text(
                f"POSTGRES_USER=purser\nPOSTGRES_DB=purser\nPOSTGRES_PASSWORD={password}\n"
            )
            app_env = Path(temp) / f"{side}-app.env"
            app_env.write_text(
                f"PURSER_DATABASE_URL=postgresql+psycopg://purser:{password}@{db}:5432/purser\n"
                f"PURSER_ENCRYPTION_KEY={key}\nPURSER_PUBLIC_URL={url}\n"
                "PURSER_DISABLE_SCHEDULER=true\n"
            )
            for f in (db_env, app_env):
                f.chmod(0o600)
            return db_env, app_env

        def start(
            host: Host, side: str, image: str, *, restore: bytes | None = None
        ) -> tuple[float, dict[str, str]]:
            network, db, app = f"{prefix}-{side}-net", f"{prefix}-{side}-db", f"{prefix}-{side}-app"
            db_env, app_env = env_files(side, db, host.url)
            host.docker("network", "create", "--label", LABEL, network)
            created.append((host, "network", network))
            created.append((host, "container", db))
            host.docker(
                "run", "-d", "--name", db, "--label", LABEL, "--network", network,
                "--env-file", str(db_env), args.postgres_image,
            )  # fmt: skip
            host.wait_database(db)
            took = 0.0
            if restore is not None:
                # pg_dump excludes cluster roles; the read-model grants need this one.
                host.sql(db, "CREATE ROLE purser_read NOLOGIN")
                began = time.monotonic()
                host.docker(
                    "exec", "-i", db, "pg_restore", "--exit-on-error", "-U", "purser",
                    "-d", "purser", data=restore,
                )  # fmt: skip
                took = time.monotonic() - began
            restored = host.snapshot(db) if restore is not None else {}
            created.append((host, "container", app))
            host.docker(
                "run", "-d", "--name", app, "--label", LABEL, "--network", network,
                "--env-file", str(app_env), "-p", f"127.0.0.1:{host.port}:8000", image,
            )  # fmt: skip
            wait_app(host.url)
            return took, restored

        try:
            start(source, "src", args.source_image)
            began = time.monotonic()
            before_facts = seed(Client(source.url), args.per_account)
            report["seed_seconds"] = round(time.monotonic() - began, 1)
            source_db = f"{prefix}-src-db"
            # The app writes as it runs (token use, sessions): stop it first.
            source.docker("stop", f"{prefix}-src-app")
            before = source.snapshot(source_db)
            began = time.monotonic()
            dump = source.docker("exec", source_db, "pg_dump", "-U", "purser", "-Fc", "purser")
            report["dump_seconds"] = round(time.monotonic() - began, 1)
            report["dump_bytes"] = len(dump)
            took, after = start(target, "dst", args.target_image, restore=dump)
            report["restore_seconds"] = round(took, 1)
            differing = sorted(
                t for t in before.keys() | after.keys() if before.get(t) != after.get(t)
            )
            assert not differing, f"restored tables differ: {differing}"
            restored = Client(target.url)
            restored.ok("POST", "/auth/login", ADMIN)
            token = before_facts.pop("_agent_token")
            after_facts = facts(restored)
            assert after_facts == before_facts, (before_facts, after_facts)
            # Importing the same file again after the restore adds nothing
            # (content-hash idempotency survives the dump).
            name, _, path = ACCOUNTS[0]
            again = restored.ok(
                "POST",
                "/import/qif",
                {"content": synthetic_qif(name, args.per_account, 0), "account_map": {name: path}},
            )
            assert again["imported"] == 0, again
            assert facts(restored)["transactions"] == before_facts["transactions"]
            seen = asyncio.run(mcp_accounts(target.url, token))
            assert path in seen, seen
            report["after_restore"] = "re-import adds 0 rows; pre-dump agent token reads over MCP"
            assert any(NARRATIVE in m for m in after_facts["sample"]), "narrative not decrypted"
            report["tables"] = len(after)
            report["rows"] = sum(int(v.split(":")[0]) for v in after.values())
            report["transactions"] = after_facts["transactions"]
            report["result"] = "exact row hashes match; sign-in, balances and narratives identical"
            print(json.dumps(report))
            return 0
        finally:
            for host, kind, name in reversed(created):
                cmd = (
                    ["docker", "rm", "-f", name]
                    if kind == "container"
                    else [
                        "docker",
                        "network",
                        "rm",
                        name,
                    ]
                )
                subprocess.run(cmd, capture_output=True, check=False, env=host.env)


if __name__ == "__main__":
    sys.exit(main())
