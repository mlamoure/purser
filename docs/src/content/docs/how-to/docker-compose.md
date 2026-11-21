---
title: Run with Docker Compose
description: What the bundled compose files run, the .env file, the database password, remote access, the API and worker split, data, health checks and upgrades.
---

Docker Compose is the supported way to run Purser. The
[Quickstart](../../tutorials/quickstart/) takes you from an empty directory to
a running instance; this page explains what the compose files do and how to
run them from then on.

## What docker-compose.yml runs

`docker-compose.yml` starts two containers on a private Compose network:

| Service | What it does |
| --- | --- |
| `purser` | The published image for one release (`ghcr.io/mlamoure/purser:v<version>`, linux/amd64 and linux/arm64). Its default command, `purser serve`, applies database migrations, starts the scheduled jobs (bank sync, matching and the other daily jobs) in the same process, then serves the web UI, the REST API and `/mcp` on port 8000. It runs with every Linux capability dropped and `no-new-privileges`, and restarts unless you stop it. |
| `postgres` | PostgreSQL 16. Its data lives in the named volume `purser-pgdata`. It is not published on any host port: only containers on the Compose network reach it. |

`purser` waits until `postgres` reports healthy before it starts. Purser keeps
everything in that one database, so the volume is the whole instance, together
with the encryption key, which is never stored in it.

## The .env file

Compose reads a file named `.env` in the same directory as
`docker-compose.yml` and substitutes its values into the compose file. Keep it
readable only by you (`chmod 600 .env`): it holds the encryption key.

```bash
PURSER_ENCRYPTION_KEY=YOUR_KEY
POSTGRES_PASSWORD=YOUR_DATABASE_PASSWORD
PURSER_PUBLIC_URL=https://purser.example.com
PURSER_BIND_ADDRESS=127.0.0.1
```

| Variable | Default | Used for |
| --- | --- | --- |
| `PURSER_ENCRYPTION_KEY` | none | Required. Compose refuses to start without it. See [Encryption at rest](../../explanation/encryption-at-rest/). |
| `POSTGRES_PASSWORD` | `purser` | The database password, given to both containers. See [Set the database password](#set-the-database-password). |
| `PURSER_PUBLIC_URL` | `http://localhost:8000` | The address people and agents use to reach Purser. |
| `PURSER_BIND_ADDRESS` | `127.0.0.1` | The host interface the published port listens on. A Compose setting only; Purser itself never reads it. |

The `.env` file only fills in the compose file. `docker-compose.yml` passes the
`purser` container its database URL, the encryption key and
`PURSER_PUBLIC_URL`, and nothing else, so another line in `.env` (for example
`PURSER_TIMEZONE`) does not reach Purser. Set other variables in an override
file (below), or set the [runtime-editable ones](../../reference/configuration/#runtime-editable)
in the admin UI under **Settings**.

## Keep your changes in an override file

Compose also reads `docker-compose.override.yml` from the same directory and
merges it over `docker-compose.yml`. Put your own changes there rather than in
`docker-compose.yml`, so that an upgrade can replace the release's file without
losing them:

```yaml
# docker-compose.override.yml
services:
  purser:
    environment:
      PURSER_TIMEZONE: America/New_York
```

Run `docker compose config` to see the merged result before you start it.

## Set the database password

The bundled database password defaults to `purser`. The database is reachable
only from the Compose network, but change the default anyway, and do it
**before the first `docker compose up`**: the PostgreSQL image sets the
password only when it initialises an empty volume. Put a long random value of
letters and digits in `.env` (the password is placed in a database URL, where
characters such as `@`, `:`, `/` or `#` would break it):

```bash
echo "POSTGRES_PASSWORD=$(openssl rand -hex 24)" >> .env
```

Once the volume exists, changing `POSTGRES_PASSWORD` in `.env` alone does not
change the database's password. Purser then fails to start, and its log shows
`password authentication failed for user "purser"`. To change it later, change
it inside PostgreSQL first, then in `.env`:

```bash
docker compose exec postgres psql -U purser -d purser
```

At the `purser=#` prompt, run `\password purser`, enter the new password twice,
then `\q`. Put the same password in `.env` as `POSTGRES_PASSWORD` and run
`docker compose up -d`, which recreates both containers with the new value.

## Reach Purser from other machines

By default the published port listens on `127.0.0.1:8000`, so only the Docker
host itself can reach Purser. Finish the first-run setup (the admin account)
before you open it to anything else: until the first admin exists, the setup
page is open to whoever reaches it first.

For access from other machines, put Purser behind an HTTPS reverse proxy and
set `PURSER_PUBLIC_URL` to the HTTPS address; see
[Put Purser behind a reverse proxy](../reverse-proxy/). Only set
`PURSER_BIND_ADDRESS` to another interface (for example a private LAN address)
when something on that network needs to reach port 8000 directly, and never
publish plain HTTP to the internet.

`PURSER_PUBLIC_URL` must be the address people actually use: it decides whether
the session cookie is marked secure, it is the base of the single sign-on
redirect address, and the MCP endpoint refuses requests for any other host
(HTTP 421). After changing it, run `docker compose up -d`.

## Run the worker separately

By default one `purser` container serves requests and also runs the scheduled
jobs. `docker-compose.split.yml` moves the jobs into a second container from
the same image:

- the `purser` container gets `PURSER_DISABLE_SCHEDULER=1` and only serves
  requests;
- a new `worker` container runs `purser worker`, which runs only the scheduled
  jobs. It publishes no port and has no health check; read its state with
  `docker compose logs worker`.

Download `docker-compose.split.yml` from the same release tag as
`docker-compose.yml`, into the same directory, then name both files:

```bash
docker compose -f docker-compose.yml -f docker-compose.split.yml up -d
```

To avoid typing the file list every time, add it to `.env`:

```bash
COMPOSE_FILE=docker-compose.yml:docker-compose.split.yml:docker-compose.override.yml
```

When you name files with `-f` or `COMPOSE_FILE`, Compose no longer reads
`docker-compose.override.yml` by itself, so list it too if you have one.

Run **exactly one worker**. The jobs take no lock against each other, so a
second worker would run every scheduled job again: each bank sync, matching
run and webhook delivery pass would happen twice. Do not scale the `worker` service, and do not
run a second copy of the stack against the same database.

## Data and the named volume

The database lives in the Docker volume `purser-pgdata`, which Compose names
after the project, normally the directory name (for example
`purser_purser-pgdata`; `docker volume ls` shows it).

- `docker compose down` removes the containers and keeps the volume.
- `docker compose down -v` **deletes the volume and every record in it.**

Back up with `pg_dump` through the `postgres` container:

```bash
umask 077
docker compose exec -T postgres pg_dump -U purser --format=custom purser > purser-backup.dump
```

[Backup and restore](../backup-restore/) covers what a dump contains, the
restore drill and key rotation.

## Health checks

Both containers have health checks, and `docker compose ps` shows their state:

- `purser` requests `GET /healthz` inside the container every 30 seconds
  (after a 15 second start period) and is unhealthy after three failures. A
  healthy answer is `{"status":"ok","database":"ok"}`; it confirms the process
  is serving and the database answers, nothing more.
- `postgres` runs `pg_isready` every 10 seconds.

Docker does not restart a container for being unhealthy; `restart:
unless-stopped` restarts it when the process exits. Purser exits at startup
when the key is missing or does not match the database, or when migrations
fail, so a container that keeps restarting means you should read
`docker compose logs purser`.

## Upgrade

Each release publishes its own compose files under its tag. To upgrade:

1. Take a backup, and keep the image tag you run now; there is no downgrade.
   [Upgrade and roll back](../backup-restore/#upgrade-and-roll-back) has the
   full procedure.
2. Download the new release's `docker-compose.yml` (and
   `docker-compose.split.yml`, if you use it) over the old one, keeping `.env`
   and `docker-compose.override.yml`. The download URL has the same form as in
   the [Quickstart](../../tutorials/quickstart/), with the new tag.
3. Pull and restart:

   ```bash
   docker compose pull
   docker compose up -d
   ```

Migrations run automatically when the new container starts; with the split
topology, the API and the worker take turns on a database lock, so they run
once.

## Related

- [Quickstart](../../tutorials/quickstart/): the first install.
- [Put Purser behind a reverse proxy](../reverse-proxy/): HTTPS for remote access.
- [Backup and restore](../backup-restore/): backups, restores, key rotation and upgrades.
- [Configuration](../../reference/configuration/): every `PURSER_*` variable.
- [Encryption at rest](../../explanation/encryption-at-rest/): what the key protects.
