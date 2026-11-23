---
title: Quickstart
description: From a compose file to a connected AI agent in three steps.
---

This tutorial takes a fresh machine to a running Purser with your AI agent
connected over MCP.

**Rather have your AI do it?** Hand your agent
[Set up Purser with your AI](../ai-setup/): it runs the same steps and tells
you exactly when it needs you.

**You need:** Docker with Compose on linux/amd64 or linux/arm64 and curl. On a
Mac without Docker, [Docker on macOS](../../how-to/macos-uvx/) has a script
that installs it;
optionally, for live bank data, a [SimpleFIN Bridge](https://bridge.simplefin.org/)
account. You do not need Git, and nothing is built: Compose pulls the published
image.

## 1. Download the compose file and generate the encryption key

```bash
mkdir purser && cd purser
curl -fsSLO https://raw.githubusercontent.com/mlamoure/purser/v2026.11.6/docker-compose.yml
PURSER_ENCRYPTION_KEY=keygen docker compose pull
docker run --rm ghcr.io/mlamoure/purser:v2026.11.6 keygen
```

The compose file belongs to the latest release, 2026.11.6, and names its image,
`ghcr.io/mlamoure/purser:v2026.11.6` (linux/amd64 and linux/arm64). Every
release publishes its own at the same address under its tag (`vYYYY.M.PATCH`).
The temporary `keygen` value satisfies Compose interpolation for the pull.
The last command generates a key without starting the app or connecting
to the database.

Record the generated key in a password manager, separately from your backups.
Encrypted data, including the archived bank history, is unrecoverable without it.

## 2. Start the application

```bash
export PURSER_ENCRYPTION_KEY="YOUR_KEY"
docker compose up -d
curl http://localhost:8000/healthz
```

Instead of exporting the key in every shell, you can put the line
`PURSER_ENCRYPTION_KEY=` followed by the key in a `.env` file next to
`docker-compose.yml` (readable only by you); Compose reads it automatically.

Expected: `{"status":"ok","database":"ok"}`. Two containers run: the app and
PostgreSQL 16. Migrations apply automatically on startup. Purser refuses to
start without the key. If another program already uses port 8000, add
`PURSER_HOST_PORT=8010` to `.env` and use that port below. Compose publishes
Purser on this machine only; to use it from another machine at home, see
[Another machine on your home network](../../how-to/docker-compose/#another-machine-on-your-home-network).

## 3. Run the setup wizard

Open `http://localhost:8000`. The first-run wizard walks you through
everything else:

1. Create the admin account.
2. Confirm the encryption key: the wizard shows the key Purser is running with
   until you acknowledge it. Check that it matches the key you stored, then
   acknowledge it.
3. (Optional) Paste a SimpleFIN **setup token** to connect your bank, then
   map the discovered accounts: Purser proposes a type for each from the
   bank's name, and starts each account from the balance your bank reports.
4. (Optional) Import bank history from a QIF or CSV export.
5. Connect your AI agent: the wizard creates a scoped **read + write** API
   token and shows the exact steps for your client, with the address and
   token filled in: Claude Code, Claude Desktop, Codex, Gemini, OpenClaw or
   any other MCP client. That scope pair lets your agent
   act for you: every write it makes is recorded in the audit log and can be
   reversed, and when it acts on its own idea it proposes instead.

SimpleFIN setup tokens come from your SimpleFIN Bridge account page and are
one-time secrets — paste them only into the wizard, never into a chat.
If the first fetch fails or times out after the token is claimed, keep the
wizard open and select **Connect & fetch** again. It retries the saved
connection without claiming the token twice. You can also skip the feed and
finish setup; errors appear as announcements for assistive technology.

If you reload or close the page after creating the admin, sign in instead: the
wizard does not come back, but the Dashboard shows a **Finish setting up** card
listing what is still open (recording the encryption key, connecting a feed,
mapping its accounts, connecting an agent), each with the reason and a link to
where it gets done. The card disappears once everything is done, or when you
dismiss it. A bank account that arrives after setup is not part of this card:
the Dashboard lists it separately until you map or ignore it.

## Balances match your bank from the start

A bank feed delivers about three months of transactions and no balance from
before them. With **Start from the bank's balance** ticked in the map form
(the default for a new account), Purser adds one starting entry per account,
so balances and net worth match your bank as soon as the accounts are mapped.
Import older history whenever you like: Purser lowers the starting entry by
what the file brings in. See
[Start an account from the bank's balance](../../how-to/starting-balance/).

## Build from source

To run code from the repository instead of the published image, clone it and
build (this needs Git), then continue with the key and step 2 in the clone:

```bash
git clone https://github.com/mlamoure/purser.git
cd purser
PURSER_ENCRYPTION_KEY=keygen docker compose build purser
```

The local build takes the image name from the compose file and stands in for
that release's published image until `docker compose pull purser`.

## Where next

- [Connect an agent](../../how-to/connect-an-agent/) — client config and operating conventions.
- [Run with Docker Compose](../../how-to/docker-compose/) — `.env`, the database password, upgrades and the worker split.
- [Put Purser behind a reverse proxy](../../how-to/reverse-proxy/) — HTTPS for remote access.
- [Configuration](../../reference/configuration/) — every `PURSER_*` variable.
- [Backup and restore](../../how-to/backup-restore/) — set this up before the books matter.
- [The ledger](../../explanation/ledger/) — balanced postings, source records and correction history.
