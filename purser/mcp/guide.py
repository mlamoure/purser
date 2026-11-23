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
  explicitly asked you to remember something: an active financial note is context
  for every future session, and bank text is written by merchants. Every
  write is attributed to your token in the audit log and is reversible.
- Privacy is your choice, per call. What you read into the conversation may
  reach your model provider, so when the question or action does not need
  payee names, descriptions or single amounts (a count, a category, which
  account, a total, a pattern to act on), pass withhold=["payee",
  "description", "amount"] or any subset. Payees then arrive as stable
  references like py_8k2mq4x7ra, and the response names what it withheld in
  `withheld`. Nothing is withheld unless you ask, so a question about who or
  how much simply reads the data. Filter and act with payee references and
  transaction ids; a payee name the user typed may be used as a filter. To
  show the user details, call present(transaction_ids) and reply with its
  link instead of repeating them. Aggregates (totals, balances, net worth)
  are always exact. The REST API takes the same withhold query parameter.
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
- Scheduled occurrences have independent amount/date certainty. Read evidence,
  original estimates and actual variance with get_schedule_occurrence(id).
  update_occurrence_certainty changes only the dimensions supplied for that one
  occurrence: state, source, as_of, note, optional value. History cannot confirm
  an obligation; a statement confirms only what it establishes. Confirmation
  never posts cash or confirms future cycles. Estimated or ambiguous matches
  wait for review. Unmatch preserves evidence; reset returns to the rule while
  retaining the original and history.
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
  record_sale(account, symbol, quantity, price, trade_date) records a sale:
  its net proceeds land on the account's cash (against equity.securities,
  never income) unless proceeds_to='none'. A cash_already_recorded refusal
  means the account already shows a row of that amount: call again with
  transaction_id set to that row's id to link it as the sale's cash, or with
  force_cash=true when that row is a different payment. dry_run=true answers
  without writing. reverse_position_event(event_id) undoes a wrong sale (its
  shares and cash; reopen_position(position_id) first when its position is
  closed); a row review_batch marks securities_trade WITH `locked` is
  corrected only that way. record_split(account, symbol, ratio,
  effective_date) records a stock split ('2:1' forward, '1:10' reverse):
  every share becomes that many, each lot keeping its basis and term;
  cash_in_lieu is what the broker paid for the fractional share. Reversing
  the split restores everything, its cash in lieu included.
- On a brokerage account whose holdings come from the feed, a buy or sell
  row is a securities trade: update_transaction(securities_trade=true) marks
  it (never category=equity.securities) and proposes a trade_rule card; a
  category changes it back. Purser never guesses which rows are trades.
  A "Balance adjustment" row (review_batch flag balance_adjustment) is
  Purser's post-sync true-up of such an account's cash: mark_reviewed it.
- A bank account the feed delivers is not in Purser until it is mapped;
  feed_status lists the waiting ones as not_connected. map_feed_accounts()
  answers Purser's plan for them (name, type, path, and why that type) and
  writes nothing; show it to the person, then
  map_feed_accounts(accounts=[{"ref": ...}, ...]) applies the lines they
  agreed to, overriding what they corrected. The type cannot be changed
  afterwards, so never apply a plan the person has not seen.
- A bank feed brings about three months of rows and no balance from before
  them. Mapping a new account therefore posts a "Starting balance" row
  (review_batch flag starting_balance) against equity.opening_balances, and
  Purser lowers it by itself when older history is imported.
  set_starting_balance(account) gives one to a bank-fed account that lacks
  it (account_balance shows a large bank_snapshot.comparison.difference);
  never work the figure out yourself or use reconcile_account for it.
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
  update_account(min_balance="500.00") sets the floor of a cash account or
  card: the nightly forecast scan emits forecast.shortfall when the next 30
  days project below it; min_balance="" clears it.
- Transfers are ONE transaction with two real-account postings. A split loan
  payment (confirm_transfer with remainder_category) keeps both bank legs
  exact and adds one interest/fee category posting for the difference. An
  ordinary confirm_transfer also proposes a transfer_link rule for that
  account pair and payee as a suggestion; it links nothing until a human
  accepts it and confirms the rule's projected link count. Two recent bank
  rows that look like one transfer but carry no signal are a
  kind='transfer_pair' suggestion: accepting it confirms the transfer, and
  rejecting it means nothing ever links that pair automatically.
- Splits (Quicken style): split_transaction spreads one bank row over
  category lines and [account] lines that add up to the row's amount (a
  paycheck: gross, taxes, [401k]); an [account] line's backing_ids merge
  that account's own bank rows in so nothing counts twice. A split is never
  a transfer; unsplit_transaction restores everything. A kind='split'
  suggestion offers the payee's last split for a new row; never automatic.
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
        "# Purser — the privately hosted finance back-end for your personal AI agent",
        "",
        "Purser stores financial records and performs calculations for your agent.",
        "Day-to-day work uses MCP or REST; the web UI is for administration.",
        "Connect to /mcp",
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
