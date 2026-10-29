---
title: Security model
description: Trust as a feature list — scoped tokens, attributable and reversible agent writes, full read and write auditing, sign-in throttling, and encryption at rest.
---

Purser's pitch is that you can hand your agent your finances and know
**precisely** what it did and what it saw. The agent is your own: it acts with
your authority, and Purser makes every write attributable and reversible. That
is not a promise, it is a list of mechanisms. This page is the map; the authoritative spec is design
section 9.

## Scopes

Every token carries one or more scopes:

| Scope | Grants |
| --- | --- |
| `read` | Every read tool and resource. |
| `write` | Every write: transactions, matches, transfers, rules, schedules, suggestions, valuations, holdings. |
| `admin` | Token and configuration management. |

The recommended default for an agent is **`read + write`**.

## Undo, not pre-approval

The safety net for an agent's writes is undo, not a review queue in front of
every change. Posted transactions are immutable: a correction is a linked
reversal pair, so nothing an agent does erases history. Every write is
recorded in the audit log against the token that made it, and the register
marks agent-made rows as agent-made.

Proposing is the agent's choice per call. When it carries out your request, it
writes directly. When it acts on its own initiative — a pattern it noticed, a
rule it thinks would help — it proposes instead: a rule or a memory stays a
draft that never runs or is served until you activate it, and a schedule lands
as a [suggestion](../../how-to/suggestions/) for you to decide. A memory stays a
draft unless you asked the agent to remember something, because an active
memory is context for every future session.

Every endpoint and tool declares the scope it requires; no route is open by
default. The few that answer without a token are deliberate: first-run setup
(until the first administrator exists), `/healthz` and `/llms.txt`.

## What an agent sees

Everything an agent reads lands in its model provider's context, so Purser
lets you keep details out unless a task needs them. Two settings (Config,
Settings, off by default) withhold payees and single amounts from agents; an
agent asks for a field on a call when your question needs it, and the audit
log lists every call that did. External account identifiers are left out for
agents unless a call asks for them. See [Privacy and agents](../../explanation/privacy-and-agents/).

## No self-escalation for non-admin tokens

`POST /tokens` requires `admin` scope. A non-admin agent token cannot mint a
copy of itself or grant itself stronger scopes. An **admin-scoped API token can
create tokens**, just as an admin's browser session can. Reserve that scope for
trusted administration; it does not distinguish a human caller from automation.

## Who can revoke a token

Any token can revoke itself. A person signed in to the web UI sees and revokes
every token of their own account on the **Tokens** page: their other sessions,
and, for the admin, every agent token, since agent tokens are created under the
admin's account. An `admin`-scoped token can revoke any token. Any other agent
token sees only itself in `GET /tokens` and can revoke only itself. A request
to revoke any other token gets the same `404` as a token that does not exist,
so a leaked read-only token can neither sign the admin out, nor cut off the
other agents, nor confirm that another token exists.

## Reads are audited too

Every mutation writes an `audit_log` entry with the actor token, action, entity,
and before/after — and so does every **read**. Each authenticated request writes
one call row (REST via middleware, MCP via a tool wrapper that never logs
credential material), and a shared `request_id` groups a call with the changes it
produced. The admin UI folds these into **virtual sessions** — per actor, split
after 30 minutes of silence — so a long-lived agent token reads as
conversation-shaped bursts. Audit retention is finite and uniform
(`audit_retention_days`, default 90, floor 7), pruned nightly; the durable
[event stream](../../reference/events-webhooks/) is separate and is the permanent
record for transfer automation.

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

Encryption is required, not optional — Purser refuses to start without
`PURSER_ENCRYPTION_KEY`, and a canary row binds the key to the database so a
wrong key fails closed at startup. The model splits data into two layers. The
**narrative** layer — the identifying words: payee names, descriptions, memos,
schedule names, audit detail, event payloads — is encrypted. The **numeric
skeleton** — amounts, dates, statuses, category paths, ids — stays plaintext,
because every aggregate and invariant computes on it. Equality and dedup survive
encryption through blind indexes and a keyed content hash, so a database dump
cannot confirm a guessed transaction. The two hash keys are wrapped keys, which
is what lets [key rotation](../../how-to/backup-restore/) re-wrap ciphertext while
leaving every index byte-identical.

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
