---
title: Set up Purser with your AI
description: Step-by-step instructions an AI agent follows to take an empty Docker host to a running Purser with itself connected over MCP.
---

This page is written for an AI agent (Claude Code, Codex, an OpenClaw agent or
any other agent that can run shell commands). A person can hand it over with
one sentence:

```text
Set up Purser for me by following https://mlamoure.github.io/purser/tutorials/ai-setup/
```

People who prefer to install by hand can follow the
[Quickstart](../quickstart/) instead; it covers the same steps.

## Instructions for the agent

You are installing Purser, a self-hosted personal-finance backend, on a Docker
host and connecting yourself to it over MCP. Follow the steps in order. You
need no prior knowledge of Purser.

Ground rules:

- **The encryption key is never yours.** Generate it straight into a file, never
  print it, never paste it into chat, never store it in your memory or notes.
  The human copies it into their password manager.
- **Some steps are human-only.** Each is marked **Handoff**, with the words to
  say. Stop there and wait for the human to confirm before you continue.
- **Never ask for a SimpleFIN setup token in chat.** The human pastes it into
  Purser's own pages.
- **Stop and report** instead of improvising when a check fails. The
  [troubleshooting table](#troubleshooting) lists the known failures. Never
  delete a Docker volume to "start fresh": it may hold someone's books.

Run every command on the Docker host. If you reach the host over SSH, run them
there and read the notes in [Remote access](#remote-access).

## 1. Check the prerequisites

Run these and compare the results:

```bash
docker version --format '{{.Server.Os}}/{{.Server.Arch}}'
docker compose version
curl --version | head -n 1
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/
docker volume ls --filter name=purser-pgdata --format '{{.Name}}'
```

| Check | Expected | If not |
| --- | --- | --- |
| Docker server platform | `linux/amd64` or `linux/arm64` | Stop. The image is published for these two only. On an Apple Silicon Mac, Docker Desktop and Colima report `linux/arm64`; see [Docker on macOS](../../how-to/macos-uvx/). |
| Compose | `Docker Compose version v2` or later | Ask the human to install or update Docker Compose. |
| curl | any version | Install curl, or ask the human to. |
| Port 8000 | `000` (nothing answers) | Something already listens on port 8000. See the troubleshooting table. |
| Existing volume | no output | An earlier Purser install (or an earlier attempt at these steps) exists. Stop and ask the human; an existing database needs its original encryption key, not a new one. |

You do not need Git, Python, Node or a database on the host, and nothing is
built: Docker pulls the published Purser image and PostgreSQL 16 and runs them
in containers.

## 2. Download the compose file

Make a directory for this installation and download the release's compose file
into it:

```bash
mkdir purser && cd purser
curl -fsSLO https://raw.githubusercontent.com/mlamoure/purser/v2026.10.1/docker-compose.yml
grep -c 'image: ghcr.io/mlamoure/purser:v2026.10.1' docker-compose.yml
```

If `mkdir` reports that `purser` already exists, stop and ask the human: it may
hold an earlier installation and its key. The last command must print `1`.
Every later command runs in this `purser` directory.

This page installs the latest release, 2026.10.1. Each release has its own
compose file at the same address under its own tag (`vYYYY.M.PATCH`), and that
file names the matching image, here `ghcr.io/mlamoure/purser:v2026.10.1`,
published for linux/amd64 and linux/arm64.

## 3. Pull the images

```bash
PURSER_ENCRYPTION_KEY=keygen docker compose pull
```

This downloads the Purser image for the host's architecture and PostgreSQL 16.
`keygen` is a placeholder that satisfies Compose for utility commands; it is
not a key and never starts the application.

## 4. Generate the encryption key

Write a new key straight into `.env`, which Compose reads automatically. The
key never appears in your output:

```bash
if [ -e .env ]; then
  echo "STOP: .env already exists"
else
  (umask 077; PURSER_ENCRYPTION_KEY=keygen docker compose run --rm --no-deps -T purser keygen \
    | sed 's/^/PURSER_ENCRYPTION_KEY=/' > .env)
fi
grep -cE '^PURSER_ENCRYPTION_KEY=[A-Za-z0-9_-]{43}=$' .env
```

If it prints `STOP`, the file was left untouched: ask the human before going
on. The last command must print `1` (one valid key). Do not `cat` the file.

**Handoff: store the key.** Say to the human:

> I generated Purser's encryption key and saved it in the file `.env` in the
> `purser` folder on this machine, without displaying it. Please open that file,
> copy the value after `PURSER_ENCRYPTION_KEY=` into your password manager, and
> keep it apart from any backups. Purser cannot decrypt your data without it.
> Tell me when it is stored. I will not read it or keep a copy.

Wait for the confirmation. The `.env` file stays in place (readable only by its
owner) so that Docker can restart and upgrade Purser. If the human would rather
not keep the key on disk, they can delete `.env` after step 5 and export
`PURSER_ENCRYPTION_KEY` themselves before any later `docker compose up`.

## 5. Start Purser

```bash
docker compose up -d
for i in $(seq 1 30); do curl -fs http://localhost:8000/healthz && break; sleep 2; done
```

Expected: `{"status":"ok","database":"ok"}`. Two containers run, `purser` and
`postgres`. Migrations apply automatically at startup. Then confirm that setup
is still open:

```bash
curl -s http://localhost:8000/auth/setup/status
```

Expected: `{"setup_needed":true}`.

## 6. Handoff: the first-run wizard

Only the human can create the administrator and mint your token. Say:

> Purser is running. Please open `http://localhost:8000` in your browser and work
> through the setup wizard:
>
> 1. **Create admin**: your email and a password of at least 8 characters.
> 2. **Record encryption key**: the wizard shows the key Purser is running with,
>    once. Check that it matches the one you just stored, tick the box, and continue.
> 3. **Connect SimpleFIN** (optional, for live bank data): paste a setup token
>    from your SimpleFIN Bridge account, then map the accounts it finds. Paste
>    it only into Purser, never into this chat. You can also skip this.
> 4. **Import history** (optional): you can skip it.
> 5. **Connect agent**: keep the name `agent`, click **Create agent token**, and
>    give me the token it shows. It has `read + write` access: I act for you,
>    every write I make is recorded in the audit log and can be reversed, and I
>    can never grant myself more access.
>
> Tell me when you are done, and paste the token here.

If the human skipped the last step, they can mint the token later on the
**API & MCP Access** page (`/tokens`): name it, keep the `read` and `write`
boxes ticked (the default), leave `admin` unticked, and click **Create token**.
The token is shown exactly once.

When you have the token, check that the wizard finished:

```bash
curl -s http://localhost:8000/auth/setup/status
```

Expected: `{"setup_needed":false}`.

The token is your credential. Keep it only in your MCP client configuration (an
environment variable such as `PURSER_TOKEN` where your client supports one) and
never write it into logs, notes or files the human did not ask for.

## 7. Register the MCP server

Purser speaks MCP over streamable HTTP:

- Endpoint: `http://localhost:8000/mcp` on the Docker host. Use the HTTPS URL
  instead when Purser is published remotely.
- Auth header: `Authorization: Bearer` followed by the token.

Pick the snippet for your own client. Each one assumes the token is in the
environment variable `PURSER_TOKEN` of the shell you run it from.

**Claude Code**:

```bash
claude mcp add --transport http purser http://localhost:8000/mcp \
  --header "Authorization: Bearer $PURSER_TOKEN"
```

**Codex** (CLI, desktop app and IDE extension share `~/.codex/config.toml`).
Add this table; Codex reads the token from `PURSER_TOKEN` at startup:

```toml
[mcp_servers.purser]
url = "http://localhost:8000/mcp"
bearer_token_env_var = "PURSER_TOKEN"
```

`PURSER_TOKEN` must then be set wherever Codex starts, in every future session.
If your sessions have no persistent environment (a CLI agent launched fresh
each time, or a GUI app that does not read your shell profile), put the header
in the file instead and make the file readable only by its owner:

```toml
[mcp_servers.purser]
url = "http://localhost:8000/mcp"
http_headers = { Authorization = "Bearer YOUR_TOKEN" }
```

```bash
chmod 600 ~/.codex/config.toml
```

Check the result with `codex mcp list`.

**OpenClaw, Gemini and any other MCP client**: use the matching recipe in
[Connect an agent](../../how-to/connect-an-agent/#connect-from-your-client)
([OpenClaw](../../how-to/connect-an-agent/#openclaw),
[Gemini](../../how-to/connect-an-agent/#gemini-cli-and-code-assist),
[any other client](../../how-to/connect-an-agent/#any-other-mcp-client)).

Most clients load new servers only in a new session; restart or reload yours
if the Purser tools do not appear.

## 8. Verify the connection

1. Call `describe` first. Check that `your_scopes` is `["read", "write"]`.
   `describe` is the first call of every future session too: it returns the
   data model, conventions, category tree and accounts in one round-trip.
2. Call `list_accounts`. It succeeds even on an empty instance.

If your client cannot load the server yet, check the endpoint directly. The
trailing slash matters for `curl`: `/mcp` answers with a redirect to `/mcp/`.

```bash
curl -s -X POST http://localhost:8000/mcp/ \
  -H "Authorization: Bearer $PURSER_TOKEN" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"describe","arguments":{}}}'
```

A reply containing `your_scopes` means the token and endpoint work.

Then tell the human what you did, which URL Purser answers on, and that their
encryption key and admin password are the two secrets only they hold.

## 9. Optional next steps

- **Live bank data**: if SimpleFIN was skipped, the human connects it later
  under **Config**, then **SimpleFIN**, by pasting a setup token there. Details:
  [Connect banks with SimpleFIN](../../how-to/simplefin/). Call `trigger_sync`
  only when the human wants fresh data; SimpleFIN Bridge allows 24 fetches a day.
- **Older history**: the human uploads a QIF or CSV export on the **Import** page.
  See [Import QIF and CSV](../../how-to/import-qif-csv/).
- **Backups**: set them up before the books matter. See
  [Backup and restore](../../how-to/backup-restore/).

## Verify it worked

- [ ] `curl http://localhost:8000/healthz` returns `{"status":"ok","database":"ok"}`.
- [ ] `docker compose ps` shows `purser` and `postgres` running, `purser` healthy.
- [ ] `/auth/setup/status` returns `{"setup_needed":false}`.
- [ ] The human confirmed the encryption key is in their password manager.
- [ ] `describe` returns `your_scopes` of `["read", "write"]`.
- [ ] `list_accounts` answers without an error.
- [ ] The key and the token appear nowhere in your chat output or notes.

## Build from source instead

The steps above run the published image. Contributors, and anyone who wants to
run code from the repository, build the image from a clone instead (this needs
Git; the first build takes several minutes):

```bash
git clone https://github.com/mlamoure/purser.git
cd purser
PURSER_ENCRYPTION_KEY=keygen docker compose build purser
```

Then continue with [step 4](#4-generate-the-encryption-key) in the `purser`
clone, where Git ignores `.env`. The local build takes the image name from the
compose file, so it stands in for that release's published image until
`docker compose pull purser` fetches the published one again.

## Troubleshooting

Read the application log with `docker compose logs purser --tail 50`.

| Symptom | Cause | Fix |
| --- | --- | --- |
| `docker compose pull` fails with `failed to resolve reference`, `TLS handshake timeout`, `no such host` or `i/o timeout` | The host cannot reach the image registry (`ghcr.io`): no network, a firewall, or a proxy Docker does not use. | Check that `curl -sI https://ghcr.io/v2/` answers; ask the human to fix the network or configure Docker's proxy, then repeat step 3. A failed pull also prints `Some service image(s) must be built from source`: ignore that hint, it needs a source clone. |
| `docker compose pull` fails with `manifest unknown` or `not found` | The compose file names a tag that does not exist, usually after an edited or partial download. | Download the compose file again (step 2); do not edit its image line. |
| `docker compose pull` or `up` fails with `no matching manifest for` a platform, or a container exits with `exec format error` | The Docker server is not `linux/amd64` or `linux/arm64`, or Docker is told to use another platform (`DOCKER_DEFAULT_PLATFORM`). | Stop. Check step 1's platform and unset `DOCKER_DEFAULT_PLATFORM`. Other architectures are unsupported. |
| `docker compose up` or `run` starts a build and fails with `failed to read dockerfile` or `unable to prepare context` | The image was never pulled, so Compose fell back to the file's `build:` section, which needs a source clone. | Run step 3 and fix the error it reports. Never build in this directory. |
| `docker compose` fails with `required variable PURSER_ENCRYPTION_KEY is missing a value` | No key in `.env` or the environment. | Repeat step 4, or have the human export the key they stored. |
| The log shows `PURSER_ENCRYPTION_KEY is not a valid Fernet key` and the container restarts | The value is truncated or not a Purser key. | Have the human compare `.env` with the stored key. |
| The log shows `PURSER_ENCRYPTION_KEY does not match this database` with two fingerprints | The database volume was created with a different key, usually an earlier install. | Stop. Ask the human for the original key. Never delete the volume to get past this. |
| `docker compose up` fails with `port is already allocated` or `address already in use` | Another program uses host port 8000. | Ask the human to stop it, or publish another port with a `docker-compose.override.yml` containing `ports: !override` and the new mapping, plus the matching `PURSER_PUBLIC_URL`. |
| `/healthz` gets no answer | The app is still starting or exited. | Wait for the loop in step 5; then check `docker compose ps` and the log. |
| `/healthz` returns HTTP 503 with `{"status":"error","database":"down"}` | Purser cannot reach PostgreSQL. | Check `docker compose ps postgres` and `docker compose logs postgres`. |
| `/auth/setup/status` stays `{"setup_needed":true}` | The human has not created the admin yet. | Wait for the handoff in step 6. |
| `/mcp` returns 401 `{"error":"invalid or missing bearer token"}` | The token is missing, mistyped, revoked or expired, or the header lacks the word `Bearer`. | Check the header format; ask the human for a new token from **API & MCP Access** if needed. |
| `/mcp` returns 421 Misdirected Request | The request's host is not `localhost` or the host in `PURSER_PUBLIC_URL`. | Set `PURSER_PUBLIC_URL` to the URL clients use, then run `docker compose up -d` again. |
| `curl` to `/mcp` returns 307 | The bare path redirects to `/mcp/`. | Use `/mcp/` with `curl`; MCP clients follow the redirect. |
| A write tool is refused for scope | The token lacks `write`. | Ask the human for a `read + write` token. |

## Remote access

Compose publishes Purser on `127.0.0.1:8000` of the Docker host only. When the
human's browser or your MCP client runs on another machine, the simplest path
is an SSH tunnel from that machine, after which `http://localhost:8000` works
there too:

```bash
ssh -L 8000:127.0.0.1:8000 YOUR_USER@YOUR_HOST
```

For permanent remote access, finish the wizard first, then put Purser behind
HTTPS and set `PURSER_BIND_ADDRESS` and `PURSER_PUBLIC_URL`. See
[Docker host binding](../../reference/configuration/#docker-host-binding) and the
[configuration reference](../../reference/configuration/).
