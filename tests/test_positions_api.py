"""REST surface of manually tracked holdings (issue #390, decisions 88/89):
the admin-only holdings_source switch, positions and their events, prices,
scopes, audit rows and events. Synthetic fixtures only (TEST / ACME-style
symbols, made-up prices, @example.com users)."""

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select

from purser import db
from purser.models import AuditLog, Event, SecurityPrice

TODAY = datetime.now(UTC).date()


def _headers(client: TestClient, scopes: list[str]) -> dict[str, str]:
    resp = client.post("/tokens", json={"name": f"t-{'-'.join(scopes)}", "scopes": scopes})
    assert resp.status_code == 201, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def _symbol() -> str:
    return f"A{uuid.uuid4().hex[:8].upper()}"


@pytest.fixture
def manual(admin_client: TestClient) -> dict[str, str]:
    """A manual brokerage account (admin switched it) and a feed one."""
    ids: dict[str, str] = {}
    for name, path in (("Broker", "assets.broker"), ("Feed Broker", "assets.feed_broker")):
        resp = admin_client.post(
            "/accounts", json={"name": name, "type": "brokerage", "path": path}
        )
        assert resp.status_code == 201, resp.text
        ids[path] = resp.json()["id"]
    resp = admin_client.patch(
        f"/accounts/{ids['assets.broker']}", json={"holdings_source": "manual"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["holdings_source"] == "manual"
    return ids


def _create(client: TestClient, account_id: str, symbol: str, **body: object) -> dict:
    resp = client.post(f"/accounts/{account_id}/positions", json={"symbol": symbol, **body})
    assert resp.status_code == 201, resp.text
    return resp.json()


class TestHoldingsSourceSwitch:
    def test_admin_only(self, manual: dict[str, str], admin_client: TestClient) -> None:
        """C3: holdings_source is feed configuration, like feed_start."""
        operator = _headers(admin_client, ["read", "write"])
        resp = admin_client.patch(
            f"/accounts/{manual['assets.feed_broker']}",
            json={"holdings_source": "manual"},
            headers=operator,
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "requires scope: admin"
        # The same commit token may still rename the account.
        resp = admin_client.patch(
            f"/accounts/{manual['assets.feed_broker']}", json={"name": "Renamed"}, headers=operator
        )
        assert resp.status_code == 200
        assert resp.json()["holdings_source"] == "feed"

    def test_brokerage_only(self, admin_client: TestClient) -> None:
        resp = admin_client.post(
            "/accounts", json={"name": "Checking", "type": "checking", "path": "assets.checking"}
        )
        resp = admin_client.patch(
            f"/accounts/{resp.json()['id']}", json={"holdings_source": "manual"}
        )
        assert resp.status_code == 422
        assert "only to brokerage" in resp.json()["detail"]

    def test_switch_is_audited_as_account_update(
        self, manual: dict[str, str], migrated_engine: Engine
    ) -> None:
        with db.get_sessionmaker()() as session:
            row = session.execute(
                select(AuditLog).where(
                    AuditLog.action == "account.update",
                    AuditLog.entity_id == uuid.UUID(manual["assets.broker"]),
                )
            ).scalar_one()
            assert row.before_after["before"]["holdings_source"] == "feed"
            assert row.before_after["after"]["holdings_source"] == "manual"


class TestPositions:
    def test_create_list_record_reverse_close(
        self, manual: dict[str, str], admin_client: TestClient
    ) -> None:
        account = manual["assets.broker"]
        symbol = _symbol()
        created = _create(
            admin_client,
            account,
            symbol.lower(),
            quantity="10.5",
            as_of=(TODAY - timedelta(days=3)).isoformat(),
            cost_basis="1050.00",
            source_note="broker holdings page",
        )
        assert created["symbol"] == symbol
        assert Decimal(created["quantity"]) == Decimal("10.5")
        assert created["market_value"] is None and created["price"] is None
        assert created["events"] == 1

        resp = admin_client.post(
            f"/positions/{created['id']}/events",
            json={
                "kind": "sell",
                "occurred_on": TODAY.isoformat(),
                "quantity": "0.5",
                "price": "101.25",
            },
        )
        assert resp.status_code == 201, resp.text
        sale = resp.json()
        assert Decimal(sale["position"]["quantity"]) == Decimal("10")
        # 1050 * 0.5 / 10.5 = 50.0000 removed at average cost.
        assert Decimal(sale["position"]["cost_basis"]) == Decimal("1000")

        listed = admin_client.get(f"/accounts/{account}/positions").json()
        assert [p["symbol"] for p in listed] == [symbol]

        resp = admin_client.post(f"/position-events/{sale['event']['id']}/reverse", json={})
        assert resp.status_code == 200, resp.text
        assert Decimal(resp.json()["position"]["quantity"]) == Decimal("10.5")

        events = admin_client.get(f"/positions/{created['id']}/events").json()
        assert [e["kind"] for e in events] == ["sell", "sell", "set_quantity"]
        reversal, original = events[0], events[1]
        assert reversal["reverses_id"] == original["id"]
        assert original["reversed_by"] == reversal["id"]
        assert events[2]["source_note"] == "broker holdings page"
        assert events[2]["origin"] == "manual"  # recorded through a browser session

        resp = admin_client.patch(f"/positions/{created['id']}", json={"closed": True})
        assert resp.status_code == 422 and "still holds" in resp.json()["detail"]
        admin_client.post(
            f"/positions/{created['id']}/events",
            json={"kind": "set_quantity", "occurred_on": TODAY.isoformat(), "quantity": "0"},
        )
        resp = admin_client.patch(f"/positions/{created['id']}", json={"closed": True})
        assert resp.status_code == 200 and resp.json()["closed_at"] is not None
        assert admin_client.get(f"/accounts/{account}/positions").json() == []
        assert (
            len(
                admin_client.get(
                    f"/accounts/{account}/positions", params={"include_closed": True}
                ).json()
            )
            == 1
        )

    def test_conflicts_and_refusals(self, manual: dict[str, str], admin_client: TestClient) -> None:
        account = manual["assets.broker"]
        symbol = _symbol()
        position = _create(admin_client, account, symbol)
        resp = admin_client.post(
            f"/accounts/{account}/positions",
            json={"symbol": symbol, "quantity": "1", "as_of": TODAY.isoformat()},
        )
        assert resp.status_code == 409
        resp = admin_client.post(
            f"/accounts/{manual['assets.feed_broker']}/positions", json={"symbol": _symbol()}
        )
        assert resp.status_code == 422 and "takes its holdings from the feed" in resp.text
        resp = admin_client.post(
            f"/positions/{position['id']}/events",
            json={
                "kind": "buy",
                "occurred_on": (TODAY + timedelta(days=1)).isoformat(),
                "quantity": "1",
            },
        )
        assert resp.status_code == 422 and "in the future" in resp.text
        resp = admin_client.post(
            f"/positions/{position['id']}/events",
            json={"kind": "sell", "occurred_on": TODAY.isoformat(), "quantity": "1"},
        )
        assert resp.status_code == 422 and TODAY.isoformat() in resp.json()["detail"]
        assert admin_client.get(f"/positions/{uuid.uuid4()}/events").status_code == 404

    def test_writes_need_write_scope_and_reads_need_read(
        self, manual: dict[str, str], admin_client: TestClient
    ) -> None:
        """Write scope (decision 73's rule, #436): a read-only token is
        refused, never turned into a draft."""
        account = manual["assets.broker"]
        agent = _headers(admin_client, ["read"])
        resp = admin_client.post(
            f"/accounts/{account}/positions", json={"symbol": _symbol()}, headers=agent
        )
        assert resp.status_code == 403
        resp = admin_client.post(
            "/prices",
            json={"symbol": _symbol(), "date": TODAY.isoformat(), "close": "1"},
            headers=agent,
        )
        assert resp.status_code == 403
        assert admin_client.get(f"/accounts/{account}/positions", headers=agent).status_code == 200
        operator = _headers(admin_client, ["read", "write"])
        created = admin_client.post(
            f"/accounts/{account}/positions",
            json={"symbol": _symbol(), "quantity": "2", "as_of": TODAY.isoformat()},
            headers=operator,
        )
        assert created.status_code == 201
        events = admin_client.get(f"/positions/{created.json()['id']}/events").json()
        assert events[0]["origin"] == "agent"  # an API token, not a browser session
        assert admin_client.post("/prices/fetch", headers=operator).status_code == 403


class TestPrices:
    def test_record_list_and_revalue(
        self, manual: dict[str, str], admin_client: TestClient
    ) -> None:
        account = manual["assets.broker"]
        symbol = _symbol()
        position = _create(
            admin_client,
            account,
            symbol,
            quantity="4",
            as_of=(TODAY - timedelta(days=2)).isoformat(),
        )
        resp = admin_client.post(
            "/prices",
            json={
                "symbol": symbol,
                "date": (TODAY - timedelta(days=1)).isoformat(),
                "close": "25.125",
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["accounts_reprojected"] == 1 and body["previous"] is None
        listed = admin_client.get(f"/accounts/{account}/positions").json()
        (row,) = [p for p in listed if p["id"] == position["id"]]
        assert Decimal(row["market_value"]) == Decimal("100.5")
        assert row["price_source"] == "manual"

        resp = admin_client.post(
            "/prices",
            json={
                "symbol": symbol,
                "date": (TODAY - timedelta(days=1)).isoformat(),
                "close": "26",
            },
        )
        assert Decimal(resp.json()["previous"]) == Decimal("25.125")
        prices = admin_client.get("/prices", params={"symbol": symbol}).json()
        assert [(p["source"], Decimal(p["close"])) for p in prices] == [("manual", Decimal("26"))]

        future = admin_client.post(
            "/prices",
            json={"symbol": symbol, "date": (TODAY + timedelta(days=1)).isoformat(), "close": "1"},
        )
        assert future.status_code == 422

        holdings = admin_client.get("/holdings", params={"account": account}).json()
        (held,) = [h for h in holdings if h["symbol"] == symbol]
        assert held["source"] == "manual" and Decimal(held["price"]) == Decimal("26")

        status = admin_client.get("/prices/status").json()
        assert status["provider"] == "none"
        (entry,) = [s for s in status["symbols"] if s["symbol"] == symbol]
        assert entry["source"] == "manual" and entry["stale"] is False

    def test_fetch_now_without_a_provider_still_reprojects(
        self, manual: dict[str, str], admin_client: TestClient
    ) -> None:
        _create(
            admin_client,
            manual["assets.broker"],
            _symbol(),
            quantity="1",
            as_of=TODAY.isoformat(),
        )
        resp = admin_client.post("/prices/fetch")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["skipped"] == "no price provider is configured"
        assert body["accounts_projected"] == 1 and body["fetched"] == 0
        # No price at all counts as stale for the digest.
        assert body["stale"][0]["price_as_of"] is None


class TestAuditAndEvents:
    def test_every_write_audits_and_emits(
        self, manual: dict[str, str], admin_client: TestClient
    ) -> None:
        account = manual["assets.broker"]
        symbol = _symbol()
        position = _create(admin_client, account, symbol, quantity="3", as_of=TODAY.isoformat())
        resp = admin_client.post(
            f"/positions/{position['id']}/events",
            json={"kind": "buy", "occurred_on": TODAY.isoformat(), "quantity": "1", "price": "2"},
        )
        admin_client.post(f"/position-events/{resp.json()['event']['id']}/reverse", json={})
        admin_client.post(
            "/prices", json={"symbol": symbol, "date": TODAY.isoformat(), "close": "9"}
        )
        with db.get_sessionmaker()() as session:
            actions = [
                a
                for (a,) in session.execute(
                    select(AuditLog.action)
                    .where(AuditLog.kind == "change")
                    .order_by(AuditLog.at, AuditLog.id)
                )
            ]
            # One request's rows share a transaction timestamp, so compare as
            # a multiset rather than in id order.
            assert sorted(a for a in actions if a.startswith(("position.", "price."))) == [
                "position.create",
                "position.record",
                "position.record",
                "position.reverse",
                "price.record",
            ]
            changes = [
                e.payload.get("change")
                for e in session.execute(
                    select(Event)
                    .where(Event.type == "position.updated")
                    .order_by(Event.at, Event.id)
                ).scalars()
            ]
            assert sorted(changes) == ["opened", "recorded", "recorded", "reversed"]


class TestSetQuantityBasis:
    """A set_quantity without cost_basis (#390 fix round 2): a re-count of the
    count held just before keeps the basis, a changed count makes it unknown,
    zero shares have a basis of zero. The Positions editor's requests."""

    def _set(self, client: TestClient, pid: str, quantity: str, on: date, **extra: str) -> dict:
        resp = client.post(
            f"/positions/{pid}/events",
            json={
                "kind": "set_quantity",
                "occurred_on": on.isoformat(),
                "quantity": quantity,
                **extra,
            },
        )
        assert resp.status_code == 201, resp.text
        return resp.json()["position"]

    def test_a_recount_keeps_the_basis_and_a_sell_out_needs_none(
        self, manual: dict[str, str], admin_client: TestClient
    ) -> None:
        created = _create(
            admin_client,
            manual["assets.broker"],
            _symbol(),
            quantity="10",
            as_of=(TODAY - timedelta(days=6)).isoformat(),
            cost_basis="1000",
        )
        pid = created["id"]
        kept = self._set(admin_client, pid, "10", TODAY - timedelta(days=5))
        assert Decimal(kept["cost_basis"]) == Decimal("1000")
        changed = self._set(admin_client, pid, "12", TODAY - timedelta(days=4))
        assert changed["cost_basis"] is None
        typed = self._set(admin_client, pid, "12", TODAY - timedelta(days=3), cost_basis="1300")
        assert Decimal(typed["cost_basis"]) == Decimal("1300")
        sold = self._set(admin_client, pid, "0", TODAY - timedelta(days=2))
        assert Decimal(sold["cost_basis"]) == 0
        rebought = self._set(admin_client, pid, "5", TODAY - timedelta(days=1))
        assert rebought["cost_basis"] is None  # never the sold-out zero

    def test_a_backdated_recount_does_not_count_a_later_buy_twice(
        self, manual: dict[str, str], admin_client: TestClient
    ) -> None:
        created = _create(
            admin_client,
            manual["assets.broker"],
            _symbol(),
            quantity="10",
            as_of=(TODAY - timedelta(days=30)).isoformat(),
            cost_basis="1000",
        )
        resp = admin_client.post(
            f"/positions/{created['id']}/events",
            json={
                "kind": "buy",
                "occurred_on": (TODAY - timedelta(days=10)).isoformat(),
                "quantity": "5",
                "price": "100",
            },
        )
        assert resp.status_code == 201, resp.text
        after = self._set(admin_client, created["id"], "10", TODAY - timedelta(days=20))
        assert Decimal(after["cost_basis"]) == Decimal("1500")


class TestZeroShares:
    def test_a_position_holding_nothing_is_not_on_a_stale_close(
        self, manual: dict[str, str], admin_client: TestClient
    ) -> None:
        """Sold down to zero and not closed: the job no longer prices it, and
        the Positions tab must not badge its aging close as stale."""
        symbol = _symbol()
        created = _create(
            admin_client,
            manual["assets.broker"],
            symbol,
            quantity="5",
            as_of=(TODAY - timedelta(days=20)).isoformat(),
        )
        resp = admin_client.post(
            "/prices",
            json={
                "symbol": symbol,
                "date": (TODAY - timedelta(days=20)).isoformat(),
                "close": "10",
            },
        )
        assert resp.status_code == 200, resp.text
        resp = admin_client.post(
            f"/positions/{created['id']}/events",
            json={
                "kind": "set_quantity",
                "occurred_on": (TODAY - timedelta(days=10)).isoformat(),
                "quantity": "0",
            },
        )
        assert resp.status_code == 201, resp.text
        (row,) = [
            p
            for p in admin_client.get(f"/accounts/{manual['assets.broker']}/positions").json()
            if p["symbol"] == symbol
        ]
        assert Decimal(row["quantity"]) == 0 and row["stale_price"] is False


class TestSmallContracts:
    """#390 phase 1a fix round: request shapes as documented, names cleaned
    once, a rename that is an event, values out of range a 422."""

    def test_a_quantity_without_as_of_holds_today(
        self, manual: dict[str, str], admin_client: TestClient
    ) -> None:
        created = _create(admin_client, manual["assets.broker"], _symbol(), quantity="3")
        assert created["opened_on"] == TODAY.isoformat()
        events = admin_client.get(f"/positions/{created['id']}/events").json()
        assert [(e["kind"], e["occurred_on"]) for e in events] == [
            ("set_quantity", TODAY.isoformat())
        ]

    def test_a_reversal_needs_no_body(
        self, manual: dict[str, str], admin_client: TestClient
    ) -> None:
        created = _create(
            admin_client,
            manual["assets.broker"],
            _symbol(),
            quantity="3",
            as_of=(TODAY - timedelta(days=2)).isoformat(),
        )
        resp = admin_client.post(
            f"/positions/{created['id']}/events",
            json={"kind": "buy", "occurred_on": TODAY.isoformat(), "quantity": "1"},
        )
        assert resp.status_code == 201, resp.text
        resp = admin_client.post(f"/position-events/{resp.json()['event']['id']}/reverse")
        assert resp.status_code == 200, resp.text
        assert Decimal(resp.json()["position"]["quantity"]) == Decimal("3")

    def test_names_are_cleaned_once_and_a_rename_is_an_event(
        self, manual: dict[str, str], admin_client: TestClient
    ) -> None:
        created = _create(admin_client, manual["assets.broker"], _symbol(), name="  Acme Corp  ")
        assert created["name"] == "Acme Corp"

        def renames() -> tuple[int, int]:
            with db.get_sessionmaker()() as session:
                audits = session.execute(
                    select(AuditLog).where(
                        AuditLog.action == "position.update",
                        AuditLog.entity_id == uuid.UUID(created["id"]),
                    )
                ).all()
                emitted = [
                    e
                    for e in session.execute(
                        select(Event).where(
                            Event.type == "position.updated",
                            Event.entity_id == uuid.UUID(created["id"]),
                        )
                    ).scalars()
                    if e.payload.get("change") == "renamed"
                ]
            return len(audits), len(emitted)

        # The same name with spaces around it is no rename at all.
        resp = admin_client.patch(f"/positions/{created['id']}", json={"name": " Acme Corp "})
        assert resp.status_code == 200, resp.text
        assert renames() == (0, 0)
        resp = admin_client.patch(f"/positions/{created['id']}", json={"name": "Acme Holdings"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["name"] == "Acme Holdings"
        assert renames() == (1, 1)

    def test_a_share_count_beyond_what_the_ledger_holds_is_a_422(
        self, manual: dict[str, str], admin_client: TestClient
    ) -> None:
        big = "99999999999999"  # the largest whole count one event takes
        created = _create(
            admin_client, manual["assets.broker"], _symbol(), quantity=big, as_of=TODAY.isoformat()
        )
        resp = admin_client.post(
            f"/positions/{created['id']}/events",
            json={"kind": "buy", "occurred_on": TODAY.isoformat(), "quantity": big},
        )
        assert resp.status_code == 422 and "out of range" in resp.text

    def test_a_cost_basis_beyond_what_the_ledger_holds_is_a_422(
        self, manual: dict[str, str], admin_client: TestClient
    ) -> None:
        created = _create(
            admin_client,
            manual["assets.broker"],
            _symbol(),
            quantity="0",
            as_of=(TODAY - timedelta(days=2)).isoformat(),
        )
        resp = admin_client.post(
            f"/positions/{created['id']}/events",
            json={
                "kind": "buy",
                "occurred_on": (TODAY - timedelta(days=1)).isoformat(),
                "quantity": "10000000",
                "price": "1500000000",
            },
        )
        assert resp.status_code == 422 and "out of range" in resp.text

    def test_a_value_out_of_range_on_read_is_flagged_not_unpriced(
        self, manual: dict[str, str], admin_client: TestClient
    ) -> None:
        """A provider close stored as is can push a value past what the
        ledger holds: that position keeps its close, has no value and says
        value_out_of_range (check the share count); the listing works."""
        symbol = _symbol()
        _create(
            admin_client,
            manual["assets.broker"],
            symbol,
            quantity="99999999999",
            as_of=(TODAY - timedelta(days=1)).isoformat(),
        )
        with db.get_sessionmaker()() as session:
            session.add(
                SecurityPrice(
                    symbol=symbol, price_date=TODAY, source="tiingo", close=Decimal("99999999")
                )
            )
            session.commit()
        resp = admin_client.get(f"/accounts/{manual['assets.broker']}/positions")
        assert resp.status_code == 200, resp.text
        (row,) = [p for p in resp.json() if p["symbol"] == symbol]
        assert row["market_value"] is None and Decimal(row["price"]) == Decimal("99999999")
        assert row["value_out_of_range"] is True
        others = [p for p in resp.json() if p["symbol"] != symbol]
        assert all(p["value_out_of_range"] is False for p in others)


def test_the_overlap_409_is_named_where_the_writes_are_described(admin_client: TestClient) -> None:
    """An agent or a client meeting the overlap refusal can read why, on the
    REST routes and on record_position (#390 fix round 2)."""
    from purser.mcp.tools_holdings import record_position

    spec = admin_client.get("/openapi.json").json()
    for path in ("/accounts/{account_id}/positions", "/positions/{position_id}/events"):
        described = spec["paths"][path]["post"]["description"]
        assert "409" in described and "CLOSED position" in described, path
    assert "CLOSED" in (record_position.__doc__ or "") and "overlap" in (
        record_position.__doc__ or ""
    )
