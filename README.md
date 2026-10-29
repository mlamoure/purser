# Purser

**Self-hosted personal finance that your AI agent runs for you.**

Purser keeps your household's books (bank accounts, cards, bills, investments)
in a proper double-entry ledger on your own hardware. You don't click through
screens to manage it. You ask your AI agent (Claude, Codex, an OpenClaw agent
or any other MCP client), and the agent works with Purser through a
purpose-built set of tools. Every write it makes is attributed, audited and
reversible.

📖 **Documentation:** https://mlamoure.github.io/purser/

## What you can ask

Once your agent is connected, everyday money questions become one sentence:

- *"How much did we spend on groceries last month, compared with the month before?"*
- *"What's our net worth, and how has it changed this year?"*
- *"Will checking stay above $500 until the end of the month?"*
- *"Which bills haven't come in yet?"*
- *"Here's my card statement email. Set the payment to the statement balance."*
- *"Anything new or unusual since yesterday?"*

Purser computes the answers on the server (totals, balances, forecasts),
so they're exact. Your agent explains them; it doesn't do the arithmetic.

## Features

### Your money, in one place
- **Automatic bank sync** through [SimpleFIN Bridge](https://bridge.simplefin.org/): checking, savings, credit cards and brokerage accounts, synced daily.
- **Import your history** from Quicken (QIF) or CSV exports, with a preview before anything is written. Re-importing never creates duplicates.
- **Investments:** holdings from your brokerage feed, or tracked by hand, priced daily and included in net worth.

### Books you can trust
- **Double-entry ledger** behind a simple API. Every transaction balances, and posted entries are never edited. Corrections are linked reversals.
- **The raw bank data is kept.** Every payload is archived exactly as it arrived before anything is derived from it.
- **Reconciliation** against your statements, so every account can be checked against the bank.
- **Transfers** between your own accounts are recognized and linked, so they don't count as spending.

### Bills and the future
- **Recurring bills and income** are detected for you, or you add them as schedules. Each expected payment is matched against what actually arrives.
- **Late bills and price changes** are flagged.
- **Cash-flow forecasting** runs each account forward from your schedules, with no guesswork models. Purser warns you before an account drops below the floor you set.

### Organized without the busywork
- **100 ready-made categories** in a hierarchy you can reshape.
- **Rules** categorize and rename payees automatically. A new rule stays a draft until you activate it.
- **A review inbox** for new transactions, plus a **suggestions inbox** where Purser proposes bills, transfer links and matches for you to accept or decline.
- **Memories:** short notes about your finances ("the HOA bill is annual") that your agent gets as context.
- **Optional AI categorization.** Everything else works without any AI provider.

### Built for agents, safe for you
- **MCP first.** 46 tools cover spending summaries, net worth, forecasts, search, schedules, rules and more, plus a full REST API with OpenAPI.
- **Scoped access.** Give your agent a `read + write` token. It can act for you but can never create tokens or widen its own access.
- **Everything is audited.** Every write *and* every read is logged against the token that made it, and every write can be reversed.
- **Privacy controls:** you choose whether payees, descriptions and single amounts reach your agent at all. Responses always say what was withheld.
- **Encryption at rest** for bank data and secrets, sign-in throttling, and optional single sign-on (OIDC).

### Fits into your setup
- **Webhooks** for events such as new transactions, late bills and forecast shortfalls.
- **Grafana dashboards** on a stable read-only schema.
- **A web UI** for setup and administration: dashboard, account registers, imports, schedules, positions, users, API tokens and the audit log.

## Get started

Purser runs with Docker on Linux (amd64 or arm64) and on Macs with Apple
Silicon. The easiest way to install it is to let your AI agent do it. Give it
this one sentence:

```text
Set up Purser for me by following https://mlamoure.github.io/purser/tutorials/ai-setup/
```

Your agent installs Purser, stops at the few steps only you should take
(storing your encryption key, creating the admin account, approving its access
token), and then connects itself.

Prefer to do it yourself? Follow the [Quickstart](https://mlamoure.github.io/purser/tutorials/quickstart/).
Agents that load skills can also use [`skills/purser/SKILL.md`](skills/purser/SKILL.md).

## Learn more

- [How the ledger works](https://mlamoure.github.io/purser/explanation/ledger/)
- [Connect an agent](https://mlamoure.github.io/purser/how-to/connect-an-agent/) and the [MCP tool catalog](https://mlamoure.github.io/purser/reference/mcp-tools/)
- [Privacy and agents](https://mlamoure.github.io/purser/explanation/privacy-and-agents/)
- [Security model](https://mlamoure.github.io/purser/explanation/security/)
- [Backup and restore](https://mlamoure.github.io/purser/how-to/backup-restore/)

## Bugs, ideas and security

Report bugs and ideas in [GitHub Issues](https://github.com/mlamoure/purser/issues).
Report security problems privately through **Report a vulnerability** on the
[Security tab](https://github.com/mlamoure/purser/security). Pull requests are
welcome; the maintainer applies accepted changes upstream.

## License

[AGPL-3.0-only](LICENSE). Third-party components and their licences are
listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
