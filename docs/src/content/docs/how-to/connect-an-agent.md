---
title: Connect an agent
description: Wire an MCP client to Purser with a scoped token and the right operating conventions.
---

Purser's primary interface is its MCP server. Any MCP client that speaks
streamable HTTP — Claude, an OpenClaw agent, a custom runtime — connects the
same way.

## Endpoint and auth

- Endpoint: `https://<purser-host>/mcp` (streamable HTTP, same process as REST)
- Auth: `Authorization: Bearer <api-token>` — the same scoped tokens REST uses

Create the token in the admin UI (**Tokens** page) — the setup wizard also
offers to mint one as its final step, with ready-to-paste config for the
clients below. Recommended agent scopes: **`read + write`**.

| Scope | Grants |
| --- | --- |
| `read` | Every read tool and resource |
| `write` | Every write, with your authority (audited and reversible) |
| `admin` | Token and configuration management (REST only) |

Your agent acts for you: with `write` it writes directly when carrying out
your request, and proposes instead when acting on its own initiative (a draft
rule or memory, or a schedule suggestion). Every write is attributed to its
token in the audit log and reversible. Token minting requires `admin` — an
agent can never create tokens, so it can never grant itself more access or
clone its own. An admin-scoped token can mint tokens: reserve it for trusted
administration.

## First call: describe

In every fresh session, call `describe` before anything else. One round-trip
returns the data model, amount conventions, your token's scopes, the category
tree, and the account list. The same guide is served unauthenticated at
`GET /llms.txt` on the instance.

## Operating conventions

- Aggregate questions (spending, net worth, forecasts, unmatched) each have a
  dedicated tool — never page transactions and sum them in-context.
- Amounts are decimal strings; negative means money out of the account.
  `spending_summary` totals are positive-for-spend.
- Accounts and categories accept a UUID or an ltree path like
  `expenses.dining.coffee`: dot-separated labels of letters, digits, `_`
  and `-`. A malformed path comes back as an error naming the bad segment.
- Bill emails: parse them agent-side and call `create_schedule` or
  `create_transaction` — Purser never parses email server-side.
- `trigger_sync` when the user asks for fresh data, not on every question —
  SimpleFIN Bridge allows 24 fetches per day.
- Full series (`forecast_balance`'s `daily_uri`, `balance_history`'s and
  `net_worth`'s `series_uri`) are `purser://` resources. A gateway or client
  that forwards tools but not resources reads them with the `read_series(uri)`
  tool instead — same body, same auth, `read` scope.

## Connect from your client

Every client below wants the same two facts: the endpoint URL and the token.
Substitute your host for `purser.example.com` throughout.

### Claude (claude.ai and Claude Desktop)

Open **Settings, then Connectors, then Add custom connector** and paste the
server URL. Put the token under **Request headers** — header name
`Authorization`, value `Bearer <api-token>` (the whole value, including the
word Bearer).

The Request headers section is a beta feature still rolling out. If your
account does not show it, the dialog only offers OAuth, which Purser does not
implement yet — use Claude Code below, or any other client on this page.

### Claude Code

```bash
claude mcp add --transport http purser https://purser.example.com/mcp \
  --header "Authorization: Bearer <api-token>"
```

### Codex

Codex (OpenAI's agent) shares one host config across the CLI, desktop app,
and IDE extension: `~/.codex/config.toml` (trusted projects can also use
`.codex/config.toml`). The recommended form keeps the token out of the file
by naming an environment variable:

```toml
[mcp_servers.purser]
url = "https://purser.example.com/mcp"
bearer_token_env_var = "PURSER_TOKEN"
```

Export `PURSER_TOKEN` where the Codex process can see it — GUI apps may not
inherit your shell config. Restart the app or open a new thread so Codex
rebuilds its tool list, then verify with `codex mcp list`. A literal
`http_headers = { Authorization = "Bearer ..." }` also works but stores the
secret in plaintext config.

ChatGPT web-chat connectors accept only OAuth or no authentication, so they
cannot reach Purser yet — OAuth support is planned.

### OpenClaw

```bash
openclaw mcp add purser --url https://purser.example.com/mcp \
  --transport streamable-http \
  --header "Authorization: Bearer <api-token>"
```

Or directly in `openclaw.json` under `mcp.servers`, with the token supplied
via environment variable:

```json
{
  "mcp": {
    "servers": {
      "purser": {
        "url": "https://purser.example.com/mcp",
        "transport": "streamable-http",
        "headers": { "Authorization": "Bearer ${PURSER_TOKEN}" }
      }
    }
  }
}
```

Restart the gateway afterwards so the tools are picked up.

### Gemini CLI and Code Assist

Both share `~/.gemini/settings.json`, so one command covers both:

```bash
gemini mcp add --transport http \
  --header "Authorization: Bearer <api-token>" \
  purser https://purser.example.com/mcp
```

Or in `settings.json` (note `httpUrl`, not `url`, for streamable HTTP):

```json
{
  "mcpServers": {
    "purser": {
      "httpUrl": "https://purser.example.com/mcp",
      "headers": { "Authorization": "Bearer ${PURSER_TOKEN}" }
    }
  }
}
```

The consumer Gemini app does not support custom MCP servers.

### Any other MCP client

```json
{
  "mcpServers": {
    "purser": {
      "type": "http",
      "url": "https://purser.example.com/mcp",
      "headers": { "Authorization": "Bearer <api-token>" }
    }
  }
}
```

Clients that only speak stdio can bridge to the HTTP endpoint with
`mcp-remote`.

The full toolset is in the [MCP tool catalog](../../reference/mcp-tools/).
