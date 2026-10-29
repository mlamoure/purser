"""llms.txt / tool-guide generation — same source as the describe tool."""

from purser.config import get_settings
from purser.mcp.server import mcp

CONVENTIONS = """\
## Conventions

- Amounts are decimal strings; negative = money out of the account.
- Transaction lifecycle: expected -> pending -> posted -> void. Posted rows
  are immutable; corrections become linked reversal+replacement pairs.
- Writes take the write scope, and you act with the user's authority:
  write directly when carrying out the user's request. When acting on your
  own initiative (a pattern you noticed, a rule you think would help),
  propose instead: leave activate false on propose_rule (a draft rule never
  runs), pass propose=true to create_schedule (a suggestion card), or leave
  it to the suggestions inbox. propose_memory stays a draft unless the user
  explicitly asked you to remember something: an active memory is context
  for every future session, and bank text is written by merchants. Every
  write is attributed to your token in the audit log and is reversible.
- Disclose only what the question needs: everything you read goes to your
  model provider. The user may privatize payees (you see stable references
  like py_8k2mq4x7ra) and single amounts by default; a response names what
  it withheld in `withheld`. Request a field only when the user's question
  needs it (include=["payee"], ["description"], ["amount"]). Filter and act
  with payee references and transaction ids; a payee name the user typed may
  be used as a filter. To show the user details, call present(transaction_ids)
  and reply with its link instead of repeating them. Aggregates (totals,
  balances, net worth) are always exact.
- Schedules are the durable rules behind the forecast; their instances are
  expected rows you can override (amount/date pins them; reset_instance
  hands one back to the rule), relabel (description/payee does not pin),
  skip, or mark paid (match). Renaming a schedule relabels its instances.
  A card-payment schedule estimates from the card's current balance; a
  card statement (email or notice) goes to
  record_statement_balance(statement_balance, account, due_date), which
  sets that payment to the statement balance (basis 'statement' in
  forecast_balance). A row the matcher proposed a bank row for shows
  proposed_posted_id in list_unmatched; the proposal is also a kind='match'
  suggestion (decide_suggestion accepts or rejects it — a rejection is
  remembered), and match(expected_id, proposed_posted_id) confirms it too. A
  recurring
  transfer (card payment, contribution) names a counterparty_account instead
  of a category. Purser proposes schedules it detects as SUGGESTIONS —
  list_suggestions / decide_suggestion — and nothing activates without a
  decision; run detect_schedules to scan history now.
- A brokerage account takes its holdings from the feed or, once an admin
  sets its holdings_source to manual, from positions recorded by hand:
  record_position(account, symbol, quantity, as_of) is "I hold N shares as
  of D" (backdating re-values history from D), record_price(symbol, date,
  close) supplies a close, and a daily provider fetch may supply the rest.
  A close resolves newest date first; a manual close only wins a same-date
  tie. list_holdings shows each account's source, value, and the positions
  with no price (adding nothing to net worth) or a stale close. A feed
  position the feed values at 0 (a plan-only fund no provider prices) is
  valued from a close recorded with record_price; until one exists it has
  no price.
- forecast_balance carries `coverage`: schedules_active == 0 means the line
  is flat by construction, not a quiet month.
- A purser:// URI a tool hands out (daily_uri, series_uri) is the full
  series: resources/read, or read_series(uri) where a gateway forwards only
  tools. Both are exempt from the response budget — read one only when the
  compact answer is not enough.
- Accounts and categories accept a UUID or an ltree path (e.g.
  expenses.dining.coffee). Categories are virtual accounts.
- update_account renames a real account. Only the display
  name changes; the path never does, so tools, rules and outside
  integrations that refer to the account keep working. The name is also a
  transfer signal in bank text. Categories are renamed in the app.
- Transfers are ONE transaction with two real-account postings. A split loan
  payment (confirm_transfer with remainder_category) keeps both bank legs
  exact and adds one interest/fee category posting for the difference. An
  ordinary confirm_transfer also proposes a transfer_link rule for that
  account pair and payee as a suggestion; it links nothing until a human
  accepts it and confirms the rule's projected link count.
- Aggregate questions (spending, net worth, balance history, forecasts,
  unmatched) each have a dedicated tool — never page transactions to compute
  totals or balances yourself.
- Balance over time: ONE account = balance_history(account, period); the whole
  portfolio = net_worth(period); forward-looking = forecast_balance(account,
  days). A running balance is recorded per row, never derived by summing.
- spending_summary totals are positive-for-spend (category-leg convention);
  search_transactions amounts are negative-for-spend.
"""


async def build_llms_txt() -> str:
    lines = [
        "# Purser — the finance backend for the personal agent stack",
        "",
        "Purser holds a correct, auditable, source-traceable financial ledger and",
        "exposes it through MCP tools an agent can operate safely. Connect to /mcp",
        "with a Bearer API token (recommended agent scopes: read + write).",
        "",
        CONVENTIONS,
        "## Tools",
        "",
    ]
    for tool in await mcp.list_tools():
        description = (tool.description or "").strip().replace("\n", " ")
        lines.append(f"- **{tool.name}**: {description}")
    lines += [
        "",
        "## First call",
        "",
        "Call `describe` in a fresh session — it returns the data model,",
        "conventions, your token's scopes, the category tree, and the account",
        "list in one compact round-trip.",
        "",
        "## Source code",
        "",
        "Purser is free software under the GNU AGPL v3 (AGPL-3.0-only). This",
        f"instance's source code: {get_settings().source_url} (also GET /source).",
        "",
    ]
    return "\n".join(lines)
