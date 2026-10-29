"""Retention: audit.prune_expired batching + audit_retention_days config."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from purser import appsettings, audit


def _seed(session: Session, *, age_days: int, action: str) -> None:
    session.execute(
        text(
            "INSERT INTO audit_log (at, action, entity_type, kind)"
            " VALUES (:at, :action, 'system', 'read')"
        ),
        {"at": datetime.now(UTC) - timedelta(days=age_days), "action": action},
    )


def _count(session: Session, action: str) -> int:
    return session.execute(
        text("SELECT count(*) FROM audit_log WHERE action = :a"), {"a": action}
    ).scalar_one()


def test_prune_deletes_only_expired_rows(clean_db: None, db_session: Session) -> None:
    _seed(db_session, age_days=100, action="prune.old")
    _seed(db_session, age_days=100, action="prune.old")
    _seed(db_session, age_days=2, action="prune.new")
    db_session.commit()

    deleted = audit.prune_expired()
    assert deleted == 2
    assert _count(db_session, "prune.old") == 0
    assert _count(db_session, "prune.new") == 1


def test_prune_respects_configured_retention(clean_db: None, db_session: Session) -> None:
    appsettings.set_values(db_session, {"audit_retention_days": 7}, actor_token_id=None)
    db_session.commit()
    appsettings.invalidate()
    try:
        _seed(db_session, age_days=10, action="prune.tenday")
        _seed(db_session, age_days=3, action="prune.threeday")
        db_session.commit()

        deleted = audit.prune_expired()
        assert deleted == 1
        assert _count(db_session, "prune.tenday") == 0
        assert _count(db_session, "prune.threeday") == 1
    finally:
        appsettings.reset_value(db_session, "audit_retention_days", actor_token_id=None)
        db_session.commit()
        appsettings.invalidate()


def test_prune_batches(
    clean_db: None, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(audit, "_PRUNE_BATCH", 3)
    for n in range(8):
        _seed(db_session, age_days=95, action=f"prune.batch.{n}")
    db_session.commit()

    assert audit.prune_expired() == 8
