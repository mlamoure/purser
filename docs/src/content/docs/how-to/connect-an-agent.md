---
title: Connect an agent
description: Wire an MCP client to Purser with a scoped token and the right operating conventions.
---

Purser's primary interface is its MCP server. Any MCP client that speaks
streamable HTTP and runs on the Purser machine or on your home network
connects the same way: Claude Code, Claude Desktop, Codex, Gemini CLI, an
OpenClaw agent or a custom runtime.

## Endpoint and auth

- Endpoint: Purser's address plus `/mcp` (streamable HTTP, same process as
  REST). On the machine Purser runs on that is `http://localhost:8000/mcp`;
  from another machine on your home network it is the address in
  `PURSER_PUBLIC_URL` plus `/mcp`, for example `http://192.168.1.20:8000/mcp`
  (see [Another machine on your home network](../docker-compose/#another-machine-on-your-home-network)).
- Auth: the header `Authorization: Bearer` followed by an API token, the same
  scoped tokens REST uses.

Create the token in the admin UI (**Settings -> Agents & API**), or in the setup
wizard's last step. Both show the steps below with your address and the new
token already filled in, so you can copy them straight from Purser.
Recommended agent scopes: **`read + write`**.

| Scope | Grants |
| --- | --- |
| `read` | Every read tool and resource |
| `write` | Every write, with your authority (audited and reversible) |
| `admin` | Token and configuration management (REST only) |

Your agent acts for you: with `write` it writes directly when carrying out
your request, and proposes instead when acting on its own initiative (a draft
rule or financial note, or a schedule suggestion). Every write is attributed to its
token in the audit log and reversible. A token with only `read + write`
cannot create tokens or grant itself more access. Token creation requires
`admin`: reserve that scope for trusted administration.

## Which clients can connect

| Client | Works with a Purser at home | How |
| --- | --- | --- |
| Claude Code | Yes | [One command](#claude-code) |
| Claude Desktop | Yes | [The `mcp-remote` bridge](#claude-desktop) in its config file |
| Codex (CLI, desktop app, IDE extension) | Yes | [`~/.codex/config.toml`](#codex) |
| Gemini CLI and Code Assist | Yes | [One command](#gemini-cli-and-code-assist) |
| OpenClaw | Yes | [One command](#openclaw) |
| Any other MCP client with streamable HTTP or stdio | Yes | [Generic config](#any-other-mcp-client) |
| claude.ai (web and mobile), and the Connectors dialog in Claude Desktop | No | They connect from Anthropic's servers, which cannot reach your machine or home network. |
| The ChatGPT app | No | It connects from OpenAI's servers, and accepts only OAuth or no authentication. Use Codex. |
| The consumer Gemini app | No | It does not support custom MCP servers. |

The clients marked No need a server on the public internet. Publishing Purser
there is your own project, with your own HTTPS reverse proxy; it is not a setup
Purser tests or supports.

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

Every client below wants the same two facts: the endpoint and the token. The
examples use `http://localhost:8000/mcp`; substitute your home-network address
when the client runs on another machine. Most clients load a new server only
in a new session, so restart the client afterwards.

### Claude Code

```bash
claude mcp add --transport http --scope user purser http://localhost:8000/mcp \
  --header "Authorization: Bearer YOUR_TOKEN"
```

`--scope user` registers Purser for every folder. Without it, Claude Code
registers the server only for the folder the command ran in, and Purser's
tools are missing everywhere else. Check with `claude mcp list`; `claude
--continue` reopens your last conversation with the new server loaded.

### Claude Desktop

Claude Desktop starts local MCP servers as programs, so it reaches Purser
through [`mcp-remote`](https://www.npmjs.com/package/mcp-remote), a small bridge
that needs [Node.js](https://nodejs.org/) installed. Open **Settings**, then
**Developer**, then **Edit Config**, and add `purser` to
`claude_desktop_config.json` (beside any servers already under `mcpServers`):

```json
{
  "mcpServers": {
    "purser": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote",
        "http://localhost:8000/mcp",
        "--header",
        "Authorization:${AUTH_HEADER}"
      ],
      "env": { "AUTH_HEADER": "Bearer YOUR_TOKEN" }
    }
  }
}
```

Quit and reopen Claude Desktop. When Purser runs on another machine on your
home network, use that address and add `"--allow-http"` after the address: the
bridge refuses a plain-HTTP address other than `localhost` unless you say the
network is trusted:

```json
"args": ["-y", "mcp-remote", "http://192.168.1.20:8000/mcp", "--allow-http", "--header", "Authorization:${AUTH_HEADER}"]
```

Do not use **Settings**, **Connectors**, **Add custom connector** for a Purser
at home: connectors are reached from Anthropic's servers.

### Codex

Codex (OpenAI's agent) shares one config across the CLI, desktop app and IDE
extension: `~/.codex/config.toml` (trusted projects can also use
`.codex/config.toml`). Add:

```toml
[mcp_servers.purser]
url = "http://localhost:8000/mcp"
http_headers = { Authorization = "Bearer YOUR_TOKEN" }
```

```bash
chmod 600 ~/.codex/config.toml
```

The file now holds the token, so keep it readable only by you. To keep the
token out of the file, name an environment variable instead:

```toml
[mcp_servers.purser]
url = "http://localhost:8000/mcp"
bearer_token_env_var = "PURSER_TOKEN"
```

`PURSER_TOKEN` must then be set wherever Codex starts; desktop apps may not
read your shell profile. Restart the app or open a new thread so Codex rebuilds
its tool list (`codex resume --last` reopens your last conversation), and check
with `codex mcp list`.

### OpenClaw

```bash
openclaw mcp add purser --url http://localhost:8000/mcp \
  --transport streamable-http \
  --header "Authorization: Bearer YOUR_TOKEN"
```

Or directly in `openclaw.json` under `mcp.servers`, with the token supplied
via environment variable:

```json
{
  "mcp": {
    "servers": {
      "purser": {
        "url": "http://localhost:8000/mcp",
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
gemini mcp add --scope user --transport http \
  --header "Authorization: Bearer YOUR_TOKEN" \
  purser http://localhost:8000/mcp
```

`--scope user` registers Purser for every folder; the default is the current
project only. The command writes this to `settings.json`:

```json
{
  "mcpServers": {
    "purser": {
      "url": "http://localhost:8000/mcp",
      "type": "http",
      "headers": { "Authorization": "Bearer YOUR_TOKEN" }
    }
  }
}
```

Gemini CLI loads MCP servers only in folders you have marked as trusted: in an
untrusted folder `gemini mcp list` shows Purser as `Disabled`. Check with
`gemini mcp list`, which should show it as `Connected`.

### Any other MCP client

```json
{
  "mcpServers": {
    "purser": {
      "type": "http",
      "url": "http://localhost:8000/mcp",
      "headers": { "Authorization": "Bearer YOUR_TOKEN" }
    }
  }
}
```

A client that only speaks stdio can use the `mcp-remote` bridge shown under
[Claude Desktop](#claude-desktop).

The full toolset is in the [MCP tool catalog](../../reference/mcp-tools/).

## If the connection fails

| Symptom | Cause | Fix |
| --- | --- | --- |
| Purser's tools are missing in some folders | The server was registered for one folder. | Register it again with `--scope user` (Claude Code, Gemini CLI). |
| HTTP 401 | The token is missing, mistyped, revoked or expired, or the header lacks the word `Bearer`. | Create a new token under **Settings -> Agents & API**. |
| HTTP 421 `misdirected request` | The client used an address other than `localhost`, the one in `PURSER_PUBLIC_URL` or a name in `PURSER_ALLOWED_HOSTS`. | Use the address the answer names, or add the name the client uses to `PURSER_ALLOWED_HOSTS` on the Purser host and restart it. |
| The bridge says `Non-HTTPS URLs are only allowed for localhost` | A plain-HTTP home-network address without the flag. | Add `--allow-http` after the address. |
| `gemini mcp list` shows Purser as `Disabled` | The current folder is not trusted in Gemini CLI. | Trust the folder when Gemini CLI asks, or start it in a folder you have trusted. |
| No answer from another machine | Purser listens on `127.0.0.1` only, or a firewall blocks the port. | [Another machine on your home network](../docker-compose/#another-machine-on-your-home-network). |

## Replace a credential without splitting its audit history

In **Settings -> Agents & API**, choose **Replace credential for ...** under
**Integration identity** when creating the replacement. The new credential keeps
the same integration ID, with its own grant and effective permissions. Update the
integrator to use it, then revoke the old credential. Creating a replacement does
not automatically revoke the old one. Enable **Show expired and revoked** if you
need to select an older credential. Only an administrator can create replacements,
and they must belong to that administrator's user.

Give separate integrations separate identities. A gateway credential shared by
several agents identifies the gateway; Purser cannot infer which agent used it.
