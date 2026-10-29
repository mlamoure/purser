"""First-admin creation serializes across independent requests/processes (#429)."""

import time
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

# A database lock, not a process-local mutex: multiple API replicas share it.
LOCK_NAMESPACE = 1347773011


def test_simultaneous_setup_has_exactly_one_administrator(
    clean_db: None, client: TestClient, migrated_engine: Engine
) -> None:
    with migrated_engine.connect() as holder, ThreadPoolExecutor(max_workers=2) as pool:
        holder.execute(
            text("SELECT pg_advisory_xact_lock(:namespace, 1)"), {"namespace": LOCK_NAMESPACE}
        )
        futures = [
            pool.submit(
                client.post,
                "/auth/setup",
                json={"email": f"admin{i}@example.com", "password": "synthetic-setup-password"},
            )
            for i in range(2)
        ]
        try:
            # Observe both requests waiting for the same DB lock before releasing
            # either. This is a state-based concurrency test, not a race won by sleeps.
            deadline = time.monotonic() + 5
            waiting = 0
            while time.monotonic() < deadline:
                with migrated_engine.connect() as observer:
                    waiting = observer.execute(
                        text(
                            "SELECT count(*) FROM pg_locks WHERE locktype='advisory' "
                            "AND classid=:namespace AND objid=1 AND NOT granted"
                        ),
                        {"namespace": LOCK_NAMESPACE},
                    ).scalar_one()
                if waiting == 2:
                    break
                time.sleep(0.02)
            assert waiting == 2, "Setup requests bypassed the database bootstrap lock"
        finally:
            holder.rollback()
        responses = [future.result(timeout=10) for future in futures]
    assert sorted(response.status_code for response in responses) == [201, 409]
    with migrated_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM users")).scalar_one() == 1
        assert conn.execute(text("SELECT count(*) FROM users WHERE role='admin'")).scalar_one() == 1
