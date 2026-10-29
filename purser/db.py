"""SQLAlchemy engine and session plumbing."""

from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from purser.config import get_settings


@lru_cache
def get_engine() -> Engine:
    return create_engine(get_settings().database_url, pool_pre_ping=True)


@lru_cache
def get_sessionmaker() -> sessionmaker[Session]:
    maker = sessionmaker(bind=get_engine(), expire_on_commit=False)
    # Structural consumer for the balance watermark (decision 38): every
    # session from this maker recomputes stored running balances at commit
    # when a writer marked it dirty. Registered here so the app, jobs, CLI,
    # and tests all inherit it; Alembic binds its own connection and runs
    # the same recompute explicitly in its backfill.
    from purser import balances

    event.listen(maker, "before_commit", balances.on_before_commit)
    return maker


def get_session() -> Iterator[Session]:
    """FastAPI dependency yielding a request-scoped session."""
    with get_sessionmaker()() as session:
        yield session
