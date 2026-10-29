---
title: Visualize with Grafana
description: Point Grafana at Purser's stable read schema, import the shipped dashboards, and enable deep links on tool responses.
---

Purser includes a forecast chart on the Schedules page and widgets for
compatible MCP clients. Grafana provides broader reporting over a stable read
schema, so your data remains available to dashboards outside the app. Three dashboards ship in the repository under
[`grafana/dashboards/`](https://github.com/mlamoure/purser/tree/main/grafana/dashboards):

- **Purser - Monthly cash flow by category** (`purser-cash-flow`)
- **Purser - Net worth over time** (`purser-net-worth`)
- **Purser - Upcoming bills & forecast** (`purser-upcoming-bills`)

## The stable read schema

The dashboards query a versioned `readmodel` schema — the query contract you can
build your own panels against:

| View | Feeds |
| --- | --- |
| `readmodel.v1_net_worth_daily` | Net worth, assets vs liabilities over time. |
| `readmodel.v1_spending_by_category_month` | Cash flow by category and month. |
| `readmodel.v1_forecast_daily` | Projected balances. |
| `readmodel.v1_holdings_latest` | Latest brokerage holdings. |
| `readmodel.v2_transactions_flat` | The flat transaction view. |

`net_worth_daily` is materialized and watermark-driven, so these read fast and
stay current as new data lands.

## Add a datasource and import

1. Create a **read-only** PostgreSQL role on the Purser database (the dashboards
   describe their input as a `purser_read` connection). Reporting should never
   hold write access to the ledger.
2. In Grafana, add a **PostgreSQL** datasource pointing at the Purser database
   with that role.
3. Import each JSON file from `grafana/dashboards/`. On import Grafana prompts
   for the **`DS_PURSER`** datasource input (labeled "Purser (PostgreSQL)") —
   select the datasource from step 2.

The dashboards carry only `grafana` and `postgres` requirements, so no plugins
are needed.

## Deep links from tool responses

Set `PURSER_GRAFANA_URL` to your Grafana base URL and Purser decorates tool
responses with `links.grafana` deep links into the matching dashboard — so an
agent's answer about net worth can offer the chart. When it is unset, links are
omitted entirely. `PURSER_GRAFANA_URL` is runtime-editable in **Config ->
Settings**; see [Configuration](../../reference/configuration/).

The design rationale for a shared read schema, built-in visualizations and
external dashboard templates is
[`DESIGN.md`](https://github.com/mlamoure/purser/blob/main/DESIGN.md)
principle 7 and section 8 (net worth).
