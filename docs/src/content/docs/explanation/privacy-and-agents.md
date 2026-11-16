---
title: Privacy and agents
description: Control financial data sharing through access scopes, MCP disclosure defaults, per-call requests and auditing.
---

You decide what financial information to share with your AI agent. Purser
provides access scopes, disclosure defaults and an audit log. It does not
prescribe how much information you should share. Data returned to an agent
may enter its model's context; where that data goes depends on the agent and
provider you use.

Token scopes control which operations are allowed. MCP disclosure settings
control which fields are returned by default. An agent with read access can
request omitted fields per call without another approval step. These settings
are not field-level access restrictions. For scope details, see
[Security model](../security/). The authoritative spec is design section 9.

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
left out for agents by default, whatever the settings. A call that passes
`include: ["external_ids"]` gets them.

## Every omission is named

A response says what it withheld, for example
`"withheld": ["amount", "payee"]`. An agent that saw a field simply missing
would conclude the value is unknown and might try to fix it.

## Asking for a field: include

A setting is a default, not a limit. Every tool that can return a withheld
field takes `include`, any of `payee`, `description`, `amount` and
`external_ids`, and returns exactly those fields on that call. The audit log
separately records additional fields requested and fields observed in the final
response. Open **Audit Log > Data shared** for all integration reads, including
ordinary reads within the defaults and aggregate answers. **Only fields returned
beyond privacy defaults** narrows the view to observed extra fields; it is not a
list of all sharing. Failed or empty requests do not prove those fields were
returned. Some free-text responses have only a partial field inventory.

Authorization means the integration has permission to act. It does not mean the
user approved each individual read. The log identifies the credential Purser
served; downstream model providers and forwarding are not verified. For evidence,
retention and capture failures, see [Security](../security/).

## Working by reference

Payee references and transaction ids work as inputs everywhere a payee or a
row is named: `search_transactions(payee=...)`, `update_transaction`,
`propose_rule`, `match`, links on financial notes. Purser resolves them. A payee name you
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

## MCP, REST and the web UI

These settings govern what Purser hands an agent over MCP. Purser's own web
pages always show you full detail. REST serves full detail to any token that
can read, except external account identifiers, which it leaves out for API
tokens without `admin` scope.
