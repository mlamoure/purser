---
title: Encryption at rest
description: What Purser encrypts in its database, what it leaves readable and why, how the key is checked and rotated, and what a database dump reveals.
---

Purser encrypts the identifying parts of your financial data inside its
database, with a key that never enters the database. This page explains what
is encrypted, what is not and why, and what that means for anyone holding a
copy of the database. The authoritative specification is
[`DESIGN.md`](https://github.com/mlamoure/purser/blob/main/DESIGN.md)
section 9.

## The key

Encryption is required. Purser uses
[Fernet](https://github.com/fernet/spec/blob/master/Spec.md): AES-128 in CBC
mode for confidentiality, with an HMAC-SHA256 tag so that altered ciphertext is
rejected rather than decrypted. The key is `PURSER_ENCRYPTION_KEY`, generated
with `purser keygen`.

- **Every role command needs it.** `purser serve`, `purser worker` and
  `purser migrate` refuse to start when the key is missing or malformed.
- **It is never stored in the database or in a backup.** Purser holds it only
  in the process environment. The setup wizard shows it once so that you can
  record it; store it apart from your backups, for example in a password
  manager.
- **It can hold several keys.** Separate them with commas: the first key
  encrypts everything new, and any listed key can decrypt. This is how
  rotation works (below).

Losing the key loses the encrypted data. Purser does not start without it, and
nothing can decrypt the archived bank data or the descriptive text without it.

## The canary: a wrong key fails closed

When its migrations first set up the database, Purser stores an
`encryption_canary` row: a known value encrypted with the key, and a short
fingerprint of the key (a truncated SHA-256 hash, which identifies the key
without revealing it). Every
start decrypts that row before serving anything. With a wrong key, Purser
stops with an error naming the fingerprint the database expects and the
fingerprint of the key it was given, instead of running against data it cannot
read or, worse, writing new data under a different key.

## What is encrypted

Purser encrypts each value in the application before it reaches PostgreSQL,
and decrypts it only when a request or job needs it. Encrypted values are never
written to logs.

- **Bank data:** the archive of every payload received from the bank feed, and
  the SimpleFIN access URLs, which are the credentials to your bank data.
- **Descriptive text (the "narrative layer"):** payee names, transaction
  descriptions, posting memos, schedule names, financial notes,
  rules' match text, payee names and notes, suggestion drafts, evidence and
  decision notes, the notes and history on bill estimates, and the notes,
  plans and references on holdings entered in Purser, tax lots and equity awards.
- **Audit and events:** the before and after detail of audit entries, the
  recorded authorization details of tokens, and the payloads of the event
  stream.
- **Secrets:** webhook signing secrets, and the secret settings (the AI
  provider API key, the OIDC client secret and the price provider API key),
  which are also write-only through the API.

## What stays readable, and why

The numbers stay plaintext: amounts, dates, statuses, currencies, balances,
category and account paths, account names, holdings and prices, and the ids
that link records. Every total, balance, forecast and ledger check is computed
by the database on these columns, and PostgreSQL cannot compute on values only
the application can decrypt. For the same reason, a direct database reader,
such as a reporting connection, sees these columns and only ciphertext for the
encrypted ones.

User emails and display names are also plaintext. Passwords and API tokens are
stored as one-way hashes, not encrypted values.

## What a database dump reveals

Assume someone has a dump of the database but not the key. They can see:

- your accounts, their names and structure, and their balances over time;
- every transaction's amount, date and category, so how much you spend in each
  category and when;
- your holdings, prices and net worth;
- schedules' amounts and timing, without their names;
- which transactions share a payee, without the payee's name.

They cannot read who you paid, what a transaction was for, your notes, the
bank's original records, your bank credentials or your secrets. Treat a dump as
sensitive financial data all the same: write it readable only by you and keep
it somewhere private.

## Searching and deduplicating encrypted data

Two things must still work on encrypted text: finding a payee by name, and
recognising a transaction that has already been imported.

- A **blind index** stores a keyed hash (HMAC-SHA256) of each normalized payee
  name next to the encrypted name. Purser finds a payee by computing the same
  hash, without decrypting every row. Equal names have equal hashes, which is
  why a dump shows which transactions share a payee.
- Each transaction's **content hash**, which makes imports idempotent, is
  keyed the same way. Without the key, someone holding a dump cannot confirm a
  guessed transaction (a date, an amount and a description) by hashing it.
- The stable payee references given to agents when payee names are withheld
  (`py_...`, see [Privacy and agents](../privacy-and-agents/)) are keyed hashes
  too.

These hashes use three separate random keys, one per purpose, each stored in
the database wrapped (encrypted) by `PURSER_ENCRYPTION_KEY`. Because they are
random rather than derived from your key, rotating your key only rewraps them:
every index, content hash and payee reference stays byte-identical.

Searching descriptions needs the decrypted text, so the Purser server keeps a
decrypted copy of each transaction's searchable text (description, memos and
payee name) in its memory. It is built in the background when the server
starts, kept up to date as rows arrive, and never written to disk or to the
database, so a dump or a backup holds nothing more than before. A search finds
the matching rows in memory, and the database then applies the account, date
and status filters. Until that copy is ready, a search decrypts the candidate
rows one by one instead: the same results, only slower. Encryption at rest
protects copies of the database, not the running server, which holds your key
all the time it runs.

## Rotating the key

Rotate with the guarded `purser admin rotate-key` command, run with the new key
first and the old key second in `PURSER_ENCRYPTION_KEY`. It re-encrypts every
encrypted value under the new key in one transaction, without exposing the
plaintext, and moves the canary to the new key. Then remove the old key from
the variable. See
[Rotate the encryption key](../../how-to/backup-restore/#rotate-the-encryption-key)
and the [CLI reference](../../reference/cli/#purser-admin-rotate-key).

## What Purser does not protect

Encryption at rest protects copies of the database: dumps, backups and stolen
database files. It does not protect against:

- **Someone with the key.** Anyone who can read your `.env` file or the
  container's environment (for example with `docker inspect`) holds the key.
  Restrict who can administer the host.
- **A token or a signed-in session.** Purser decrypts data for whoever is
  authorized to read it. What an agent receives is governed by its token's
  [scopes](../security/) and by what the agent asks Purser to `withhold` on
  each call, not by encryption.
- **The plaintext columns on disk.** Encrypting the disk or the volume that
  holds the database, and the disks that hold your backups, is outside
  Purser's control and still recommended.
