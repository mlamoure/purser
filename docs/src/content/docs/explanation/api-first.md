---
title: API-first design
description: Why computation lives on the server, judgment lives in the client, and the web UI is only for administration.
---

Purser is an agent's backend first and a web app second. MCP and REST are the
primary interfaces; the admin SPA is a thin operator console over the *same*
public API an agent uses. There are no private admin endpoints — which is exactly
why an agent can do anything the UI can. The one deliberate exception is the
page an agent's `present` link opens: it shows you rows in full and is for a
signed-in person only.

## Computation server-side, judgment client-side

The dividing line is deliberate. **Financial computation happens on the server; the agent normally
provides interpretation and advice.** An agent should never page rows and sum them in
context to answer "how much did I spend on dining this quarter?" — that is slow,
expensive, and error-prone. Instead every aggregate question has a dedicated tool
that computes the answer in one call: `spending_summary`, `net_worth`,
`balance_history`, `forecast_balance`, `list_recurring`, `list_unmatched`. The
agent decides what to ask and how to narrate it; Purser does the arithmetic.
Optional server-side AI categorization and review are exceptions to the default:
they use the configured provider and documented review controls. The core works
with AI disabled.

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

Because the interface is the product, orientation is a single call. In every
fresh session an agent calls [`describe`](../../how-to/connect-an-agent/) first: one
round-trip returns the data model, amount conventions, the caller's token scopes,
the category tree, and the account list. The same guide is served unauthenticated
at `GET /llms.txt`, and the [`search_docs`](../../reference/mcp-tools/) tool answers
questions about operating Purser from this documentation.

## Why the UI is admin-only

The SPA provides account registers, schedules, positions, and an embedded
forecast chart alongside setup, tokens, feed mapping, and review controls.
[Grafana](../../how-to/grafana/) supplies broader dashboards over the stable read
schema. Both use the same underlying financial data and public interfaces.

The authoritative spec is
[`DESIGN.md`](https://github.com/mlamoure/purser/blob/main/DESIGN.md)
sections 1 (principles) and 10 (the MCP toolset and presentation layer).
