---
title: Configuration
description: Every PURSER_* environment variable, and which settings are runtime-editable in the admin UI.
---

Purser is configured through `PURSER_*` environment variables (a `.env` file
next to the process also works). A subset can be **overridden at runtime** in
the admin UI (under **Settings**: **General**, **Security**, **Connections**,
**Market data** and **Categories**) or via the admin-scoped
`/config` REST API; overrides persist in the database and win over the environment.
**Settings -> Categories** has three tabs: **Categories** (with the review
setting), **Rules**, and, for an admin, **AI categorization**, whose **Test
connection** (`POST /config/ai/test`) checks a provider setup before you save it.

## Required

| Variable | Purpose |
| --- | --- |
| `PURSER_ENCRYPTION_KEY` | Fernet key for data encrypted at rest (raw bank envelopes, SimpleFIN access URLs, webhook secrets, narrative text). **Every role command fails fast without it.** Accepts comma-separated keys during [rotation](../../reference/cli/#purser-admin-rotate-key) — the first key encrypts. Generate with `purser keygen`. Record it: encrypted data is unrecoverable without it. |

## Environment-only

Infrastructure identity and a few server behaviours — never runtime-editable.

| Variable | Default | Purpose |
| --- | --- | --- |
| `PURSER_DATABASE_URL` | `postgresql+psycopg://purser:purser@localhost:5432/purser` | PostgreSQL DSN (psycopg v3 driver). |
| `PURSER_PUBLIC_URL` | `http://localhost:8000` | External base URL of the instance. Drives cookie security (`https://` = secure cookies), the OIDC redirect URI, and the MCP endpoint's Host/Origin allowlist (with `PURSER_ALLOWED_HOSTS`: requests for any other host get HTTP 421, and the answer names the address to use and how to allow another name). The [Helm chart](../../how-to/kubernetes-helm/) derives it from the ingress host. Serve Purser at the root of its host (`https://purser.example.com`): a path prefix (`https://example.com/purser`) is not supported, because the web UI, its icons, the API and `/mcp/` all use root-absolute paths. |
| `PURSER_ALLOWED_HOSTS` | unset | The machine's other names, comma-separated: exact host names or IP addresses, for example `192.168.1.20,homeserver,homeserver.local`. The MCP endpoint and the web UI's cross-site check accept them as well as the host in `PURSER_PUBLIC_URL` and `localhost`; links, connection steps and the single sign-on address keep using `PURSER_PUBLIC_URL`. The MCP endpoint accepts a name on any port; add `:port` to a name (`homeserver:8010`) only when browsers reach it on another port than `PURSER_PUBLIC_URL`'s. Wildcards are refused, because the endpoint's check exists to stop DNS rebinding, and a malformed entry stops Purser at startup with an `error:` line naming it. See [Another machine on your home network](../../how-to/docker-compose/#another-machine-on-your-home-network). |
| `PURSER_BIND_ADDRESS` | unset (`127.0.0.1` in the Compose file) | The address the deployment publishes Purser's port on. In the Compose file it is the Docker host interface the port listens on (`127.0.0.1`: this machine only; `0.0.0.0`: the home network), and Compose passes the same value to Purser, because the app cannot see the host's port mapping from inside its container. Purser only reports it: **Settings**, **Agents & API** (How Purser is reached), `GET /config/network` and `reach` in MCP `describe`. Unset, the report says the mode is not reported instead of guessing; an `https://` `PURSER_PUBLIC_URL` is reported as a reverse proxy. See [Docker host binding](#docker-host-binding). |
| `PURSER_HOST` | `0.0.0.0` | Bind address inside the container (or on the machine, without Docker). |
| `PURSER_PORT` | `8000` | Bind port. |
| `PURSER_LOG_LEVEL` | `INFO` | Log level. |
| `PURSER_MAX_REQUEST_BYTES` | `33554432` (32 MiB) | Largest request body accepted. A bigger one is refused with 413 before it is read, signed in or not. `0` turns the limit off; a reverse proxy's own limit applies as well. |
| `PURSER_DISABLE_SCHEDULER` | `false` | Set on the API process when a dedicated `purser worker` runs the jobs. The Helm chart sets it on the API pods when `worker.enabled=true`. |
| `PURSER_WIDGETS_DIR` | `/app/widgets` | Directory of built MCP Apps widget bundles. |
| `PURSER_STATIC_DIR` | `/app/static` | Directory of the built web UI (SPA) bundle. Dev falls back to `frontend/dist` when this has no `index.html`. |
| `PURSER_DOCS_URL` | unset | Base URL of the deployed docs site. `search_docs` returns absolute links when set, site-relative paths when not. |
| `PURSER_SOURCE_URL` | `https://github.com/mlamoure/purser` | Where this instance's source code is published. `GET /source` redirects here, and the web UI footer and `/llms.txt` point to it. If you run a modified Purser, set it to your own published source (see [Licence and notices](#licence-and-notices)). |

## Sign-in throttling

Environment-only. Password sign-in (`POST /auth/login`) counts failures per
email and per client address. At a limit, further attempts get
`429 Too Many Requests` with a `Retry-After` header (seconds) and the password
is not checked — see [Security model](../../explanation/security/#sign-in-throttling).
Counts live in memory, per process. `0` turns a limit off.

| Variable | Default | Purpose |
| --- | --- | --- |
| `PURSER_LOGIN_MAX_FAILURES` | `5` | Consecutive failed sign-ins for one email (case-insensitive) before its attempts are refused. A successful sign-in resets it. |
| `PURSER_LOGIN_IP_MAX_FAILURES` | `20` | Failed sign-ins from one client address, across all emails, before its attempts are refused. A success does not reset it. IPv6 addresses count per /64. |
| `PURSER_LOGIN_BACKOFF_SECONDS` | `30` | First lockout. It doubles with each further failure. |
| `PURSER_LOGIN_BACKOFF_MAX_SECONDS` | `900` | Longest lockout. |
| `PURSER_LOGIN_FAILURE_RESET_SECONDS` | `3600` | A count is forgotten after this long with no failure and no lockout in force. |

The client address is the connection's peer as uvicorn reports it. Behind a
reverse proxy (Traefik, nginx, an ingress) every request comes from the proxy
unless uvicorn is told to believe the proxy's `X-Forwarded-For`: set uvicorn's
own `FORWARDED_ALLOW_IPS` variable to the proxy's address or network (default
`127.0.0.1,::1`), and list nothing else — any address on that list can claim to be
any client. Without it, the per-address limit counts the whole instance's
failed sign-ins together, so a password spray pauses password sign-in for
everyone until the lockout ends. SSO sign-in and agent tokens are unaffected. With
Docker Compose, see
[Put Purser behind a reverse proxy](../../how-to/reverse-proxy/#forwarded-headers-and-forwarded_allow_ips).

## Runtime-editable

Environment value is the bootstrap default; the admin UI or `PUT /config` can
override each of these live. Secrets (`ai_api_key`, `oidc_client_secret`, `price_api_key`) are
write-only and encrypted at rest.

| Variable | Default | Purpose |
| --- | --- | --- |
| `PURSER_SESSION_TTL_DAYS` | `7` | Absolute browser-session lifetime. |
| `PURSER_SESSION_IDLE_HOURS` | `8` | Sliding idle timeout for browser sessions. |
| `PURSER_AUDIT_RETENTION_DAYS` | `90` | Audit call/change rows older than this are pruned nightly (floor 7). Runtime-editable through `PUT /config` (`audit_retention_days`) only: the audit log shows the retention, and no admin-UI control changes it. |
| `PURSER_TRANSFER_AUTO_LINK` | `true` | Deterministic transfer auto-linking at import. Off = every candidate is confirm-first. |
| `PURSER_AI_PROVIDER` | `none` | AI categorization provider: `none`; `local`, a model you run behind an OpenAI-compatible API (Ollama, LM Studio, vLLM, a llama.cpp server, a LiteLLM gateway); `openai`, a hosted OpenAI-compatible service; or `anthropic`. `local` and `openai` send the same requests; `local` needs its own `PURSER_AI_BASE_URL` and never uses the hosted default. See [Use a local model](../../how-to/local-model/) and [what Purser sends](../../explanation/privacy-and-agents/#what-purser-itself-sends-to-a-model-provider). Runtime-editable in **Settings -> Categories -> AI categorization** or `PUT /config` (`ai_provider`). |
| `PURSER_AI_BASE_URL` | `https://api.openai.com/v1` | The OpenAI-compatible address for `local` or `openai`, usually ending in `/v1` (`http://host.docker.internal:11434/v1` for Ollama on the Docker host). The default serves `openai` only: saving `local` without an address of its own is refused with 422. Ignored for `anthropic`. |
| `PURSER_AI_API_KEY` | unset | Provider API key (write-only secret), optional for `local`. **Clear key** on the settings card (`PUT /config` with `"ai_api_key": null`) stores "no key", which also overrides this variable; `DELETE /config/ai_api_key` returns to it. |
| `PURSER_AI_MODEL` | unset | Model name. The settings card's **Find models** lists the server's models (`POST /config/ai/models`). |
| `PURSER_AI_TIMEOUT_SECONDS` | `30` | Seconds to wait for one provider answer. A model on modest hardware can need 120 or more. 5 to 600 through the settings card or `PUT /config` (`ai_timeout_seconds`). |
| `PURSER_AI_MIN_CONFIDENCE` | `0.7` | The AI categorizer's confidence floor, 0 to 1: a category guess below it is not applied, so the row stays uncategorized (and a bank-sync row then [needs review](../../how-to/review-new-transactions/)). A guess that names one of your accounts (`[account]`, a transfer) needs at least 0.85 whatever this says. Runtime-editable in **Settings -> Categories -> AI categorization** (**Minimum confidence**, as a percentage) or `PUT /config` (`ai_min_confidence`). |
| `PURSER_REVIEW_UNCATEGORIZED` | `true` | Flag bank-sync transactions with no category as [needing review](../../how-to/review-new-transactions/). Reconciliation adjustments are flagged whatever this says. Runtime-editable on the **Categories** tab of **Settings -> Categories** or `PUT /config` (`review_uncategorized`). |
| `PURSER_OIDC_ISSUER` | unset | OpenID Provider issuer URL (e.g. an Authentik instance). SSO enables when issuer, client id, and secret are all set; password login always remains as fallback. |
| `PURSER_OIDC_CLIENT_ID` | unset | OIDC client id. |
| `PURSER_OIDC_CLIENT_SECRET` | unset | OIDC client secret (write-only secret). |
| `PURSER_OIDC_AUTO_PROVISION` | `false` | Auto-create a member account for an SSO identity whose email matches no account, ignoring letter case (requires the provider to assert `email_verified`). Off by default: such sign-ins get "account not provisioned" — pre-create users in the admin UI (**Settings -> Users**) instead. |
| `PURSER_GRAFANA_URL` | unset | Grafana base URL for deep links on tool responses; links omitted when unset. |
| `PURSER_PRICE_PROVIDER` | `none` | Daily closes for holdings entered in Purser (and SimpleFIN holdings reported with no value): `none` (no provider; enter closes by hand), `tiingo` (US stocks, ETFs and mutual funds) or `eodhd` (exchanges worldwide and US mutual funds). The provider sees ticker symbols only. See [Enter holdings in Purser](../../how-to/manual-holdings/). |
| `PURSER_PRICE_FALLBACK_PROVIDER` | `none` | A second provider asked for what the provider does not carry or fails to answer: `none`, `tiingo` or `eodhd`, never the same as `PURSER_PRICE_PROVIDER`. Runtime-editable through **Settings -> Market data** or `PUT /config` (`price_fallback_provider`). |
| `PURSER_PRICE_STALE_DAYS` | `7` | A position entered in Purser whose newest close is older than this many days is flagged `stale` (and `price.stale` fires once). 1 to 365. |
| `PURSER_FEED_STALE_DAYS` | `4` | A mapped SimpleFIN account whose bank balance date is more than this many days old is stale: `feed.stale` fires once per frozen date and `whats_new` lists it until the bank updates. 1 to 90. Runtime-editable through `PUT /config` (`feed_stale_days`). See [Connect a bank with SimpleFIN](../../how-to/simplefin/). |
| `PURSER_TIMEZONE` | `UTC` | The household time zone, an IANA name such as `America/New_York`. Its calendar date is "today" everywhere Purser reasons about days: due and overdue bills, matching's late bills, snoozes, the digest's week ahead, net worth and balance defaults, reconciliation's "no future dates", price and feed staleness, and the Grafana forecast view. Timestamps (audit log, fetches, sessions) are stored in UTC and shown in the household time zone by the admin app; the scheduled job times stay UTC (06:45 matching and the rest), except the daily SimpleFIN sync, whose time (**Settings -> Connections**) is a clock time in the household time zone and keeps it across daylight-saving changes; changing the zone keeps that clock time in the new zone. The default keeps an existing install on the day it already had. Runtime-editable through **Settings -> General** or `PUT /config` (`timezone`); an unknown zone is refused with 422, and an unknown `PURSER_TIMEZONE` stops startup. Any token with read scope gets the current ledger day and zone from `GET /clock`, for a client that dates its own writes. |

The price providers' **API keys** have no environment variables on purpose:
Purser is the party that sends them, so they live only in Purser's encrypted
settings (**Settings -> Market data**, or `PUT /config` with `price_api_key`
and `price_fallback_api_key`). They are write-only: responses report only
`price_api_key_set` and `price_fallback_api_key_set`.

The SimpleFIN **sync schedule** (daily / every 12h / every 6h / hourly) is
runtime-only — set it in **Settings -> Connections**.

## Compose extras

The bundled `docker-compose.yml` also reads settings of its own from `.env`.
Purser itself never sees the first two; `PURSER_BIND_ADDRESS` it only reports:

| Variable | Default | Purpose |
| --- | --- | --- |
| `POSTGRES_PASSWORD` | `purser` | The sidecar database's password. Set it before the first start; see [Run with Docker Compose](../../how-to/docker-compose/#set-the-database-password). |
| `PURSER_HOST_PORT` | `8000` | The port published on the Docker host. Change it when another program uses 8000; the default `PURSER_PUBLIC_URL` follows it. Not the same as `PURSER_PORT`, the port inside the container, which stays 8000. |
| `PURSER_BIND_ADDRESS` | `127.0.0.1` | The host interface the port listens on, passed to Purser as well so it can report how it is reached. See [Docker host binding](#docker-host-binding). |

Of Purser's own variables, the compose file passes `PURSER_ENCRYPTION_KEY`,
`PURSER_PUBLIC_URL`, `PURSER_ALLOWED_HOSTS`, `PURSER_BIND_ADDRESS` and
`PURSER_TIMEZONE` through from `.env`; set any other in
an [override file](../../how-to/docker-compose/#keep-your-changes-in-an-override-file).

## Licence and notices

Purser is licensed under the GNU Affero General Public License, version 3 only
(AGPL-3.0-only). The AGPL asks a network service to offer its source code to
the people who use it, so every Purser screen, signed in or not, has a
**Source code** link in its footer. The link opens `GET /source`, a public
route that redirects to `PURSER_SOURCE_URL`; `/llms.txt` names the same URL
for agents, and the OpenAPI document carries the licence. Running Purser
unmodified, the default is correct. If you change the code and let others use
your instance, publish your changed source and point `PURSER_SOURCE_URL` at it.

Third-party licences ship with every build:

- `THIRD_PARTY_NOTICES.md` at the repository root lists every third-party
  component the image, the web UI and the docs site ship (name, version,
  licence, source), with their licence and notice texts. The container image
  carries it and `LICENSE` in `/usr/share/doc/purser/`.
- The web UI serves the licences of the code bundled into it at
  `/third-party-licenses.txt` (the footer's **Third-party licences** link).
- Each MCP Apps widget carries its bundled packages' licences in a comment at
  the end of its HTML.

## Removed variables

Purser ignores these and logs a warning at startup while one is still set.

| Variable | Replaced by |
| --- | --- |
| `PURSER_PRIVACY_PURCHASE` | Agents ask for privacy per call with `withhold`; see [Privacy and agents](../../explanation/privacy-and-agents/). |
| `PURSER_PRIVACY_AMOUNT` | Agents ask for privacy per call with `withhold`; see [Privacy and agents](../../explanation/privacy-and-agents/). |
| `PURSER_AI_REVIEW` | Nothing: Purser no longer clears rows with a model after a sync. A transaction needs review only for a reason; see [Transactions that need review](../../how-to/review-new-transactions/). |
| `PURSER_REVIEW_MAX_AGE_DAYS` | Nothing: rows no longer age out of review. The flag clears when the row is fixed; see [Transactions that need review](../../how-to/review-new-transactions/). |

## Docker host binding

The Compose file publishes Purser on `PURSER_BIND_ADDRESS` (default
`127.0.0.1`) and `PURSER_HOST_PORT` (default `8000`) of the Docker host. These
are Compose settings; Compose also hands `PURSER_BIND_ADDRESS` to Purser, which
shows the result under **Settings**, **Agents & API**: this machine only, the
home network at `PURSER_PUBLIC_URL`, a reverse proxy, or not reported. To use Purser from another
machine on your home network, set `PURSER_BIND_ADDRESS=0.0.0.0` and
`PURSER_PUBLIC_URL` to the host's address (for example `http://192.168.1.20:8000`) together,
and list the machine's other names in `PURSER_ALLOWED_HOSTS`; see
[Another machine on your home network](../../how-to/docker-compose/#another-machine-on-your-home-network).
Beyond the home network, keep the default binding and put your own HTTPS
reverse proxy in front (see [Put Purser behind a reverse proxy](../../how-to/reverse-proxy/)).
