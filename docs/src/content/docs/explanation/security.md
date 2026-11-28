---
title: Security model
description: Access scopes, audit logs, correction history, sign-in throttling and encryption at rest.
---

Purser controls access through token scopes and records reads and writes in
an audit log. Posted transactions retain correction history. This page covers
those controls, sign-in protection and encryption; the authoritative spec is
design section 9.

## Scopes

Every token carries one or more scopes:

| Scope | Grants |
| --- | --- |
| `read` | Financial reads: transactions, ledger and bank balances, reports, tools and resources. |
| `write` | Every write: transactions, matches, transfers, rules, schedules, suggestions, valuations, holdings. |
| `admin` | Token and configuration management. |

The recommended default for an agent is **`read + write`**.

Account read access includes the bank-reported balance, available balance,
freshness and comparison basis, as well as transactions and the ledger balance.
Use `GET /accounts/{id}`, `GET /accounts/{id}/balance` or MCP `account_balance`;
each includes `bank_snapshot`. Permissions currently apply across the instance;
there are no per-account or per-tenant access lists.

Connection discovery is a configuration surface and remains admin-only, as do
credentials and setup. Mapping a delivered bank account needs write scope
(MCP `map_feed_accounts`, `POST /connections/map-accounts`); replacing the
feed of an account that already has one needs admin. Reading an account's
bank balances does not require that access. The bank snapshot never includes
credentials, bank account numbers or provider identifiers, even for an
administrator.

## Undo, not pre-approval

The safety net for an agent's writes is undo, not a review queue in front of
every change. Posted transactions are immutable: a correction is a linked
reversal pair, so nothing an agent does erases history. Every write is
recorded in the audit log against the token that made it, and the register
marks agent-made rows as agent-made.

Proposing is the agent's choice per call. When it carries out your request, it
writes directly. When it acts on its own initiative — a pattern it noticed, a
rule it thinks would help — it proposes instead: a rule or a financial note stays a
draft that never runs or is served until you activate it, and a schedule lands
as a [suggestion](../../how-to/suggestions/) for you to decide. A note stays a
draft unless you asked the agent to remember something, because an active
note can be served as context in future sessions.

Every endpoint and tool declares the scope it requires; no route is open by
default. The few that answer without a token are deliberate: sign-in
(`POST /auth/login`, `GET /auth/methods`, and the single sign-on login and
callback), first-run setup (until the first administrator exists) and its
status probe `GET /auth/setup/status`, `/healthz`, `/llms.txt`, `/source`, and
the API description (`/openapi.json`, `/docs` and `/redoc`).

## What an agent sees

You choose the agent and its access scopes. Data an agent reads into its
conversation may reach its model provider. On any MCP tool call or REST read,
the agent can ask Purser to leave out payee names, descriptions or individual
amounts the question does not need (`withhold`); nothing is left out unless it
asks. This is a privacy aid, not an access control: scopes decide what a token
can read. External account identifiers are omitted by default from MCP
responses. The audit log records, for every call, whether privacy was used
and whether the answer returned identifiable data. See
[Privacy and agents](../../explanation/privacy-and-agents/) for how it works.

## No self-escalation for non-admin tokens

`POST /tokens` requires `admin` scope. A non-admin agent token cannot mint a
copy of itself or grant itself stronger scopes. An **admin-scoped API token can
create tokens**, just as an admin's browser session can. Reserve that scope for
trusted administration; it does not distinguish a human caller from automation.

## Who can revoke a token

Any token can revoke itself. A person signed in to the web UI sees and revokes
every token of their own account under **Settings -> Agents & API**: their other sessions,
and, for the admin, every agent token, since agent tokens are created under the
admin's account. An `admin`-scoped token can revoke any token. Any other agent
token sees only itself in `GET /tokens` and can revoke only itself. A request
to revoke any other token gets the same `404` as a token that does not exist,
so a leaked read-only token can neither sign the admin out, nor cut off the
other agents, nor confirm that another token exists.

## Reads are audited too

The audit log (**Settings -> Audit log**) opens on **Data shared**: what Purser returned to an authorized
integration, the accounts and dates identified in the response, fields withheld,
and the outcome. **Changes made**, **Access & permissions**, and **Technical
activity** keep mutations, grants and browser/system activity available separately.
Expand an entry for its authorization, evidence ID and linked activity.
Response summaries show recorded categories and counts; empty category, account
and withheld-field summaries are omitted. Recorded field paths remain available
under **Technical details**, even when no category was assigned.

Filters run on the server across the retained history, not just loaded entries.
Use **Load older entries** to continue, or **Export CSV/JSON** for every matching
entry. Filter URLs can be bookmarked. Dates show the browser's local timezone;
export timestamps include their UTC offset. Account, data-type, privacy and
external-identifier filters require recorded evidence, so they cannot match older
entries that lack it.

New records contain a field inventory and counts, not a copy of financial values.
An inventory can be partial when a response includes free text or exceeds inventory
limits. Requested coverage is separate from dates actually found in the response.
Historical entries explicitly state that detailed sharing information was not
recorded. They do not acquire guessed authorization or response data on upgrade.

An integration's response is recorded before Purser releases it. If capture fails,
Purser withholds the response: REST returns 503 and MCP returns an error. **A write
may already have completed; check its state before retrying.** REST integration
responses above 16 MiB are withheld with 413; narrow the request. Browser activity
remains best-effort so an administrator can investigate an audit outage.

A separate completion record identifies an HTTP send or an MCP result returned to
the transport. Missing completion evidence appears as **Delivery uncertain**.
Purser cannot prove that a client received the data or where it forwarded it.
Capture-failure notices describe the current server process since its start, not
all servers or historical uptime.

Each mutation retains its before/after entry, linked to the call by request ID.
Audit access and exports require admin scope. Sensitive audit details and grant
snapshots are encrypted. Retention is finite and uniform (`audit_retention_days`,
default 90, floor 7), pruned nightly, including completion records. The page shows
the configured duration and earliest retained entry. The durable
[event stream](../../reference/events-webhooks/) is separate.

## Sign-in throttling

Password sign-in is throttled against online guessing. Failures are counted per
email and per client address: after five in a row for one email, or twenty from
one address, further attempts are refused with `429 Too Many Requests` and a
`Retry-After` header — before the password is even checked, so a locked-out
guesser learns nothing. The lockout starts at 30 seconds and doubles with each
further failure, up to 15 minutes. A successful sign-in clears its email's
count; the address count only fades with time, so one valid password cannot
cover a spray across other accounts. Every failed and every throttled attempt
is audited (`auth.login_failed`, `auth.login_throttled`) with the email as
entered and the client address — never the password.

A refused sign-in never says whether an account exists. An unknown email gets
the same `401` as a wrong password, and it takes as long: when there is no
password to check, Purser checks the guess against a stand-in hash, so the
response time gives nothing away either.

The counts live in memory, per process: a restart clears them, and each API
replica keeps its own. SSO sign-in and agent tokens never pass through it. The
limits, and the proxy setting that lets the per-address count see real clients,
are in [Configuration](../../reference/configuration/#sign-in-throttling).

## Single sign-on

SSO uses the OpenID Connect authorization code flow with state, nonce and
PKCE, and validates the id_token before any account lookup: an `RS256` or
`ES256` signature from the provider's published keys, then issuer, audience,
expiry and nonce. An SSO identity is the provider together with its subject.
A provider's email links to an existing account, or creates one, only when the
provider asserts it is verified, and an auto-created account is a member, never
an admin. An account linked to one identity refuses another identity from the
same provider until an admin unlinks it; a verified email moves it to a
different provider. Emails match regardless of letter case. Only an admin can change the SSO settings, and
password sign-in stays available whatever they say. The full list of checks is
in [Single sign-on with OIDC](../../how-to/oidc/#what-purser-checks).

## Requests from other sites

The web UI signs you in with a session cookie, and a browser sends that cookie
with any request to Purser, even one a page elsewhere triggers. So a write
made with the cookie is refused (`403`) when the browser reports that another
page sent it (`Sec-Fetch-Site`, or `Origin`). Reads, agent tokens and scripts
are not affected. Request bodies are capped at 32 MiB
(`PURSER_MAX_REQUEST_BYTES`), so no request, signed in or not, can make the
server hold more.

## Encryption at rest

Encryption is required: Purser refuses to start without
`PURSER_ENCRYPTION_KEY`, and a canary row makes a wrong key fail closed at
startup. Descriptive text (payees, descriptions, memos, notes, audit detail),
the archived bank data, bank credentials and secrets are encrypted; amounts,
dates, statuses, category paths and ids stay plaintext so the database can
compute on them. Keyed hashes keep search and deduplication working without
letting a database dump confirm a guessed transaction. See
[Encryption at rest](../encryption-at-rest/) for the full model, what a dump
reveals, and key rotation.

## The raw archive underneath

Beneath the ledger, every bank payload is archived to `raw_fetches`
**append-only, before** anything touches the books — no code path may update or
delete it. The ledger is a projection that can always be re-derived from those
bytes, which is the last line of the trust story: even the ledger cannot quietly
diverge from what the bank actually sent. See [The ledger](../../explanation/ledger/).

The authoritative spec is
[`DESIGN.md`](https://github.com/mlamoure/purser/blob/main/DESIGN.md)
section 9.

## First administrator

Complete setup before exposing a new instance to other users. The setup endpoint
is intentionally unauthenticated until the first administrator exists. Concurrent
setup attempts serialize through a PostgreSQL transaction lock, so exactly one
can create the first administrator even when multiple API processes run. Later
attempts receive `409 Conflict`; a failed transaction releases the lock.

## Reporting bugs and vulnerabilities

- **Bugs and feature requests:** open an issue at
  [github.com/mlamoure/purser/issues](https://github.com/mlamoure/purser/issues).
  Use synthetic data in examples; never paste real account numbers, tokens,
  access URLs or encryption keys.
- **Pull requests** are welcome. Purser is developed in a separate repository
  and published to GitHub as one commit per release, so a pull request is not
  merged as is: the maintainer applies the change upstream and it ships in the
  next release.
- **Security vulnerabilities:** report privately with **Report a
  vulnerability** on the repository's
  [Security tab](https://github.com/mlamoure/purser/security), never in a
  public issue. Fixes land in the latest release, the only supported version.
