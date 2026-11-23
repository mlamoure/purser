# ruff: noqa: F811  (pytest fixtures re-exported from sibling modules)
"""One split request's bounds, the same on every surface (#648 L2): at most
50 lines, memos of at most 500 characters and 50 backing rows across the
lines. REST had the first two in its request model; MCP ``split_transaction``
and a split suggestion's edited lines had none, so one call could hold a
worker and many row locks. Synthetic data only.
"""

import uuid
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from purser import splits
from tests.test_mcp import LiveServer, _mcp_call
from tests.test_splits import (  # noqa: F401 (fixture re-export)
    CHECKING,
    RETIREMENT,
    SALARY,
    TAXES,
    _audit,
    _paycheck,
    seeded,
)


def _line(**kw: Any) -> splits.LineInput:
    return splits.LineInput(target=TAXES, amount=Decimal("-1.00"), **kw)


class TestCheckLimits:
    def test_the_bounds_hold_and_the_edges_pass(self) -> None:
        splits.check_limits([_line()] * splits.MAX_LINES)
        splits.check_limits([_line(memo="m" * splits.MAX_MEMO_CHARS), _line()])
        ids = tuple(uuid.uuid4() for _ in range(splits.MAX_BACKING_ROWS))
        splits.check_limits([_line(backing_ids=ids[:25]), _line(backing_ids=ids[25:])])

        with pytest.raises(splits.SplitError, match="at most 50 lines, not 51"):
            splits.check_limits([_line()] * (splits.MAX_LINES + 1))
        with pytest.raises(splits.SplitError, match="line 2: a memo is at most 500"):
            splits.check_limits([_line(), _line(memo="m" * (splits.MAX_MEMO_CHARS + 1))])
        more = (*ids, uuid.uuid4())
        with pytest.raises(splits.SplitError, match=r"at most 50 backing rows .* not 51"):
            # Across lines, not only per line.
            splits.check_limits([_line(backing_ids=more[:26]), _line(backing_ids=more[26:])])


@pytest.fixture(scope="module")
def live_server(migrated_engine: Engine) -> Iterator[LiveServer]:
    server = LiveServer()
    server.start()
    yield server
    server.stop()


def _paycheck_lines(**on_taxes: Any) -> list[dict[str, Any]]:
    return [
        {"category": SALARY, "amount": "4000.00"},
        {"category": TAXES, "amount": "-900.00", **on_taxes},
        {"category": f"[{RETIREMENT}]", "amount": "-600.00"},
    ]


class TestSurfaces:
    def test_mcp_split_transaction_is_bounded_like_rest(
        self,
        seeded: None,
        admin_client: TestClient,
        migrated_engine: Engine,
        live_server: LiveServer,
    ) -> None:
        pay = _paycheck(admin_client, migrated_engine)
        token = admin_client.post(
            "/tokens", json={"name": "agent", "scopes": ["read", "write"]}
        ).json()["token"]
        many = [{"category": TAXES, "amount": "-1.00"}] * (splits.MAX_LINES + 1)
        backing = [str(uuid.uuid4()) for _ in range(splits.MAX_BACKING_ROWS + 1)]
        backed = {"category": f"[{RETIREMENT}]", "amount": "-600.00", "backing_ids": backing}
        calls = [
            ("split_transaction", {"transaction_id": pay, "lines": many}),
            (
                "split_transaction",
                {"transaction_id": pay, "lines": _paycheck_lines(memo="m" * 501)},
            ),
            (
                "split_transaction",
                {
                    "transaction_id": pay,
                    "lines": [{"category": SALARY, "amount": "3100.00"}, backed],
                },
            ),
            (
                "split_transaction",
                {"transaction_id": pay, "lines": _paycheck_lines(amount="Infinity")},
            ),
            # Within the bounds it splits.
            ("split_transaction", {"transaction_id": pay, "lines": _paycheck_lines(memo="ok")}),
        ]
        lines, memo, rows, infinite, ok = _mcp_call(live_server.url, token, calls)
        assert "at most 50 lines, not 51" in lines["error"]
        # The memo bound is splits.check_limits, inside split_transaction:
        # the path a split suggestion's edited lines take too.
        assert "a memo is at most 500 characters" in memo["error"]
        assert "backing_ids is a list of at most 50 ids" in rows["error"]
        assert "amount must be a finite number" in infinite["error"]
        assert "error" not in ok, ok
        assert _audit(migrated_engine, "transaction.split") == 1

    def test_rest_bounds_backing_rows_per_line_and_across_lines(
        self, seeded: None, admin_client: TestClient, migrated_engine: Engine
    ) -> None:
        pay = _paycheck(admin_client, migrated_engine)
        ids = [str(uuid.uuid4()) for _ in range(splits.MAX_BACKING_ROWS + 1)]
        one_line = admin_client.put(
            f"/transactions/{pay}/split",
            json={
                "lines": [
                    {"category": SALARY, "amount": "3100.00"},
                    {"category": f"[{RETIREMENT}]", "amount": "-600.00", "backing_ids": ids},
                ]
            },
        )
        assert one_line.status_code == 422 and "at most 50 items" in one_line.text
        across = admin_client.put(
            f"/transactions/{pay}/split",
            json={
                "lines": [
                    {"category": SALARY, "amount": "3100.00"},
                    {"category": f"[{RETIREMENT}]", "amount": "-300.00", "backing_ids": ids[:26]},
                    {"category": f"[{CHECKING}]", "amount": "-300.00", "backing_ids": ids[26:]},
                ]
            },
        )
        assert across.status_code == 422 and "at most 50 backing rows" in across.text
        assert _audit(migrated_engine, "transaction.split") == 0
