"""Setup drill: follow the published setup guide on a fresh host (#581).

The other drills start Purser themselves. This one tests the *guide*: it runs
the shell commands printed in docs/src/content/docs/tutorials/ai-setup.md, in
order, as the user's agent would, on a host that has Docker and nothing else.
It then plays the person's part of the first-run wizard in a real browser,
connects each supported MCP client the way the guide tells it to, and repeats
the connection from a second machine over plain HTTP.

Supported setups, and so the scenarios (decided 2026-11-21):

  linux   A Linux host that already runs Docker. Purser is used from the same
          machine and from another machine on the home network over plain
          HTTP. The default host is a disposable "rig" container (Ubuntu,
          systemd, Docker Engine, an ordinary user); `--host ssh:USER@HOST`
          runs the same drill on a real machine.
  macos   A Mac with no Docker. `--macos-script` first runs
          scripts/install-docker-macos.sh there (it installs Colima), then
          the drill is the same. Needs `--host ssh:USER@MAC`.

Not tested, on purpose: access from outside the home network, reverse
proxies, TLS certificates of any kind, and Linux hosts without Docker (the
guide stops there and tells the person to install it).

Run (from the repository root):

  uv run --group e2e python tests/e2e/run_setup_drill.py --image purser:dev
  uv run --group e2e python tests/e2e/run_setup_drill.py --published
  uv run --group e2e python tests/e2e/run_setup_drill.py --image purser:dev --port 8010
  uv run --group e2e python tests/e2e/run_setup_drill.py --published \
      --host ssh:me@my-mac --macos-script --lan-address 192.168.1.20

`--image` loads a local image under the name the compose file expects and
uses this checkout's compose file (the only two stand-ins; the report says
so). `--published` downloads and pulls exactly what the guide names.
`--clients` adds real clients: `gemini` (free: it only lists the server) and
the agent CLIs `claude` and `codex`, which spend the operator's own
subscription. The default clients (`http`, `bridge`) cost nothing. Everything
created is disposable and removed unless `--keep`.

`--agent claude` replaces the scripted agent with a real one: Claude Code gets
the sentence a person would give it ("Set up Purser for me by following ...")
and where things run, and follows the guide itself on the rig, which it
reaches as "my home server" from "my laptop". The harness plays the person at
each handoff. This is the check that the guide can be followed by an agent
that has never seen Purser; run it before a release that changes the guide.
The agent may run `./host`, `curl`, and Purser's own tools, nothing else.

The checks have stable ids so two runs can be compared; `--report` writes them
as JSON. A check that fails is a regression in the guide, the compose file,
the wizard or the server, and its line says which.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
GUIDE = REPO / "docs/src/content/docs/tutorials/ai-setup.md"
MAC_SCRIPT = REPO / "scripts/install-docker-macos.sh"
# The SimpleFIN Bridge's public demo setup token (its developer guide prints it).
DEMO_SETUP_TOKEN = "aHR0cHM6Ly9iZXRhLWJyaWRnZS5zaW1wbGVmaW4ub3JnL3NpbXBsZWZpbi9jbGFpbS9ERU1P"
ADMIN_EMAIL = "drill@example.com"
ADMIN_PASSWORD = "synthetic-drill-password"
KEY_RE = re.compile(r"[A-Za-z0-9_-]{43}=")

RIG_DOCKERFILE = """\
FROM ubuntu:24.04
ENV container=docker DEBIAN_FRONTEND=noninteractive
RUN apt-get update \\
 && apt-get install -y --no-install-recommends systemd systemd-sysv dbus sudo curl \\
      ca-certificates iproute2 iptables kmod socat \\
 && curl -fsSL https://get.docker.com | sh \\
 && rm -rf /var/lib/apt/lists/* \\
 && mkdir -p /etc/docker \\
 && echo '{"features":{"containerd-snapshotter":false},"storage-driver":"overlay2"}' \\
      > /etc/docker/daemon.json \\
 && useradd -m -s /bin/bash -G sudo,docker alex && echo 'alex:synthetic' | chpasswd
STOPSIGNAL SIGRTMIN+3
VOLUME /var/lib/docker
CMD ["/sbin/init"]
"""


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------


@dataclass
class Check:
    id: str
    what: str
    ok: bool
    detail: str = ""


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    human_actions: list[str] = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)

    def add(self, id_: str, what: str, ok: bool, detail: str = "") -> bool:
        self.checks.append(Check(id_, what, bool(ok), detail.strip()[:400]))
        print(
            f"  [{'PASS' if ok else 'FAIL'}] {id_} {what}"
            + (f" — {detail.strip()[:200]}" if detail and not ok else ""),
            flush=True,
        )
        return bool(ok)

    def failed(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]

    def dump(self, path: str | None) -> None:
        print("\n== Setup drill ==")
        for c in self.checks:
            print(
                f"{'PASS' if c.ok else 'FAIL'}  {c.id:<4} {c.what}"
                + (f"\n          {c.detail}" if c.detail and not c.ok else "")
            )
        print(f"\nPerson's actions ({len(self.human_actions)}):")
        for i, action in enumerate(self.human_actions, 1):
            print(f"  {i}. {action}")
        for name, seconds in self.timings.items():
            print(f"time  {name}: {seconds:.0f}s")
        for note in self.notes:
            print(f"note  {note}")
        print(f"\n{len(self.checks) - len(self.failed())} passed, {len(self.failed())} failed")
        if path:
            Path(path).write_text(
                json.dumps(
                    {
                        "checks": [c.__dict__ for c in self.checks],
                        "human_actions": self.human_actions,
                        "timings": self.timings,
                        "notes": self.notes,
                    },
                    indent=1,
                )
            )


# --------------------------------------------------------------------------
# Hosts
# --------------------------------------------------------------------------


@dataclass
class Result:
    rc: int
    out: str

    @property
    def ok(self) -> bool:
        return self.rc == 0


class Host:
    """Where the guide's commands run. `sh` is the agent's shell: everything it
    prints is kept in `transcript`, which the drill later searches for secrets."""

    lan_address: str = ""

    def __init__(self) -> None:
        self.transcript: list[str] = []

    def _run(self, script: str, timeout: int) -> Result:
        raise NotImplementedError

    def sh(self, script: str, *, timeout: int = 900, record: bool = True) -> Result:
        result = self._run(script, timeout)
        if record:
            self.transcript.append(f"$ {script}\n{result.out}")
        return result

    def put(self, source: Path, name: str) -> str:
        """Copy a file to the host's home directory; returns its path there."""
        raise NotImplementedError

    def forward(self, port: int) -> subprocess.Popen[bytes] | threading.Thread:
        """Make http://127.0.0.1:PORT here reach 127.0.0.1:PORT on the host: the
        person's browser and the agent's MCP client on 'the same machine'."""
        raise NotImplementedError

    def start(self) -> None: ...

    def stop(self) -> None: ...


class RigHost(Host):
    """A disposable privileged container standing in for a fresh Linux server."""

    def __init__(self, keep: bool) -> None:
        super().__init__()
        self.name = "purser-drill-" + uuid.uuid4().hex[:8]
        # Named after its recipe, so it is built once and rebuilt when the recipe changes.
        self.image = "purser-drill-rig:" + hashlib.sha256(RIG_DOCKERFILE.encode()).hexdigest()[:12]
        self.keep = keep

    def start(self) -> None:
        if subprocess.run(
            ["docker", "image", "inspect", self.image], capture_output=True
        ).returncode:
            print("building the rig image (Ubuntu + Docker Engine)…", flush=True)
            subprocess.run(
                ["docker", "build", "-q", "-t", self.image, "-"],
                input=RIG_DOCKERFILE.encode(),
                check=True,
                capture_output=True,
            )
        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                self.name,
                "--hostname",
                "drillhost",
                "--label",
                "purser.drill.disposable=true",
                "--privileged",
                "--cgroupns=private",
                "--tmpfs",
                "/run",
                "--tmpfs",
                "/run/lock",
                self.image,
            ],
            check=True,
            capture_output=True,
        )
        for _ in range(60):
            if self.root("docker info >/dev/null 2>&1").ok:
                break
            time.sleep(1)
        else:
            raise RuntimeError("Docker did not start inside the rig")
        self.lan_address = subprocess.run(
            [
                "docker",
                "inspect",
                "-f",
                "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                self.name,
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def root(self, script: str, data: bytes | None = None) -> Result:
        """Rig plumbing, not part of the guide: never recorded as agent output."""
        r = subprocess.run(
            ["docker", "exec", "-i", self.name, "bash", "-c", script],
            input=data,
            capture_output=True,
        )
        return Result(r.returncode, (r.stdout + r.stderr).decode(errors="replace"))

    def _run(self, script: str, timeout: int) -> Result:
        r = subprocess.run(
            ["docker", "exec", "-u", "alex", "-w", "/home/alex", self.name, "bash", "-lc", script],
            capture_output=True,
            timeout=timeout,
        )
        return Result(r.returncode, (r.stdout + r.stderr).decode(errors="replace"))

    def put(self, source: Path, name: str) -> str:
        subprocess.run(["docker", "cp", str(source), f"{self.name}:/home/alex/{name}"], check=True)
        self.root(f"chown alex:alex /home/alex/{name}")
        return f"/home/alex/{name}"

    def load_image(self, local: str, as_name: str) -> None:
        save = subprocess.Popen(["docker", "save", local], stdout=subprocess.PIPE)
        subprocess.run(
            ["docker", "exec", "-i", self.name, "docker", "load"],
            stdin=save.stdout,
            check=True,
            capture_output=True,
        )
        save.wait()
        self.root(f"docker tag {local} {as_name}")

    def forward(self, port: int) -> threading.Thread:
        # Inside the rig, a relay from its network address to Purser's loopback
        # port; here, a plain TCP proxy from this machine's loopback to the
        # relay. A client then talks to http://localhost:PORT, as it would on
        # the Docker host itself, and Purser sees that Host.
        relay = port + 10000
        self.root(
            f"systemd-run --unit=drill-relay-{port} socat"
            f" TCP-LISTEN:{relay},fork,reuseaddr,bind={self.lan_address} TCP:127.0.0.1:{port}"
        )
        address = self.lan_address

        async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            with contextlib.suppress(Exception):
                while data := await reader.read(65536):
                    writer.write(data)
                    await writer.drain()
            with contextlib.suppress(Exception):
                if writer.can_write_eof():
                    writer.write_eof()

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                up_reader, up_writer = await asyncio.open_connection(address, relay)
            except OSError:
                writer.close()
                return
            await asyncio.gather(pipe(reader, up_writer), pipe(up_reader, writer))
            for stream in (writer, up_writer):
                with contextlib.suppress(Exception):
                    stream.close()

        def serve() -> None:
            async def main() -> None:
                server = await asyncio.start_server(handle, "127.0.0.1", port)
                async with server:
                    await server.serve_forever()

            with contextlib.suppress(Exception):
                asyncio.run(main())

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        time.sleep(0.5)
        return thread

    def stop(self) -> None:
        if self.keep:
            print(
                f"kept: docker exec -it -u alex {self.name} bash"
                f"   (remove: docker rm -f -v {self.name})"
            )
            return
        subprocess.run(["docker", "rm", "-f", "-v", self.name], capture_output=True)


class SshHost(Host):
    """A real machine. The drill only ever touches ~/purser and the Docker
    resources the guide creates there; it refuses a host that already has them."""

    def __init__(self, target: str, lan_address: str) -> None:
        super().__init__()
        self.target = target
        self.lan_address = lan_address
        self._tunnels: list[subprocess.Popen[bytes]] = []

    def _run(self, script: str, timeout: int) -> Result:
        r = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", self.target, "bash", "-ls"],
            input=script.encode(),
            capture_output=True,
            timeout=timeout,
        )
        return Result(r.returncode, (r.stdout + r.stderr).decode(errors="replace"))

    def put(self, source: Path, name: str) -> str:
        subprocess.run(["scp", "-q", str(source), f"{self.target}:{name}"], check=True)
        return name

    def forward(self, port: int) -> subprocess.Popen[bytes]:
        tunnel = subprocess.Popen(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ExitOnForwardFailure=yes",
                "-N",
                "-L",
                f"{port}:127.0.0.1:{port}",
                self.target,
            ]
        )
        self._tunnels.append(tunnel)
        time.sleep(2)
        return tunnel

    def stop(self) -> None:
        for tunnel in self._tunnels:
            tunnel.terminate()


# --------------------------------------------------------------------------
# The guide
# --------------------------------------------------------------------------


class Guide:
    """The setup page, as the commands an agent would copy from it. Steps are
    the numbered `## N.` sections; `lan` is the section about reaching Purser
    from another machine on the home network."""

    LAN_HEADING = re.compile(r"another machine|home network", re.I)

    def __init__(self, path: Path) -> None:
        self.text = path.read_text()
        self.blocks: dict[str, list[str]] = {}
        self.sections: dict[str, str] = {}
        key, fence, lang = "", False, ""
        buffer: list[str] = []
        for line in self.text.splitlines():
            if not fence and line.startswith("## "):
                heading = line[3:].strip()
                number = re.match(r"(\d+)\.", heading)
                key = (
                    number.group(1)
                    if number
                    else ("lan" if self.LAN_HEADING.search(heading) else heading.lower())
                )
                self.sections[key] = ""
                continue
            if key:
                self.sections[key] += line + "\n"
            if line.startswith("```"):
                if fence and lang == "bash":
                    self.blocks.setdefault(key, []).append("\n".join(buffer))
                fence, lang, buffer = (not fence), line[3:].strip(), []
                continue
            if fence:
                buffer.append(line)

    def step(self, key: str) -> list[str]:
        if key not in self.blocks:
            raise KeyError(f"the guide has no bash block in section '{key}'")
        return self.blocks[key]


# --------------------------------------------------------------------------
# MCP clients
# --------------------------------------------------------------------------


def http_json(
    url: str,
    *,
    data: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 20,
) -> tuple[int, str]:
    request = urllib.request.Request(
        url,
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace")
    except (urllib.error.URLError, OSError) as exc:
        return 0, str(exc)


def mcp_call(
    base: str,
    token: str,
    tool: str,
    arguments: dict[str, Any] | None = None,
    *,
    host: str | None = None,
) -> Any:
    """One tool call over streamable HTTP, the way the guide's curl check does it.
    `host` addresses the request to another name of the same machine, as a
    client that resolved that name would."""
    status, body = http_json(
        base + "/mcp/",
        data={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments or {}},
        },
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
            **({"Host": host} if host else {}),
        },
        timeout=60,
    )
    if status != 200:
        raise RuntimeError(f"HTTP {status}: {body[:200]}")
    for line in body.splitlines():
        if line.startswith("data: "):
            result = json.loads(line[6:]).get("result", {})
            if result.get("isError"):
                raise RuntimeError(json.dumps(result.get("content"))[:300])
            content = result.get("structuredContent")
            return (
                content.get("result", content)
                if isinstance(content, dict) and set(content) == {"result"}
                else content
            )
    raise RuntimeError("no result in the MCP response")


def client_http(base: str, token: str) -> tuple[bool, str]:
    described = mcp_call(base, token, "describe")
    mcp_call(base, token, "list_accounts")
    return described.get("your_scopes") == [
        "read",
        "write",
    ], f"scopes {described.get('your_scopes')}"


def client_bridge(base: str, token: str) -> tuple[bool, str]:
    """Claude Desktop reaches a local server through the `mcp-remote` stdio
    bridge; off localhost the bridge needs --allow-http for a plain-HTTP URL."""
    if not shutil.which("npx"):
        return False, "npx is not installed here"
    local = re.match(r"http://(localhost|127\.0\.0\.1)[:/]", base)
    messages = (
        "\n".join(
            json.dumps(m)
            for m in (
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "setup-drill", "version": "0"},
                    },
                },
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            )
        )
        + "\n"
    )
    with tempfile.TemporaryDirectory(prefix="purser-drill-bridge-") as scratch:
        log = Path(scratch) / "bridge.log"
        with log.open("wb") as errors:
            # Its own process group: npx starts the bridge as a grandchild, which
            # would otherwise outlive the kill and hold the pipes open.
            proc = subprocess.Popen(
                [
                    "npx",
                    "-y",
                    "mcp-remote@latest",
                    base + "/mcp",
                    *([] if local else ["--allow-http"]),
                    "--header",
                    "Authorization:${AUTH_HEADER}",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=errors,
                cwd=scratch,
                start_new_session=True,
                env={
                    **os.environ,
                    "AUTH_HEADER": f"Bearer {token}",
                    "MCP_REMOTE_CONFIG_DIR": scratch,
                },
            )
            assert proc.stdin and proc.stdout
            proc.stdin.write(messages.encode())
            proc.stdin.flush()
            found: list[int] = []

            def read() -> None:
                assert proc.stdout
                for line in proc.stdout:
                    try:
                        count = len(json.loads(line).get("result", {}).get("tools", []))
                    except (ValueError, AttributeError):
                        continue
                    if count:
                        found.append(count)
                        return

            reader = threading.Thread(target=read, daemon=True)
            reader.start()
            reader.join(90)
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
        tools = found[0] if found else 0
        err = log.read_text(errors="replace").replace(token, "<token>")
    return tools > 0, f"{tools} tools" if tools else err[-300:]


AGENT_PROMPT = (
    "Call the purser MCP tool named describe. Reply with exactly one line: "
    "SCOPES=<the your_scopes value>. If the tool is unavailable or errors, reply "
    "with one line starting FAIL and the error text. Do not run shell commands."
)


def client_claude(base: str, token: str) -> tuple[bool, str]:
    if not shutil.which("claude"):
        return False, "claude is not installed here"
    with tempfile.TemporaryDirectory(prefix="purser-drill-claude-") as scratch:
        config = Path(scratch) / "mcp.json"
        config.touch(mode=0o600)
        config.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "purser": {
                            "type": "http",
                            "url": base + "/mcp",
                            "headers": {"Authorization": f"Bearer {token}"},
                        }
                    }
                }
            )
        )
        r = subprocess.run(
            [
                "claude",
                "-p",
                AGENT_PROMPT,
                "--mcp-config",
                str(config),
                "--strict-mcp-config",
                "--allowedTools",
                "mcp__purser__describe",
            ],
            capture_output=True,
            text=True,
            timeout=240,
            cwd=scratch,
        )
    line = (r.stdout.strip().splitlines() or [r.stderr.strip()[-200:]])[-1]
    return 'SCOPES=["read","write"]' in line.replace(" ", ""), line[:200]


def client_codex(base: str, token: str) -> tuple[bool, str]:
    """Codex with the config the token screen shows: the header in the file."""
    if not shutil.which("codex"):
        return False, "codex is not installed here"
    with tempfile.TemporaryDirectory(prefix="purser-drill-codex-") as scratch:
        r = subprocess.run(
            [
                "codex",
                "exec",
                "--skip-git-repo-check",
                "-c",
                f'mcp_servers.purser.url="{base}/mcp"',
                "-c",
                f'mcp_servers.purser.http_headers={{Authorization="Bearer {token}"}}',
                AGENT_PROMPT,
            ],
            capture_output=True,
            text=True,
            timeout=300,
            cwd=scratch,
        )
    lines = [x for x in r.stdout.strip().splitlines() if x.startswith(("SCOPES", "FAIL"))]
    line = lines[-1] if lines else r.stderr.strip()[-200:].replace(token, "<token>")
    return 'SCOPES=["read","write"]' in line.replace(" ", ""), line[:200]


def client_gemini(base: str, token: str) -> tuple[bool, str]:
    """Gemini CLI with the token screen's command, in a throwaway home: `mcp
    list` connects to each server and reports it, without a Google sign-in."""
    if not shutil.which("npx"):
        return False, "npx is not installed here"
    with tempfile.TemporaryDirectory(prefix="purser-drill-gemini-") as scratch:
        # Gemini CLI loads servers only in a folder its user has trusted.
        env = {**os.environ, "HOME": scratch, "GEMINI_CLI_TRUST_WORKSPACE": "true"}
        gemini = ["npx", "-y", "@google/gemini-cli@latest", "mcp"]
        added = subprocess.run(
            [
                *gemini,
                "add",
                "--scope",
                "user",
                "--transport",
                "http",
                "--header",
                f"Authorization: Bearer {token}",
                "purser",
                base + "/mcp",
            ],
            capture_output=True,
            text=True,
            timeout=300,
            cwd=scratch,
            env=env,
        )
        listed = subprocess.run(
            [*gemini, "list"], capture_output=True, text=True, timeout=300, cwd=scratch, env=env
        )
    output = (listed.stdout + listed.stderr).replace(token, "<token>")
    line = next((x.strip() for x in output.splitlines() if "purser" in x), output[-200:])
    return added.returncode == 0 and "purser" in line and "Connected" in line, line[:200]


CLIENTS = {
    "http": client_http,
    "bridge": client_bridge,
    "claude": client_claude,
    "codex": client_codex,
    "gemini": client_gemini,
}


# --------------------------------------------------------------------------
# The drill
# --------------------------------------------------------------------------


def has_line(output: str, line: str) -> bool:
    """Whether a command printed exactly this line (stdout and stderr are merged)."""
    return line in (x.strip() for x in output.splitlines())


class Drill:
    def __init__(self, host: Host, guide: Guide, args: argparse.Namespace, report: Report) -> None:
        self.host, self.guide, self.args, self.report = host, guide, args, report
        self.port: int = args.port
        self.local = f"http://localhost:{self.port}"
        self.token = ""
        self.lan = ""
        self.bank_total: Decimal | None = None
        # The demo Bridge dates its balances at the next UTC midnight, a day
        # ahead of a UTC household. Net worth is as of the household's today, so
        # the drill's household lives where that date has already begun
        # (UTC+14). Before 10:00 UTC no such place exists and
        # the balance checks are reported as not checkable.
        self.zone = "" if args.no_feed or datetime.now(UTC).hour < 10 else "Pacific/Kiritimati"

    def block(self, script: str) -> str:
        """A guide block as this run executes it: the port the person chose."""
        return script.replace(":8000", f":{self.port}") if self.port != 8000 else script

    def in_dir(self, script: str, **kw: Any) -> Result:
        return self.host.sh("cd purser && " + self.block(script), **kw)

    # -- steps 1 to 5 -------------------------------------------------------

    def install(self) -> bool:
        r, host, guide = self.report, self.host, self.guide
        started = time.time()

        out = host.sh(self.block(guide.step("1")[0])).out
        lines = [x.strip() for x in out.splitlines() if x.strip()]
        ready = (
            len(lines) >= 4
            and re.fullmatch(r"linux/(amd64|arm64)", lines[0]) is not None
            and "Docker Compose version" in lines[1]
            and lines[2].startswith("curl ")
            and lines[3] == "000"
            and len(lines) == 4
        )
        if not r.add("A1", "step 1: a ready host passes every prerequisite check", ready, out):
            return False

        download = guide.step("2")[0]
        if self.args.image:
            compose = host.put(REPO / "docker-compose.yml", "drill-compose.yml")
            download, n = re.subn(
                r"curl -fsSLO https://raw\.githubusercontent\.com/\S+/docker-compose\.yml",
                f"cp {compose} docker-compose.yml",
                download,
            )
            if not n:
                return r.add(
                    "A2", "step 2: the guide downloads the release's compose file", False, download
                )
            r.notes.append(
                "stand-in: this checkout's docker-compose.yml instead of the release download"
            )
        out = host.sh(download).out
        if not r.add(
            "A2",
            "step 2: the compose file arrives and names the release image",
            has_line(out, "1"),
            out,
        ):
            return False

        if self.args.image:
            image = re.search(r"image: (ghcr\.io/\S+)", (REPO / "docker-compose.yml").read_text())
            assert image and isinstance(host, RigHost), "--image needs the rig host"
            host.load_image(self.args.image, image.group(1))
            pulled = self.in_dir("PURSER_ENCRYPTION_KEY=keygen docker compose pull postgres")
            r.notes.append(
                f"stand-in: local image {self.args.image} loaded as {image.group(1)}"
                " instead of the registry pull"
            )
        else:
            pulled = self.in_dir(guide.step("3")[0])
        if not r.add("A3", "step 3: the images are pulled", pulled.ok, pulled.out[-400:]):
            return False

        self.key_step_is_safe()
        out = self.in_dir(guide.step("4")[0]).out
        if not r.add(
            "A5",
            "step 4: one valid key is written to .env",
            has_line(out, "1") and "STOP" not in out,
            out,
        ):
            return False
        volumes = host.sh(
            "docker volume ls -q --filter name=purser-pgdata", record=False
        ).out.strip()
        r.add(
            "A6",
            "step 4 creates no database volume, so step 1 can be run again after an interruption",
            not volumes,
            f"volume present: {volumes}",
        )
        if self.port != 8000:
            host.sh(f"cd purser && echo 'PURSER_HOST_PORT={self.port}' >> .env")
        if self.zone:
            host.sh(f"cd purser && echo 'PURSER_TIMEZONE={self.zone}' >> .env")

        for script in guide.step("5"):
            out = self.in_dir(script).out
        healthy = self.in_dir(f"curl -fs http://localhost:{self.port}/healthz", record=False).out
        if not r.add(
            "A7",
            "step 5: Purser is healthy and setup is open",
            '"database":"ok"' in healthy and '"setup_needed":true' in out,
            healthy + out,
        ):
            return False
        r.timings["install (steps 1-5)"] = time.time() - started
        return True

    def key_step_is_safe(self) -> None:
        """Step 4 against an image that cannot run must not leave a .env behind:
        a first attempt that fails (a network problem at step 3) is then
        repaired by simply running the steps again."""
        step4 = self.guide.step("4")[0]
        probe = (
            "rm -rf drill-probe && mkdir drill-probe && cd drill-probe && "
            "sed 's#image: ghcr.io/[^ ]*#image: ghcr.io/mlamoure/purser:v0.0.0-missing#'"
            " ../purser/docker-compose.yml > docker-compose.yml\n"
            + step4
            + "\n[ -e .env ] && echo LEFT_ENV; "
            "docker volume ls -q --filter name=drill-probe | grep -q . && echo LEFT_VOLUME; true"
        )
        out = self.host.sh(probe, timeout=300).out
        self.host.sh(
            "cd drill-probe 2>/dev/null && PURSER_ENCRYPTION_KEY=keygen docker compose down -v"
            " >/dev/null 2>&1; cd; rm -rf drill-probe",
            record=False,
        )
        self.report.add(
            "A4",
            "step 4 after a failed pull leaves no .env and no volume behind",
            "LEFT_ENV" not in out and "LEFT_VOLUME" not in out,
            ", ".join(x for x in ("LEFT_ENV", "LEFT_VOLUME") if x in out),
        )

    # -- step 6: the person -------------------------------------------------

    def wizard(self, base: str = "") -> bool:
        """The person's part. `base` is the address their browser uses: empty
        for the same machine (through the forward), or the home-network address."""
        from playwright.sync_api import sync_playwright

        r, started = self.report, time.time()
        if not base:
            self.host.forward(self.port)
            base = self.local
        actions = r.human_actions
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            # A desktop browser lets a click write to the clipboard on localhost;
            # headless Chromium needs to be told so.
            context = browser.new_context()
            context.grant_permissions(["clipboard-read", "clipboard-write"], origin=base)
            page = context.new_page()
            page.set_default_timeout(30000)
            try:
                page.goto(base, wait_until="networkidle")
                page.get_by_label("Email", exact=True).fill(ADMIN_EMAIL)
                page.get_by_label("Password (8+ characters)").fill(ADMIN_PASSWORD)
                page.get_by_role("button", name="Create admin", exact=True).click()
                actions.append("Open the address the agent gives and create the admin account")
                page.get_by_text("Record your encryption key", exact=True).wait_for()
                shown = KEY_RE.search(page.locator("body").inner_text())
                self.copy_feedback(
                    page, "H2", "the key's Copy button confirms the copy (same machine)"
                )
                page.get_by_label("I have stored this key somewhere safe").check()
                page.get_by_role("button", name="Continue", exact=True).click()
                actions.append("Copy the encryption key into a password manager and tick the box")
                r.add("H1", "the wizard shows the key for the person to store", shown is not None)

                if self.args.no_feed:
                    page.get_by_role("button", name="Skip for now", exact=True).click()
                else:
                    page.get_by_label("Setup token", exact=True).fill(DEMO_SETUP_TOKEN)
                    page.get_by_role("button", name="Connect & fetch", exact=True).click()
                    page.get_by_role("heading", name="Map discovered accounts").wait_for(
                        timeout=120000
                    )
                    actions.append("Paste a SimpleFIN setup token into Purser")
                    if self.args.agent_maps:
                        # The person leaves the mapping to the agent (#583).
                        page.get_by_role("button", name="Continue", exact=True).click()
                    else:
                        self.map_accounts(page)
                page.get_by_text("Import bank history (optional)", exact=True).wait_for()
                page.get_by_role("button", name="Skip for now", exact=True).click()
                actions.append("Skip (or do) the history import")
                page.get_by_role("button", name="Create agent token", exact=True).click()
                page.get_by_text(re.compile(r"shown\s+once", re.I)).first.wait_for()
                body = page.locator("body").inner_text()
                found = re.search(r"\bpurser_[A-Za-z0-9_\-]{20,}", body)
                self.token = found.group(0) if found else ""
                self.client_instructions(page)
                actions.append(
                    "Create the agent token and run the command Purser shows for the client"
                )
                page.get_by_role("button", name=re.compile("Finish")).click()
                page.wait_for_timeout(1500)
            except Exception as exc:
                page.screenshot(
                    path=str(Path(tempfile.gettempdir()) / "purser-drill-wizard-failure.png")
                )
                r.add("H9", "the wizard runs from start to finish", False, repr(exc)[:300])
                return False
            finally:
                browser.close()
        r.timings["wizard (scripted person)"] = time.time() - started
        status = http_json(base + "/auth/setup/status")[1]
        return r.add(
            "H9",
            "the wizard runs from start to finish",
            bool(self.token) and '"setup_needed":false' in status,
            status,
        )

    def map_accounts(self, page: Any) -> None:
        """Map every discovered account, accepting what the form proposes: the
        cheapest thing a person can do, so the proposal has to be right."""
        proposals: dict[str, str] = {}
        for _ in range(40):
            opener = page.get_by_role("button", name="Map", exact=True)
            if opener.count() == 0:
                break
            opener.first.click()
            form = page.get_by_role("form").first
            form.wait_for()
            name = form.get_by_label("Account name").input_value()
            proposals[name] = form.get_by_label("Account type").input_value()
            form.get_by_role("button", name="Map account", exact=True).click()
            form.wait_for(state="detached", timeout=60000)
        self.report.human_actions.append(f"Map each bank account ({len(proposals)} in this run)")
        wrong = {n: t for n, t in proposals.items() if "savings" in n.lower() and t != "savings"}
        self.report.add(
            "H3",
            "the mapping form proposes the account type from the bank's name",
            not wrong,
            f"proposed {wrong}",
        )
        page.get_by_role("button", name="Continue", exact=True).click()

    def copy_feedback(self, page: Any, id_: str, what: str) -> None:
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        button = page.get_by_role("button", name=re.compile(r"^Copy")).first
        button.click()
        page.wait_for_timeout(600)
        confirmed = page.get_by_role("button", name=re.compile(r"Copied")).count() > 0
        self.report.add(
            id_,
            what,
            confirmed and not errors,
            "; ".join(errors)[:200] or "no confirmation after the click",
        )

    def client_instructions(self, page: Any) -> None:
        """The token screen must tell the person how to connect each supported
        client, and must not offer a route that cannot reach a home server."""
        seen: dict[str, str] = {}
        for label in ("Claude Code", "Claude Desktop", "Codex", "Gemini", "OpenClaw", "Other"):
            tab = page.get_by_role("button", name=label, exact=True)
            if tab.count():
                tab.first.click()
                page.wait_for_timeout(150)
                seen[label] = page.locator("body").inner_text()
        text = "\n".join(seen.values())
        missing = [
            label
            for label in ("Claude Code", "Claude Desktop", "Codex", "Gemini", "OpenClaw", "Other")
            if label not in seen
        ]
        self.report.add(
            "H4",
            "the token screen has instructions for every supported client",
            not missing,
            f"missing: {missing}",
        )
        self.report.add(
            "H5",
            "Claude Code is registered for every folder (--scope user)",
            "--scope user" in text,
        )
        self.report.add(
            "H6",
            "Claude Desktop is given the mcp-remote bridge, not the cloud connector dialog",
            "mcp-remote" in seen.get("Claude Desktop", "") and "Add custom connector" not in text,
        )

    # -- steps 7 to 9: the agent, connected ----------------------------------

    def clients(self, where: str, base: str, prefix: str) -> None:
        for name in self.args.clients:
            try:
                ok, detail = CLIENTS[name](base, self.token)
            except Exception as exc:
                ok, detail = False, repr(exc)[:300]
            self.report.add(
                f"{prefix}-{name}", f"{name} client reaches Purser ({where})", ok, detail
            )

    def finish(self) -> None:
        """Step 9: the first sync brings a few months of transactions and no
        balance from before them. The map form starts each new account from
        the bank's reported balance (decision 123), so the guide's finish step
        is a check, with one tool for an account that lacks one."""
        if self.args.no_feed:
            return
        section = self.guide.sections.get("9", "")
        documented = "set_starting_balance" in section and "reconcile_account" in section
        self.report.add(
            "F1",
            "the guide tells the agent to check each account against the bank and how to"
            " start one that lacks a starting balance",
            documented,
        )
        if self.args.agent_maps:
            self.agent_maps()
        accounts = mcp_call(self.local, self.token, "list_accounts")
        total = Decimal(0)
        needed: list[str] = []
        unsettled: list[str] = []
        for account in accounts:
            if account["type"] not in ("checking", "savings", "cash", "credit_card"):
                continue
            path = {"account": account["path"]}
            snapshot = (
                mcp_call(self.local, self.token, "account_balance", path).get("bank_snapshot") or {}
            )
            if snapshot.get("status") != "ok" or snapshot.get("balance") is None:
                continue
            total += Decimal(snapshot["balance"])
            difference = (snapshot.get("comparison") or {}).get("difference")
            if difference is not None and Decimal(difference) == 0:
                continue
            # Exactly what step 9 tells the agent to do: one call, no arguments
            # beyond the account.
            needed.append(account["path"])
            mcp_call(self.local, self.token, "set_starting_balance", path)
            after = mcp_call(self.local, self.token, "account_balance", path)
            difference = ((after.get("bank_snapshot") or {}).get("comparison") or {}).get(
                "difference"
            )
            if difference is None or Decimal(difference) != 0:
                unsettled.append(f"{account['path']}: {difference}")
        self.report.add(
            "F4",
            "mapping leaves every account matching the bank with no further agent step",
            not needed,
            f"needed set_starting_balance: {', '.join(needed)}",
        )
        self.report.add(
            "F3",
            "every cash and card account shows no difference from the bank",
            not unsettled,
            "; ".join(unsettled),
        )
        self.bank_total = total
        if not self.zone:
            # Net worth is as of the ledger day; the demo Bridge dates rows at
            # the next UTC midnight, which no household has reached before
            # 10:00 UTC, so today's net worth leaves them out until then.
            self.report.notes.append(
                "F2 not checked: the demo Bridge dates rows ahead of every household's"
                " today before 10:00 UTC"
            )
            return
        net = Decimal(mcp_call(self.local, self.token, "net_worth")["net_worth"])
        self.report.add(
            "F2",
            "net worth equals what the bank reports once setup is finished",
            net == total,
            f"Purser {net}, bank {total}",
        )

    def agent_maps(self) -> None:
        """Step 9, first half, when the person left the mapping to the agent
        (#583): one call for Purser's plan, one to apply it as proposed."""
        section = self.guide.sections.get("9", "")
        self.report.add(
            "M1",
            "the guide tells the agent to show the plan and get a yes before mapping",
            "map_feed_accounts" in section and "confident" in section,
        )
        plan = mcp_call(self.local, self.token, "map_feed_accounts")["plan"]
        leaked = [
            line["ref"]
            for line in plan
            if any(key in line for key in ("external_id", "balance", "conn_id"))
        ]
        self.report.add(
            "M2",
            "Purser proposes a mapping for every bank account, without ids or balances",
            bool(plan) and not leaked and all(line["action"] in ("map", "ignore") for line in plan),
            f"{len(plan)} lines; leaked {leaked}",
        )
        wrong = {
            line["bank_name"]: line["type"]
            for line in plan
            if "savings" in str(line["bank_name"]).lower() and line["type"] != "savings"
        }
        self.report.add("M3", "the proposed types follow the bank's names", not wrong, f"{wrong}")
        lines = [{"ref": line["ref"]} for line in plan]
        done = mcp_call(self.local, self.token, "map_feed_accounts", {"accounts": lines})
        left = mcp_call(self.local, self.token, "map_feed_accounts")["plan"]
        self.report.add(
            "M4",
            "applying the plan maps every account in one call and leaves none waiting",
            len(done["results"]) == len(plan) and left == [],
            f"{len(done['results'])} of {len(plan)} applied; {len(left)} left",
        )

    # -- another machine on the home network ----------------------------------

    def home_network(self) -> None:
        r = self.report
        try:
            script = self.guide.step("lan")[0]
        except KeyError:
            r.add(
                "L1",
                "the guide has a section for another machine on the home network",
                False,
                "no such section with a bash block",
            )
            return
        out = self.in_dir(script).out
        address = re.search(r"Purser now answers at (http://[0-9A-Za-z.\-]+:\d+)", out)
        self.lan = address.group(1) if address else f"http://{self.host.lan_address}:{self.port}"
        if isinstance(self.host, SshHost) and self.host.lan_address:
            self.lan = f"http://{self.host.lan_address}:{self.port}"
        healthy = ""
        for _ in range(30):
            status, healthy = http_json(self.lan + "/healthz", timeout=4)
            if status == 200:
                break
            time.sleep(2)
        if not r.add(
            "L1",
            "the home-network section makes Purser answer from a second machine",
            '"database":"ok"' in healthy,
            out[-300:] + healthy,
        ):
            return
        self.clients("second machine, plain HTTP", self.lan, "L2")
        self.clients("same machine, after the change", self.local, "L3")

        status, body = http_json(
            self.lan + "/mcp/",
            data={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={
                "Authorization": f"Bearer {self.token}",
                "Host": f"some-other-name:{self.port}",
                "Accept": "application/json, text/event-stream",
            },
        )
        r.add(
            "L4",
            "a wrong address gets an error that says which address to use and how to allow it",
            status == 421 and "PURSER_PUBLIC_URL" in body and "PURSER_ALLOWED_HOSTS" in body,
            f"HTTP {status}: {body[:160]}",
        )
        self.other_names()

        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_context().new_page()
            page.set_default_timeout(30000)
            try:
                page.goto(self.lan + "/#/settings/agents", wait_until="networkidle")
                page.get_by_label("Email", exact=True).fill(ADMIN_EMAIL)
                page.get_by_label("Password", exact=True).fill(ADMIN_PASSWORD)
                page.get_by_role("button", name=re.compile(r"^(Sign in|Log in)$")).click()
                page.get_by_role("link", name="Settings").first.wait_for()
                page.goto(self.lan + "/#/settings/agents", wait_until="networkidle")
                page.get_by_text("Create an API token").first.wait_for()
                r.add("L5", "the person signs in to the web UI from a second machine", True)
                self.reach_card(page)
                self.copy_feedback(page, "L6", "Copy buttons work on plain HTTP off localhost")
            except Exception as exc:
                r.add(
                    "L5",
                    "the person signs in to the web UI from a second machine",
                    False,
                    repr(exc)[:300],
                )
            finally:
                browser.close()

    def reach_card(self, page: Any) -> None:
        """#632: Compose tells Purser how it is published, so Settings says
        the home network, names the agent address, and sees no mismatch."""
        try:
            page.get_by_text("your home network", exact=True).first.wait_for(timeout=15000)
            ok = (
                page.get_by_text(f"{self.lan}/mcp", exact=True).count() > 0
                and page.get_by_text("an address agents cannot use").count() == 0
            )
            detail = "" if ok else "agent address missing, or a mismatch warning shown"
        except Exception as exc:
            ok, detail = False, repr(exc)[:300]
        self.report.add(
            "L9",
            "Settings says Purser is reachable from the home network, at the agent address",
            ok,
            detail,
        )

    def other_names(self) -> None:
        """#633: the guide lists the machine's address, its name and
        <name>.local, and an agent connects by each of them. The second machine
        cannot resolve the rig's name, so the request goes to the address with
        the name in its Host header, which is what resolving the name does."""
        r = self.report
        env = self.host.sh("cat purser/.env", record=False).out
        listed = re.search(r"^PURSER_ALLOWED_HOSTS=(\S*)$", env, re.M)
        names = [n for n in (listed.group(1).split(",") if listed else []) if n]
        name = self.host.sh(
            "(scutil --get LocalHostName 2>/dev/null || hostname -s) | tr '[:upper:]' '[:lower:]'",
            record=False,
        ).out.strip()
        if not r.add(
            "L7",
            "the home-network section lists the machine's address, name and name.local",
            listed is not None and {name, f"{name}.local"} <= set(names) and len(names) >= 3,
            f"PURSER_ALLOWED_HOSTS={listed.group(1) if listed else '(missing)'}; name {name!r}",
        ):
            return
        failures = []
        for each in names:
            try:
                scopes = mcp_call(self.lan, self.token, "describe", host=f"{each}:{self.port}").get(
                    "your_scopes"
                )
                if scopes != ["read", "write"]:
                    failures.append(f"{each}: scopes {scopes}")
            except Exception as exc:
                failures.append(f"{each}: {exc}")
        r.add(
            "L8",
            "an agent on a second machine connects by IP address and by host name",
            not failures,
            "; ".join(failures),
        )

    # -- secrets --------------------------------------------------------------

    def secrets(self) -> None:
        env = self.host.sh("cat purser/.env", record=False).out
        key = re.search(r"^PURSER_ENCRYPTION_KEY=(\S+)$", env, re.M)
        transcript = "\n".join(self.host.transcript)
        leaked = [
            name
            for name, secret in (
                ("encryption key", key.group(1) if key else ""),
                ("agent token", self.token),
            )
            if secret and secret in transcript
        ]
        self.report.add(
            "S1",
            "neither the key nor the token appears in the agent's command output",
            not leaked,
            f"leaked: {leaked}",
        )


# --------------------------------------------------------------------------
# A real agent follows the guide
# --------------------------------------------------------------------------

AGENT_BRIEF = """Set up Purser for me by following {guide}

About my setup: Purser should run on my home server, which already has Docker. You are on my \
laptop. Run a command on the server with ./host '<command>' : it runs there as my user, in my \
home directory, like ssh. My browser is on this laptop too. You are Claude Code. I live on \
Kiritimati, so my time zone is Pacific/Kiritimati."""


class AgentRun:
    """`--agent claude`: Claude Code is given the one sentence a person would
    give it, plus where things run, and follows the guide itself. The harness
    is the person: it does the wizard when the agent hands off, "restarts" the
    agent with the server the wizard's steps register, and says yes when asked.
    Nothing here tells the agent what the guide says."""

    def __init__(self, drill: Drill) -> None:
        self.drill, self.report, self.host = drill, drill.report, drill.host
        self.args = drill.args
        self.workspace = Path(tempfile.mkdtemp(prefix="purser-drill-agent-"))
        self.session = ""
        self.transcript = ""
        self.turns = 0

    def prepare(self) -> str:
        host = self.host
        if isinstance(host, RigHost):
            command = f'exec docker exec -i -u alex -w /home/alex {host.name} bash -lc "$1"'
        else:
            assert isinstance(host, SshHost)
            command = f'exec ssh -o BatchMode=yes {host.target} "$1"'
        wrapper = self.workspace / "host"
        wrapper.write_text(f"#!/bin/bash\n{command}\n")
        wrapper.chmod(0o755)
        if self.args.published:
            return "https://mlamoure.github.io/purser/tutorials/ai-setup/"
        # A candidate build is in no registry and its compose file at no tag:
        # the agent's copy of the guide takes both from the host instead.
        assert isinstance(host, RigHost)
        compose = host.put(REPO / "docker-compose.yml", "drill-compose.yml")
        image = re.search(r"image: (ghcr\.io/\S+)", (REPO / "docker-compose.yml").read_text())
        assert image
        host.load_image(self.args.image, image.group(1))
        text, n = re.subn(
            r"curl -fsSLO https://raw\.githubusercontent\.com/\S+/docker-compose\.yml",
            f"cp {compose} docker-compose.yml",
            Path(self.args.guide).read_text(),
        )
        text, m = re.subn(
            r"PURSER_ENCRYPTION_KEY=keygen docker compose pull\n",
            "PURSER_ENCRYPTION_KEY=keygen docker compose pull postgres\n",
            text,
        )
        assert n and m, "the guide's download or pull command was not found"
        (self.workspace / "ai-setup.md").write_text(text)
        self.report.notes.append(
            "stand-in: the agent's copy of the guide copies this checkout's compose file and"
            f" pulls only PostgreSQL; {self.args.image} is preloaded under the release name"
        )
        return "the guide in the file ai-setup.md in this folder."

    def say(self, message: str, *, mcp: Path | None = None) -> str:
        """One turn of the conversation; returns what the agent said last."""
        self.turns += 1
        command = [
            "claude", "-p", message, "--output-format", "stream-json", "--verbose",
            "--setting-sources", "project",
            "--allowedTools", "Bash(./host:*)", "Bash(curl:*)", "Read", "mcp__purser",
            "WebFetch(domain:mlamoure.github.io)",
        ]  # fmt: skip
        if self.args.agent_model:
            command += ["--model", self.args.agent_model]
        if self.session:
            command += ["--resume", self.session]
        if mcp:
            command += ["--mcp-config", str(mcp), "--strict-mcp-config"]
        started = time.time()
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=2400, cwd=self.workspace
        )
        self.report.timings[f"agent turn {self.turns}"] = time.time() - started
        self.transcript += result.stdout
        said = ""
        for line in result.stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "result":
                self.session = event.get("session_id", self.session)
                said = str(event.get("result", ""))
        print(f"\n--- agent, turn {self.turns} ---\n{said[-1800:]}\n", flush=True)
        return said or result.stderr[-400:]

    def run(self) -> None:
        r, drill = self.report, self.drill
        said = self.say(AGENT_BRIEF.format(guide=self.prepare()))

        # Turn 1 must end at the wizard handoff, at an address this "laptop" can open.
        addresses = [
            a
            for a in re.findall(r"http://[0-9A-Za-z.\-]+:\d+", said)
            if not re.match(r"http://(localhost|127\.)", a)
        ]
        base = next(
            (a for a in addresses if '"database":"ok"' in http_json(a + "/healthz", timeout=5)[1]),
            "",
        )
        open_ = bool(base) and '"setup_needed":true' in http_json(base + "/auth/setup/status")[1]
        if not r.add(
            "G1",
            "the agent installs Purser and hands off at an address the person's machine can open",
            open_,
            said[-400:],
        ):
            return
        drill.port = int(base.rsplit(":", 1)[1])
        # The person chooses how the token reaches the agent (decision 122):
        # the handoff names both ways. This person then takes the page's steps.
        both = re.search(r"\bpaste\b", said, re.I) and re.search(r"\b(steps|tab)\b", said, re.I)
        r.add("G2", "the handoff offers both ways to hand over the token", bool(both), said[-600:])
        if not drill.wizard(base):
            return

        # The wizard's Claude Code steps register the server; the person then
        # restarts the agent, which is what resuming with the server does here.
        mcp = self.workspace / "mcp.json"
        mcp.touch(mode=0o600)
        mcp.write_text(json.dumps({"mcpServers": {"purser": {
            "type": "http", "url": base + "/mcp",
            "headers": {"Authorization": f"Bearer {drill.token}"}}}}))  # fmt: skip
        r.human_actions.append("Restart the agent so that it loads Purser")
        said = self.say(
            "Done. I created the admin, put the key in my password manager, connected SimpleFIN"
            " and mapped the accounts. I followed the Claude Code steps on this laptop and"
            " restarted you.",
            mcp=mcp,
        )
        for _ in range(3):
            if "?" not in said:
                break
            r.human_actions.append("Answer the agent's question with yes")
            said = self.say("Yes, please go ahead.", mcp=mcp)

        r.add("G3", "the agent verifies its connection with describe",
              "mcp__purser__describe" in self.transcript)  # fmt: skip
        accounts = mcp_call(base, drill.token, "list_accounts")
        bank = Decimal(0)
        for account in accounts:
            snapshot = (
                mcp_call(base, drill.token, "account_balance", {"account": account["path"]}).get(
                    "bank_snapshot"
                )
                or {}
            )
            if snapshot.get("status") == "ok" and snapshot.get("balance") is not None:
                bank += Decimal(snapshot["balance"])
        net = Decimal(mcp_call(base, drill.token, "net_worth")["net_worth"])
        if drill.zone:
            r.add("G4", "after the agent's finish step, net worth equals what the bank reports",
                  net == bank, f"Purser {net}, bank {bank}")  # fmt: skip
        else:
            r.notes.append(
                "G4 not checked: the demo Bridge's balance date is ahead before 10:00 UTC"
            )
        env = self.host.sh("cat purser/.env", record=False).out
        key = re.search(r"^PURSER_ENCRYPTION_KEY=(\S+)$", env, re.M)
        leaked = [
            name
            for name, secret in (
                ("encryption key", key.group(1) if key else ""),
                ("agent token", drill.token),
            )
            if secret and secret in self.transcript
        ]
        r.add("G5", "neither the key nor the token appears anywhere in the agent's session",
              not leaked, f"leaked: {leaked}")  # fmt: skip
        zone = re.search(r"^PURSER_TIMEZONE=(\S+)$", env, re.M)
        recorded = bool(zone) and zone.group(1) == "Pacific/Kiritimati"
        detail = zone.group(0) if zone else "not set"
        r.add("G6", "the agent recorded the household's time zone", recorded, detail)
        shutil.rmtree(self.workspace, ignore_errors=True)


def macos_script(host: Host, report: Report) -> bool:
    path = host.put(MAC_SCRIPT, "install-docker-macos.sh")
    out = host.sh(f"bash {path}", timeout=1800)
    report.human_actions.append(
        "Install Homebrew if the script asks for it (it needs the Mac's password)"
    )
    return report.add(
        "M1",
        "the macOS script leaves a working `docker compose`",
        out.ok and host.sh("docker compose version").ok,
        out.out[-400:],
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--image", help="local candidate image (rig host only)")
    source.add_argument(
        "--published", action="store_true", help="download and pull what the guide names"
    )
    parser.add_argument("--host", default="rig", help="'rig' (default) or ssh:USER@HOST")
    parser.add_argument(
        "--lan-address", default="", help="the ssh host's address on the home network"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="host port; anything but 8000 exercises the guide's port setting",
    )
    parser.add_argument(
        "--clients", default="http,bridge", help="comma list of: " + ", ".join(CLIENTS)
    )
    parser.add_argument(
        "--no-feed",
        action="store_true",
        help="skip SimpleFIN (no internet access to the demo Bridge)",
    )
    parser.add_argument("--no-lan", action="store_true", help="skip the second-machine part")
    parser.add_argument(
        "--agent-maps",
        action="store_true",
        help="the person leaves the bank accounts unmapped and the agent maps them (step 9)",
    )
    parser.add_argument(
        "--macos-script",
        action="store_true",
        help="run scripts/install-docker-macos.sh on the host first",
    )
    parser.add_argument("--guide", default=str(GUIDE))
    parser.add_argument(
        "--agent",
        choices=["claude"],
        help="have a real agent follow the guide (spends the operator's subscription)",
    )
    parser.add_argument("--agent-model", default="", help="model for --agent (default: the CLI's)")
    parser.add_argument("--report", help="write the checks as JSON here")
    parser.add_argument("--keep", action="store_true", help="leave the rig running for inspection")
    args = parser.parse_args()
    args.clients = [c for c in args.clients.split(",") if c]
    unknown = [c for c in args.clients if c not in CLIENTS]
    if unknown:
        parser.error(f"unknown clients: {unknown}")
    if args.host != "rig" and args.image:
        parser.error("--image works on the rig only; use --published for a real host")

    report = Report()
    host: Host = (
        RigHost(args.keep)
        if args.host == "rig"
        else SshHost(args.host.removeprefix("ssh:"), args.lan_address)
    )
    try:
        host.start()
        if isinstance(host, RigHost) and args.port != 8000:
            # Another program already on 8000, as on many home servers.
            host.root(
                "systemd-run --unit=other-app socat TCP-LISTEN:8000,fork,reuseaddr "
                "SYSTEM:'printf \"HTTP/1.0 200 OK\\r\\n\\r\\nanother app\"'"
            )
        if args.macos_script and not macos_script(host, report):
            return 1
        drill = Drill(host, Guide(Path(args.guide)), args, report)
        if args.agent:
            AgentRun(drill).run()
        elif drill.install() and drill.wizard():
            drill.clients("same machine", drill.local, "C1")
            drill.finish()
            if not args.no_lan:
                drill.home_network()
            drill.secrets()
    finally:
        report.dump(args.report)
        host.stop()
    return 1 if report.failed() else 0


if __name__ == "__main__":
    sys.exit(main())
