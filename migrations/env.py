from alembic import context
from sqlalchemy import Connection, create_engine

from purser.config import get_settings


def _database_url() -> str:
    configured = context.config.get_main_option("sqlalchemy.url")
    return configured or get_settings().database_url


def run_migrations_offline() -> None:
    context.configure(url=_database_url(), literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def _run_on(connection: Connection) -> None:
    context.configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # `purser migrate/serve/worker` hand over the connection that holds the
    # migration advisory lock (purser/cli.py run_migrations, issue #215), so
    # every revision runs on the locked session. Plain `alembic` invocations
    # and tests that drive alembic directly build their own engine.
    handed_over = context.config.attributes.get("connection")
    if handed_over is not None:
        _run_on(handed_over)
        return
    engine = create_engine(_database_url())
    with engine.connect() as connection:
        _run_on(connection)
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
