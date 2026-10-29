---
title: CLI
description: One image, multiple roles — purser serve, worker, migrate, plus keygen, seed-demo, and guarded admin commands.
---

The `purser` command is the single entrypoint for every role. All role
commands **fail fast without a valid `PURSER_ENCRYPTION_KEY`**, and a
wrong-key canary check fails closed before anything serves.

## Role commands

### purser serve

Apply migrations, then serve the API, the MCP endpoint, the admin SPA, and
the in-process job scheduler. The default single-process deployment.

### purser worker

Run only the scheduled jobs (sync, matching, forecasting, retention). For
the optional role split: run `serve` with `PURSER_DISABLE_SCHEDULER=true`
alongside one `worker` — same image, same configuration.

### purser migrate

Apply database migrations to head and exit. `serve` and `worker` migrate on
startup, so this exists for init containers and manual control.

Every role holds a Postgres advisory lock while it upgrades, so processes that
start together (a `serve` + `worker` split) take turns instead of racing: the
later one waits, logs that it is waiting, then finds the schema already at
head.

## Utilities

### purser keygen

Print a fresh Fernet key for `PURSER_ENCRYPTION_KEY`.

### purser seed-demo

Seed a small realistic demo dataset: a few accounts, two months of synthetic
transactions, a transfer pair, one recurring bill and one card-payment
transfer schedule — enough to exercise
matching, forecasting, and every read tool without a bank connection.

Skips silently when the database already holds user data — any real
(non-category) account or any transaction. `--force` seeds anyway and writes
a `seed.force` audit row.

## Guarded admin commands

Offline remediation. Back up first.

### purser admin delete-account

```bash
purser admin delete-account <path-or-uuid>        # dry-run pre-flight report
purser admin delete-account <path-or-uuid> --yes  # execute
```

Hard-delete a **duplicate** account and free its content hashes for
re-import. Dry-run by default; refuses virtual, SimpleFIN-attached, and
parent accounts. The append-only raw feed archive is never touched. This is
deliberately a CLI, not an API — no token scope can delete an account.

### purser admin purge-feed-rows

```bash
purser admin purge-feed-rows <path-or-uuid>                      # dry-run report
purser admin purge-feed-rows <path-or-uuid> --since 2026-05-01   # bounded
purser admin purge-feed-rows <path-or-uuid> --yes                # execute
```

Hard-delete an account's **feed-origin** rows (any status) so the archive
can re-derive them — the repair half-step for feed data that imported wrong,
such as a sign-inverted stream. QIF, manual, and agent rows are never
touched; merge-linked transfers unlink first so the counterpart's bank line
survives; the raw feed archive stays whole. Dry-run by default. Follow with
`replay-archive`.

### purser admin replay-archive

```bash
purser admin replay-archive <connection-uuid>                    # plan only
purser admin replay-archive <connection-uuid> --since 2026-07-01 # bounded
purser admin replay-archive <connection-uuid> --yes              # execute
```

Re-import a connection's archived payloads, oldest first. Content-hash
idempotent: rows already in the ledger skip, purged rows re-land — with
`feed_invert` now set on an account, its rows re-land sign-corrected under
their original identities. A posted row that a fetch carried more than a week
back under a new description, beside an original the ledger holds, skips as
re-described, just as the sync skipped it (`skipped_redescribed` in the
summary). Commits per fetch, so an interrupted replay keeps its progress and
re-running is safe.

A replay re-derives transactions only. A loan or property balance and a
brokerage's holdings describe the moment each fetch was made, and the live
sync already recorded them, so a replay records no valuations and no
holdings snapshots.

### purser admin purge-retired-rows

```bash
purser admin purge-retired-rows <path-or-uuid>                                # list batches
purser admin purge-retired-rows <path-or-uuid> --batch <created_at>           # dry-run report
purser admin purge-retired-rows <path-or-uuid> --batch <created_at> --apply   # execute
```

Remove import rows that have **no source fetch**: rows an ingest path that
no longer exists left behind, whose archived payloads are gone, so
`purge-feed-rows` — which selects rows by fetch — cannot reach them. Manual
QIF and CSV imports have no source fetch either, so the unit of removal is a
**creation batch**: every row one import commits shares that transaction's
`created_at` timestamp.

Without `--batch`, the command lists the account's batches, oldest first,
each with its row count by status, its date range, and the audit actions
recorded at the same instant — `qif.import` or `csv.import` marks a manual
import. The audit trail is pruned after `audit_retention_days`, so an old
batch may show none. Name the batch you mean with `--batch`, copying the
timestamp exactly as listed (repeat the flag for several); the dry run then
prints the counts and a sample of the rows.

`--apply` (alias `--yes`) removes the batch by the ledger's own rules:

- **pending** and **expected** rows are voided through the normal void path
  and stay as lineage;
- **posted** rows are hard-deleted through the same guarded delete
  `purge-feed-rows` uses;
- rows already **void** are left alone.

A row with a live link refuses unless you pass `--unlink`: a proposed match
is rejected, a confirmed match is undone (the bill it settled is expected
again), and a merged transfer is unlinked — the other account's bank line
comes back on its own account while this account's side goes with the batch.
A row that was corrected after import, or that posts to another real
account, always refuses. The apply run recomputes the plan and refuses if it
no longer matches what the dry run showed. Each run is audited and emits
`account.retired_rows_purged`; the raw feed archive is never touched.

### purser admin rotate-key

Re-encrypt every encrypted column under a new primary key. Run with both
keys configured — `PURSER_ENCRYPTION_KEY="<new>,<old>"` — and ciphertexts
re-wrap without exposing plaintext; blind indexes and content hashes stay
byte-identical. Drop the old key afterwards.
