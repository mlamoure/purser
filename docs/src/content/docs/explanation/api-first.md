---
title: API-first design
description: Mostly headless operation through your AI agent, with server-side calculations and an administrative UI.
---

Purser is a privately hosted finance back-end designed for your personal AI
agent. It is mostly headless: day-to-day work happens through the agent over
MCP or REST. The web UI provides administrative controls and record inspection.
Its operations use the public API and require the corresponding scopes;
a normal agent token cannot manage tokens or configuration.

Purser is an AI enablement engine. It stores records, performs financial
calculations and supplies tools and context to an external agent. It is not
an AI and requires no AI subscription. Optional model integrations are
described below.

An agent's `present` link opens selected records in full detail for a signed-in
person, without including those records in the tool response.

## Computation server-side, judgment client-side

Purser computes financial results; your agent interprets them. Aggregate tools
return answers without requiring the agent to retrieve and sum transactions:
`spending_summary`, `net_worth`,
`balance_history`, `forecast_balance`, `list_recurring`, `list_unmatched`. The
agent decides what to ask and how to narrate it; Purser does the arithmetic.

## Optional model integrations

Purser can call a separately configured model to categorize transactions.
This integration is off by default and uses the provider, endpoint and
credentials you supply. It can use a local or hosted model; any provider
charges or subscription belong to that service. The finance engine works with
it disabled. See [Configuration](../../reference/configuration/).

## Token budgets

Tool responses are budgeted. Aggregate tools return **summaries, not row dumps**,
targeting well under 500 tokens for a typical answer, and no tool forces the
caller to do cross-row math. This keeps answers cheap enough to ask freely and
small enough to fit an agent's working context.

## Three-tier answers

Visualization-worthy tools answer in three tiers, so a caller takes exactly what
it can use:

1. a one-sentence **text headline** — enough to narrate;
2. compact **`structuredContent`** — a declared, validated shape within the token
   budget;
3. a **`_meta.ui.resourceUri`** naming a self-contained widget for MCP Apps hosts.

Full-resolution series stay out of the structured payload behind `purser://`
resources, which share the same auth and are exempt from the budget. The
`read_series` tool returns the same series, byte for byte, to clients and
gateways that forward tools but not resources — it shares the exemption, so it
is read only when the compact answer is not enough. A host with no widget
support still gets a complete answer from tiers 1 and 2. Every other tool also
carries its answer as text, and an empty list arrives as `[]`, so a gateway that
rebuilds results from their text blocks never loses a "nothing found" answer.

## Describe first

In every fresh session an agent calls
[`describe`](../../how-to/connect-an-agent/) first. One call returns the data model, amount conventions, the caller's token scopes,
the category tree, and the account list. The same guide is served unauthenticated
at `GET /llms.txt`, and the [`search_docs`](../../reference/mcp-tools/) tool answers
questions about operating Purser from this documentation.

## Why the UI is admin-only

The web UI covers setup, connections, settings, tokens and auditing. It also
provides account registers, schedules, positions and a forecast chart for
direct inspection. Its navigation is **Dashboard**, **Accounts** (each opens
its register), **Search** (every account at once, with the
[search syntax](../../how-to/search-transactions/)), **Scheduled
transactions**, **Import**, and one **Settings**
area: General, Connections, Categories (with Rules and AI categorization as
its tabs), Agent memory, Market data, Agents & API, Security, Users and Audit
log. A member sees the sections their own
access works in; the admin-only ones (General, Security, Users, Audit log)
are hidden from them. Your agent is the intended interface for day-to-day work.
[Grafana](../../how-to/grafana/) supplies broader dashboards over the stable read
schema. Both use the same underlying financial data and public interfaces.

The authoritative spec is
[`DESIGN.md`](https://github.com/mlamoure/purser/blob/main/DESIGN.md)
sections 1 (principles) and 10 (the MCP toolset and presentation layer).
