---
title: Set up Purser with your AI
description: Step-by-step instructions an AI agent follows to take a machine with Docker to a running Purser, on that machine or the home network, with itself connected over MCP.
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
- **How your API token reaches you is the human's choice.** They create it in
  Purser, then either follow the steps Purser shows for your client or paste
  the token to you. Say once what each means, then do what they choose. Treat
  both the same.
- **Stop and report** instead of improvising when a check fails. The
  [troubleshooting table](#troubleshooting) lists the known failures. Never
  delete a Docker volume to "start fresh": it may hold someone's finances.

### Where things run

Decide this before step 1, and ask the human if you cannot tell:

- **The Docker host** is the machine Purser will run on. Run every command on
  this page there, directly or over SSH.
- **Same machine.** The human's browser and you both run on the Docker host
  (a Mac or a Linux desktop). Purser's address is `http://localhost:8000`,
  as written throughout this page.
- **Another machine on the home network.** The Docker host is a home server,
  and the human's browser, or you, run on a different machine. After step 5,
  follow [Use Purser from another machine on the home network](#use-purser-from-another-machine-on-the-home-network),
  and use the address it prints wherever a later step says
  `http://localhost:8000`.

Purser is meant for the machine it runs on and for the home network. Reaching
it from anywhere else is the human's own project (see
[Put Purser behind a reverse proxy](../../how-to/reverse-proxy/)) and is not
part of this setup.

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
| Docker server platform | `linux/amd64` or `linux/arm64` | `docker: command not found`, or it cannot connect: see [No Docker yet](#no-docker-yet). Any other platform: stop, the image is published for these two only. On an Apple Silicon Mac, Colima and Docker Desktop report `linux/arm64`. |
| Compose | `Docker Compose version v2` or later | `'compose' is not a docker command`: see [No Docker yet](#no-docker-yet). |
| curl | any version | Install curl, or ask the human to. |
| Port 8000 | `000` (nothing answers) | Another program uses port 8000. Pick another port, for example 8010, check it the same way, and use it wherever this page says 8000. Step 4 records it. |
| Existing volume | no output | An earlier Purser install exists. Stop and ask the human; an existing database needs its original encryption key, not a new one. |

You do not need Git, Python, Node or a database on the host, and nothing is
built: Docker pulls the published Purser image and PostgreSQL 16 and runs them
in containers.

### No Docker yet

**On a Mac**, run the install script. It installs [Colima](https://github.com/abiosoft/colima)
(a small Linux virtual machine for containers), the Docker command and Compose
with Homebrew, starts Colima and sets it to start at login. It does nothing if
Docker already works, and it stops with instructions if Docker Desktop or
OrbStack is installed but not running:

```bash
curl -fsSLO https://raw.githubusercontent.com/mlamoure/purser/v2026.11.7/scripts/install-docker-macos.sh
bash install-docker-macos.sh
```

It ends with a line starting `OK:`. If it prints `STOP: Homebrew is not
installed`, **hand off**: installing Homebrew asks for the Mac's password, so
the human runs the command the script printed in their own Terminal window and
tells you when it has finished; then run the script again. Details:
[Docker on macOS](../../how-to/macos-uvx/).

**On Linux**, stop: installing Docker needs the administrator password, which
you do not have. Say to the human:

> Purser needs Docker Engine and its Compose plugin on this machine, and
> installing them needs your administrator password. Please follow
> https://docs.docker.com/engine/install/ for this system, then add your user
> to the `docker` group with `sudo usermod -aG docker $USER`, log out and back
> in, and tell me when `docker compose version` works.

If `docker` exists but answers `permission denied` for `docker.sock`, the user
is not in the `docker` group yet, or this session started before they were
added: ask for the `usermod` command above, a fresh login, and a restart of
your own session.

Then repeat step 1.

## 2. Download the compose file

Make a directory for this installation and download the release's compose file
into it:

```bash
mkdir purser && cd purser
curl -fsSLO https://raw.githubusercontent.com/mlamoure/purser/v2026.11.7/docker-compose.yml
grep -c 'image: ghcr.io/mlamoure/purser:v2026.11.7' docker-compose.yml
```

If `mkdir` reports that `purser` already exists, stop and ask the human: it may
hold an earlier installation and its key. The last command must print `1`.
Every later command runs in this `purser` directory.

This page installs the latest release, 2026.11.7. Each release has its own
compose file at the same address under its own tag (`vYYYY.M.PATCH`), and that
file names the matching image, here `ghcr.io/mlamoure/purser:v2026.11.7`,
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
  IMAGE=$(PURSER_ENCRYPTION_KEY=keygen docker compose config --images | grep -m1 '/purser:')
  (umask 077; docker run --rm --pull never "$IMAGE" keygen 2>/dev/null \
    | sed 's/^/PURSER_ENCRYPTION_KEY=/' > .env.new)
  if [ "$(grep -cE '^PURSER_ENCRYPTION_KEY=[A-Za-z0-9_-]{43}=$' .env.new)" = 1 ] \
     && [ "$(wc -l < .env.new)" -eq 1 ]; then
    mv .env.new .env
  else
    rm -f .env.new
    echo "FAILED: no key was generated"
  fi
fi
grep -cE '^PURSER_ENCRYPTION_KEY=[A-Za-z0-9_-]{43}=$' .env
```

The last command must print `1` (one valid key). Do not `cat` the file.

- `STOP`: the file was left untouched. Ask the human before going on.
- `FAILED`: nothing was written. The image is not on this machine: repeat
  step 3, fix what it reports, then run this step again.

The `.env` file stays in place, readable only by its owner, so that Docker can
restart and upgrade Purser. The human records the key from Purser's own setup
page in step 6, so there is nothing to hand over here.

Then record what step 1 and the human's situation call for, one appended line
each (never open the file to do it):

| When | Append to `.env` |
| --- | --- |
| Port 8000 was taken in step 1 | `echo 'PURSER_HOST_PORT=8010' >> .env`, with the port you chose |
| The household's time zone is not UTC | `echo 'PURSER_TIMEZONE=America/New_York' >> .env`, with the IANA name of the zone the human lives in. `timedatectl show -p Timezone --value` (Linux) or `readlink /etc/localtime` (macOS) shows the host's; ask if the host's zone is UTC or you are unsure. |

Purser uses the time zone to decide which day a bill is due and when the daily
bank sync runs. The human can change it later under **Settings**, **General**.

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

If the human's browser, or you, run on another machine, do
[Use Purser from another machine on the home network](#use-purser-from-another-machine-on-the-home-network)
now, before the handoff.

## 6. Handoff: the first-run wizard

Only the human can create the administrator, record the key and create your
token. How the token then reaches you is their choice: offer both ways in step
5 and do not steer. Say (with Purser's address, and your own client's name in
step 5):

> Purser is running. Please open `http://localhost:8000` in your browser now and
> work through the setup wizard:
>
> 1. **Create admin**: your email and a password of at least 8 characters.
> 2. **Record encryption key**: copy the key it shows into your password
>    manager, keep it apart from any backups, tick the box and continue. Purser
>    cannot decrypt your data without this key, and I never see it. It is the
>    same key I saved, unread, in the file `.env` in the `purser` folder, which
>    Docker needs in order to restart Purser.
> 3. **Connect SimpleFIN** (optional, for live bank data): paste a setup token
>    from your SimpleFIN Bridge account. Paste the token only into Purser,
>    never into this chat. Purser then lists the bank accounts it found. You
>    can map them there (check the type Purser proposes for each, leave
>    **Start from the bank's balance** ticked and select **Map account**), or
>    select **Continue** and I will map them with you once I am connected.
>    You can also skip this step entirely.
> 4. **Import history** (optional): you can skip it.
> 5. **Connect agent**: keep the name `agent` and select **Create agent token**.
>    Then connect me in whichever way you prefer:
>    - choose the tab for the client I am and follow the steps it shows, on the
>      machine where I run. The token then stays on that machine.
>    - or paste the token to me here and I will connect myself. The token is
>      then also kept in this conversation's history and sent to my model
>      provider.
>
>    The token has `read + write` access: I act for you, every write I make is
>    recorded in the audit log and can be reversed, and I can never grant
>    myself more access. You can revoke it at any time on the
>    **Settings -> Agents & API** page.
>
> If the page reloads partway through, sign in: the Dashboard's **Finish setting
> up** card links to each step still open.
>
> Tell me when you are done.

If the first page the human sees is a sign-in form and they have not created
the admin, stop: someone else reached this instance first.

If the human skipped the last step, they can create the token later on the
**Settings -> Agents & API** page: name it, keep the `read` and `write`
boxes ticked (the default), leave `admin` unticked, and select **Create token**.
The same per-client steps appear under the new token, which is shown once.

When the human confirms, check that the admin account exists:

```bash
curl -s http://localhost:8000/auth/setup/status
```

Expected: `{"setup_needed":false}`. This turns false at the wizard's first
step, so it does not show the rest finished; the token from its last step
answering `describe` in step 8 does.

## 7. Connect your client

Purser speaks MCP over streamable HTTP at `http://localhost:8000/mcp` (or the
home-network address plus `/mcp`), with the header `Authorization: Bearer`
followed by the token.

**The human followed the wizard's steps for your client.** The server is then
registered, and most clients load new servers only in a new session. Ask the
human to restart you in a way that keeps this conversation, then go to step 8:

| Client | Restart that keeps the conversation |
| --- | --- |
| Claude Code | exit, then `claude --continue` |
| Codex | exit, then `codex resume --last` |
| Claude Desktop | quit and reopen the app, then return to this chat |
| Another client | open a new session and tell it to continue at step 8 of this page |

**The human pasted the token to you.** Register the server yourself, keep the
token only in your client's configuration, and never write it into logs, notes
or files the human did not ask for. Each snippet assumes the token
is in the environment variable `PURSER_TOKEN` of the shell you run it from.

Claude Code (`--scope user` makes Purser available in every folder, not only
the current one):

```bash
claude mcp add --transport http --scope user purser http://localhost:8000/mcp \
  --header "Authorization: Bearer $PURSER_TOKEN"
```

Codex (the CLI, desktop app and IDE extension share `~/.codex/config.toml`).
Add this table, with the token in place of `YOUR_TOKEN`, and make the file
readable only by its owner:

```toml
[mcp_servers.purser]
url = "http://localhost:8000/mcp"
http_headers = { Authorization = "Bearer YOUR_TOKEN" }
```

```bash
chmod 600 ~/.codex/config.toml
```

Gemini CLI:

```bash
gemini mcp add --scope user --transport http \
  --header "Authorization: Bearer $PURSER_TOKEN" purser http://localhost:8000/mcp
```

Claude Desktop, OpenClaw and any other client: use the recipe in
[Connect an agent](../../how-to/connect-an-agent/#connect-from-your-client).
That page also says which clients cannot reach a Purser on the home network:
the connector dialogs in claude.ai, Claude Desktop and the ChatGPT app connect
from the vendor's servers.

## 8. Verify the connection

1. Call `describe` first. Check that `your_scopes` is `["read", "write"]`.
   `describe` is the first call of every future session too: it returns the
   data model, conventions, category tree and accounts in one round-trip.
2. Call `list_accounts`. It succeeds even on an empty instance.

If the Purser tools are missing, the client has not loaded the server: check
its list (`claude mcp list`, `codex mcp list`, `gemini mcp list`) and restart
it. If you hold the token, you can check the endpoint directly. The trailing
slash matters for `curl`: `/mcp` answers with a redirect to `/mcp/`.

```bash
curl -s -X POST http://localhost:8000/mcp/ \
  -H "Authorization: Bearer $PURSER_TOKEN" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"describe","arguments":{}}}'
```

A reply containing `your_scopes` means the token and endpoint work.

## 9. Map the bank accounts and check the balances

Skip this step if the human did not connect SimpleFIN.

**Map what the human left for you.** A bank account is not in Purser, and
nothing from it is imported, until it is mapped to a ledger account. Call
`map_feed_accounts` with no arguments. It answers a `plan` and writes
nothing: one line for each bank account still waiting, with the name, type and
path Purser proposes. An empty plan means the human mapped everything: go on
to the balances.

Otherwise show the human the plan as a table (bank, the bank's name for the
account, proposed name, proposed type) and ask them to confirm or correct it.
Say these three things with it:

- The account type cannot be changed after mapping, so it is worth a look.
- A line with `confident: false` is one where the bank's data does not settle
  what the account is: ask what it is instead of offering the proposal. When
  the line has a `note`, the bank names a cash product but also lists
  investments in it: ask whether it is an investment account (type
  `brokerage`).
- A line with `action: ignore` looks like the same account arriving through a
  second login at the same bank (`duplicate_of`): mapping it twice would count
  the money twice.

On their answer, call `map_feed_accounts` again with
`accounts=[{"ref": ...}, ...]`: one line for each account they agreed to, and
only the fields they corrected (`name`, `type`, `path`, or
`action: "ignore"`). A line with only `ref` takes Purser's proposal. Leave out
any account they want to decide later. The batch lands whole or not at all; if
it is refused, the message names each line's problem. Never apply a plan the
human has not seen.

**Check the balances.** The first sync brings in about three months of each
account's transactions and no balance from before them. Mapping therefore
gives each new account a **starting balance** from what the bank reports, so
the accounts normally match the bank already. You only check, and fill in
what is missing.

For each account in `list_accounts` whose `type` is `checking`, `savings`,
`cash` or `credit_card`:

1. Call `account_balance` with the account's `path`. When
   `bank_snapshot.status` is `ok` and `bank_snapshot.comparison.difference` is
   zero, the account matches the bank: go to the next one.
2. Otherwise call `set_starting_balance` with the account's `path`. You pass
   nothing else: Purser takes the bank's figure, the date, the sign and the
   basis.
   - `status` is `set`: call `account_balance` again and check that the
     difference is now zero.
   - `status` is `unchanged`: the account already has a starting balance and
     still differs from the bank. Tell the human the account and the
     difference. Do not correct it yourself.
   - The call is refused: tell the human the account and the reason it gives.

Never work a starting balance out yourself and never use `reconcile_account`
for it. Loans, brokerage accounts and assets need nothing here: Purser takes
their value from the feed.

Then call `net_worth` and tell the human the figure, so they can compare it
with their bank. Finish by telling them what you did, which address Purser
answers on, and that their encryption key and admin password are the two
secrets only they hold.

## 10. Optional next steps

- **Live bank data**: if SimpleFIN was skipped, the human connects it later
  under **Settings**, then **Connections**, by pasting a setup token there. Details:
  [Connect banks with SimpleFIN](../../how-to/simplefin/). Then do step 9. Call
  `trigger_sync` only when the human wants fresh data; SimpleFIN Bridge allows
  24 fetches a day.
- **Older history**: the human uploads a QIF or CSV export on the **Import** page.
  See [Import QIF and CSV](../../how-to/import-qif-csv/). It can be done at any
  time: Purser lowers each account's starting balance by what the file brings
  in, so the balances keep matching the bank.
- **Backups**: set them up before the finances matter. See
  [Backup and restore](../../how-to/backup-restore/).

## Use Purser from another machine on the home network

Compose publishes Purser on `127.0.0.1` of the Docker host only. To use it from
another machine on the same home network (the human's laptop, or the machine
you run on), let it listen on the network, tell it its address and list the
other names the machine goes by. Run this in the `purser` directory on the
Docker host:

```bash
ADDRESS=$( (hostname -I 2>/dev/null || ipconfig getifaddr en0 2>/dev/null) | awk '{print $1}')
NAME=$( (scutil --get LocalHostName 2>/dev/null || hostname -s) | tr '[:upper:]' '[:lower:]')
case "$NAME" in ''|*[!a-z0-9-]*) NAMES=$ADDRESS ;; *) NAMES=$ADDRESS,$NAME,$NAME.local ;; esac
PORT=$(sed -n 's/^PURSER_HOST_PORT=//p' .env); PORT=${PORT:-8000}
if [ -z "$ADDRESS" ]; then
  echo "STOP: could not find this machine's network address"
elif grep -qE '^PURSER_(BIND_ADDRESS|PUBLIC_URL|ALLOWED_HOSTS)=' .env; then
  echo "STOP: .env already sets PURSER_BIND_ADDRESS, PURSER_PUBLIC_URL or PURSER_ALLOWED_HOSTS"
else
  printf 'PURSER_BIND_ADDRESS=0.0.0.0\nPURSER_PUBLIC_URL=http://%s:%s\nPURSER_ALLOWED_HOSTS=%s\n' \
    "$ADDRESS" "$PORT" "$NAMES" >> .env
  docker compose up -d
  echo "Purser now answers at http://$ADDRESS:$PORT (names it accepts: $NAMES)"
fi
```

Then check from the other machine, with the address it printed:

```bash
curl http://192.168.1.20:8000/healthz
```

Expected: `{"status":"ok","database":"ok"}`. From here on, that address
replaces `http://localhost:8000` in the wizard handoff, in your client's
configuration and in what you tell the human. `http://localhost:8000` keeps
working on the Docker host itself. After the wizard, **Settings**, **Agents &
API** shows the human "Reachable from: your home network" and the address
agents connect to.

What to know, and to tell the human:

- **Its names.** Agents connect at the address in `PURSER_PUBLIC_URL`, at the
  names in `PURSER_ALLOWED_HOSTS` (the command lists the machine's address,
  its name and the name with `.local`) and at `localhost` on the host itself.
  Use the address in your configuration: a `.local` name works only where the
  other machine resolves it. A request to any other name gets `421`, which
  says how to allow it: add the exact name to the comma-separated
  `PURSER_ALLOWED_HOSTS` line in `.env` and run `docker compose up -d`. Wildcards
  are refused, and so is a malformed line: Purser then does not start, and its
  log names the entry. Give the machine a fixed address in the router so that
  it does not change.
- **Plain HTTP.** On the home network the admin password and your token travel
  unencrypted. That is the supported setup for a network the human trusts.
  Purser has no certificate support of its own; a human who wants encryption,
  or access from outside the home, puts their own HTTPS reverse proxy in front
  ([Put Purser behind a reverse proxy](../../how-to/reverse-proxy/)).
- **Do the wizard straight away.** Until the admin account exists, anyone who
  can reach the address can create it.
- **A firewall on the Docker host** may block the port: if the check above
  fails while `http://localhost:8000/healthz` works on the host, ask the human
  to allow the port on the home network.

To undo it, ask the human to remove the three lines from `.env`, then run
`docker compose up -d`.

An SSH tunnel is an alternative for a single session, with nothing to change on
the host: `ssh -L 8000:127.0.0.1:8000 YOUR_USER@YOUR_HOST` on the other machine
makes `http://localhost:8000` work there while the tunnel is open.

## Verify it worked

- [ ] `curl http://localhost:8000/healthz` returns `{"status":"ok","database":"ok"}`.
- [ ] `docker compose ps` shows `purser` and `postgres` running, `purser` healthy.
- [ ] `/auth/setup/status` returns `{"setup_needed":false}` (the admin account exists).
- [ ] The human confirmed the encryption key is in their password manager.
- [ ] `describe` returns `your_scopes` of `["read", "write"]`.
- [ ] `list_accounts` answers without an error.
- [ ] With SimpleFIN connected: `map_feed_accounts` answers an empty plan, or
      the human chose to leave what remains.
- [ ] With SimpleFIN connected: every checked account's
      `bank_snapshot.comparison.difference` is zero, and the human has seen the
      net worth figure.
- [ ] The key appears nowhere in your chat output or notes, and the token only
      if the human chose to paste it.

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
| `docker: command not found`, or `'compose' is not a docker command` | No Docker runtime, or no Compose plugin. | [No Docker yet](#no-docker-yet). |
| `permission denied` while connecting to `docker.sock` | The user is not in the `docker` group, or this session predates the change. | Ask the human to run `sudo usermod -aG docker $USER`, log out and in, and restart your session. |
| `docker compose pull` fails with `failed to resolve reference`, `TLS handshake timeout`, `no such host` or `i/o timeout` | The host cannot reach the image registry (`ghcr.io`): no network, a firewall, or a proxy Docker does not use. | Check that `curl -sI https://ghcr.io/v2/` answers; ask the human to fix the network or configure Docker's proxy, then repeat step 3. A failed pull also prints `Some service image(s) must be built from source`: ignore that hint, it needs a source clone. |
| `docker compose pull` fails with `manifest unknown` or `not found` | The compose file names a tag that does not exist, usually after an edited or partial download. | Download the compose file again (step 2); do not edit its image line. |
| `docker compose pull` or `up` fails with `no matching manifest for` a platform, or a container exits with `exec format error` | The Docker server is not `linux/amd64` or `linux/arm64`, or Docker is told to use another platform (`DOCKER_DEFAULT_PLATFORM`). | Stop. Check step 1's platform and unset `DOCKER_DEFAULT_PLATFORM`. Other architectures are unsupported. |
| Step 4 prints `FAILED: no key was generated` | The Purser image is not on this machine. | Repeat step 3 and fix what it reports, then run step 4 again. Nothing was written. |
| `docker compose up` starts a build and fails with `failed to read dockerfile` or `unable to prepare context` | The image was never pulled, so Compose fell back to the file's `build:` section, which needs a source clone. | Run step 3 and fix the error it reports. Never build in this directory. |
| `docker compose` fails with `required variable PURSER_ENCRYPTION_KEY is missing a value` | No key in `.env` or the environment. | Repeat step 4, or have the human export the key they stored. |
| The log shows `PURSER_ENCRYPTION_KEY is not a valid Fernet key` and the container restarts | The value is truncated or not a Purser key. | Have the human compare `.env` with the stored key. |
| The log shows `PURSER_ENCRYPTION_KEY does not match this database` with two fingerprints | The database volume was created with a different key, usually an earlier install. | Stop. Ask the human for the original key. Never delete the volume to get past this. |
| `docker compose up` fails with `port is already allocated` or `address already in use` | Another program uses the host port. | Append `PURSER_HOST_PORT=8010` (or another free port) to `.env`, run `docker compose up -d` again, and use that port wherever this page says 8000. |
| The log shows `PURSER_TIMEZONE must be an IANA time zone name` | The zone name in `.env` is not an IANA name. | Ask the human to correct the `PURSER_TIMEZONE` line (for example `America/New_York`), then `docker compose up -d`. |
| `/healthz` gets no answer | The app is still starting or exited. | Wait for the loop in step 5; then check `docker compose ps` and the log. |
| `/healthz` returns HTTP 503 with `{"status":"error","database":"down"}` | Purser cannot reach PostgreSQL. | Check `docker compose ps postgres` and `docker compose logs postgres`. |
| `/auth/setup/status` stays `{"setup_needed":true}` | The human has not created the admin yet. | Wait for the handoff in step 6. |
| The Purser tools do not appear in your client | The server is not registered for this session, or the client was not restarted. Claude Code and Gemini register a server for one folder unless told `--scope user`. | Check the client's server list, register with `--scope user`, and restart the client. |
| `/mcp` returns 401 `{"error":"invalid or missing bearer token"}` | The token is missing, mistyped, revoked or expired, or the header lacks the word `Bearer`. | Check the header format; ask the human for a new token from **Settings -> Agents & API** if needed. |
| `/mcp` returns 421 `misdirected request` | The request's host is not `localhost`, the host in `PURSER_PUBLIC_URL` or a name in `PURSER_ALLOWED_HOSTS`. The answer names the address that works. | Connect to that address, or add the name to `PURSER_ALLOWED_HOSTS` in `.env` and run `docker compose up -d`; see [Use Purser from another machine on the home network](#use-purser-from-another-machine-on-the-home-network). |
| The container restarts and the log shows `error: PURSER_ALLOWED_HOSTS:` | An entry in `.env` is not an exact host name or IP address (a wildcard, `http://`, a path or a typo). The line names the entry. | Correct or remove that entry, then `docker compose up -d`. |
| The address works on the Docker host but not from another machine | Purser still listens on `127.0.0.1` only, or a firewall blocks the port. | Follow the home-network section; then ask the human to allow the port in the host's firewall. |
| `curl` to `/mcp` returns 307 | The bare path redirects to `/mcp/`. | Use `/mcp/` with `curl`; MCP clients follow the redirect. |
| A write tool is refused for scope | The token lacks `write`. | Ask the human for a `read + write` token. |
| In step 9 `set_starting_balance` is refused | The bank's balance is stale (`stale_balance`), the bank reported no balance Purser can use for the account, or the account is reconciled and differs from the bank (`reconciled`). The message names which. | Tell the human the account and the reason. A stale balance needs a sync first; do not work a starting balance out yourself or use `reconcile_account` for it. |
