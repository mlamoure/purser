---
title: Privacy and agents
description: How Purser lets your agent see only what a task needs — privatized defaults, include, payee references, and present.
---

Everything an agent reads from Purser lands in its model provider's context.
Your agent is your own and may see anything, but Purser lets it expose only
what a task needs, the way an agent can use a password-manager secret without
the secret entering the chat. There is no approval step: the agent chooses,
the tool guidance teaches it to choose well, and your settings set the
defaults. The authoritative spec is design section 9.

## Two settings

Both are in Config, Settings, under **Privacy with agents**, and both are off
by default (`PURSER_PRIVACY_PURCHASE`, `PURSER_PRIVACY_AMOUNT`; see
[Configuration](../../reference/configuration/)).

| Setting | What an agent receives |
| --- | --- |
| Privatize the purchase | Each payee as a stable reference such as `py_8k2mq4x7ra` instead of its name. Descriptions, posting memos and bill (schedule) names are left out. Accounts and categories stay, because their names are yours and they carry most of the meaning. |
| Privatize the amount | No single-transaction amount: no row amount, no posting amount, no running balance (it would give each row's amount away by subtraction), no individual bill or forecast-event amount. |

Totals stay exact whatever the settings: `spending_summary` totals,
`account_balance`, `net_worth`, `balance_history` series and the forecast's end
balance. When an agent asks for a total, the total is the answer.

Amounts are exact or left out, never rounded or made up: a number that is not
true is something an agent might reason from or repeat.

External account identifiers (the bank's account ids and connection ids) are
left out for agents by default, whatever the settings: no agent task needs
them. A call that passes `include: ["external_ids"]` gets them.

## Every omission is named

A response says what it withheld, for example
`"withheld": ["amount", "payee"]`. An agent that saw a field simply missing
would conclude the value is unknown and might try to fix it.

## Asking for a field: include

A setting is a default, not a limit. Every tool that can return a withheld
field takes `include`, any of `payee`, `description`, `amount` and
`external_ids`, and returns exactly those fields on that call. The audit log
marks every call that asked for more than your defaults: open **Audit Log**
and choose **Agent disclosures beyond privacy defaults** to see what your AI
chose to see.

## Working by reference

Payee references and transaction ids work as inputs everywhere a payee or a
row is named: `search_transactions(payee=...)`, `update_transaction`,
`propose_rule`, `match`, memory links. Purser resolves them. A payee name you
typed yourself is still accepted as a filter, because you disclosed it, not
Purser; the answer stays privatized.

A reference is never accepted inside free text (a description, a memo, a
note, a name), so a reference cannot turn into a payee's real name. A
reference stays the same when the encryption key is rotated, so an agent's
long-term notes about it stay valid.

## Showing you the details: present

`present(transaction_ids, note)` returns a link that opens those rows, with
full detail, in Purser for you when you are signed in. The agent can reply
"your four largest charges this week" followed by the link and the values never pass
through its model. The link carries only transaction ids and the note.

## What this is not

These settings govern what Purser hands an agent over MCP. Purser's own web
pages always show you full detail. REST serves full detail to any token that
can read, except external account identifiers, which it leaves out for API
tokens without `admin` scope.
