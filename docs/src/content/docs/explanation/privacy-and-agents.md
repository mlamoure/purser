---
title: Privacy and agents
description: Your agent decides, call by call, which financial details a question needs; Purser leaves out the rest, and the audit log shows what was shared.
---

When an AI agent works with your finances, what it reads into its
conversation may be sent to its model provider. Purser lets the agent ask for
less: on any call, it can tell Purser to leave out payee names, descriptions
or individual amounts that the question does not need. Purser removes them
before the answer leaves your server, says what it removed, and records the
choice in the audit log.

This is the agent's choice, made per call. There is no setting to turn on,
and nothing is left out unless the call asks.

## How it works

1. You ask your agent something, for example *"How many times did we eat out
   this month?"*
2. The answer needs a count, not the names of the restaurants or what each
   meal cost. So the agent adds `withhold: ["payee", "description", "amount"]`
   to its call.
3. Purser builds the full answer, then removes those fields before sending
   it: each payee becomes a stable reference such as `py_8k2mq4x7ra`,
   descriptions and individual amounts are dropped, and the response lists
   what it left out in `withheld`.
4. The agent answers from what is left (the count, the category, the
   dates), and can still act on the rows by reference.
5. If you want to see the rows themselves, the agent sends you a link
   (`present`) that opens them in Purser. The details reach you without
   passing through the model.
6. The audit log marks the call **Privacy used** and lists the fields that
   were withheld. A call that returned payees, descriptions or amounts is
   marked **Identifiable** instead.

The same `withhold` choice works over MCP and over the REST API.

## Why the agent decides

Most financial questions need some detail. *"Who did we pay most at
restaurants?"* needs payee names; *"Was the electric bill higher than
usual?"* needs amounts. A switch that always hid those fields would be
overridden on almost every call, and would suggest a protection it does not
give. The agent knows the question, so it makes the choice:

- When the question or action does not need a field, the agent withholds it.
- When it does, the agent reads the data normally.

The tool guidance Purser gives agents (the MCP instructions, `describe`, the
tool descriptions, `GET /llms.txt` and the Purser skill) tells them to
withhold what a question does not need.

This is a privacy aid, not an access control: an agent that can read can
always read more on its next call. To limit what an agent can do, give it a
token with fewer [scopes](../security/).

## What can be withheld

| Field | What the call leaves out | What stays |
| --- | --- | --- |
| `payee` | Payee names. Each payee becomes a stable reference such as `py_8k2mq4x7ra`, the same on every call. | The reference, which the agent can filter and act with. |
| `description` | Bank descriptions, posting memos, bill (schedule) names and rule text. | Accounts and categories: you name them, and they carry most of the meaning. |
| `amount` | Individual amounts: a row's amount, posting amounts, running balances (they give each row's amount away by subtraction) and individual bill or forecast amounts. | Totals and balances. |

Totals are always exact: spending totals, account balances, net worth,
balance history and the forecast's balances. When the question is about a
total, the total is the answer, and asking for it is the disclosure choice.

Amounts are either exact or left out, never rounded or made up: a number that
is not true is something an agent might reason from or repeat.

## Examples

A count that needs no names or amounts, over MCP:

```json
{
  "name": "search_transactions",
  "arguments": {
    "category": "expenses.dining",
    "start": "2026-11-01",
    "withhold": ["payee", "description", "amount"]
  }
}
```

The answer keeps the rows, their dates and categories, and names what it
left out:

```json
{
  "items": [
    {
      "id": "0b5c4c86-5b1e-4f7a-9a43-2f1d8f1c2a10",
      "date": "2026-11-14",
      "payee": "py_8k2mq4x7ra",
      "category": "expenses.dining",
      "status": "posted"
    }
  ],
  "total": 1,
  "withheld": ["amount", "description", "payee"]
}
```

The same request over the REST API, where `withhold` is a query parameter
(repeat it, or separate the fields with commas):

```sh
curl -H "Authorization: Bearer $PURSER_TOKEN" \
  "http://purser.local:8000/transactions?category=expenses.dining&start=2026-11-01&withhold=payee,description,amount"
```

A question that needs the details, such as *"Who did we pay most at
restaurants this month?"*, sends no `withhold` and gets the payee names.

## Every omission is named

A response says what it withheld, for example
`"withheld": ["amount", "payee"]`, on each item of a list or on the response
itself. An agent that saw a field simply missing would conclude the value is
unknown and might try to fix it. A response that left nothing out has no
`withheld` key.

## Working by reference

Payee references and transaction ids work as inputs everywhere a payee or a
row is named: `search_transactions(payee=...)`, `update_transaction`,
`propose_rule`, `match`, links on financial notes, and the REST equivalents
(`GET /transactions?payee=py_8k2mq4x7ra`, a `payee` field in a JSON body).
Purser resolves them on the server. A payee name you typed yourself is still
accepted as a filter, because you disclosed it, not Purser.

A reference is never accepted inside free text (a description, a memo, a
note, a name), so a reference cannot turn into a payee's real name. A
reference stays the same when the encryption key is rotated, so an agent's
long-term notes about it stay valid.

## Showing you the details: present

`present(transaction_ids, note)` (MCP) and `POST /transactions/present`
(REST, with `{"transaction_ids": [...], "note": "..."}`) return a link that
opens those rows, with full detail, in Purser for you when you are signed in.
The agent can reply "your four largest charges this week" followed by the
link, and the values never pass through its model. The link carries only
transaction ids and the note, and an API token cannot open it.

## Where the data goes

Data an agent reads into its conversation may reach its model provider. An
agent that sends API output straight to a file or another program, without
reading it into the conversation, keeps it out; an agent running on a local
model has no outside provider at all. Purser cannot see where data goes after
it leaves: the audit log records what Purser served and to which credential,
not what the agent or provider did with it.

## What Purser itself sends to a model provider

Purser has optional AI features of its own, off by default: categorizing new
transactions, and a review pass that checks them after a sync. They run on
the server, separately from any agent, and send data to the provider you
choose in **Settings -> Categories -> AI categorization**. Each request
carries one transaction.

To categorize a transaction that no rule or earlier choice covers, Purser
sends:

- its payee, description and amount
- your categories and their descriptions
- the names and paths of your open accounts, offered as transfer targets
- your financial notes that apply everywhere, and the ones linked to its account or payee

With the review pass on (`PURSER_AI_REVIEW`), to review a transaction, Purser
sends:

- its Purser id, date, account, payee, description, amount, status and origin
- its category, what set it, and the other account of a transfer
- its review flags, and the ids of a trade that holds it
- your financial notes that apply everywhere, and the ones linked to its account or payee
- your reviewer checklist

Nothing else is sent: no balances, no other transactions, and never your bank
login or Purser's encryption key. The settings card shows the same list, and
Purser's tests keep it in step with what the prompts contain.

Where it goes depends on the provider you choose:

- **Local model (OpenAI-compatible):** a server you run, such as Ollama, LM
  Studio, vLLM or a llama.cpp server. With the model on your own hardware,
  the data stays on your network. **Test connection** warns when a local
  provider's address is on the public internet. See
  [Use a local model](../../how-to/local-model/).
- **Hosted OpenAI-compatible service** or **Anthropic:** the data leaves your
  network, and that provider's terms apply.

## External account identifiers

The bank's account and connection identifiers are a separate, security
default. MCP tools leave them out unless a call passes
`include: ["external_ids"]`. The REST API leaves them out for API tokens
without the `admin` scope. You can always pass a reference to name a lot or
an award.

## What the audit log records

Every MCP tool call, MCP resource read and REST read by an integration has a
privacy record in **Settings -> Audit log > Data shared**:

- **Privacy requested:** the fields the call asked to withhold. The entry
  shows a **Privacy used** badge.
- **Withheld from this answer:** the fields the response actually left out.
- **Identifiable data returned:** the payee, description and amount fields the
  response returned with real values. The entry shows an **Identifiable**
  badge.

The **Privacy use** filter narrows the view to calls that used privacy or to
calls that returned identifiable data. The same filters are on the API
(`GET /audit/activity?privacy=requested` or `privacy=identifiable`;
`GET /audit?privacy_requested=true` or `identifiable=true`), and the CSV
export has `privacy_requested` and `identifiable` columns.

The identifiable record comes from the response's field names and each
route's declared fields; a payee name inside free text is not always
detected. Entries written before this record existed show only the fields
withheld. Authorization means the integration had permission to act; it does
not mean you approved each read. For evidence, retention and capture
failures, see [Security](../security/).

## MCP, REST and the web UI

Every MCP tool and REST read route that can return payees, descriptions or
individual amounts takes `withhold`; the MCP tool catalog and the REST API
reference list it on each one. Write responses echo what was written and are
not scrubbed. Purser's own web pages always show you full detail: the web app
never passes `withhold`.

The authoritative design is section 9 of DESIGN.md.

## Upgrading

Earlier versions had two user settings that withheld payees and amounts from
every MCP answer until the agent asked for them back. They have been removed:
`PURSER_PRIVACY_PURCHASE` and `PURSER_PRIVACY_AMOUNT` are ignored (Purser logs
a warning when either is still set), and stored values are deleted when the
database is upgraded. An agent that followed the old guidance and passes
`include: ["payee"]`, `["description"]` or `["amount"]` gets an error: the
tools that return external identifiers (`list_accounts`, `list_lots`,
`list_awards`, `read_series`) say to use `withhold`, and every other tool
refuses `include` as an unknown argument. Update the agent's instructions or
the Purser skill.
