---
title: Mark brokerage buys and sells
description: Mark a brokerage account's buy and sell rows as securities trades instead of spending or income, let a rule mark the next ones, and keep the account's cash in step with the broker's balance.
---

A brokerage account whose holdings come from the feed sends two kinds of
facts: its holdings (the shares and what they are worth) and its cash rows.
A buy ("YOU BOUGHT", "Buy", "Reinvestment", the wording varies by broker)
exchanges cash for shares the holdings already count. It is neither spending
nor income, so Purser books it as a **securities trade**: the system account
`equity.securities`, which no report reads as spending or income.

Purser never decides by itself which rows are trades, because brokers word
them differently. You mark them, and you write a rule for the rest; marking
one row proposes that rule for you.

This page covers brokerage accounts whose holdings come from the feed. On an
account whose holdings you [track by hand](../../how-to/manual-holdings/), record the
trade itself (a buy or sell position event, or an agent's `record_sale`) and
link the bank row as its cash: the shares and the cash then move together.
Marking a row there is refused.

## Mark a row as a securities trade

In the account's register, open the row's category picker and choose
**Securities trade** (under "Investments"). The option appears only on a
brokerage register whose holdings come from the feed.

Over the API, send `securities_trade: true` in place of a category:

```http
PATCH /transactions/{id}
{"securities_trade": true}
```

An agent calls `update_transaction(transaction_id, securities_trade=true)`.

The mark is an ordinary category edit: a pending row changes in place, a
posted row gets a correction (a reversal and a replacement), and the account's
balance does not move, because only the category side changes. Sending
`equity.securities` as a `category` is still refused; the flag is the one way
to choose it.

It is refused on any other account, on a transfer, on a split row, on an
expected (scheduled) row, and on the cash of a trade you recorded by hand.

## Change it back

Choose any category in the picker, or send the category:

```http
PATCH /transactions/{id}
{"category": "expenses.fees"}
```

A row you, an agent or a rule marked is always editable. Only the cash of a
recorded trade stays read-only (`securities_trade_locked: true` on the row):
reverse that trade to correct it.

## Let a rule mark the next ones

When you mark a row, Purser proposes a rule in the
[suggestions inbox](../../how-to/suggestions/) (Needs attention, "Securities trade
rules"): rows on this account whose payee or description starts with the same
first two words, in the same direction (money out for a buy, money in for a
sale), become securities trades. The response to the mark carries its
`suggestion_id`; marking more rows like it refreshes the same card.

On the card:

- **Edit & accept** changes what the rule matches: payee, description or
  either; starts with, contains or is; the text; the direction.
- **Create draft rule** creates the rule as a draft and says how many rows in
  your history it matches today. A draft marks nothing.
- **Activate** turns it on for new rows. To mark past rows, use the rule's
  apply to history on the Rules page, which previews first.
- **Reject** remembers the proposal; **Never for this account** stops trade
  rule proposals for the account.

You can also write the rule yourself. A rule that marks rows as a securities
trade must name its accounts, every one a brokerage account whose holdings
come from the feed:

```http
POST /rules
{
  "type": "categorize",
  "securities_trade": true,
  "account_ids": ["<brokerage account id>"],
  "match_field": "payee_or_description",
  "match_mode": "prefix",
  "match_text": "YOU BOUGHT",
  "direction": "debit"
}
```

An agent calls `propose_rule(type="categorize", securities_trade=true,
account=..., match_field=..., match_mode=..., match_text=...)`. See
[rules](../../how-to/rules/) for how rules run.

## Keep the account's cash in step with the broker

Purser reports a brokerage account as its cash plus its holdings. When the
feed sends a deposit but never the buy row that spent it, the cash keeps the
deposit while the holdings already include the shares, and the account reads
above the broker's balance by exactly the missing buy.

After every sync, Purser compares the balance the broker reported with the
account's cash plus the holdings the same sync reported. When they differ by a
cent or more, it posts one entry for the difference, labelled **Balance
adjustment (invested cash)** in the register, dated the broker's balance
date. Afterwards the account's balance, and net worth, equal what the broker
reported. The entry is a securities trade too: never spending or income.

It is on by default for every brokerage account whose holdings come from the
feed, and it never fires on data it cannot trust:

- the balance has no date, is dated after the holdings, or is older than the
  feed-stale threshold (`feed_stale_days`, see
  [configuration](../../reference/configuration/));
- the holdings are missing, list a symbol twice, or include a fund with no
  price;
- the holdings Purser keeps for the day differ from what this sync reported.
  Purser keeps one set of holdings per day, the first sync's, so a later sync
  that day is adjusted only while its holdings (symbols, share counts and
  values) still match that set; a price that moved during the day waits for
  the next day's first sync;
- the account is closed, its holdings are tracked by hand, or the switch is
  off;
- the same sync already adjusted the account.

Each adjustment is audited (`account.balance_true_up`), emits an
`account.balance_adjusted` [event](../../reference/events-webhooks/), and
waits in [review](../../how-to/review-new-transactions/) flagged `balance_adjustment`.
If you know what the gap was (a fee, say), recategorize the entry. A buy row
the broker sends later is matched by the next sync's adjustment.

### Turn it off

Clear **Keep cash in step with the broker's balance** in the account's
settings, or:

```http
PATCH /accounts/{id}
{"balance_true_up": false}
```

An agent calls `update_account(account, balance_true_up=false)`. Turning it
off stops future adjustments; past ones stay in the register. Switching the
account's holdings to tracked-by-hand turns it off; switching back to the feed
turns it on again.

For the design, see decision 119 in
[DESIGN.md](https://github.com/mlamoure/purser/blob/main/DESIGN.md).
