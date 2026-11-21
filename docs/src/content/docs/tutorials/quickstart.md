---
title: Quickstart
description: From a compose file to a connected AI agent in three steps.
---

This tutorial takes a fresh machine to a running Purser with your AI agent
connected over MCP.

**Rather have your AI do it?** Hand your agent
[Set up Purser with your AI](../ai-setup/): it runs the same steps and tells
you exactly when it needs you.

**You need:** Docker with Compose on linux/amd64 or linux/arm64 (including an
Apple Silicon Mac; see [Docker on macOS](../../how-to/macos-uvx/)) and curl;
optionally, for live bank data, a [SimpleFIN Bridge](https://bridge.simplefin.org/)
account. You do not need Git, and nothing is built: Compose pulls the published
image.

## 1. Download the compose file and generate the encryption key

```bash
mkdir purser && cd purser
curl -fsSLO https://raw.githubusercontent.com/mlamoure/purser/v2026.11.4/docker-compose.yml
PURSER_ENCRYPTION_KEY=keygen docker compose pull
PURSER_ENCRYPTION_KEY=keygen docker compose run --rm --no-deps purser keygen
```

The compose file belongs to the latest release, 2026.11.4, and names its image,
`ghcr.io/mlamoure/purser:v2026.11.4` (linux/amd64 and linux/arm64). Every
release publishes its own at the same address under its tag (`vYYYY.M.PATCH`).
The temporary `keygen` value satisfies Compose interpolation for these utility
commands. The last one generates a key without starting the app or connecting
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
start without the key. Configure `PURSER_PUBLIC_URL` if you access the app
through another host or reverse proxy. Compose binds to localhost by default.
Finish admin setup first, then set `PURSER_BIND_ADDRESS` to the interface that
should accept remote traffic and use HTTPS for remote access.

## 3. Run the setup wizard

Open `http://localhost:8000`. The first-run wizard walks you through
everything else:

1. Create the admin account.
2. Confirm the encryption key: the wizard shows the key Purser is running with
   until you acknowledge it. Check that it matches the key you stored, then
   acknowledge it.
3. (Optional) Paste a SimpleFIN **setup token** to connect your bank, then
   map the discovered accounts.
4. (Optional) Import bank history from a QIF or CSV export.
5. Connect your AI agent: the wizard mints a scoped **read + write** API
   token and shows exact connection steps for your client — Claude, Codex,
   OpenClaw, Gemini, or any other MCP client. That scope pair lets your agent
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
