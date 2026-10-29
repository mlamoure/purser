---
title: Backup and restore
description: Back up the single Postgres database, keep the encryption key safe separately, rotate keys, and run a restore drill.
---

Purser keeps everything — ledger, append-only raw archive, config, audit — in
**one PostgreSQL database**. Backup and restore are therefore ordinary Postgres
operations, with one rule that matters more than any other.

## The one rule: the key is not in the backup

Data at rest is encrypted, and `PURSER_ENCRYPTION_KEY` is **not** stored in the
database. A database dump contains ciphertext and the wrapped data-encryption
keys, but not the master key that unwraps them. **A backup is useless without
the key.** Record the key separately — a password manager, not the same bucket
as your dumps — or your archived bank history is unrecoverable. This is the same
key the setup wizard reveals once at install.

## Back up

```bash
umask 077   # the dump is readable by you alone
pg_dump --format=custom --file=purser-2026-07-17.dump \
  "postgresql://purser@db.example.com:5432/purser"
```

Amounts, dates and account names are plaintext in a dump (see below), so keep
dumps private: `umask 077` before writing one, and a private bucket for copies.

That single dump is the whole instance. Store the encryption key alongside your
records of *where* the dumps live — never inside them.

## What is and is not encrypted

Encrypted at rest (ciphertext in the dump): the raw bank envelopes, SimpleFIN
access URLs, webhook secrets, the AI and OIDC config secrets, and the
**narrative layer** — payee names, transaction descriptions, posting memos,
schedule names, audit before/after detail, and event payloads.

Left as plaintext (the numeric skeleton every query computes on): amounts,
dates, statuses, ltree category paths, ids, and the keyed content hashes and
blind indexes that make dedup and search work over ciphertext.

Encrypting the database *files* themselves is a storage and deployment concern,
outside Purser's control.

## Rotate the encryption key

Rotate with the guarded CLI. Run with **both** keys configured — the new one
first — so ciphertext re-wraps under the new key without ever exposing
plaintext, and blind indexes and content hashes stay byte-identical:

```bash
export PURSER_ENCRYPTION_KEY="<new-key>,<old-key>"
purser admin rotate-key
```

Then drop the old key from the variable. See
[`purser admin rotate-key`](../../reference/cli/#purser-admin-rotate-key) for detail.

## Restore drill

Practice this before you need it:

1. Provision an empty PostgreSQL 16 database.
2. Before restoring into a fresh cluster, create the database owner/login used
   by the application and the read-model role: `CREATE ROLE purser_read NOLOGIN;`.
   A database dump does not include cluster roles. If this role already exists,
   preserve it. Restore any separately managed reporting login configuration
   from your secure operational records; do not enable a new login by default.
3. `pg_restore --exit-on-error --dbname=... purser-2026-07-17.dump`.
4. Start Purser with the **same** `PURSER_ENCRYPTION_KEY` set on the process
   (`PURSER_DATABASE_URL` pointing at the restored database).

At startup Purser checks an `encryption_canary` row that binds the key to the
database: a wrong or missing key **fails closed** before serving anything, naming
both fingerprints. A clean `GET /healthz` (`{"status":"ok","database":"ok"}`)
confirms startup and database connectivity, not the completeness of a restore.
Compare account balances and historical totals with the original instance,
open encrypted descriptions, check record counts and raw archives, and verify
that importing the same data again creates no duplicates. Do not treat health
alone as proof that your financial data has been recovered.

## Upgrade and roll back

Upgrades move the database schema forward, and there is no downgrade. Your
rollback point is a backup taken **before** the upgrade, together with the
image you were running and the same key.

1. Note the image you run now (the exact tag or digest), and keep your key.
2. Take a backup (see [Back up](#back-up)) and check it restores, ideally into a
   scratch database.
3. Change the image to the new version and restart. Migrations run
   automatically at startup. The API (`serve`) and the `worker` may start at the
   same time: they take turns on a database lock, so the migration runs once.
4. Check the instance as in the restore drill: balances, historical totals and
   encrypted descriptions match what you saw before.

To roll back, restore the pre-upgrade backup into a **fresh** database (the
upgraded one has the newer schema) and start the image you noted, with the same
key. Anything written after that backup is lost, so schedule upgrades when no
import or agent work is running, and take the backup immediately before.

Agent tokens carry over an upgrade. A token issued with a scope that a newer
version retired is converted to its replacement during the upgrade, keeps
working, and the conversion is recorded in the audit log.

The authoritative spec for the encryption model is
[`DESIGN.md`](https://github.com/mlamoure/purser/blob/main/DESIGN.md)
section 9; see also [Configuration](../../reference/configuration/) for the key.
