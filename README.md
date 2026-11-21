# Purser

**The privately hosted finance back-end, designed for use by your personal AI Agent.**

Purser syncs bank accounts, categorizes transactions, tracks bills, reconciles
statements, forecasts cash flow and values investments. It is mostly headless:
you work through your own AI agent over MCP or REST. A web UI provides the
administrative controls for setup, configuration, access and record inspection.

Purser is not an AI. It is an AI enablement engine: it stores financial data,
performs calculations and provides tools for your agent to use. Purser requires
no AI subscription. Optional integrations can call a model you configure for
categorization and review; they are off by default.

You choose what to share with your agent. Purser provides access scopes,
disclosure settings and an audit log; it does not prescribe how much financial
information you should share.

**Documentation:** https://mlamoure.github.io/purser/

## Hosting and access

- **Privately hosted.** Purser runs on your own hardware with Docker. Bank
  data is stored on your server, with sensitive fields encrypted at rest.
  There is no Purser cloud account.
- **Scoped access.** Give an agent read access or read and write access.
  Token creation and configuration require a separate admin scope.
- **Disclosure controls.** MCP settings can replace payee names with stable
  references and omit individual amounts. These are defaults: an agent can
  request those fields per call. REST reads have different disclosure behavior;
  see [Privacy and agents](https://mlamoure.github.io/purser/explanation/privacy-and-agents/).
- **Server-side calculations.** Tools return totals, balances, net worth and
  forecasts without requiring the agent to read every transaction. An agent
  can also link to records for you to inspect in Purser.
- **Auditing.** The Data shared view identifies integrations, returned field
  inventories, coverage, omissions and outcomes, with filters and exports.
  Older entries and incomplete evidence are labeled explicitly. Integration
  responses are recorded before release; a capture error withholds the response,
  although a requested write may already have completed. Posted transactions
  are corrected through linked reversal and replacement entries.

```mermaid
flowchart LR
  subgraph local["Your server (local)"]
    P[Purser] <--> DB[(Purser database<br/>PostgreSQL)]
  end
  subgraph either["Local or hosted: your choice"]
    A[Personal agent] --> M[AI model]
  end
  S["SimpleFIN Bridge<br/>(hosted, optional)"] -- "bank data, fetched by Purser over HTTPS" --> P
  A -- "MCP / REST, scoped token" --> P
  P -. "optional categorization and review<br/>(off by default)" .-> M
```

Purser and its database run on your server. Your agent reaches Purser with a
scoped token and uses whichever model you choose; Purser itself calls a model
only if you configure one.

## What you can ask your agent

- *"How much did we spend on groceries last month, compared with the month before?"*
- *"What's our net worth, and how has it changed this year?"*
- *"Will checking stay above $500 until the end of the month?"*
- *"Which bills haven't come in yet?"*
- *"Here's my card statement email. Set the payment to the statement balance."*
- *"Anything new or unusual since yesterday?"*

## Features

### Accounts and imports

- **Bank sync** through [SimpleFIN Bridge](https://bridge.simplefin.org/) for checking, savings, credit cards and brokerage accounts.
- **QIF and CSV imports** with a preview and duplicate detection.
- **Investments:** holdings from your brokerage feed or tracked by hand, priced daily and included in net worth.
- **Tax lots and equity awards** for holdings you track by hand: load a broker's lot list and vest schedule in one previewed step. Sales name the lots they draw on. Home includes potential value inside the Net worth and Assets cards when unvested awards exist; users without them see only current owned totals. Unvested awards remain outside owned net worth until vesting.

### Ledger and reconciliation

- **Balanced entries.** Transaction postings sum to zero per currency. Corrections to posted transactions preserve the original entry and its history.
- **Source records.** Original bank payloads are retained for tracing imported transactions.
- **Reconciliation** against account statements, including explicit cash-only reconciliation for manually tracked brokerage accounts.
- **Transfers** between your own accounts are linked and excluded from spending totals.

### Bills and forecasts

- **Recurring bills and income** are detected or entered as schedules, then matched to incoming transactions.
- **Occurrence estimates:** amount and date uncertainty are tracked separately, with evidence, original estimates and linked actuals. Fixed planning values can remain estimates, including projected slots without transaction IDs, without changing forecast amounts or dates. Confirm one payment without confirming later ones; ambiguous matches wait for review.
- **Late bills and price changes** are flagged.
- **Cash-flow forecasts** use schedules to project account balances and flag balances below your chosen floor.

### Categories, rules and notes

- **Categories** come with a default hierarchy you can edit.
- **Rules** categorize transactions and rename payees. Draft rules take effect when activated.
- **Review and suggestions inboxes** collect new transactions, proposed schedules, transfer links and matches.
- **[Financial notes](https://mlamoure.github.io/purser/explanation/memories/)** keep context about your finances in one place, such as "the HOA bill is annual," for your agent to use.

## The admin interface

Day-to-day work happens through your AI agent. The web UI provides the
administrative controls for setup, connections, settings, tokens and auditing.
It also lets you inspect accounts and records directly. These screenshots use
synthetic demonstration data.

![Dashboard with net worth, Needs attention (a detected scheduled transaction and the transaction review summary) and accounts](docs/public/screenshots/dashboard.png)

**Needs attention:** review missing expected payments first, expand suggestions
by type, and open the exact account register for posted or pending review.

| | |
|---|---|
| ![Settings, Agents & API: create a token and the token list](docs/public/screenshots/tokens.png) **Access:** create scoped agent tokens under Settings, Agents & API | ![Privacy with agents card](docs/public/screenshots/privacy.png) **Disclosure:** set defaults for MCP responses, on the same page |
| ![Audit log with an agent's read expanded](docs/public/screenshots/audit.png) **Audit log:** review sharing and changes under Settings, Audit log | ![Account register with categories and transfer links](docs/public/screenshots/register.png) **Record inspection:** view transactions, balances and transfer links |

![Scheduled transactions: the account forecast above each account's schedules by payee and memo](docs/public/screenshots/schedules.png)

**Scheduled transactions:** the bills, paychecks and transfers behind the
forecast, by payee and memo, with the projected balance for each account.

The interface follows your system's light or dark appearance.

## Get started

Purser runs with Docker on Linux (amd64 or arm64) and on Macs with Apple
Silicon. To have your agent install it, give it this instruction:

```text
Set up Purser for me by following https://mlamoure.github.io/purser/tutorials/ai-setup/
```

The setup guide asks you to store your encryption key, create the admin
account and approve the agent's access token.

For manual installation, follow the [Quickstart](https://mlamoure.github.io/purser/tutorials/quickstart/).
Agents that load skills can also use [`skills/purser/SKILL.md`](skills/purser/SKILL.md).

## Reference

- [REST API](https://mlamoure.github.io/purser/reference/rest-api/)
- [MCP tools](https://mlamoure.github.io/purser/reference/mcp-tools/)
- [Connect an agent](https://mlamoure.github.io/purser/how-to/connect-an-agent/)
- [Privacy and agents](https://mlamoure.github.io/purser/explanation/privacy-and-agents/)
- [Ledger](https://mlamoure.github.io/purser/explanation/ledger/)
- [Security model](https://mlamoure.github.io/purser/explanation/security/)
- [Encryption at rest](https://mlamoure.github.io/purser/explanation/encryption-at-rest/)
- [Run with Docker Compose](https://mlamoure.github.io/purser/how-to/docker-compose/)
- [Put Purser behind a reverse proxy](https://mlamoure.github.io/purser/how-to/reverse-proxy/)
- [Backup and restore](https://mlamoure.github.io/purser/how-to/backup-restore/)

## Bugs, ideas and security

Report bugs and ideas in [GitHub Issues](https://github.com/mlamoure/purser/issues).
Report security problems privately through **Report a vulnerability** on the
[Security tab](https://github.com/mlamoure/purser/security). Pull requests are
welcome; the maintainer applies accepted changes upstream.

## License

[AGPL-3.0-only](LICENSE). Third-party components and their licences are
listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Release security checks

Release qualification audits the locked Python runtime dependencies and scans
native amd64 and arm64 container images. The publication job scans the exact
digest again before creating the release tag. High and critical findings block
publication unless an unfixed finding has a reviewed, package-version-specific
exception in `.trivyignore.yaml`; exceptions expire and never hide an available
fix. Full scan reports remain available as workflow artifacts.

To reproduce locally on Linux, run `bash scripts/audit_python.sh` and
`bash scripts/scan_image.sh <image>`. Both write to `security-reports/`.
