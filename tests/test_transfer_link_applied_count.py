"""A transfer_link rule counts the links it makes (issue #314).

``rules.applied_count`` and ``last_applied_at`` record a rule's applications.
Categorize and rename rules count once per row they shape; a transfer_link
rule counts once per link the auto-link pass makes because of it — the link
whose audit row and event carry ``signal: rule:<id>`` — in both stages
(standard and widened, decision 66) and both phases (absorb and pair). The
activation confirm's projection applies nothing, and unlinking does not take
an application back.

Synthetic data only. Every seeded date is a fixed day (offsets from DAY0 in
test_transfer_link_window), so nothing here depends on today (#262).
"""

from datetime import datetime
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from tests.test_matching import _imported
from tests.test_transfer_link_window import (  # noqa: F401 (fixture re-export)
    CARD,
    CHECKING,
    IN_TEXT,
    SAVINGS,
    _activate,
    _day,
    _merged_into,
    _pair,
    _rule,
    _sweep,
    seeded,
)


def _applied(engine: Engine, rule_id: str) -> tuple[int, datetime | None]:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT applied_count, last_applied_at FROM rules WHERE id = :id"),
            {"id": rule_id},
        ).one()
    return int(row.applied_count), row.last_applied_at


def _manual_transfer(client: TestClient, amount: str, day: int) -> str:
    """A transfer entered by hand, both legs unbacked: its card side is open
    for the bank row to be absorbed into."""
    resp = client.post(
        "/transactions/transfer",
        json={
            "from_account": CHECKING,
            "to_account": CARD,
            "amount": amount,
            "date": _day(day).isoformat(),
            "status": "posted",
        },
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


def _signals(client: TestClient, action: str) -> list[Any]:
    items = client.get("/audit", params={"action": action}).json()["items"]
    return [item["before_after"]["after"]["signal"] for item in items]


class TestTransferLinkAppliedCount:
    def test_each_link_the_rule_makes_counts_once_in_every_stage(
        self,
        seeded: dict[str, str],  # noqa: F811
        client: TestClient,
        migrated_engine: Engine,
    ) -> None:
        """Five links on the declared pair — a merge and an absorb in the
        standard stage, a merge and an absorb in the widened one, and a merge
        that also carries a name signal (the rule is credited first, as in the
        signal and the projection) — are five applications. A pair a name
        signal links on an undeclared account pair counts for no rule."""
        near_out, _ = _pair(client, migrated_engine, "120.00", 0, 2)  # standard merge
        far_out, _ = _pair(client, migrated_engine, "184.50", 10, 14)  # widened merge
        named_out, _ = _pair(client, migrated_engine, "99.99", 20, 21, out_text="PAYMENT SENT")
        near_transfer = _manual_transfer(client, "250.00", 30)
        near_card = _imported(client, migrated_engine, CARD, "250.00", _day(31), IN_TEXT)
        far_transfer = _manual_transfer(client, "310.00", 40)
        far_card = _imported(client, migrated_engine, CARD, "310.00", _day(44), IN_TEXT)
        undeclared_out, _ = _pair(
            client,
            migrated_engine,
            "75.25",
            50,
            51,
            to=SAVINGS,
            out_text="TRANSFER TO RESERVE",
            in_text="TRANSFER FROM EVERYDAY",
        )
        rule_id = _rule(client, seeded, window_days=5)

        assert _activate(client, rule_id) == 5
        # The activation confirm projects the plan; it applies nothing.
        assert _applied(migrated_engine, rule_id) == (0, None)

        assert _sweep() == {"transfers_absorbed": 2, "transfers_auto_linked": 4}
        for out_id in (near_out, far_out, named_out, undeclared_out):
            assert _merged_into(migrated_engine, out_id) is not None
        assert _merged_into(migrated_engine, near_card) == near_transfer
        assert _merged_into(migrated_engine, far_card) == far_transfer

        count, stamped = _applied(migrated_engine, rule_id)
        assert count == 5
        assert stamped is not None
        # The count is the links whose audit rows name the rule.
        signals = _signals(client, "transfer.auto_link") + _signals(client, "transfer.absorb")
        assert signals.count(f"rule:{rule_id}") == count
        assert sorted(s for s in signals if not s.startswith("rule:")) == ["name"]

        # Exactly once per link: a sweep that links nothing counts nothing.
        assert _sweep() == {"transfers_absorbed": 0, "transfers_auto_linked": 0}
        assert _applied(migrated_engine, rule_id) == (5, stamped)

    def test_unlinking_takes_no_application_back(
        self,
        seeded: dict[str, str],  # noqa: F811
        client: TestClient,
        migrated_engine: Engine,
    ) -> None:
        """A declared pair links: the rule goes from 0 applications to 1.
        Unlinking takes nothing back, and the unlinked pair is not linked —
        or counted — again."""
        out_id, _ = _pair(client, migrated_engine, "184.50", 0, 4)
        rule_id = _rule(client, seeded, window_days=5)
        assert _activate(client, rule_id) == 1
        assert _applied(migrated_engine, rule_id) == (0, None)
        _sweep()
        merged = _merged_into(migrated_engine, out_id)
        assert merged is not None
        count, stamped = _applied(migrated_engine, rule_id)
        assert count == 1
        assert stamped is not None

        resp = client.post(f"/transactions/{merged}/unlink-transfer")
        assert resp.status_code == 200, resp.text
        assert _applied(migrated_engine, rule_id) == (1, stamped)
        # The pair stays unlinked, so the next sweep links and counts nothing.
        assert _sweep()["transfers_auto_linked"] == 0
        assert _applied(migrated_engine, rule_id) == (1, stamped)
