"""Net worth engine: postings + snapshots + valuations, consistent at any T."""

import base64
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

# UTC, not local: the net-worth engine defaults `as_of` to
# `datetime.now(UTC).date()`, so a local TODAY puts these fixtures a day into
# the engine's future on any host east of UTC (issue #140).
TODAY = datetime.now(UTC).date()


@pytest.fixture
def seeded(admin_client: TestClient) -> dict[str, str]:
    ids: dict[str, str] = {}
    for name, type_, path in [
        ("Checking", "checking", "assets.checking"),
        ("Brokerage", "brokerage", "assets.brokerage"),
        ("House", "property", "assets.house"),
        ("Mortgage", "loan", "liabilities.mortgage"),
    ]:
        resp = admin_client.post("/accounts", json={"name": name, "type": type_, "path": path})
        ids[path] = resp.json()["id"]
    admin_client.post(
        "/categories",
        json={"name": "Salary", "type": "category_income", "path": "income.salary"},
    )
    return ids


def _income(client: TestClient, amount: str, on: date) -> None:
    resp = client.post(
        "/transactions",
        json={
            "account": "assets.checking",
            "category": "income.salary",
            "amount": amount,
            "date": on.isoformat(),
            "status": "posted",
        },
    )
    assert resp.status_code == 201, resp.text


class TestNetWorthEngine:
    def test_historical_dates_are_consistent(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        _income(client, "1000.00", TODAY - timedelta(days=10))
        _income(client, "500.00", TODAY - timedelta(days=5))

        def nw(as_of: date) -> Decimal:
            resp = client.get("/networth", params={"as_of": as_of.isoformat()})
            return Decimal(resp.json()["net_worth"])

        assert nw(TODAY - timedelta(days=11)) == Decimal("0")
        assert nw(TODAY - timedelta(days=7)) == Decimal("1000.00")
        assert nw(TODAY) == Decimal("1500.00")
        # Re-query: identical (deterministic, no drift).
        assert nw(TODAY - timedelta(days=7)) == Decimal("1000.00")

    def test_property_valuation_and_loan_shape_net_worth(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        _income(client, "1000.00", TODAY - timedelta(days=10))
        house = seeded["assets.house"]
        mortgage = seeded["liabilities.mortgage"]
        r = client.post(
            f"/accounts/{house}/valuations",
            json={"as_of": (TODAY - timedelta(days=30)).isoformat(), "value": "400000.00"},
        )
        assert r.status_code == 201, r.text
        client.post(
            f"/accounts/{mortgage}/valuations",
            json={"as_of": (TODAY - timedelta(days=30)).isoformat(), "value": "-250000.00"},
        )
        result = client.get("/networth").json()
        assert Decimal(result["net_worth"]) == Decimal("151000.00")
        assert Decimal(result["assets"]) == Decimal("401000.00")
        assert Decimal(result["liabilities"]) == Decimal("250000.00")
        methods = {b["path"]: b["method"] for b in result["breakdown"]}
        assert methods["assets.house"] == "valuation"
        assert methods["liabilities.mortgage"] == "valuation"

        # A new valuation updates the series from its date forward only.
        client.post(
            f"/accounts/{house}/valuations",
            json={"as_of": TODAY.isoformat(), "value": "425000.00"},
        )
        now = client.get("/networth").json()
        assert Decimal(now["net_worth"]) == Decimal("176000.00")
        past = client.get(
            "/networth", params={"as_of": (TODAY - timedelta(days=1)).isoformat()}
        ).json()
        assert Decimal(past["net_worth"]) == Decimal("151000.00")

    def test_series_and_materialization(self, seeded: dict[str, str], client: TestClient) -> None:
        _income(client, "1000.00", TODAY - timedelta(days=3))
        series = client.get(
            "/networth/series",
            params={
                "start": (TODAY - timedelta(days=4)).isoformat(),
                "end": TODAY.isoformat(),
            },
        ).json()
        assert len(series) == 5
        assert Decimal(series[0]["net_worth"]) == Decimal("0")
        assert Decimal(series[-1]["net_worth"]) == Decimal("1000.00")

        resp = client.post(
            "/networth/materialize",
            params={
                "start": (TODAY - timedelta(days=4)).isoformat(),
                "end": TODAY.isoformat(),
            },
        )
        assert resp.status_code == 200
        assert resp.json()["days_materialized"] == 5
        # Idempotent upsert.
        assert (
            client.post(
                "/networth/materialize",
                params={
                    "start": (TODAY - timedelta(days=4)).isoformat(),
                    "end": TODAY.isoformat(),
                },
            ).json()["days_materialized"]
            == 5
        )


def _valuation(
    client: TestClient, account_id: str, value: str, *, days_ago: int = 3, **extra: str
) -> httpx.Response:
    return client.post(
        f"/accounts/{account_id}/valuations",
        json={"as_of": (TODAY - timedelta(days=days_ago)).isoformat(), "value": value, **extra},
    )


class TestValuationSign:
    """Issue #276 (decision 71): valuations are stored in ledger sign, where a
    liability is negative while owed. A positive figure on a liability is
    refused unless the caller says it is the statement's figure, which is then
    converted exactly as a reconciliation's statement balance is."""

    def test_liability_positive_is_rejected_with_a_hint(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        mortgage = seeded["liabilities.mortgage"]
        resp = _valuation(client, mortgage, "250000.00")
        assert resp.status_code == 422
        hint = resp.json()["detail"]
        assert "liabilities.mortgage is a loan account" in hint
        assert "Send -250000.00" in hint
        assert "convention 'statement'" in hint
        assert client.get(f"/accounts/{mortgage}/valuations").json() == []
        assert client.get("/networth").json()["breakdown"] == []

    def test_liability_negative_is_accepted_as_owed(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        resp = _valuation(client, seeded["liabilities.mortgage"], "-250000.00")
        assert resp.status_code == 201, resp.text
        assert Decimal(resp.json()["value"]) == Decimal("-250000.00")
        result = client.get("/networth").json()
        assert Decimal(result["assets"]) == Decimal("0")
        assert Decimal(result["liabilities"]) == Decimal("250000.00")

    def test_liability_statement_figure_is_converted_to_ledger_sign(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        mortgage = seeded["liabilities.mortgage"]
        resp = _valuation(client, mortgage, "250000.00", convention="statement")
        assert resp.status_code == 201, resp.text
        assert Decimal(resp.json()["value"]) == Decimal("-250000.00")
        (stored,) = client.get(f"/accounts/{mortgage}/valuations").json()
        assert Decimal(stored["value"]) == Decimal("-250000.00")
        result = client.get("/networth").json()
        assert Decimal(result["liabilities"]) == Decimal("250000.00")
        assert Decimal(result["net_worth"]) == Decimal("-250000.00")
        account = client.get(f"/accounts/{mortgage}").json()
        assert account["balance_method"] == "valuation"
        assert Decimal(account["balance"]) == Decimal("-250000.00")

    def test_asset_positive_is_accepted_in_either_convention(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        house = seeded["assets.house"]
        resp = _valuation(client, house, "400000.00")
        assert resp.status_code == 201, resp.text
        assert Decimal(resp.json()["value"]) == Decimal("400000.00")
        # An asset's statement figure reads the same as its ledger figure.
        resp = _valuation(client, house, "410000.00", days_ago=1, convention="statement")
        assert resp.status_code == 201, resp.text
        assert Decimal(resp.json()["value"]) == Decimal("410000.00")
        result = client.get("/networth").json()
        assert Decimal(result["assets"]) == Decimal("410000.00")
        assert Decimal(result["liabilities"]) == Decimal("0")

    def test_an_unknown_convention_is_rejected(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        resp = _valuation(client, seeded["assets.house"], "1.00", convention="bank")
        assert resp.status_code == 422


class TestValuationAccountTypes:
    """Issue #343 (decision 81): every reader of valuations reads loan and
    property accounts only, so a valuation on any other type was stored,
    audited and announced, and then never read. It is refused instead, and the
    refusal says where that account's balance comes from."""

    @pytest.mark.parametrize("type_", ["checking", "savings", "cash", "credit_card", "brokerage"])
    def test_a_non_valuation_account_is_refused_and_nothing_lands(
        self, seeded: dict[str, str], client: TestClient, type_: str
    ) -> None:
        path = f"assets.other_{type_}"
        resp = client.post("/accounts", json={"name": type_, "type": type_, "path": path})
        assert resp.status_code == 201, resp.text
        account = resp.json()["id"]
        # Neither convention gets past the type check, whatever the sign.
        for value, extra in (
            ("425.00", {}),
            ("-425.00", {}),
            ("425.00", {"convention": "statement"}),
        ):
            refused = _valuation(client, account, value, **extra)
            assert refused.status_code == 422, refused.text
            detail = refused.json()["detail"]
            assert f"{path} is a {type_} account" in detail
            assert "valuations apply only to loan and property accounts" in detail
            if type_ == "brokerage":
                assert "holdings snapshot" in detail
            else:
                assert "reconcile_account" in detail
        assert client.get(f"/accounts/{account}/valuations").json() == []
        assert client.get("/audit", params={"action": "valuation.create"}).json()["items"] == []
        assert client.get("/events", params={"type": "valuation.recorded"}).json() == []

    def test_a_loan_and_a_property_still_take_valuations(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        assert _valuation(client, seeded["assets.house"], "400000.00").status_code == 201
        assert _valuation(client, seeded["liabilities.mortgage"], "-250000.00").status_code == 201
        methods = {b["path"]: b["method"] for b in client.get("/networth").json()["breakdown"]}
        assert methods == {"assets.house": "valuation", "liabilities.mortgage": "valuation"}


class TestHoldingsViaFeed:
    def test_brokerage_holdings_visible_with_cost_basis_and_market_value(
        self,
        seeded: dict[str, str],
        client: TestClient,
        migrated_engine: Engine,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Encryption key comes suite-wide from conftest (required at startup).
        payload = {
            "errors": [],
            "accounts": [
                {
                    "id": "SF-BROKERAGE",
                    "name": "Vanguard",
                    "currency": "USD",
                    "balance": "1200.00",
                    "transactions": [],
                    "holdings": [
                        {
                            "symbol": "VTI",
                            "shares": "10.5",
                            "cost_basis": "2100.00",
                            "market_value": "2625.00",
                        },
                        {
                            "symbol": "BND",
                            "shares": "20",
                            "cost_basis": "1600.00",
                            "market_value": "1580.00",
                        },
                    ],
                }
            ],
        }
        access_url = "https://user:secret@bridge.example.com/simplefin"
        setup_token = base64.b64encode(
            b"https://bridge.example.com/simplefin/claim/TEST123"
        ).decode()

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/claim/TEST123"):
                return httpx.Response(200, text=access_url)
            return httpx.Response(200, json=payload)

        transport = httpx.MockTransport(handler)
        real_client = httpx.Client

        def patched(*args: object, **kwargs: object) -> httpx.Client:
            kwargs["transport"] = transport
            return real_client(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(httpx, "Client", patched)

        conn = client.post("/connections", json={"setup_token": setup_token})
        connection_id = conn.json()["id"]
        client.post(
            f"/connections/{connection_id}/map",
            json={
                "external_id": "SF-BROKERAGE",
                "account_id": seeded["assets.brokerage"],
            },
        )
        sync = client.post(f"/connections/{connection_id}/sync").json()
        assert sync.get("holdings_snapshots", 0) == 2

        holdings = client.get("/holdings", params={"account": "assets.brokerage"}).json()
        by_symbol = {h["symbol"]: h for h in holdings}
        assert Decimal(by_symbol["VTI"]["cost_basis"]) == Decimal("2100.00")
        assert Decimal(by_symbol["VTI"]["market_value"]) == Decimal("2625.00")
        assert Decimal(by_symbol["BND"]["quantity"]) == Decimal("20")

        # Net worth includes brokerage holdings' market value.
        result = client.get("/networth").json()
        brokerage = next(b for b in result["breakdown"] if b["path"] == "assets.brokerage")
        assert Decimal(brokerage["value"]) == Decimal("4205.00")
        assert brokerage["method"] == "holdings"

        # Re-sync same payload: no duplicate snapshots.
        client.post(f"/connections/{connection_id}/sync")
        holdings2 = client.get("/holdings", params={"account": "assets.brokerage"}).json()
        assert len(holdings2) == 2


# --------------------------------------------------------------------------
# Issue #145 / decision 38: the watermark keeps stored days truthful
# --------------------------------------------------------------------------

import uuid  # noqa: E402

from hypothesis import given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402
from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from purser import ledger, networth, reconciliation  # noqa: E402
from purser.core import ledger as core  # noqa: E402
from purser.db import get_sessionmaker  # noqa: E402
from purser.models import Account, ApiToken, NetWorthDaily, User  # noqa: E402
from tests.conftest import reset_db  # noqa: E402


def _service_token(session: Session) -> ApiToken:
    suffix = uuid.uuid4().hex[:10]
    user = User(email=f"nw_{suffix}@example.com")
    session.add(user)
    session.flush()
    token = ApiToken(
        user_id=user.id, name=f"tok_{suffix}", token_hash=uuid.uuid4().hex, scopes=["write"]
    )
    session.add(token)
    session.flush()
    return token


def _flat(session: Session, token: ApiToken, account, category, amount: str, on: date) -> None:
    ledger.create_flat_transaction(
        session,
        account_ref=str(account.id),
        category_ref=str(category.id),
        amount=Decimal(amount),
        date=on,
        payee_name="NW",
        description="nw row",
        requested_status=core.Status.POSTED,
        token=token,
    )


def _assert_stored_rows_match_live(session: Session) -> int:
    rows = session.execute(select(NetWorthDaily)).scalars().all()
    for row in rows:
        live = networth.net_worth_at(session, row.as_of)
        assert Decimal(row.net_worth) == live["net_worth"], row.as_of
        assert Decimal(row.assets) == live["assets"], row.as_of
        assert Decimal(row.liabilities) == live["liabilities"], row.as_of
    return len(rows)


class TestWatermarkRecompute:
    @settings(max_examples=8, deadline=None)
    @given(data=st.data())
    def test_stored_days_equal_live_after_any_backdated_mutations(
        self, migrated_engine: Engine, data: st.DataObject
    ) -> None:
        """The #145 property verbatim: after ANY random sequence of backdated
        mutations followed by a consumer run, every stored net_worth_daily row
        equals a live net_worth_at() of the same day."""
        reset_db(migrated_engine)
        today = datetime.now(UTC).date()
        with get_sessionmaker()() as session:
            token = _service_token(session)
            checking = ledger.create_account(
                session,
                name="NW Checking",
                type_="checking",
                path=f"assets.nw_{uuid.uuid4().hex[:8]}",
                actor_token_id=token.id,
            )
            house = ledger.create_account(
                session,
                name="NW House",
                type_="property",
                path=f"assets.house_{uuid.uuid4().hex[:8]}",
                actor_token_id=token.id,
            )
            category = ledger.ensure_category_path(
                session, "income.nw_seed", actor_token_id=token.id
            )
            _flat(session, token, checking, category, "1000.00", today - timedelta(days=45))
            _flat(session, token, checking, category, "250.00", today - timedelta(days=20))
            session.commit()
        assert networth.consume_watermark() > 0  # stored range exists

        posted: list[uuid.UUID] = []
        with get_sessionmaker()() as session:
            token = session.execute(select(ApiToken)).scalars().first()
            assert token is not None
            checking_row = ledger.account_by_path(session, checking.path)
            house_row = ledger.account_by_path(session, house.path)
            category_row = ledger.account_by_path(session, "income.nw_seed")
            for _ in range(data.draw(st.integers(min_value=1, max_value=4), label="n_ops")):
                op = data.draw(st.sampled_from(["flat", "valuation", "correct"]), label="op")
                days = data.draw(st.integers(min_value=0, max_value=44), label="days")
                if op == "flat":
                    tx = ledger.create_flat_transaction(
                        session,
                        account_ref=str(checking_row.id),
                        category_ref=str(category_row.id),
                        amount=Decimal("77.77"),
                        date=today - timedelta(days=days),
                        payee_name="Backdated",
                        description="backdated",
                        requested_status=core.Status.POSTED,
                        token=token,
                    )
                    posted.append(tx.id)
                elif op == "valuation":
                    networth.record_valuation(
                        session,
                        account_ref=str(house_row.id),
                        as_of=today - timedelta(days=days),
                        value=Decimal("300000.00") + days,
                        actor_token_id=token.id,
                    )
                elif op == "correct" and posted:
                    tx_id = posted.pop()
                    replacement = ledger.update_transaction(
                        session,
                        tx_id=tx_id,
                        token=token,
                        date=today - timedelta(days=days),
                        amount=Decimal("-33.33"),
                    )
                    posted.append(replacement.id)
                session.commit()

        networth.consume_watermark()
        with get_sessionmaker()() as session:
            assert _assert_stored_rows_match_live(session) > 0
            assert (
                session.execute(
                    text("SELECT networth_dirty_from FROM balance_watermark")
                ).scalar_one_or_none()
                is None
            )

    def test_series_never_serves_stale_rows_while_dirty(self, migrated_engine: Engine) -> None:
        """Decision 38 staleness guard: between a backdated write and the next
        consumer run, series() computes the dirty range live instead of serving
        the stale stored rows."""
        reset_db(migrated_engine)
        today = datetime.now(UTC).date()
        with get_sessionmaker()() as session:
            token = _service_token(session)
            checking = ledger.create_account(
                session,
                name="Stale Checking",
                type_="checking",
                path=f"assets.stale_{uuid.uuid4().hex[:8]}",
                actor_token_id=token.id,
            )
            category = ledger.ensure_category_path(
                session, "income.stale_seed", actor_token_id=token.id
            )
            _flat(session, token, checking, category, "100.00", today - timedelta(days=10))
            session.commit()
        networth.consume_watermark()

        with get_sessionmaker()() as session:
            token = session.execute(select(ApiToken)).scalars().first()
            checking_row = ledger.account_by_path(session, checking.path)
            category_row = ledger.account_by_path(session, "income.stale_seed")
            _flat(session, token, checking_row, category_row, "50.00", today - timedelta(days=5))
            session.commit()
            # No consumer has run: stored rows >= 5d ago are stale, and the
            # guard must serve live values for them.
            rows = networth.series(session, start=today - timedelta(days=6), end=today)
            by_day = {r["as_of"]: r["net_worth"] for r in rows}
            assert by_day[today - timedelta(days=5)] == Decimal("150.00")
            assert by_day[today] == Decimal("150.00")
            # The stored row itself is still the stale value until consumed.
            stored = session.execute(
                select(NetWorthDaily).where(NetWorthDaily.as_of == today - timedelta(days=5))
            ).scalar_one()
            assert Decimal(stored.net_worth) == Decimal("100.00")

        networth.consume_watermark()
        with get_sessionmaker()() as session:
            _assert_stored_rows_match_live(session)

    def test_csv_import_self_heals_history(self, migrated_engine: Engine) -> None:
        """The #143 regression shape: a historical CSV import + one consumer
        run leaves no stale stored day — no manual re-materialization."""
        from purser import qif_import

        reset_db(migrated_engine)
        today = datetime.now(UTC).date()
        d_hist = today - timedelta(days=30)
        with get_sessionmaker()() as session:
            token = _service_token(session)
            checking = ledger.create_account(
                session,
                name="Import Checking",
                type_="checking",
                path=f"assets.imp_{uuid.uuid4().hex[:8]}",
                actor_token_id=token.id,
            )
            category = ledger.ensure_category_path(
                session, "income.imp_seed", actor_token_id=token.id
            )
            _flat(session, token, checking, category, "10.00", today - timedelta(days=3))
            session.commit()
        networth.consume_watermark()

        with get_sessionmaker()() as session:
            csv_content = (
                f"Date,Payee,Amount,Account\n{d_hist.isoformat()},History Payee,123.45,Old\n"
            )
            qif_import.import_qif(
                session,
                content=csv_content,
                account_map={"Old": checking.path},
                fmt="csv",
                token=None,
            )
            session.commit()
        networth.consume_watermark()
        with get_sessionmaker()() as session:
            _assert_stored_rows_match_live(session)
            stored = session.execute(
                select(NetWorthDaily).where(NetWorthDaily.as_of == d_hist)
            ).scalar_one()
            assert Decimal(stored.net_worth) == Decimal("123.45")


class TestValuationSignForEveryCaller:
    def test_the_rule_lives_in_the_one_write_path(
        self, seeded: dict[str, str], db_session: Session
    ) -> None:
        """record_valuation is the only write path for manual valuations (REST
        and MCP record_valuation), so no surface can skip the rule."""
        on = TODAY - timedelta(days=3)
        with pytest.raises(ledger.LedgerError, match="a positive valuation would make it an asset"):
            networth.record_valuation(
                db_session,
                account_ref="liabilities.mortgage",
                as_of=on,
                value=Decimal("250000.00"),
                actor_token_id=None,
            )
        with pytest.raises(ledger.LedgerError, match="convention must be one of"):
            networth.record_valuation(
                db_session,
                account_ref="liabilities.mortgage",
                as_of=on,
                value=Decimal("-250000.00"),
                convention="bank",
                actor_token_id=None,
            )
        converted = networth.record_valuation(
            db_session,
            account_ref="liabilities.mortgage",
            as_of=on,
            value=Decimal("250000.00"),
            convention=networth.CONVENTION_STATEMENT,
            actor_token_id=None,
        )
        assert converted.value == Decimal("-250000.00")

    def test_a_figure_the_column_would_round_is_refused(
        self, seeded: dict[str, str], db_session: Session
    ) -> None:
        """REST's request model refuses these before the service; MCP hands its
        decimal string straight through, so the one write path refuses what
        NUMERIC(20,4) would round away or overflow (decision 73)."""
        on = TODAY - timedelta(days=3)
        for figure, reason in (
            ("400000.00001", "more than 4 decimal places"),
            ("1e16", "out of range"),
            ("NaN", "out of range"),
        ):
            with pytest.raises(ledger.LedgerError, match=reason):
                networth.record_valuation(
                    db_session,
                    account_ref="assets.house",
                    as_of=on,
                    value=Decimal(figure),
                    actor_token_id=None,
                )
        assert networth.list_valuations(db_session, uuid.UUID(seeded["assets.house"])) == []


_ACCOUNT_TYPES = ("checking", "savings", "cash", "credit_card", "brokerage", "property", "loan")


class TestValuationSignProperty:
    @settings(max_examples=300, deadline=None)
    @given(
        type_=st.sampled_from(_ACCOUNT_TYPES),
        value=st.decimals(
            min_value=Decimal("-1000000000000"),
            max_value=Decimal("1000000000000"),
            places=4,
            allow_nan=False,
            allow_infinity=False,
        ),
        convention=st.sampled_from(networth.CONVENTIONS),
    )
    def test_a_liability_never_becomes_an_asset_by_accident(
        self, type_: str, value: Decimal, convention: str
    ) -> None:
        """Whatever the type, figure and convention: a liability figure in
        ledger convention is never positive, a statement figure is converted
        by reconciliation's own rule, and anything else is stored as given —
        so a liability is stored positive only when the caller explicitly gave
        a statement credit balance."""
        account = Account(type=type_, path="assets.synthetic")
        liability = type_ in reconciliation.LIABILITY_TYPES
        if convention == networth.CONVENTION_LEDGER and liability and value > 0:
            with pytest.raises(ledger.LedgerError):
                networth.ledger_signed_value(account, value, convention)
            return
        stored = networth.ledger_signed_value(account, value, convention)
        if convention == networth.CONVENTION_STATEMENT:
            assert stored == reconciliation.to_ledger_sign(type_, value)
        assert stored == (-value if liability and convention == "statement" else value)
        if liability and stored > 0:
            assert convention == networth.CONVENTION_STATEMENT and value < 0


class TestValuationTypeRuleForEveryCaller:
    def test_the_type_rule_lives_in_the_one_write_path_before_the_sign_rule(
        self, seeded: dict[str, str], db_session: Session
    ) -> None:
        """Decision 81 sits in record_valuation, so REST and MCP refuse alike,
        and it runs before decision 71's sign rule: a positive figure on a
        credit card is refused for its type, not given a sign hint."""
        on = TODAY - timedelta(days=3)
        for account_ref, type_ in (
            ("assets.checking", "checking"),
            ("assets.brokerage", "brokerage"),
        ):
            with pytest.raises(ledger.LedgerError, match=f"is a {type_} account: valuations apply"):
                networth.record_valuation(
                    db_session,
                    account_ref=account_ref,
                    as_of=on,
                    value=Decimal("1.00"),
                    actor_token_id=None,
                )
        card = ledger.create_account(
            db_session,
            name="Card",
            type_="credit_card",
            path="liabilities.card",
            actor_token_id=None,
        )
        with pytest.raises(ledger.LedgerError) as refused:
            networth.record_valuation(
                db_session,
                account_ref=str(card.id),
                as_of=on,
                value=Decimal("425.00"),
                actor_token_id=None,
            )
        assert "valuations apply only to loan and property accounts" in str(refused.value)
        assert "positive valuation" not in str(refused.value)
        assert networth.list_valuations(db_session, card.id) == []


class TestValuationTypeProperty:
    @settings(max_examples=60, deadline=None)
    @given(
        type_=st.sampled_from(_ACCOUNT_TYPES),
        value=st.decimals(
            min_value=Decimal("-1000000000"),
            max_value=Decimal("1000000000"),
            places=2,
            allow_nan=False,
            allow_infinity=False,
        ),
        convention=st.sampled_from(networth.CONVENTIONS),
        days_ago=st.integers(min_value=0, max_value=30),
    )
    def test_a_recorded_valuation_is_always_read(
        self,
        migrated_engine: Engine,
        type_: str,
        value: Decimal,
        convention: str,
        days_ago: int,
    ) -> None:
        """Decision 81's promise: whenever record_valuation accepts a figure,
        net worth reads that account by its valuation, at the stored figure.
        Every other type is refused, whatever the figure and convention."""
        today = datetime.now(UTC).date()
        with get_sessionmaker()() as session:
            try:
                account = ledger.create_account(
                    session,
                    name=f"Synthetic {type_}",
                    type_=type_,
                    path=f"assets.prop_{uuid.uuid4().hex[:10]}",
                    actor_token_id=None,
                )
                try:
                    stored = networth.record_valuation(
                        session,
                        account_ref=str(account.id),
                        as_of=today - timedelta(days=days_ago),
                        value=value,
                        convention=convention,
                        actor_token_id=None,
                    )
                except ledger.LedgerError:
                    assert type_ not in networth.VALUATION_TYPES or (
                        type_ == "loan" and convention == networth.CONVENTION_LEDGER and value > 0
                    )
                    assert networth.list_valuations(session, account.id) == []
                    return
                assert type_ in networth.VALUATION_TYPES
                (entry,) = [
                    b
                    for b in networth.net_worth_at(session, today)["breakdown"]
                    if b["account_id"] == account.id
                ]
                assert entry["method"] == "valuation"
                assert entry["value"] == stored.value
            finally:
                session.rollback()


# --------------------------------------------------------------------------
# Issue #390 / decisions 88-89: manually tracked holdings in net worth
# --------------------------------------------------------------------------


class TestManualHoldings:
    """A manual brokerage account reports postings + the market value of its
    positions on every account surface and in net worth, and a backdated
    set_quantity heals net_worth_daily through the watermark alone."""

    @pytest.fixture
    def broker(self, admin_client: TestClient) -> dict[str, str]:
        resp = admin_client.post(
            "/accounts", json={"name": "Broker", "type": "brokerage", "path": "assets.broker"}
        )
        account_id = resp.json()["id"]
        assert (
            admin_client.patch(
                f"/accounts/{account_id}", json={"holdings_source": "manual"}
            ).status_code
            == 200
        )
        admin_client.post(
            "/categories",
            json={"name": "Fees", "type": "category_expense", "path": "expenses.fees"},
        )
        # Cash that left the account: the shape of a cash sweep that only
        # ever saw disbursements.
        resp = admin_client.post(
            "/transactions",
            json={
                "account": "assets.broker",
                "category": "expenses.fees",
                "amount": "-100.00",
                "date": (TODAY - timedelta(days=20)).isoformat(),
                "status": "posted",
            },
        )
        assert resp.status_code == 201, resp.text
        symbol = f"N{uuid.uuid4().hex[:7].upper()}"
        resp = admin_client.post(
            "/prices",
            json={
                "symbol": symbol,
                "date": (TODAY - timedelta(days=30)).isoformat(),
                "close": "50",
            },
        )
        assert resp.status_code == 200, resp.text
        resp = admin_client.post(
            f"/accounts/{account_id}/positions",
            json={
                "symbol": symbol,
                "quantity": "10",
                "as_of": (TODAY - timedelta(days=5)).isoformat(),
            },
        )
        assert resp.status_code == 201, resp.text
        return {"account": account_id, "symbol": symbol, "position": resp.json()["id"]}

    def test_every_surface_reports_postings_plus_holdings(
        self, broker: dict[str, str], admin_client: TestClient
    ) -> None:
        single = admin_client.get(f"/accounts/{broker['account']}").json()
        assert single["balance_method"] == "postings+holdings"
        assert Decimal(single["balance"]) == Decimal("400.00")  # -100 cash + 10 x 50
        assert Decimal(single["postings_balance"]) == Decimal("-100.00")
        assert single["balance_as_of"] == (TODAY - timedelta(days=5)).isoformat()

        listed = {
            a["path"]: a
            for a in admin_client.get("/accounts", params={"include_balances": True}).json()
        }
        assert Decimal(listed["assets.broker"]["balance"]) == Decimal("400.00")

        point = admin_client.get(f"/accounts/{broker['account']}/balance").json()
        assert point["method"] == "postings+holdings" and Decimal(point["balance"]) == Decimal(
            "400.00"
        )
        entry = next(
            b
            for b in admin_client.get("/networth").json()["breakdown"]
            if b["path"] == "assets.broker"
        )
        assert entry["method"] == "postings+holdings" and Decimal(entry["value"]) == Decimal(
            "400.00"
        )
        # Before the first manual row the account has only its cash.
        before = admin_client.get(
            "/networth", params={"as_of": (TODAY - timedelta(days=6)).isoformat()}
        ).json()
        entry = next(b for b in before["breakdown"] if b["path"] == "assets.broker")
        assert Decimal(entry["value"]) == Decimal("-100.00")

    def test_backdated_set_quantity_heals_net_worth_daily(
        self, broker: dict[str, str], admin_client: TestClient, migrated_engine: Engine
    ) -> None:
        # Materialize the last two weeks, then clear the cursor.
        assert networth.consume_watermark() >= 0
        with get_sessionmaker()() as session:
            for offset in range(15):
                networth.materialize_day(session, TODAY - timedelta(days=offset))
            session.execute(text("UPDATE balance_watermark SET networth_dirty_from = NULL"))
            session.commit()
        backdated = TODAY - timedelta(days=12)
        resp = admin_client.post(
            f"/positions/{broker['position']}/events",
            json={"kind": "set_quantity", "occurred_on": backdated.isoformat(), "quantity": "4"},
        )
        assert resp.status_code == 201, resp.text
        # The TestClient ran the post-commit kick; the cursor is consumed and
        # the stored days now include the backdated shares.
        with get_sessionmaker()() as session:
            cursor = session.execute(
                text("SELECT networth_dirty_from FROM balance_watermark")
            ).scalar()
            assert cursor is None
            stored = {
                row.as_of: Decimal(row.net_worth)
                for row in session.execute(
                    select(NetWorthDaily).where(NetWorthDaily.as_of >= backdated)
                ).scalars()
            }
        live = Decimal(
            admin_client.get("/networth", params={"as_of": backdated.isoformat()}).json()[
                "net_worth"
            ]
        )
        assert stored[backdated] == live
        # 4 shares x 50 counted from the backdated day; the later set_quantity
        # (10 shares) still holds from its own date.
        broker_value = next(
            b
            for b in admin_client.get(
                "/networth", params={"as_of": (backdated + timedelta(days=1)).isoformat()}
            ).json()["breakdown"]
            if b["path"] == "assets.broker"
        )
        assert Decimal(broker_value["value"]) == Decimal("100.00")  # -100 + 4 x 50
        assert stored[TODAY] == Decimal(admin_client.get("/networth").json()["net_worth"])

    def test_mcp_record_valuation_still_refuses_brokerage(
        self, broker: dict[str, str], admin_client: TestClient
    ) -> None:
        """Decision 81 is unchanged: a manual brokerage account is valued by
        its positions, never by a valuation row."""
        resp = admin_client.post(
            f"/accounts/{broker['account']}/valuations",
            json={"as_of": TODAY.isoformat(), "value": "1.00"},
        )
        assert resp.status_code == 422
