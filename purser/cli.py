"""Role commands: one image, multiple roles (serve / worker / migrate)."""

import time
from pathlib import Path
from typing import Annotated

import typer
import uvicorn
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.pool import NullPool

from purser import crypto
from purser.config import get_settings
from purser.logging import configure_logging, get_logger

app = typer.Typer(help="Purser — the finance backend for the personal agent stack.")

admin_app = typer.Typer(help="Guarded remediation commands (offline; back up first).")
app.add_typer(admin_app, name="admin")

log = get_logger(__name__)

# Session-level advisory lock serializing startup migrations (issue #215):
# `serve` and `worker` both migrate at startup, so roles started together (the
# split compose file, the Helm chart's worker) would otherwise race two Alembic
# upgrades. Any fixed bigint works as long as every purser process uses the
# same one; Postgres scopes advisory locks to the current database.
MIGRATION_LOCK_KEY = 0x7075727365720001  # ASCII "purser", then 0x0001


def _require_encryption_key() -> None:
    """Every role fails fast, pre-bind, without a usable key (issue #93)."""
    try:
        crypto.ensure_key()
    except (crypto.EncryptionKeyMissing, crypto.EncryptionKeyInvalid) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc


def _acquire_migration_lock(connection: Connection) -> None:
    """Blocking acquire: a process that loses the race waits here until the
    winner's upgrade has committed, then finds the schema at head."""
    key = {"key": MIGRATION_LOCK_KEY}
    if not connection.execute(text("SELECT pg_try_advisory_lock(:key)"), key).scalar_one():
        log.info("migration lock held by another purser process; waiting for it")
        connection.execute(text("SELECT pg_advisory_lock(:key)"), key)
    # The lock is session-level, so it outlives this commit; the upgrade then
    # opens a fresh transaction whose snapshot sees the winner's schema.
    connection.commit()


def _release_migration_lock(connection: Connection) -> None:
    connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": MIGRATION_LOCK_KEY})
    connection.commit()


def run_migrations(database_url: str | None = None) -> None:
    """Upgrade the database to head under the migration lock, then check the
    encryption canary. ``database_url`` defaults to PURSER_DATABASE_URL.

    The lock and the upgrade share one connection: it is handed to
    migrations/env.py via ``cfg.attributes``, so the lock is held by the very
    session that runs every revision (env.py runs them all in one transaction)
    and is released in ``finally`` only after that transaction has committed
    or rolled back. NullPool makes closing the connection end the session, so
    the lock can never linger in a connection pool.
    """
    from alembic import command
    from alembic.config import Config

    # Repo layout first; fall back to the copy packaged into the wheel so
    # wheel installs (`uvx --from git+<repo-url> purser` — not on PyPI)
    # can migrate without a checkout (issue #15).
    root = Path(__file__).resolve().parent.parent
    ini = root / "alembic.ini"
    migrations = root / "migrations"
    if not migrations.exists():
        packaged = Path(__file__).resolve().parent / "_migrations"
        if packaged.exists():
            ini = packaged / "alembic.ini"
            migrations = packaged / "migrations"
    cfg = Config(str(ini))
    cfg.set_main_option("script_location", str(migrations))

    engine = create_engine(database_url or get_settings().database_url, poolclass=NullPool)
    try:
        with engine.connect() as connection:
            _acquire_migration_lock(connection)
            try:
                cfg.attributes["connection"] = connection
                command.upgrade(cfg, "head")
                # env.py has committed already; committing again pins the order
                # "commit, then unlock" whatever its transaction handling becomes.
                connection.commit()
            except BaseException:
                connection.rollback()  # leave no aborted transaction for the unlock
                raise
            finally:
                _release_migration_lock(connection)

            # Wrong-key detection: the canary was encrypted under the key this
            # database was initialized with — fail closed before serving anything.
            crypto.verify_canary(connection)
    finally:
        engine.dispose()


def _run_migrations_checked() -> None:
    try:
        run_migrations()
    except crypto.EncryptionKeyMismatch as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command()
def keygen() -> None:
    """Print a fresh Fernet key for PURSER_ENCRYPTION_KEY."""
    from cryptography.fernet import Fernet

    typer.echo(Fernet.generate_key().decode())


@app.command()
def migrate() -> None:
    """Apply database migrations to head."""
    _require_encryption_key()
    configure_logging(get_settings().log_level)
    _run_migrations_checked()
    log.info("migrations applied")


@app.command()
def serve() -> None:
    """Run migrations, then serve the API with the in-process scheduler."""
    _require_encryption_key()
    settings = get_settings()
    configure_logging(settings.log_level)
    _run_migrations_checked()
    log.info("migrations applied")

    scheduler = None
    if not settings.disable_scheduler:
        from purser.scheduler import build_scheduler

        scheduler = build_scheduler()
        scheduler.start()
    try:
        uvicorn.run(
            "purser.api.app:create_app",
            factory=True,
            host=settings.host,
            port=settings.port,
            log_config=None,
        )
    finally:
        if scheduler is not None:
            scheduler.shutdown(wait=False)


@app.command()
def seed_demo(
    force: bool = typer.Option(
        False,
        "--force",
        help="Seed even though the database already holds user data "
        "(real accounts, transactions, or connections). The run is audited.",
    ),
) -> None:
    """Seed a small realistic demo dataset (accounts, two months of
    transactions, a transfer pair, one recurring bill). Skips silently when
    the database already holds user data unless --force is given."""
    configure_logging(get_settings().log_level)
    from purser.seed import seed_demo_command

    seed_demo_command(force=force)


@app.command()
def worker() -> None:
    """Run only the scheduled jobs (optional role split; same image)."""
    _require_encryption_key()
    settings = get_settings()
    configure_logging(settings.log_level)
    _run_migrations_checked()

    from purser.scheduler import build_scheduler

    scheduler = build_scheduler()
    scheduler.start()
    log.info("worker started")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        scheduler.shutdown(wait=False)


@admin_app.command("delete-account")
def delete_account(
    ref: str = typer.Argument(..., help="Account path or UUID to hard-delete."),
    yes: bool = typer.Option(
        False, "--yes", help="Execute the delete (default is a dry-run pre-flight report)."
    ),
) -> None:
    """Hard-delete a DUPLICATE account and free its content hashes for re-import.

    Dry-run by default: prints a pre-flight report and changes nothing. Pass
    --yes to execute. Refuses on virtual / SimpleFIN-attached / parent accounts.
    """
    _require_encryption_key()
    configure_logging(get_settings().log_level)
    _run_migrations_checked()

    from purser.admin.account_delete import run_delete

    run_delete(ref, execute=yes)


@admin_app.command("purge-feed-rows")
def purge_feed_rows(
    ref: str = typer.Argument(..., help="Account path or UUID whose feed rows to purge."),
    since: str | None = typer.Option(
        None, "--since", help="Only purge rows dated on/after this ISO date."
    ),
    yes: bool = typer.Option(
        False, "--yes", help="Execute the purge (default is a dry-run pre-flight report)."
    ),
) -> None:
    """Hard-delete an account's FEED-ORIGIN rows so the archive can re-derive them.

    The repair half-step for wrong-imported feed data (e.g. a sign-inverted
    stream, issue #170): purge frees the content hashes, then
    `replay-archive` re-imports from raw_fetches — with `feed_invert` set,
    rows re-land corrected under their original identities. QIF/manual/agent
    rows are never touched. Dry-run by default.
    """
    _require_encryption_key()
    configure_logging(get_settings().log_level)
    _run_migrations_checked()

    from datetime import date as date_type

    from purser.admin.feed_repair import run_purge

    run_purge(ref, since=date_type.fromisoformat(since) if since else None, execute=yes)


@admin_app.command("replay-archive")
def replay_archive(
    connection_id: str = typer.Argument(..., help="Connection UUID whose archive to replay."),
    since: str | None = typer.Option(
        None, "--since", help="Only replay fetches archived on/after this ISO date."
    ),
    yes: bool = typer.Option(
        False, "--yes", help="Execute the replay (default reports the plan only)."
    ),
) -> None:
    """Re-import a connection's archived payloads, oldest first.

    Content-hash idempotent: rows already in the ledger skip, purged rows
    re-land. Commits per fetch, so an interrupted replay keeps its progress
    and a re-run is safe.
    """
    _require_encryption_key()
    configure_logging(get_settings().log_level)
    _run_migrations_checked()

    from datetime import date as date_type

    from purser.admin.feed_repair import run_replay

    run_replay(connection_id, since=date_type.fromisoformat(since) if since else None, execute=yes)


@admin_app.command("purge-retired-rows")
def purge_retired_rows(
    ref: Annotated[
        str, typer.Argument(help="Account path or UUID whose retired-origin rows to purge.")
    ],
    batch: Annotated[
        list[str] | None,
        typer.Option(
            "--batch",
            help="A creation batch to purge: its created_at timestamp exactly as the "
            "listing prints it (repeatable). Omit to list the account's batches.",
        ),
    ] = None,
    unlink: Annotated[
        bool,
        typer.Option(
            "--unlink",
            help="Unlink live matches and merged transfers holding batch rows first, "
            "instead of refusing.",
        ),
    ] = False,
    apply: Annotated[
        bool,
        typer.Option("--apply", "--yes", help="Execute the purge (default is a dry run)."),
    ] = False,
) -> None:
    """Remove one creation batch of import rows that have no source fetch.

    For rows a retired ingest path left behind once its archive was purged
    (issue #284): `purge-feed-rows` selects rows by fetch and cannot reach
    them. Without --batch, lists the account's batches. With --batch, a dry
    run prints counts and a sample; --apply voids the pending rows through
    the normal void path and hard-deletes the posted ones through the guarded
    delete, audited. Manual QIF/CSV imports are batches too — name only the
    batch you mean.
    """
    _require_encryption_key()
    configure_logging(get_settings().log_level)
    _run_migrations_checked()

    from purser.admin.retired_rows import run_purge_retired

    run_purge_retired(ref, batch_values=batch or [], unlink=unlink, execute=apply)


@admin_app.command("rotate-key")
def rotate_key() -> None:
    """Re-encrypt every encrypted column under the new primary key (#137).

    Run with both keys configured (`PURSER_ENCRYPTION_KEY="<new>,<old>"`);
    MultiFernet re-wraps each ciphertext under the new primary without exposing
    plaintext. Blind indexes and content hashes stay byte-identical (the wrapped
    DEK rows are re-wrapped, not re-derived). Drop the old key afterwards.
    """
    _require_encryption_key()
    configure_logging(get_settings().log_level)

    from purser.admin.rotate_key import run_rotate

    run_rotate()


if __name__ == "__main__":
    app()
