---
title: Configuration
description: Every PURSER_* environment variable, and which settings are runtime-editable in the admin UI.
---

Purser is configured through `PURSER_*` environment variables (a `.env` file
next to the process also works). A subset can be **overridden at runtime** in
the admin UI (**Config -> Settings**) or via the admin-scoped `/config` REST
API; overrides persist in the database and win over the environment.

## Required

| Variable | Purpose |
| --- | --- |
| `PURSER_ENCRYPTION_KEY` | Fernet key for data encrypted at rest (raw bank envelopes, SimpleFIN access URLs, webhook secrets, narrative text). **Every role command fails fast without it.** Accepts comma-separated keys during [rotation](../../reference/cli/#purser-admin-rotate-key) — the first key encrypts. Generate with `purser keygen`. Record it: encrypted data is unrecoverable without it. |

## Environment-only

Infrastructure identity — never runtime-editable.

| Variable | Default | Purpose |
| --- | --- | --- |
| `PURSER_DATABASE_URL` | `postgresql+psycopg://purser:purser@localhost:5432/purser` | PostgreSQL DSN (psycopg v3 driver). |
| `PURSER_PUBLIC_URL` | `http://localhost:8000` | External base URL of the instance. Drives cookie security (`https://` = secure cookies), the OIDC redirect URI, and the MCP endpoint's Host/Origin allowlist (requests for any other host get HTTP 421). The [Helm chart](../../how-to/kubernetes-helm/) derives it from the ingress host. |
| `PURSER_HOST` | `0.0.0.0` | Bind address. |
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
`127.0.0.1`), and list nothing else — any address on that list can claim to be
any client. Without it, the per-address limit counts the whole instance's
failed sign-ins together, so a password spray pauses password sign-in for
everyone until the lockout ends. SSO sign-in and agent tokens are unaffected.

## Runtime-editable

Environment value is the bootstrap default; the admin UI can override each of
these live. Secrets (`ai_api_key`, `oidc_client_secret`, `price_api_key`) are
write-only and encrypted at rest.

| Variable | Default | Purpose |
| --- | --- | --- |
| `PURSER_SESSION_TTL_DAYS` | `7` | Absolute browser-session lifetime. |
| `PURSER_SESSION_IDLE_HOURS` | `8` | Sliding idle timeout for browser sessions. |
| `PURSER_AUDIT_RETENTION_DAYS` | `90` | Audit call/change rows older than this are pruned nightly (floor 7). |
| `PURSER_TRANSFER_AUTO_LINK` | `true` | Deterministic transfer auto-linking at import. Off = every candidate is confirm-first. |
| `PURSER_PRIVACY_PURCHASE` | `false` | Agents receive payees as stable references (`py_...`), with descriptions, memos and bill names left out, unless a call asks for them with `include`. See [Privacy and agents](../../explanation/privacy-and-agents/). |
| `PURSER_PRIVACY_AMOUNT` | `false` | Agents receive no single-transaction or bill amounts unless a call asks for them. Totals and balances stay exact. |
| `PURSER_AI_PROVIDER` | `none` | AI categorization provider: `none`, `openai` (any OpenAI-compatible endpoint), or `anthropic`. |
| `PURSER_AI_BASE_URL` | `https://api.openai.com/v1` | OpenAI-compatible endpoint (Ollama, LM Studio, vLLM, OpenRouter...). |
| `PURSER_AI_API_KEY` | unset | Provider API key (write-only secret). |
| `PURSER_AI_MODEL` | unset | Model name. |
| `PURSER_AI_REVIEW` | `false` | Run the review pass server-side instead of waiting for a connected agent. Requires a configured provider on top of this — turning a provider on grants nothing by itself. |
| `PURSER_REVIEW_MAX_AGE_DAYS` | `90` | Unreviewed transactions older than this are auto-marked `aged` by a nightly sweep, so the set cannot grow without bound. The count is reported rather than cleared silently. |
| `PURSER_OIDC_ISSUER` | unset | OpenID Provider issuer URL (e.g. an Authentik instance). SSO enables when issuer, client id, and secret are all set; password login always remains as fallback. |
| `PURSER_OIDC_CLIENT_ID` | unset | OIDC client id. |
| `PURSER_OIDC_CLIENT_SECRET` | unset | OIDC client secret (write-only secret). |
| `PURSER_OIDC_AUTO_PROVISION` | `false` | Auto-create a member account for an SSO identity whose email matches no account, ignoring letter case (requires the provider to assert `email_verified`). Off by default: such sign-ins get "account not provisioned" — pre-create users in the admin UI instead. |
| `PURSER_GRAFANA_URL` | unset | Grafana base URL for deep links on tool responses; links omitted when unset. |
| `PURSER_PRICE_PROVIDER` | `none` | Daily closes for hand-tracked positions: `none` (record closes by hand) or `tiingo`. The provider sees ticker symbols only. See [Track holdings by hand](../../how-to/manual-holdings/). |
| `PURSER_PRICE_STALE_DAYS` | `7` | A hand-tracked position whose newest close is older than this many days is flagged `stale` (and `price.stale` fires once). 1 to 365. |

The price provider's **API key** has no environment variable on purpose: Purser
is the party that sends it, so it lives only in Purser's encrypted settings
(**Config -> Prices**, or `PUT /config` with `price_api_key`). It is write-only:
responses report only `price_api_key_set`.

The SimpleFIN **sync schedule** (daily / every 12h / every 6h / hourly) is
runtime-only — set it in **Config -> SimpleFIN**.

## Compose extras

The bundled `docker-compose.yml` also reads `POSTGRES_PASSWORD` (defaults to
`purser`) for the sidecar database.

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

## Docker host binding

The Compose file uses `PURSER_BIND_ADDRESS` (default `127.0.0.1`) for its
published host port. This is a Compose setting, not an application setting.
Complete first-admin setup before exposing a remote interface. Compose also
passes `PURSER_PUBLIC_URL` through to the application; use your HTTPS URL behind
a reverse proxy.
