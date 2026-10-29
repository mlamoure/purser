"""Schedules & forecasting: RRULE materialization, detection, projections."""

import itertools
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import Engine, text

from purser import crypto
from purser.core import forecasting as core_forecasting


def _near_term_bymonthday() -> int:
    """A BYMONTHDAY whose next two monthly occurrences always fit the 60-day
    materialization horizon, regardless of the date the suite runs.

    Day of (today + 3), clamped to <= 28 so the day exists in every month.
    The first occurrence lands in [today, today+3] (clamping day 29-31 down
    to 28 moves it earlier, but never before today), so the second lands at
    most ~34 days out — safely inside MATERIALIZE_HORIZON_DAYS=60. Hardcoded
    days failed deterministically when run just after the rule day: the
    first occurrence slid ~30 days out and the second past the horizon.
    """
    return min((date.today() + timedelta(days=3)).day, 28)


class TestCoreForecasting:
    def test_rrule_expansion_monthly(self) -> None:
        dates = core_forecasting.expand_rrule(
            "FREQ=MONTHLY;BYMONTHDAY=19",
            dtstart=date(2026, 1, 1),
            start=date(2026, 7, 1),
            end=date(2026, 8, 31),
        )
        assert dates == [date(2026, 7, 19), date(2026, 8, 19)]

    def test_rrule_expansion_end_bound_inclusive(self) -> None:
        """The [start, end] window includes an occurrence exactly at the end
        bound and excludes one past it — the horizon-boundary semantics the
        60-day materialization window depends on."""
        rule = "FREQ=MONTHLY;BYMONTHDAY=19"
        dtstart = date(2026, 1, 1)
        at_bound = core_forecasting.expand_rrule(
            rule, dtstart=dtstart, start=date(2026, 7, 1), end=date(2026, 8, 19)
        )
        assert at_bound == [date(2026, 7, 19), date(2026, 8, 19)]
        # End one day before the next occurrence (2026-09-19): it must be excluded.
        past_bound = core_forecasting.expand_rrule(
            rule, dtstart=dtstart, start=date(2026, 7, 1), end=date(2026, 9, 18)
        )
        assert past_bound == [date(2026, 7, 19), date(2026, 8, 19)]

    def test_until_utc_z_form_accepted(self) -> None:
        # RFC 5545 mandates UTC ("Z") UNTIL values when a time is present;
        # the naive dtstart used to make dateutil reject exactly that (#221).
        dates = core_forecasting.expand_rrule(
            "FREQ=MONTHLY;BYMONTHDAY=19;UNTIL=20260801T000000Z",
            dtstart=date(2026, 1, 1),
            start=date(2026, 7, 1),
            end=date(2026, 12, 31),
        )
        assert dates == [date(2026, 7, 19)]

    def test_sub_daily_freq_rejected(self) -> None:
        # Sub-daily rules expand to thousands of duplicate-date occurrences (#221).
        for bad in ("FREQ=HOURLY", "FREQ=MINUTELY;INTERVAL=1", "FREQ=SECONDLY", "BYMONTHDAY=19"):
            with pytest.raises(ValueError, match="FREQ"):
                core_forecasting.validate_rrule(bad)
        core_forecasting.validate_rrule("FREQ=DAILY")  # the finest allowed

    def test_dtstart_anchors_anchor_sensitive_rules(self) -> None:
        # Every-two-weeks carries no BYDAY: the phase comes from dtstart alone
        # (#231). Anchoring on the 3rd vs the 10th gives disjoint series.
        a = core_forecasting.expand_rrule(
            "FREQ=WEEKLY;INTERVAL=2",
            dtstart=date(2026, 7, 3),
            start=date(2026, 8, 1),
            end=date(2026, 8, 31),
        )
        b = core_forecasting.expand_rrule(
            "FREQ=WEEKLY;INTERVAL=2",
            dtstart=date(2026, 7, 10),
            start=date(2026, 8, 1),
            end=date(2026, 8, 31),
        )
        assert a == [date(2026, 8, 14), date(2026, 8, 28)]
        assert b == [date(2026, 8, 7), date(2026, 8, 21)]

    def test_estimate_bases(self) -> None:
        actuals = [Decimal("-214"), Decimal("-180"), Decimal("-190"), Decimal("-100")]
        EB = core_forecasting.EstimateBasis
        assert core_forecasting.estimate_amount(EB.LAST, actuals, Decimal(0)) == Decimal("-214")
        assert core_forecasting.estimate_amount(EB.ROLLING_AVG_3, actuals, Decimal(0)) == Decimal(
            "-194.6667"
        )
        assert core_forecasting.estimate_amount(EB.LAST, [], Decimal("-50")) == Decimal("-50")
        # counterparty_balance without a resolvable balance estimates like a
        # 3-period average (the service overlays the real balance, #232).
        assert core_forecasting.estimate_amount(
            EB.COUNTERPARTY_BALANCE, actuals, Decimal(0)
        ) == Decimal("-194.6667")

    def test_forecast_dip_and_recovery_annotated(self) -> None:
        start = date(2026, 7, 10)
        forecast = core_forecasting.forecast_balance(
            start_balance=Decimal("1000"),
            start=start,
            horizon_days=15,
            items=[
                core_forecasting.ExpectedItem(
                    date=date(2026, 7, 19), amount=Decimal("-1500"), label="Mortgage"
                ),
                core_forecasting.ExpectedItem(
                    date=date(2026, 7, 20), amount=Decimal("2000"), label="Paycheck"
                ),
            ],
            min_balance=Decimal("0"),
        )
        by_date = {d.date: d for d in forecast.days}
        assert by_date[date(2026, 7, 19)].balance == Decimal("-500")
        assert by_date[date(2026, 7, 20)].balance == Decimal("1500")
        # every dip is explained
        assert by_date[date(2026, 7, 19)].events[0].label == "Mortgage"
        assert forecast.first_shortfall == date(2026, 7, 19)

    def test_overdue_expected_lands_today(self) -> None:
        start = date(2026, 7, 10)
        forecast = core_forecasting.forecast_balance(
            start_balance=Decimal("100"),
            start=start,
            horizon_days=5,
            items=[
                core_forecasting.ExpectedItem(
                    date=date(2026, 7, 1), amount=Decimal("-60"), label="Late bill"
                )
            ],
        )
        assert forecast.days[0].balance == Decimal("40")


@pytest.fixture
def seeded(admin_client: TestClient) -> None:
    admin_client.post(
        "/accounts",
        json={"name": "Checking", "type": "checking", "path": "assets.checking"},
    )
    admin_client.post(
        "/categories",
        json={"name": "Housing", "type": "category_expense", "path": "expenses.housing"},
    )


class TestSchedulesApi:
    def test_fixed_schedule_materializes_expected_instances(
        self, seeded: None, client: TestClient
    ) -> None:
        resp = client.post(
            "/schedules",
            json={
                "name": "Mortgage",
                "account": "assets.checking",
                "rrule": f"FREQ=MONTHLY;BYMONTHDAY={_near_term_bymonthday()}",
                "amount_type": "fixed",
                "amount": "-1500.00",
                "category": "expenses.housing",
                "payee": "Wells Fargo Mortgage",
            },
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["status"] == "active"  # session has commit
        assert resp.json()["next_due"] is not None

        page = client.get("/transactions", params={"status": "expected"}).json()
        assert page["total"] >= 2  # 60-day horizon covers two monthly instances
        for item in page["items"]:
            assert Decimal(item["amount"]) == Decimal("-1500.00")
            assert item["origin"] == "rule"

        # Idempotent: re-materializing creates nothing new.
        first_total = page["total"]
        client.post("/schedules/materialize")
        again = client.get("/transactions", params={"status": "expected"}).json()
        assert again["total"] == first_total

    def test_propose_true_creates_a_proposal_not_active(
        self, seeded: None, client: TestClient
    ) -> None:
        agent = client.post("/tokens", json={"name": "agent", "scopes": ["read", "write"]})
        headers = {"Authorization": f"Bearer {agent.json()['token']}"}
        reader = client.post("/tokens", json={"name": "reader", "scopes": ["read"]})
        reader_headers = {"Authorization": f"Bearer {reader.json()['token']}"}
        resp = client.post(
            "/schedules",
            json={
                "name": "Internet",
                "account": "assets.checking",
                "rrule": f"FREQ=MONTHLY;BYMONTHDAY={_near_term_bymonthday()}",
                "amount_type": "fixed",
                "amount": "-80.00",
                "propose": True,
            },
            headers=headers,
        )
        assert resp.status_code == 201
        assert resp.json()["status"] == "proposed"
        assert resp.json()["source"] == "agent"
        assert resp.json()["suggestion_id"] == resp.json()["id"]  # it is a suggestion (#233)
        assert client.get("/schedules").json() == []
        # No instances until confirmed.
        page = client.get("/transactions", params={"status": "expected"}).json()
        assert page["total"] == 0
        decide = f"/suggestions/{resp.json()['id']}/decide"
        # Deciding is a write: a read-only token cannot.
        denied = client.post(decide, json={"decision": "accept"}, headers=reader_headers)
        assert denied.status_code == 403
        # The one-release /activate wrapper is retired (#347): deciding the
        # suggestion is the only path.
        assert client.post(f"/schedules/{resp.json()['id']}/activate").status_code == 404
        assert client.post(f"/schedules/{resp.json()['id']}/reject").status_code == 404
        # Human accept creates the schedule active and materializes.
        confirmed = client.post(decide, json={"decision": "accept"})
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["schedule"]["status"] == "active"
        page = client.get("/transactions", params={"status": "expected"}).json()
        assert page["total"] >= 2
        assert client.get(f"/suggestions/{resp.json()['id']}").json()["status"] == "accepted"

    def test_session_created_schedule_is_manual_source(
        self, seeded: None, client: TestClient
    ) -> None:
        # Source keys off the server-controlled token kind (session → manual).
        resp = client.post(
            "/schedules",
            json={
                "name": "Rent",
                "account": "assets.checking",
                "rrule": f"FREQ=MONTHLY;BYMONTHDAY={_near_term_bymonthday()}",
                "amount_type": "fixed",
                "amount": "-1000.00",
            },
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["source"] == "manual"

    def test_detected_schedules_require_confirmation(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        # Three monthly Netflix charges in import history.
        with migrated_engine.begin() as conn:
            account_id = conn.execute(
                text("SELECT id FROM accounts WHERE path = 'assets.checking'")
            ).scalar_one()
            cat_id = conn.execute(
                text(
                    "INSERT INTO accounts (name, type, is_virtual, path) VALUES "
                    "('Uncat', 'category_expense', true, 'expenses.uncategorized') RETURNING id"
                )
            ).scalar_one()
            payee_id = conn.execute(
                text("INSERT INTO payees (name_enc, name_bidx) VALUES (:ne, :nb) RETURNING id"),
                {"ne": crypto.encrypt("Netflix"), "nb": crypto.blind_index("netflix")},
            ).scalar_one()
            today = date.today()
            for months_ago in (3, 2, 1):
                d = (today.replace(day=15) - timedelta(days=30 * months_ago)).replace(day=15)
                tx_id = conn.execute(
                    text(
                        "INSERT INTO transactions"
                        " (date, payee_id, description_enc, status, origin) "
                        "VALUES (:d, :p, :desc, 'posted', 'import') RETURNING id"
                    ),
                    {"d": d, "p": payee_id, "desc": crypto.encrypt("NETFLIX.COM")},
                ).scalar_one()
                conn.execute(
                    text(
                        "INSERT INTO postings (transaction_id, account_id, amount) VALUES "
                        "(:tx, :acct, -15.99), (:tx, :cat, 15.99)"
                    ),
                    {"tx": tx_id, "acct": account_id, "cat": cat_id},
                )

        detected = client.post("/schedules/detect").json()
        assert detected["created"] == 1 and detected["refreshed"] == 0
        ref = detected["suggestions"][0]
        assert ref["status"] == "proposed"  # NEVER auto-active: it is a suggestion (#233)
        assert ref["source"] == "detected"
        row = client.get(f"/suggestions/{ref['suggestion_id']}").json()
        assert "FREQ=MONTHLY" in row["draft"]["rrule"]
        assert row["evidence_summary"]["count"] == 3
        assert client.get("/schedules").json() == []  # no schedule row exists yet

        # Re-detection refreshes the same row instead of duplicating.
        again = client.post("/schedules/detect").json()
        assert again["created"] == 0 and again["refreshed"] == 1
        assert client.get(f"/suggestions/{ref['suggestion_id']}").json()["seen_count"] == 2

        # No expected instances materialize while it is only suggested.
        page = client.get("/transactions", params={"status": "expected"}).json()
        assert page["total"] == 0

        # Rejecting is durable: the next detection skips the pattern.
        client.post(f"/suggestions/{ref['suggestion_id']}/decide", json={"decision": "reject"})
        assert client.post("/schedules/detect").json()["suggestions"] == []

    def test_sub_daily_rrule_is_422(self, seeded: None, client: TestClient) -> None:
        resp = client.post(
            "/schedules",
            json={
                "name": "Runaway",
                "account": "assets.checking",
                "rrule": "FREQ=HOURLY",
                "amount_type": "fixed",
                "amount": "-1.00",
            },
        )
        assert resp.status_code == 422
        assert "FREQ" in resp.text

    def test_until_z_rrule_is_accepted(self, seeded: None, client: TestClient) -> None:
        resp = client.post(
            "/schedules",
            json={
                "name": "Bounded",
                "account": "assets.checking",
                "rrule": (
                    f"FREQ=MONTHLY;BYMONTHDAY={_near_term_bymonthday()};UNTIL=20330101T000000Z"
                ),
                "amount_type": "fixed",
                "amount": "-10.00",
            },
        )
        assert resp.status_code == 201, resp.text

    def test_dtstart_is_persisted_and_anchors_materialization(
        self, seeded: None, client: TestClient
    ) -> None:
        # Biweekly with no BYDAY: without a persisted anchor the phase drifted
        # to whatever day the row was created (#221). Anchor 3 days ago.
        anchor = date.today() - timedelta(days=3)
        resp = client.post(
            "/schedules",
            json={
                "name": "Paycheck",
                "account": "assets.checking",
                "rrule": "FREQ=WEEKLY;INTERVAL=2",
                "dtstart": anchor.isoformat(),
                "amount_type": "fixed",
                "amount": "2000.00",
            },
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["dtstart"] == anchor.isoformat()
        detail = client.get(f"/schedules/{resp.json()['id']}").json()
        dates = [date.fromisoformat(i["date"]) for i in detail["instances"]]
        assert len(dates) >= 4  # 60-day horizon, every 14 days
        assert all((d - anchor).days % 14 == 0 for d in dates)
        assert dates[0] == anchor + timedelta(days=14)
        assert all(i["occurrence_date"] == i["date"] for i in detail["instances"])
        assert resp.json()["next_due"] == dates[0].isoformat()

    def _monthly(self, client: TestClient, **overrides: object) -> dict[str, object]:
        body: dict[str, object] = {
            "name": "Mortgage",
            "account": "assets.checking",
            "rrule": f"FREQ=MONTHLY;BYMONTHDAY={_near_term_bymonthday()}",
            "amount_type": "fixed",
            "amount": "-1500.00",
            "category": "expenses.housing",
        }
        body.update(overrides)
        resp = client.post("/schedules", json=body)
        assert resp.status_code == 201, resp.text
        return dict(resp.json())

    def _instances(self, client: TestClient, schedule_id: object) -> list[dict[str, object]]:
        return list(client.get(f"/schedules/{schedule_id}").json()["instances"])

    def test_date_edit_pins_the_instance_and_keeps_its_slot(
        self, seeded: None, client: TestClient
    ) -> None:
        schedule = self._monthly(client)
        first = self._instances(client, schedule["id"])[0]
        early = (date.fromisoformat(str(first["date"])) - timedelta(days=2)).isoformat()
        patched = client.patch(f"/transactions/{first['transaction_id']}", json={"date": early})
        assert patched.status_code == 200, patched.text

        # The slot (occurrence_date) is unchanged, so re-materializing does
        # NOT create a duplicate for the original date (#231).
        client.post("/schedules/materialize")
        instances = self._instances(client, schedule["id"])
        moved = next(i for i in instances if i["transaction_id"] == first["transaction_id"])
        assert moved["date"] == early
        assert moved["occurrence_date"] == first["occurrence_date"]
        assert moved["pinned"] is True
        assert [i["occurrence_date"] for i in instances].count(first["occurrence_date"]) == 1
        # next_due follows the earliest live instance, i.e. the moved date.
        assert client.get(f"/schedules/{schedule['id']}").json()["next_due"] == early

    def test_skip_keeps_the_slot_filled(self, seeded: None, client: TestClient) -> None:
        schedule = self._monthly(client)
        before = self._instances(client, schedule["id"])
        assert len(before) >= 2
        skipped = client.post(f"/transactions/{before[0]['transaction_id']}/void")
        assert skipped.status_code == 200, skipped.text
        client.post("/schedules/materialize")
        after = self._instances(client, schedule["id"])
        assert [i["occurrence_date"] for i in after] == [
            i["occurrence_date"] for i in before[1:]
        ]  # the skipped slot never comes back
        assert client.get(f"/schedules/{schedule['id']}").json()["next_due"] == after[0]["date"]

    def test_pause_releases_unpinned_instances_and_resume_restores_them(
        self, seeded: None, client: TestClient
    ) -> None:
        schedule = self._monthly(client)
        before = self._instances(client, schedule["id"])
        pinned_id = before[0]["transaction_id"]
        client.patch(f"/transactions/{pinned_id}", json={"amount": "-1600.00"})

        paused = client.post(f"/schedules/{schedule['id']}/pause")
        assert paused.status_code == 200, paused.text
        assert paused.json()["status"] == "paused"
        assert paused.json()["next_due"] is None
        live = self._instances(client, schedule["id"])
        assert [i["transaction_id"] for i in live] == [pinned_id]  # the user's row survives
        # The released rows are void with no slot; the pinned one keeps its slot.
        page = client.get("/transactions", params={"status": "void"}).json()
        assert page["total"] == len(before) - 1

        resumed = client.post(f"/schedules/{schedule['id']}/resume")
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["status"] == "active"
        restored = self._instances(client, schedule["id"])
        assert [i["occurrence_date"] for i in restored] == [i["occurrence_date"] for i in before]
        assert (
            next(i for i in restored if i["transaction_id"] == pinned_id)["amount"] == "-1600.0000"
        )

    def test_end_is_terminal(self, seeded: None, client: TestClient) -> None:
        schedule = self._monthly(client)
        ended = client.post(f"/schedules/{schedule['id']}/end")
        assert ended.status_code == 200, ended.text
        assert ended.json()["status"] == "ended"
        assert ended.json()["ended_at"] is not None
        assert ended.json()["next_due"] is None
        assert self._instances(client, schedule["id"]) == []
        assert client.post(f"/schedules/{schedule['id']}/resume").status_code == 422
        assert client.patch(f"/schedules/{schedule['id']}", json={"name": "x"}).status_code == 422
        client.post("/schedules/materialize")
        assert self._instances(client, schedule["id"]) == []

    def test_rule_edit_resyncs_slots_and_released_slots_come_back(
        self, seeded: None, client: TestClient
    ) -> None:
        day = _near_term_bymonthday()
        other = day - 1 if day > 1 else day + 1
        schedule = self._monthly(client)
        original = [i["occurrence_date"] for i in self._instances(client, schedule["id"])]

        moved = client.patch(
            f"/schedules/{schedule['id']}", json={"rrule": f"FREQ=MONTHLY;BYMONTHDAY={other}"}
        )
        assert moved.status_code == 200, moved.text
        assert moved.json()["rrule"] == f"FREQ=MONTHLY;BYMONTHDAY={other}"
        after_edit = [i["occurrence_date"] for i in moved.json()["instances"]]
        assert after_edit and all(date.fromisoformat(str(d)).day == other for d in after_edit)
        assert not set(after_edit) & set(original)

        # Editing back re-fills the released slots — a resync void frees the
        # slot, unlike a user skip.
        back = client.patch(
            f"/schedules/{schedule['id']}", json={"rrule": f"FREQ=MONTHLY;BYMONTHDAY={day}"}
        )
        assert [i["occurrence_date"] for i in back.json()["instances"]] == original

    def test_amount_edit_on_the_rule_moves_unpinned_instances_only(
        self, seeded: None, client: TestClient
    ) -> None:
        schedule = self._monthly(client)
        instances = self._instances(client, schedule["id"])
        pinned_id = instances[0]["transaction_id"]
        client.patch(f"/transactions/{pinned_id}", json={"amount": "-1234.00"})
        edited = client.patch(f"/schedules/{schedule['id']}", json={"amount": "-1550.00"})
        assert edited.status_code == 200, edited.text
        amounts = {i["transaction_id"]: i["amount"] for i in edited.json()["instances"]}
        assert amounts[pinned_id] == "-1234.0000"
        assert all(a == "-1550.0000" for tid, a in amounts.items() if tid != pinned_id)

    def test_forecast_explains_the_dip_in_one_call(self, seeded: None, client: TestClient) -> None:
        # Give checking a real starting balance of $1000.
        client.post(
            "/categories",
            json={"name": "Salary", "type": "category_income", "path": "income.salary"},
        )
        client.post(
            "/transactions",
            json={
                "account": "assets.checking",
                "category": "income.salary",
                "amount": "1000.00",
                "date": date.today().isoformat(),
                "status": "posted",
            },
        )
        # Mortgage on the 19th of next month; paycheck the day after.
        next_month = (date.today().replace(day=1) + timedelta(days=32)).replace(day=1)
        mortgage_day = next_month.replace(day=19)
        payday = next_month.replace(day=20)
        for amount, day, category, label in [
            ("-1500.00", mortgage_day, "expenses.housing", "Mortgage"),
            ("2000.00", payday, "income.salary", "Paycheck"),
        ]:
            client.post(
                "/transactions",
                json={
                    "account": "assets.checking",
                    "category": category,
                    "amount": amount,
                    "date": day.isoformat(),
                    "description": label,
                    "status": "expected",
                },
            )

        days = (payday - date.today()).days + 3
        forecast = client.get(
            "/forecast", params={"account": "assets.checking", "days": days}
        ).json()
        by_date = {d["date"]: d for d in forecast["days"]}
        assert Decimal(by_date[mortgage_day.isoformat()]["balance"]) == Decimal("-500.00")
        assert by_date[mortgage_day.isoformat()]["events"][0]["label"] == "Mortgage"
        assert Decimal(by_date[payday.isoformat()]["balance"]) == Decimal("1500.00")
        assert forecast["first_shortfall"] == mortgage_day.isoformat()

    def test_estimated_amount_improves_with_matched_actuals(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        # Schedule with estimate basis 'last' and a $-100 fallback. Weekly so
        # the 60-day horizon always holds several instances to refresh (#231).
        resp = client.post(
            "/schedules",
            json={
                "name": "Utilities",
                "account": "assets.checking",
                "rrule": "FREQ=WEEKLY",
                "amount_type": "estimated",
                "amount": "-100.00",
                "estimate_basis": "last",
                "payee": "City Utilities",
            },
        )
        schedule = resp.json()
        page = client.get("/transactions", params={"status": "expected"}).json()
        instance = page["items"][0]
        assert Decimal(instance["amount"]) == Decimal("-100.00")

        # A posted import lands at -214 and matches the instance.
        with migrated_engine.begin() as conn:
            account_id = conn.execute(
                text("SELECT id FROM accounts WHERE path = 'assets.checking'")
            ).scalar_one()
            cat_id = (
                conn.execute(
                    text("SELECT id FROM accounts WHERE path = 'expenses.uncategorized'")
                ).scalar_one_or_none()
                or conn.execute(
                    text(
                        "INSERT INTO accounts (name, type, is_virtual, path) VALUES "
                        "('Uncat', 'category_expense', true, 'expenses.uncategorized') RETURNING id"
                    )
                ).scalar_one()
            )
            payee_id = conn.execute(
                text("SELECT id FROM payees WHERE name_bidx = :nb"),
                {"nb": crypto.blind_index("city utilities")},
            ).scalar_one()
            tx_id = conn.execute(
                text(
                    "INSERT INTO transactions (date, payee_id, description_enc, status, origin) "
                    "VALUES (:d, :p, :desc, 'posted', 'import') RETURNING id"
                ),
                {"d": instance["date"], "p": payee_id, "desc": crypto.encrypt("CITY UTILITIES")},
            ).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO postings (transaction_id, account_id, amount) VALUES "
                    "(:tx, :acct, -214.00), (:tx, :cat, 214.00)"
                ),
                {"tx": tx_id, "acct": account_id, "cat": cat_id},
            )
        stats = client.post("/matching/run").json()
        assert stats["auto_matched"] == 1

        # The remaining live instances follow the matched actual (-214) on the
        # next materialization — no deletion needed (#231 estimate refresh) —
        # except a pinned one, which the user overrode.
        live_before = client.get(f"/schedules/{schedule['id']}").json()["instances"]
        assert len(live_before) >= 3, "weekly leaves several unmatched instances"
        pinned_id = live_before[-1]["transaction_id"]
        client.patch(f"/transactions/{pinned_id}", json={"amount": "-99.00"})
        client.post("/schedules/materialize")
        after = {
            i["transaction_id"]: i["amount"]
            for i in client.get(f"/schedules/{schedule['id']}").json()["instances"]
        }
        assert after[pinned_id] == "-99.0000"
        assert all(a == "-214.0000" for tid, a in after.items() if tid != pinned_id)
        assert schedule["amount_type"] == "estimated"
        refreshes = client.get("/audit", params={"action": "schedule.refresh"}).json()["items"]
        assert len(refreshes) >= 1  # guardrail 5: the refresh is an audited write


# Instance operations a user can perform, drawn at random (#231, guardrail 3:
# the slot invariants are ledger invariants and get a property test).
_OPS = st.lists(
    st.one_of(
        st.just(("materialize",)),
        st.tuples(st.just("skip"), st.integers(0, 3)),
        st.tuples(st.just("shift"), st.integers(0, 3), st.integers(-3, 3).filter(lambda d: d != 0)),
        st.tuples(st.just("override"), st.integers(0, 3)),
        st.just(("pause",)),
        st.just(("resume",)),
        st.tuples(st.just("rule"), st.integers(1, 28)),
    ),
    min_size=1,
    max_size=8,
)


class TestSlotInvariants:
    @settings(
        max_examples=12, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
    )
    @given(ops=_OPS)
    def test_materialization_is_idempotent_under_instance_edits(
        self, seeded: None, client: TestClient, ops: list[tuple[object, ...]]
    ) -> None:
        created = client.post(
            "/schedules",
            json={
                "name": "Gym",
                "account": "assets.checking",
                "rrule": "FREQ=WEEKLY",
                "amount_type": "fixed",
                "amount": "-60.00",
            },
        )
        assert created.status_code == 201, created.text
        sid = created.json()["id"]

        def live() -> list[dict[str, object]]:
            return list(client.get(f"/schedules/{sid}").json()["instances"])

        for op in ops:
            rows = live()
            kind = op[0]
            if kind == "materialize":
                client.post("/schedules/materialize")
            elif kind == "pause":
                client.post(f"/schedules/{sid}/pause")
            elif kind == "resume":
                client.post(f"/schedules/{sid}/resume")
            elif kind == "rule":
                client.patch(
                    f"/schedules/{sid}", json={"rrule": f"FREQ=MONTHLY;BYMONTHDAY={op[1]}"}
                )
            elif rows:
                target = rows[int(op[1]) % len(rows)]  # type: ignore[arg-type]
                tid = target["transaction_id"]
                if kind == "skip":
                    client.post(f"/transactions/{tid}/void")
                elif kind == "shift":
                    moved = date.fromisoformat(str(target["date"])) + timedelta(days=int(op[2]))  # type: ignore[arg-type]
                    client.patch(f"/transactions/{tid}", json={"date": moved.isoformat()})
                elif kind == "override":
                    client.patch(f"/transactions/{tid}", json={"amount": "-61.00"})

            # Invariants after every step:
            after = live()
            slots = [i["occurrence_date"] for i in after]
            assert len(slots) == len(set(slots)), "two live instances share a slot"
            detail = client.get(f"/schedules/{sid}").json()
            if detail["status"] == "active":
                assert detail["next_due"] == (after[0]["date"] if after else detail["next_due"])
            else:
                assert detail["next_due"] is None
            # Materializing again changes nothing (idempotent on slots).
            client.post("/schedules/materialize")
            assert [i["transaction_id"] for i in live()] == [i["transaction_id"] for i in after]


@pytest.fixture
def with_card(seeded: None, client: TestClient) -> None:
    resp = client.post(
        "/accounts",
        json={"name": "Citi Card", "type": "credit_card", "path": "liabilities.citicard"},
    )
    assert resp.status_code == 201, resp.text


def _import_row(
    engine: Engine,
    *,
    account_path: str,
    amount: str,
    on: date,
    payee: str,
    description: str,
    status: str = "posted",
    origin: str = "import",
) -> str:
    """A one-sided bank row as the importer lands it (origin import; posted
    unless ``status`` says otherwise). ``origin='system'`` is the bank
    reality Purser re-derives (decision 61's review scope holds it without a
    source fetch)."""
    with engine.begin() as conn:
        account_id = conn.execute(
            text("SELECT id FROM accounts WHERE path = :p"), {"p": account_path}
        ).scalar_one()
        cat_id = (
            conn.execute(
                text("SELECT id FROM accounts WHERE path = 'expenses.uncategorized'")
            ).scalar_one_or_none()
            or conn.execute(
                text(
                    "INSERT INTO accounts (name, type, is_virtual, path) VALUES "
                    "('Uncat', 'category_expense', true, 'expenses.uncategorized') RETURNING id"
                )
            ).scalar_one()
        )
        payee_id = (
            conn.execute(
                text("SELECT id FROM payees WHERE name_bidx = :nb"),
                {"nb": crypto.blind_index(payee.lower())},
            ).scalar_one_or_none()
            or conn.execute(
                text("INSERT INTO payees (name_enc, name_bidx) VALUES (:ne, :nb) RETURNING id"),
                {"ne": crypto.encrypt(payee), "nb": crypto.blind_index(payee.lower())},
            ).scalar_one()
        )
        tx_id = conn.execute(
            text(
                "INSERT INTO transactions (date, payee_id, description_enc, status, origin) "
                "VALUES (:d, :p, :desc, :status, :origin) RETURNING id"
            ),
            {
                "d": on,
                "p": payee_id,
                "desc": crypto.encrypt(description),
                "status": status,
                "origin": origin,
            },
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO postings (transaction_id, account_id, amount) VALUES "
                "(:tx, :acct, :amt), (:tx, :cat, :neg)"
            ),
            {
                "tx": tx_id,
                "acct": account_id,
                "cat": cat_id,
                "amt": Decimal(amount),
                "neg": -Decimal(amount),
            },
        )
    return str(tx_id)


class TestTransferSchedules:
    """Transfer-aware schedules (#232, decision 56)."""

    def _payment(self, client: TestClient, **overrides: object) -> dict[str, object]:
        body: dict[str, object] = {
            "name": "Citi payment",
            "account": "assets.checking",
            "counterparty_account": "liabilities.citicard",
            "rrule": f"FREQ=MONTHLY;BYMONTHDAY={_near_term_bymonthday()}",
            "amount_type": "fixed",
            "amount": "-500.00",
        }
        body.update(overrides)
        resp = client.post("/schedules", json=body)
        assert resp.status_code == 201, resp.text
        return dict(resp.json())

    def test_instances_are_two_leg_expected_transfers(
        self, with_card: None, client: TestClient
    ) -> None:
        schedule = self._payment(client)
        assert schedule["counterparty_account_id"] is not None
        instances = client.get(f"/schedules/{schedule['id']}").json()["instances"]
        assert instances and all(i["amount"] == "-500.0000" for i in instances)
        detail = client.get(f"/transactions/{instances[0]['transaction_id']}").json()
        legs = {p["account_path"]: Decimal(p["amount"]) for p in detail["postings"]}
        assert legs == {
            "assets.checking": Decimal("-500.00"),
            "liabilities.citicard": Decimal("500.00"),
        }
        assert detail["status"] == "expected"
        # The late-bills report sees the payer's leg, not null.
        late = client.get("/matching/unmatched", params={"direction": "expected"}).json()
        assert {r["transaction_id"] for r in late} >= {i["transaction_id"] for i in instances}
        assert all(Decimal(r["amount"]) == Decimal("-500.00") for r in late)

    def test_category_and_counterparty_are_exclusive(
        self, with_card: None, client: TestClient
    ) -> None:
        resp = client.post(
            "/schedules",
            json={
                "name": "Both",
                "account": "assets.checking",
                "counterparty_account": "liabilities.citicard",
                "category": "expenses.housing",
                "rrule": "FREQ=MONTHLY;BYMONTHDAY=1",
                "amount": "-1.00",
            },
        )
        assert resp.status_code == 422
        assert "either" in resp.text
        # counterparty_balance needs a counterparty to read.
        resp = client.post(
            "/schedules",
            json={
                "name": "Flat",
                "account": "assets.checking",
                "category": "expenses.housing",
                "rrule": "FREQ=MONTHLY;BYMONTHDAY=1",
                "amount_type": "estimated",
                "estimate_basis": "counterparty_balance",
            },
        )
        assert resp.status_code == 422

    def test_expected_transfer_matches_the_merged_transfer_and_stops_double_counting(
        self, with_card: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        # Give checking an opening balance so the forecast has something to lose.
        client.post(
            "/categories",
            json={"name": "Salary", "type": "category_income", "path": "income.salary"},
        )
        client.post(
            "/transactions",
            json={
                "account": "assets.checking",
                "category": "income.salary",
                "amount": "2000.00",
                "date": date.today().isoformat(),
                "status": "posted",
            },
        )
        schedule = self._payment(client)
        first = client.get(f"/schedules/{schedule['id']}").json()["instances"][0]
        due = date.fromisoformat(str(first["date"]))

        # The bank rows land on both sides and get linked into one transfer
        # (the auto-linker does this at import; a human confirm is the same
        # merge with the confirming actor's origin).
        out_id = _import_row(
            migrated_engine,
            account_path="assets.checking",
            amount="-500.00",
            on=due,
            payee="CITI AUTOPAY",
            description="CITI AUTOPAY PAYMENT",
        )
        in_id = _import_row(
            migrated_engine,
            account_path="liabilities.citicard",
            amount="500.00",
            on=due,
            payee="PAYMENT THANK YOU",
            description="PAYMENT - THANK YOU",
        )
        merged = client.post(
            "/matching/confirm-transfer", json={"outgoing_id": out_id, "incoming_id": in_id}
        )
        assert merged.status_code == 200, merged.text
        merged_id = merged.json()["merged_transaction_id"]

        stats = client.post("/matching/run").json()
        assert stats["auto_matched"] == 1
        detail = client.get(f"/transactions/{first['transaction_id']}").json()
        assert detail["status"] == "void"
        assert detail["superseded_by"] == merged_id
        assert first["transaction_id"] not in {
            r["transaction_id"]
            for r in client.get("/matching/unmatched", params={"direction": "expected"}).json()
        }

        # No double count: the payment is in the opening balance (posted) and
        # the expected is gone, so the forecast holds 1,500 through the due date.
        days = (due - date.today()).days + 2
        forecast = client.get(
            "/forecast", params={"account": "assets.checking", "days": max(days, 1)}
        ).json()
        assert Decimal(forecast["start_balance"]) == Decimal("1500.00")
        by_date = {d["date"]: d for d in forecast["days"]}
        assert by_date[due.isoformat()]["events"] == []
        assert Decimal(by_date[due.isoformat()]["balance"]) == Decimal("1500.00")

        # Unmatch recreates the two-leg expected in the same slot.
        with migrated_engine.connect() as conn:
            mid = conn.execute(
                text("SELECT id FROM matches WHERE expected_id = :e"),
                {"e": first["transaction_id"]},
            ).scalar_one()
        undone = client.post(f"/matching/{mid}/unmatch")
        assert undone.status_code == 200, undone.text
        clone = client.get(f"/transactions/{undone.json()['recreated_expected_id']}").json()
        assert clone["status"] == "expected"
        assert {p["account_path"] for p in clone["postings"]} == {
            "assets.checking",
            "liabilities.citicard",
        }
        restored = client.get(f"/schedules/{schedule['id']}").json()["instances"]
        assert [i["occurrence_date"] for i in restored].count(first["occurrence_date"]) == 1

    def test_counterparty_balance_sizes_the_payment_from_the_card(
        self, with_card: None, client: TestClient
    ) -> None:
        client.post(
            "/categories",
            json={"name": "Dining", "type": "category_expense", "path": "expenses.dining"},
        )
        # Two purchases on the card: it owes 340.
        for amount in ("-120.00", "-220.00"):
            client.post(
                "/transactions",
                json={
                    "account": "liabilities.citicard",
                    "category": "expenses.dining",
                    "amount": amount,
                    "date": date.today().isoformat(),
                    "status": "posted",
                },
            )
        schedule = self._payment(
            client, amount_type="estimated", estimate_basis="counterparty_balance", amount=None
        )
        instances = client.get(f"/schedules/{schedule['id']}").json()["instances"]
        assert instances and all(i["amount"] == "-340.0000" for i in instances)

        # Another purchase: the live instances follow the balance on the next
        # materialization (estimate refresh), so "pay the statement" stays true.
        client.post(
            "/transactions",
            json={
                "account": "liabilities.citicard",
                "category": "expenses.dining",
                "amount": "-60.00",
                "date": date.today().isoformat(),
                "status": "posted",
            },
        )
        client.post("/schedules/materialize")
        instances = client.get(f"/schedules/{schedule['id']}").json()["instances"]
        assert all(i["amount"] == "-400.0000" for i in instances)

    def test_reshaping_flat_to_transfer_releases_old_instances(
        self, with_card: None, client: TestClient
    ) -> None:
        flat = client.post(
            "/schedules",
            json={
                "name": "Card",
                "account": "assets.checking",
                "category": "expenses.housing",
                "rrule": f"FREQ=MONTHLY;BYMONTHDAY={_near_term_bymonthday()}",
                "amount": "-500.00",
            },
        ).json()
        before = client.get(f"/schedules/{flat['id']}").json()["instances"]
        reshaped = client.patch(
            f"/schedules/{flat['id']}", json={"counterparty_account": "liabilities.citicard"}
        )
        assert reshaped.status_code == 200, reshaped.text
        assert reshaped.json()["counterparty_account_id"] is not None
        after = reshaped.json()["instances"]
        assert [i["occurrence_date"] for i in after] == [i["occurrence_date"] for i in before]
        assert not {i["transaction_id"] for i in after} & {i["transaction_id"] for i in before}
        legs = client.get(f"/transactions/{after[0]['transaction_id']}").json()["postings"]
        assert {p["account_path"] for p in legs} == {"assets.checking", "liabilities.citicard"}


def _card_purchase(client: TestClient, amount: str, card: str = "liabilities.citicard") -> None:
    resp = client.post(
        "/transactions",
        json={
            "account": card,
            "category": "expenses.housing",
            "amount": amount,
            "date": date.today().isoformat(),
            "status": "posted",
        },
    )
    assert resp.status_code == 201, resp.text


def _legs(client: TestClient, tx_id: object) -> dict[str, Decimal]:
    detail = client.get(f"/transactions/{tx_id}").json()
    return {p["account_path"]: Decimal(p["amount"]) for p in detail["postings"]}


def _pay_the_card(client: TestClient, card: str = "liabilities.citicard") -> str:
    """A card-payment schedule sized from the card's balance (decision 56)."""
    resp = client.post(
        "/schedules",
        json={
            "name": "Citi payment",
            "account": "assets.checking",
            "counterparty_account": card,
            "rrule": f"FREQ=MONTHLY;BYMONTHDAY={_near_term_bymonthday()}",
            "amount_type": "estimated",
            "estimate_basis": "counterparty_balance",
        },
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


def _transfer_create(
    engine: Engine, *, amount: str, status: str, schedule_id: str | None = None
) -> str:
    """checking -> card, written through the ledger (REST creates no expected rows)."""
    import uuid as _uuid

    from purser import ledger as ledger_service
    from purser.core import ledger as core
    from purser.db import get_sessionmaker

    with get_sessionmaker()() as session:
        tx = ledger_service.create_transfer(
            session,
            from_account_ref="assets.checking",
            to_account_ref="liabilities.citicard",
            amount=Decimal(amount),
            date=date.today(),
            description="Manual transfer",
            requested_status=core.Status(status),
            origin=core.Origin.RULE,
            token=None,
            schedule_id=_uuid.UUID(schedule_id) if schedule_id else None,
        )
        session.commit()
        return str(tx.id)


class TestTransferInstanceOverride:
    """Overriding a transfer instance moves both legs (#350, decision 78)."""

    def test_card_side_amount_moves_both_legs_pins_and_survives_refresh(
        self, with_card: None, client: TestClient
    ) -> None:
        _card_purchase(client, "-340.00")
        sid = _pay_the_card(client)
        first, second = client.get(f"/schedules/{sid}").json()["instances"][:2]
        resp = client.patch(
            f"/transactions/{first['transaction_id']}",
            json={"amount": "8347.00", "account": "liabilities.citicard"},
        )
        assert resp.status_code == 200, resp.text
        assert _legs(client, first["transaction_id"]) == {
            "assets.checking": Decimal("-8347.00"),
            "liabilities.citicard": Decimal("8347.00"),
        }
        audit = client.get("/audit", params={"action": "transaction.update"}).json()["items"]
        (row,) = [a for a in audit if a["entity_id"] == first["transaction_id"]]
        assert {p["amount"] for p in row["before_after"]["after"]["postings"]} == {
            "-8347.0000",
            "8347.0000",
        }

        # The card moves: the un-pinned instance follows it, the override stays.
        _card_purchase(client, "-60.00")
        client.post("/schedules/materialize")
        live = {i["transaction_id"]: i for i in client.get(f"/schedules/{sid}").json()["instances"]}
        assert live[first["transaction_id"]]["pinned"] is True
        assert live[first["transaction_id"]]["amount"] == "-8347.0000"
        assert live[second["transaction_id"]]["pinned"] is False
        assert live[second["transaction_id"]]["amount"] == "-400.0000"

    def test_date_only_override_of_a_transfer_instance(
        self, with_card: None, client: TestClient
    ) -> None:
        _card_purchase(client, "-340.00")
        sid = _pay_the_card(client)
        first = client.get(f"/schedules/{sid}").json()["instances"][0]
        early = (date.fromisoformat(first["date"]) - timedelta(days=1)).isoformat()
        resp = client.patch(f"/transactions/{first['transaction_id']}", json={"date": early})
        assert resp.status_code == 200, resp.text
        moved = client.get(f"/schedules/{sid}").json()["instances"][0]
        assert (moved["date"], moved["amount"], moved["pinned"]) == (early, "-340.0000", True)

    def test_a_sign_that_flips_the_direction_is_refused_and_changes_nothing(
        self, with_card: None, client: TestClient
    ) -> None:
        _card_purchase(client, "-340.00")
        sid = _pay_the_card(client)
        first = client.get(f"/schedules/{sid}").json()["instances"][0]
        before = _legs(client, first["transaction_id"])
        # No account: the amount is the schedule account's (checking's) leg.
        resp = client.patch(f"/transactions/{first['transaction_id']}", json={"amount": "8347.00"})
        assert resp.status_code == 422
        assert "assets.checking leg is -340.0000" in resp.json()["detail"]
        resp = client.patch(
            f"/transactions/{first['transaction_id']}",
            json={"amount": "-8347.00", "account": "liabilities.citicard"},
        )
        assert resp.status_code == 422
        assert "liabilities.citicard leg is 340.0000" in resp.json()["detail"]
        assert _legs(client, first["transaction_id"]) == before
        assert client.get(f"/schedules/{sid}").json()["instances"][0]["pinned"] is False

    def test_account_must_be_a_side(self, with_card: None, client: TestClient) -> None:
        _card_purchase(client, "-340.00")
        sid = _pay_the_card(client)
        first = client.get(f"/schedules/{sid}").json()["instances"][0]
        resp = client.patch(
            f"/transactions/{first['transaction_id']}",
            json={"amount": "-100.00", "account": "expenses.housing"},
        )
        assert resp.status_code == 422
        assert "not a side of this transfer" in resp.json()["detail"]

    def test_non_instance_expected_transfer_needs_a_side(
        self, with_card: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        tx_id = _transfer_create(migrated_engine, amount="100.00", status="expected")
        resp = client.patch(f"/transactions/{tx_id}", json={"amount": "-120.00"})
        assert resp.status_code == 422
        assert "name the side" in resp.json()["detail"]
        resp = client.patch(
            f"/transactions/{tx_id}", json={"amount": "-120.00", "account": "assets.checking"}
        )
        assert resp.status_code == 200, resp.text
        assert _legs(client, tx_id) == {
            "assets.checking": Decimal("-120.00"),
            "liabilities.citicard": Decimal("120.00"),
        }

    def test_pending_manual_transfer_moves_both_balances(
        self, with_card: None, client: TestClient
    ) -> None:
        created = client.post(
            "/transactions/transfer",
            json={
                "from_account": "assets.checking",
                "to_account": "liabilities.citicard",
                "amount": "100.00",
                "date": date.today().isoformat(),
                "status": "pending",
            },
        )
        assert created.status_code == 201, created.text

        def balances() -> dict[str, Decimal]:
            rows = client.get("/accounts", params={"include_balances": True}).json()
            return {a["path"]: Decimal(a["balance"]) for a in rows}

        assert balances()["liabilities.citicard"] == Decimal("100.00")
        resp = client.patch(
            f"/transactions/{created.json()['id']}",
            json={"amount": "250.00", "account": "liabilities.citicard"},
        )
        assert resp.status_code == 200, resp.text
        after = balances()
        assert after["assets.checking"] == Decimal("-250.00")
        assert after["liabilities.citicard"] == Decimal("250.00")

    def test_bank_backed_split_and_posted_transfers_are_refused(
        self, with_card: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        on = date(2026, 7, 15)

        def merge(out_amount: str, in_amount: str, status: str, **extra: str) -> str:
            out_id = _import_row(
                migrated_engine,
                account_path="assets.checking",
                amount=out_amount,
                on=on,
                payee="CITI AUTOPAY",
                description="CITI AUTOPAY PAYMENT",
                status=status,
            )
            in_id = _import_row(
                migrated_engine,
                account_path="liabilities.citicard",
                amount=in_amount,
                on=on,
                payee="PAYMENT THANK YOU",
                description="PAYMENT - THANK YOU",
                status=status,
            )
            resp = client.post(
                "/matching/confirm-transfer",
                json={"outgoing_id": out_id, "incoming_id": in_id, **extra},
            )
            assert resp.status_code == 200, resp.text
            return str(resp.json()["merged_transaction_id"])

        bank_pending = merge("-500.00", "500.00", "pending")
        split = merge("-612.40", "540.15", "pending", remainder_category="expenses.housing")
        posted = merge("-75.00", "75.00", "posted")
        manual_posted = client.post(
            "/transactions/transfer",
            json={
                "from_account": "assets.checking",
                "to_account": "liabilities.citicard",
                "amount": "40.00",
                "date": on.isoformat(),
                "status": "posted",
            },
        ).json()["id"]
        card = "liabilities.citicard"
        for tx_id, body, message in (
            (bank_pending, {"amount": "450.00", "account": card}, "unlink it first"),
            (split, {"amount": "500.00", "account": card}, "split loan payment"),
            (posted, {"date": "2026-07-16"}, "corrected by unlinking it"),
            (posted, {"amount": "80.00", "account": card}, "corrected by unlinking it"),
            (manual_posted, {"description": "Card"}, "convert it back to a category"),
        ):
            before = _legs(client, tx_id)
            resp = client.patch(f"/transactions/{tx_id}", json=body)
            assert resp.status_code == 422, (body, resp.text)
            assert message in resp.json()["detail"]
            assert _legs(client, tx_id) == before

        # Category plus amount on a transfer keeps its "separately" error.
        resp = client.patch(
            f"/transactions/{manual_posted}",
            json={"category": "expenses.housing", "account": card, "amount": "1.00"},
        )
        assert resp.status_code == 422
        assert "separately" in resp.json()["detail"]


_CARD_SEQ = itertools.count()
_OVERRIDES = st.lists(
    st.tuples(
        st.sampled_from(["assets.checking", "card"]),
        st.integers(min_value=0, max_value=3),
        st.decimals(min_value=Decimal("0.0001"), max_value=Decimal("99999.9999"), places=4),
        st.booleans(),  # flip the sign: refused, and nothing changes
        st.decimals(min_value=Decimal("0.01"), max_value=Decimal("999.99"), places=2),
    ),
    min_size=1,
    max_size=5,
)


class TestTransferOverrideInvariants:
    """Guardrail 3 for #350: an override of a counterparty_balance transfer
    instance from either side keeps the instance zero-sum, takes the amount
    on the side it names, pins it, and leaves every other instance on the
    card's balance — and a direction flip changes nothing."""

    @settings(
        max_examples=10, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
    )
    @given(ops=_OVERRIDES)
    def test_overrides_from_either_side(
        self, seeded: None, client: TestClient, ops: list[tuple[object, ...]]
    ) -> None:
        # The fixture runs once for all examples: each example pays its own card.
        card = f"liabilities.card_{next(_CARD_SEQ)}"
        resp = client.post("/accounts", json={"name": "Card", "type": "credit_card", "path": card})
        assert resp.status_code == 201, resp.text
        _card_purchase(client, "-340.00", card)
        owed = Decimal("340.00")
        sid = _pay_the_card(client, card)
        overrides: dict[str, Decimal] = {}  # instance -> its checking leg

        def check() -> None:
            live = client.get(f"/schedules/{sid}").json()["instances"]
            assert live
            for instance in live:
                tid = instance["transaction_id"]
                legs = _legs(client, tid)
                assert sum(legs.values()) == 0
                assert set(legs) == {"assets.checking", card}
                expected = overrides.get(tid, -owed)
                assert legs["assets.checking"] == expected
                assert legs[card] == -expected
                assert instance["pinned"] is (tid in overrides)

        for side_ref, index, magnitude, flip, purchase in ops:
            side = card if side_ref == "card" else "assets.checking"
            rows = client.get(f"/schedules/{sid}").json()["instances"]
            target = rows[int(index) % len(rows)]  # type: ignore[call-overload]
            tid = target["transaction_id"]
            amount = Decimal(magnitude) if side == card else -Decimal(magnitude)  # type: ignore[arg-type]
            before = _legs(client, tid)
            resp = client.patch(
                f"/transactions/{tid}",
                json={"amount": str(-amount if flip else amount), "account": side},
            )
            if flip:
                assert resp.status_code == 422, resp.text
                assert "would reverse" in resp.json()["detail"]
                assert _legs(client, tid) == before
            else:
                assert resp.status_code == 200, resp.text
                overrides[tid] = amount if side == "assets.checking" else -amount
                assert _legs(client, tid)[side] == amount
            check()
            # The card moves; materialize refreshes only what is not pinned.
            _card_purchase(client, str(-Decimal(purchase)), card)  # type: ignore[arg-type]
            owed += Decimal(purchase)  # type: ignore[arg-type]
            client.post("/schedules/materialize")
            check()


def _bill(client: TestClient, **overrides: object) -> dict[str, object]:
    """A payee-less flat bill: its name is its instances' payee too."""
    body: dict[str, object] = {
        "name": "Gym",
        "account": "assets.checking",
        "rrule": f"FREQ=MONTHLY;BYMONTHDAY={_near_term_bymonthday()}",
        "amount_type": "fixed",
        "amount": "-60.00",
        "category": "expenses.housing",
    }
    body.update(overrides)
    resp = client.post("/schedules", json=body)
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


def _live(client: TestClient, schedule_id: object) -> dict[str, dict[str, object]]:
    rows = client.get(f"/schedules/{schedule_id}").json()["instances"]
    return {str(i["transaction_id"]): i for i in rows}


def _tx(client: TestClient, tx_id: object) -> dict[str, object]:
    resp = client.get(f"/transactions/{tx_id}")
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


def _audit(client: TestClient, action: str) -> list[dict[str, object]]:
    return list(client.get("/audit", params={"action": action}).json()["items"])


class TestInstanceLabels:
    """A renamed schedule relabels its live instances (#353, decision 79)."""

    def test_rename_relabels_a_payee_less_bill_everywhere_and_it_keeps_estimating(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        sid = _bill(client, amount_type="estimated", estimate_basis="last")["id"]
        first, second = list(_live(client, sid))
        resp = client.patch(f"/schedules/{sid}", json={"name": "Fitness club"})
        assert resp.status_code == 200, resp.text

        for tid in (first, second):
            tx = _tx(client, tid)
            assert (tx["description"], tx["payee"]) == ("Fitness club", "Fitness club")
        assert not any(i["pinned"] for i in _live(client, sid).values())
        (relabel,) = _audit(client, "schedule.relabel")
        change = relabel["before_after"]
        assert change["before"]["name"] == "Gym"  # type: ignore[index]
        assert sorted(change["after"]["instances"]) == sorted([first, second])  # type: ignore[index]

        # Materialized and projected forecast events both carry the new name.
        forecast = client.get("/forecast", params={"account": "assets.checking", "days": 120})
        events = [e for d in forecast.json()["days"] for e in d["events"]]
        assert {e["label"] for e in events} == {"Fitness club"}
        assert any(e["projected"] for e in events) and any(e["transaction_id"] for e in events)
        # whats_new's bills-due list names instances by payee first.
        from purser import digest
        from purser.db import get_sessionmaker

        with get_sessionmaker()() as session:
            names = {i["name"] for i in digest.whats_new(session)["upcoming"]["items"]}
        assert names == {"Fitness club"}

        # Still the rule's: a confirmed match moves the estimate and the
        # relabeled survivor follows it.
        bank = _import_row(
            migrated_engine,
            account_path="assets.checking",
            amount="-75.00",
            on=date.fromisoformat(str(_live(client, sid)[first]["date"])),
            payee="FITNESS CLUB",
            description="FITNESS CLUB DUES",
        )
        matched = client.post("/matching/match", json={"expected_id": first, "posted_id": bank})
        assert matched.status_code == 200, matched.text
        client.post("/schedules/materialize")
        survivor = _live(client, sid)[second]
        assert (survivor["amount"], survivor["pinned"]) == ("-75.0000", False)

    def test_transfer_instances_take_the_name_and_no_payee(
        self, with_card: None, client: TestClient
    ) -> None:
        resp = client.post(
            "/schedules",
            json={
                "name": "Citi payment",
                "account": "assets.checking",
                "counterparty_account": "liabilities.citicard",
                "rrule": f"FREQ=MONTHLY;BYMONTHDAY={_near_term_bymonthday()}",
                "amount": "-500.00",
            },
        )
        sid = resp.json()["id"]
        client.patch(f"/schedules/{sid}", json={"name": "Citi card payment"})
        for tid in _live(client, sid):
            tx = _tx(client, tid)
            assert (tx["description"], tx["payee"]) == ("Citi card payment", None)

    def test_overrides_and_custom_labels(self, seeded: None, client: TestClient) -> None:
        sid = _bill(client)["id"]
        first, second = list(_live(client, sid))
        client.patch(f"/transactions/{first}", json={"amount": "-99.00"})  # an override: pins
        labelled = client.patch(f"/transactions/{second}", json={"description": "My gym"})
        assert labelled.status_code == 200, labelled.text
        live = _live(client, sid)
        assert (live[first]["pinned"], live[second]["pinned"]) == (True, False)

        client.patch(f"/schedules/{sid}", json={"name": "Fitness club"})
        pinned, custom = _tx(client, first), _tx(client, second)
        assert (pinned["description"], pinned["payee"]) == ("Fitness club", "Fitness club")
        assert (custom["description"], custom["payee"]) == ("My gym", "Fitness club")
        # The pinned amount stays; the label-edited row still follows the rule.
        client.patch(f"/schedules/{sid}", json={"amount": "-65.00"})
        live = _live(client, sid)
        assert (live[first]["amount"], live[first]["pinned"]) == ("-99.0000", True)
        assert (live[second]["amount"], live[second]["pinned"]) == ("-65.0000", False)

    def test_a_paused_schedules_pinned_survivor_is_relabeled(
        self, seeded: None, client: TestClient
    ) -> None:
        sid = _bill(client)["id"]
        first, _second = list(_live(client, sid))
        moved = date.fromisoformat(str(_live(client, sid)[first]["date"])) + timedelta(days=1)
        client.patch(f"/transactions/{first}", json={"date": moved.isoformat()})
        client.post(f"/schedules/{sid}/pause")
        assert list(_live(client, sid)) == [first]
        resp = client.patch(f"/schedules/{sid}", json={"name": "Fitness club"})
        assert resp.status_code == 200, resp.text
        assert _tx(client, first)["description"] == "Fitness club"

    def test_an_unchanged_name_writes_no_relabel(self, seeded: None, client: TestClient) -> None:
        sid = _bill(client)["id"]
        client.patch(f"/schedules/{sid}", json={"name": "Gym"})
        client.patch(f"/schedules/{sid}", json={"name": "Gym", "amount": "-61.00"})
        assert _audit(client, "schedule.relabel") == []

    def test_a_rule_payee_moves_with_a_payee_edit_not_a_rename(
        self, seeded: None, client: TestClient
    ) -> None:
        sid = _bill(client, payee="Planet Fitness")["id"]
        client.patch(f"/schedules/{sid}", json={"name": "Fitness club"})
        for tid in _live(client, sid):
            tx = _tx(client, tid)
            assert (tx["description"], tx["payee"]) == ("Fitness club", "Planet Fitness")
        client.patch(f"/schedules/{sid}", json={"payee": "PF Gyms"})
        assert {_tx(client, tid)["payee"] for tid in _live(client, sid)} == {"PF Gyms"}


class TestResetInstance:
    """reset_instance hands an instance back to the rule (#353, decision 79)."""

    def test_reset_restores_date_amount_label_payee_category_and_unpins(
        self, seeded: None, client: TestClient
    ) -> None:
        client.post(
            "/categories",
            json={"name": "Fitness", "type": "category_expense", "path": "expenses.fitness"},
        )
        sid = _bill(client)["id"]
        first = next(iter(_live(client, sid).values()))
        slot = str(first["occurrence_date"])
        edited = client.patch(
            f"/transactions/{first['transaction_id']}",
            json={
                "amount": "-99.00",
                "date": (date.fromisoformat(slot) - timedelta(days=2)).isoformat(),
                "description": "Gym (annual fee)",
                "payee": "Someone Else",
                "category": "expenses.fitness",
            },
        )
        assert edited.status_code == 200, edited.text
        assert _live(client, sid)[str(first["transaction_id"])]["pinned"] is True

        resp = client.post(f"/transactions/{first['transaction_id']}/reset-instance")
        assert resp.status_code == 200, resp.text
        tx = resp.json()
        assert (tx["date"], tx["description"], tx["payee"], tx["status"]) == (
            slot,
            "Gym",
            "Gym",
            "expected",
        )
        assert tx["category_path"] == "expenses.housing"
        assert Decimal(tx["amount"]) == Decimal("-60.00")
        assert _live(client, sid)[str(first["transaction_id"])]["pinned"] is False
        assert client.get(f"/schedules/{sid}").json()["next_due"] == slot
        (row,) = _audit(client, "schedule.instance_reset")
        assert row["before_after"]["before"]["pinned"] is True  # type: ignore[index]
        assert row["before_after"]["after"]["pinned"] is False  # type: ignore[index]

    def test_a_slot_the_rule_no_longer_produces_is_released(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        sid = _bill(client)["id"]
        first = next(iter(_live(client, sid)))
        client.patch(f"/transactions/{first}", json={"amount": "-99.00"})
        day = _near_term_bymonthday() % 28 + 1  # a different day of the month
        client.patch(f"/schedules/{sid}", json={"rrule": f"FREQ=MONTHLY;BYMONTHDAY={day}"})
        assert first in _live(client, sid)  # pinned: survived the rule edit

        resp = client.post(f"/transactions/{first}/reset-instance")
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "void"
        assert first not in _live(client, sid)
        with migrated_engine.connect() as conn:
            slot = conn.execute(
                text("SELECT occurrence_date FROM transactions WHERE id = :t"), {"t": first}
            ).scalar_one()
        assert slot is None  # released, like a resync

    def test_reset_on_a_transfer_instance_sets_both_legs(
        self, with_card: None, client: TestClient
    ) -> None:
        _card_purchase(client, "-340.00")
        sid = _pay_the_card(client)
        first = next(iter(_live(client, sid)))
        client.patch(
            f"/transactions/{first}", json={"amount": "8347.00", "account": "liabilities.citicard"}
        )
        _card_purchase(client, "-60.00")
        resp = client.post(f"/transactions/{first}/reset-instance")
        assert resp.status_code == 200, resp.text
        assert _legs(client, first) == {
            "assets.checking": Decimal("-400.00"),
            "liabilities.citicard": Decimal("400.00"),
        }
        assert _live(client, sid)[first]["pinned"] is False

    def test_a_pinned_instance_left_over_from_a_reshape_is_rematerialized(
        self, with_card: None, client: TestClient
    ) -> None:
        sid = _bill(client)["id"]
        first = next(iter(_live(client, sid).values()))
        tid = str(first["transaction_id"])
        client.patch(f"/transactions/{tid}", json={"amount": "-99.00"})
        client.patch(f"/schedules/{sid}", json={"counterparty_account": "liabilities.citicard"})
        assert _live(client, sid)[tid]["pinned"] is True  # flat legs under a transfer rule

        resp = client.post(f"/transactions/{tid}/reset-instance")
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "void"
        fresh = next(
            i
            for i in _live(client, sid).values()
            if i["occurrence_date"] == first["occurrence_date"]
        )
        assert set(_legs(client, fresh["transaction_id"])) == {
            "assets.checking",
            "liabilities.citicard",
        }

    def test_scope_and_targets(self, seeded: None, client: TestClient) -> None:
        sid = _bill(client)["id"]
        first = next(iter(_live(client, sid)))
        agent = client.post("/tokens", json={"name": "agent", "scopes": ["read"]})
        headers = {"Authorization": f"Bearer {agent.json()['token']}"}
        assert (
            client.post(f"/transactions/{first}/reset-instance", headers=headers).status_code == 403
        )

        posted = client.post(
            "/transactions",
            json={
                "account": "assets.checking",
                "category": "expenses.housing",
                "amount": "-5.00",
                "date": date.today().isoformat(),
                "status": "posted",
            },
        ).json()
        resp = client.post(f"/transactions/{posted['id']}/reset-instance")
        assert resp.status_code == 422
        assert "not a live schedule instance" in resp.json()["detail"]

        client.patch(f"/transactions/{first}", json={"amount": "-99.00"})
        client.post(f"/schedules/{sid}/pause")
        resp = client.post(f"/transactions/{first}/reset-instance")
        assert resp.status_code == 422
        assert "active schedule" in resp.json()["detail"]


_LABEL_NAMES = st.sampled_from(["Gym", "Fitness club", "Gym club", "GYM"])
_LABEL_TEXT = st.sampled_from(["Gym", "My gym", "Fitness club", "Annual dues"])


class TestLabelPinStateMachine:
    """Guardrail 3 for decision 79: random renames, label edits, overrides,
    resets and estimate moves. After every step each live instance is
    zero-sum, pinned exactly when it was overridden since its last reset,
    and its labels are the rule's unless the user set them."""

    def test_labels_and_pins(self, with_card: None, client: TestClient) -> None:
        from hypothesis.stateful import (
            RuleBasedStateMachine,
            initialize,
            invariant,
            rule,
            run_state_machine_as_test,
        )

        class Machine(RuleBasedStateMachine):
            def __init__(self) -> None:
                super().__init__()
                self.sid = ""
                self.transfer = False
                self.name = "Gym"
                self.amount = Decimal("-60.00")
                self.pinned: dict[str, bool] = {}
                self.description: dict[str, str] = {}
                self.payee: dict[str, str | None] = {}
                self.checking: dict[str, Decimal] = {}

            @initialize(transfer=st.booleans())
            def create(self, transfer: bool) -> None:
                self.transfer = transfer
                body: dict[str, object] = {
                    "name": self.name,
                    "account": "assets.checking",
                    "rrule": f"FREQ=MONTHLY;BYMONTHDAY={_near_term_bymonthday()}",
                    "amount": str(self.amount),
                }
                if transfer:
                    body["counterparty_account"] = "liabilities.citicard"
                else:
                    body["category"] = "expenses.housing"
                resp = client.post("/schedules", json=body)
                assert resp.status_code == 201, resp.text
                self.sid = str(resp.json()["id"])
                for tid in _live(client, self.sid):
                    self.pinned[tid] = False
                    self.description[tid] = self.name
                    self.payee[tid] = None if transfer else self.name
                    self.checking[tid] = self.amount

            def _target(self, index: int) -> str:
                ids = sorted(self.pinned)
                return ids[index % len(ids)]

            @rule(name=_LABEL_NAMES)
            def rename(self, name: str) -> None:
                resp = client.patch(f"/schedules/{self.sid}", json={"name": name})
                assert resp.status_code == 200, resp.text
                old, self.name = self.name, name
                from purser.core import ledger as core

                for tid in self.pinned:
                    if old != name and self.description[tid] == old:
                        self.description[tid] = name
                    payee = self.payee[tid]
                    if (
                        payee is not None
                        and core.normalize_payee(payee) == core.normalize_payee(old)
                        and core.normalize_payee(old) != core.normalize_payee(name)
                    ):
                        self.payee[tid] = name

            @rule(index=st.integers(0, 3), text_=_LABEL_TEXT, payee=st.booleans())
            def label(self, index: int, text_: str, payee: bool) -> None:
                tid = self._target(index)
                body = {"payee": text_} if payee and not self.transfer else {"description": text_}
                resp = client.patch(f"/transactions/{tid}", json=body)
                assert resp.status_code == 200, resp.text
                if "payee" in body:
                    # The payee row is shared by normalized name.
                    self.payee[tid] = text_
                else:
                    self.description[tid] = text_

            @rule(index=st.integers(0, 3), cents=st.integers(1, 99999), shift=st.booleans())
            def override(self, index: int, cents: int, shift: bool) -> None:
                tid = self._target(index)
                amount = -Decimal(cents) / 100
                body: dict[str, str] = {"amount": str(amount)}
                if shift:
                    row = _live(client, self.sid)[tid]
                    body["date"] = (date.fromisoformat(str(row["date"])) + timedelta(1)).isoformat()
                resp = client.patch(f"/transactions/{tid}", json=body)
                assert resp.status_code == 200, resp.text
                self.pinned[tid] = True
                self.checking[tid] = amount

            @rule(index=st.integers(0, 3))
            def reset(self, index: int) -> None:
                tid = self._target(index)
                resp = client.post(f"/transactions/{tid}/reset-instance")
                assert resp.status_code == 200, resp.text
                assert resp.json()["status"] == "expected"
                self.pinned[tid] = False
                self.description[tid] = self.name
                self.payee[tid] = None if self.transfer else self.name
                self.checking[tid] = self.amount

            @rule(cents=st.integers(1, 99999))
            def move_estimate(self, cents: int) -> None:
                self.amount = -Decimal(cents) / 100
                resp = client.patch(f"/schedules/{self.sid}", json={"amount": str(self.amount)})
                assert resp.status_code == 200, resp.text
                for tid, pinned in self.pinned.items():
                    if not pinned:
                        self.checking[tid] = self.amount

            @invariant()
            def consistent(self) -> None:
                if not self.sid:
                    return
                from purser.core import ledger as core

                live = _live(client, self.sid)
                assert set(live) == set(self.pinned)
                for tid, row in live.items():
                    tx = _tx(client, tid)
                    legs = {p["account_path"]: Decimal(p["amount"]) for p in tx["postings"]}  # type: ignore[union-attr,index]
                    assert sum(legs.values()) == 0
                    assert legs["assets.checking"] == self.checking[tid]
                    assert row["pinned"] is self.pinned[tid]
                    if not self.pinned[tid]:
                        assert row["date"] == row["occurrence_date"]
                    assert tx["description"] == self.description[tid]
                    expected_payee = self.payee[tid]
                    if expected_payee is None:
                        assert tx["payee"] is None
                    else:
                        assert core.normalize_payee(str(tx["payee"])) == core.normalize_payee(
                            expected_payee
                        )

        run_state_machine_as_test(
            Machine,
            settings=settings(
                max_examples=6,
                stateful_step_count=8,
                deadline=None,
                suppress_health_check=[HealthCheck.function_scoped_fixture],
            ),
        )


class TestForecastProjection:
    """Forecast honesty (#235): the horizon is honoured past the
    materialized instances, and the payload says what the line is made of."""

    def _forecast(self, client: TestClient, account: str, days: int) -> dict[str, object]:
        resp = client.get("/forecast", params={"account": account, "days": days})
        assert resp.status_code == 200, resp.text
        return dict(resp.json())

    def test_projects_beyond_the_materialized_horizon(
        self, seeded: None, client: TestClient
    ) -> None:
        client.post(
            "/schedules",
            json={
                "name": "Rent",
                "account": "assets.checking",
                "rrule": f"FREQ=MONTHLY;BYMONTHDAY={_near_term_bymonthday()}",
                "amount": "-1800.00",
                "category": "expenses.housing",
            },
        )
        forecast = self._forecast(client, "assets.checking", 180)
        events = [(d["date"], e) for d in forecast["days"] for e in d["events"]]  # type: ignore[index]
        assert len(events) >= 6  # six monthly occurrences over 180 days
        horizon_edge = (date.today() + timedelta(days=60)).isoformat()
        near = [e for d, e in events if d <= horizon_edge]
        far = [e for d, e in events if d > horizon_edge]
        assert near and all(e["projected"] is False and e["transaction_id"] for e in near)
        assert far and all(e["projected"] is True and e["transaction_id"] is None for e in far)
        coverage = forecast["coverage"]
        assert coverage["schedules_active"] == 1  # type: ignore[index]
        assert coverage["instances"] == len(near)  # type: ignore[index]
        assert coverage["projected"] == len(far)  # type: ignore[index]
        # The balance keeps stepping down through the projected months.
        assert Decimal(str(forecast["days"][-1]["balance"])) == -Decimal("1800.00") * len(events)  # type: ignore[index]

    def test_zero_schedules_is_reported_not_disguised(
        self, seeded: None, client: TestClient
    ) -> None:
        forecast = self._forecast(client, "assets.checking", 30)
        assert forecast["coverage"] == {
            "schedules_active": 0,
            "instances": 0,
            "projected": 0,
            "suggestions_open": 0,
            "matches_pending": 0,
            "matches_pending_net": None,
        }
        assert all(d["events"] == [] for d in forecast["days"])  # type: ignore[index]

    def test_skipped_slot_stays_silent_and_overdue_is_flagged(
        self, seeded: None, client: TestClient
    ) -> None:
        schedule = client.post(
            "/schedules",
            json={
                "name": "Gym",
                "account": "assets.checking",
                "rrule": "FREQ=WEEKLY",
                "amount": "-60.00",
                "category": "expenses.housing",
            },
        ).json()
        instances = client.get(f"/schedules/{schedule['id']}").json()["instances"]
        skipped, moved = instances[1], instances[2]
        client.post(f"/transactions/{skipped['transaction_id']}/void")
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        client.patch(f"/transactions/{moved['transaction_id']}", json={"date": yesterday})

        forecast = self._forecast(client, "assets.checking", 90)
        by_date = {d["date"]: d["events"] for d in forecast["days"]}  # type: ignore[index]
        assert by_date[skipped["date"]] == []  # a skip never comes back, projected or not
        today_events = by_date[date.today().isoformat()]
        assert any(
            e["overdue"] is True and e["transaction_id"] == moved["transaction_id"]
            for e in today_events
        )
        projected = [e for evs in by_date.values() for e in evs if e["projected"]]
        assert projected, "weekly slots past day 60 are projected"
        assert forecast["coverage"]["projected"] == len(projected)  # type: ignore[index]

    def test_counterparty_side_sees_the_incoming_leg(
        self, with_card: None, client: TestClient
    ) -> None:
        client.post(
            "/schedules",
            json={
                "name": "Citi payment",
                "account": "assets.checking",
                "counterparty_account": "liabilities.citicard",
                "rrule": f"FREQ=MONTHLY;BYMONTHDAY={_near_term_bymonthday()}",
                "amount": "-500.00",
            },
        )
        card = self._forecast(client, "liabilities.citicard", 120)
        events = [e for d in card["days"] for e in d["events"]]  # type: ignore[index]
        assert events and all(Decimal(str(e["amount"])) == Decimal("500.00") for e in events)
        assert any(e["projected"] for e in events) and any(not e["projected"] for e in events)
        assert card["coverage"]["schedules_active"] == 1  # type: ignore[index]


class TestMatchedActuals:
    """What an estimate learns from (#273, decision 69): every confirmed match
    is followed to its live chain head, only a POSTED head counts, and the
    amount is the head's leg on the schedule account."""

    def _actuals(self, schedule_id: str) -> list[tuple[Decimal, date]]:
        import uuid

        from purser import db
        from purser import schedules as schedules_service
        from purser.models import Schedule

        with db.get_sessionmaker()() as session:
            schedule = session.get(Schedule, uuid.UUID(schedule_id))
            assert schedule is not None
            return schedules_service.matched_actuals(session, schedule)

    def _match(self, client: TestClient, expected_id: str, bank_id: str) -> None:
        resp = client.post(
            "/matching/match", json={"expected_id": expected_id, "posted_id": bank_id}
        )
        assert resp.status_code == 200, resp.text

    def test_only_live_posted_heads_count(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        schedule = client.post(
            "/schedules",
            json={
                "name": "Utilities",
                "account": "assets.checking",
                "rrule": "FREQ=WEEKLY",
                "amount_type": "estimated",
                "amount": "-100.00",
                "estimate_basis": "last",
                "payee": "City Utilities",
            },
        ).json()
        instances = client.get(f"/schedules/{schedule['id']}").json()["instances"]
        assert len(instances) >= 4
        today = date.today()

        def bank(amount: str, days_ago: int, status: str = "posted") -> str:
            return _import_row(
                migrated_engine,
                account_path="assets.checking",
                amount=amount,
                on=today - timedelta(days=days_ago),
                payee="City Utilities",
                description="CITY UTILITIES",
                status=status,
            )

        posted = bank("-50.00", 40)
        still_pending = bank("-60.00", 30, status="pending")
        dropped = bank("-70.00", 20, status="pending")
        superseded = bank("-80.00", 10, status="pending")
        successor = bank("-85.00", 9)
        for instance, bank_id in zip(
            instances, (posted, still_pending, dropped, superseded), strict=False
        ):
            self._match(client, instance["transaction_id"], bank_id)
        with migrated_engine.begin() as conn:
            conn.execute(
                text("UPDATE transactions SET status = 'void' WHERE id = :id"), {"id": dropped}
            )
            conn.execute(
                text("UPDATE transactions SET status = 'void', superseded_by = :s WHERE id = :id"),
                {"id": superseded, "s": successor},
            )

        # The successor's amount, not the superseded pending's; a still-pending
        # row and a dropped one count for nothing — posted amount wins.
        assert self._actuals(schedule["id"]) == [
            (Decimal("-85.00"), today - timedelta(days=9)),
            (Decimal("-50.00"), today - timedelta(days=40)),
        ]
        # A correction moves the actual to the replacement row.
        fixed = client.patch(f"/transactions/{posted}", json={"amount": "-55.00"})
        assert fixed.status_code == 200, fixed.text
        assert self._actuals(schedule["id"])[-1] == (
            Decimal("-55.00"),
            today - timedelta(days=40),
        )
        # And the estimate ('last') is the newest posted actual.
        client.post("/schedules/materialize")
        live = client.get(f"/schedules/{schedule['id']}").json()["instances"]
        assert live and all(i["amount"] == "-85.0000" for i in live)

    def test_a_transfer_counts_once_through_the_schedule_leg(
        self, with_card: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        schedule = client.post(
            "/schedules",
            json={
                "name": "Citi payment",
                "account": "assets.checking",
                "counterparty_account": "liabilities.citicard",
                "rrule": f"FREQ=MONTHLY;BYMONTHDAY={_near_term_bymonthday()}",
                "amount_type": "estimated",
                "amount": "-300.00",
                "estimate_basis": "last",
            },
        ).json()
        first, second = client.get(f"/schedules/{schedule['id']}").json()["instances"][:2]
        due = date.fromisoformat(first["date"])
        out_id = _import_row(
            migrated_engine,
            account_path="assets.checking",
            amount="-320.00",
            on=due,
            payee="CITI AUTOPAY",
            description="CITI AUTOPAY PAYMENT",
        )
        in_id = _import_row(
            migrated_engine,
            account_path="liabilities.citicard",
            amount="320.00",
            on=due,
            payee="PAYMENT THANK YOU",
            description="PAYMENT - THANK YOU",
        )
        self._match(client, first["transaction_id"], out_id)
        self._match(client, second["transaction_id"], in_id)
        # Each bank row is seen from checking: the card-side row negated.
        assert self._actuals(schedule["id"]) == [
            (Decimal("-320.00"), due),
            (Decimal("-320.00"), due),
        ]

        merged = client.post(
            "/matching/confirm-transfer", json={"outgoing_id": out_id, "incoming_id": in_id}
        )
        assert merged.status_code == 200, merged.text
        # Both matches now lead to ONE merged transfer: one payment, read
        # through its checking leg (never its card leg as a second actual).
        assert self._actuals(schedule["id"]) == [(Decimal("-320.00"), due)]


# ---------------------------------------------------------------------------
# #352 (decision 76): an open proposal settles its instance pending
# confirmation — out of the line, counted in coverage
# ---------------------------------------------------------------------------


def _propose(engine: Engine, expected_id: str, posted_id: str, *, variance: str = "0") -> str:
    """A proposed match, as the matcher's middle tier writes it."""
    with engine.begin() as conn:
        return str(
            conn.execute(
                text(
                    "INSERT INTO matches (expected_id, posted_id, score, variance, state) "
                    "VALUES (:e, :p, 0.6, :v, 'proposed') RETURNING id"
                ),
                {"e": expected_id, "p": posted_id, "v": Decimal(variance)},
            ).scalar_one()
        )


class TestProposedMatchSettlement:
    def _opening(self, client: TestClient, account: str, amount: str) -> None:
        client.post(
            "/categories",
            json={"name": "Salary", "type": "category_income", "path": "income.salary"},
        )
        resp = client.post(
            "/transactions",
            json={
                "account": account,
                "category": "income.salary",
                "amount": amount,
                "date": (date.today() - timedelta(days=20)).isoformat(),
                "status": "posted",
            },
        )
        assert resp.status_code == 201, resp.text

    def _expected(
        self, client: TestClient, amount: str, on: date, *, account: str = "assets.checking"
    ) -> str:
        resp = client.post(
            "/transactions",
            json={
                "account": account,
                "category": "expenses.housing",
                "amount": amount,
                "date": on.isoformat(),
                "payee": "City Utilities",
                "status": "expected",
            },
        )
        assert resp.status_code == 201, resp.text
        return str(resp.json()["id"])

    def _forecast(self, client: TestClient, account: str, days: int = 30) -> dict[str, object]:
        resp = client.get("/forecast", params={"account": account, "days": days})
        assert resp.status_code == 200, resp.text
        return dict(resp.json())

    @staticmethod
    def _events(forecast: dict[str, object]) -> list[dict[str, object]]:
        return [e for d in forecast["days"] for e in d["events"]]  # type: ignore[attr-defined]

    def test_overdue_proposal_is_left_out_until_rejected(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        """The acceptance pair: an overdue instance whose proposal names a
        posted row already in the balance does not land; rejecting the
        proposal lands it on day 0 again. The proposal is the matcher's own."""
        self._opening(client, "assets.checking", "1000.00")
        today = date.today()
        expected_id = self._expected(client, "-180.00", today - timedelta(days=4))
        posted_id = _import_row(
            migrated_engine,
            account_path="assets.checking",
            amount="-95.00",  # far-off amount, close payee: the review tier
            on=today - timedelta(days=1),
            payee="City Utilities Pmt",
            description="CITY UTILITIES PMT",
        )
        assert client.post("/matching/run").json()["proposed"] == 1

        settled = self._forecast(client, "assets.checking")
        assert Decimal(str(settled["start_balance"])) == Decimal("905.00")  # the row counts once
        assert self._events(settled) == []
        assert Decimal(str(settled["days"][-1]["balance"])) == Decimal("905.00")  # type: ignore[index]
        coverage = settled["coverage"]
        assert coverage["matches_pending"] == 1  # type: ignore[index]
        # The instance's own leg, not the bank amount: what confirming removes.
        assert Decimal(str(coverage["matches_pending_net"])) == Decimal("-180.00")  # type: ignore[index]
        assert coverage["instances"] == 0  # type: ignore[index]

        (proposal,) = client.get("/matching/proposed").json()
        assert proposal["posted_id"] == posted_id
        assert client.post(f"/matching/{proposal['id']}/reject").status_code == 200

        landed = self._forecast(client, "assets.checking")
        (event,) = self._events(landed)
        assert event["transaction_id"] == expected_id and event["overdue"] is True
        assert landed["days"][0]["date"] == today.isoformat()  # type: ignore[index]
        assert Decimal(str(landed["days"][0]["balance"])) == Decimal("725.00")  # type: ignore[index]
        assert landed["coverage"]["matches_pending"] == 0  # type: ignore[index]
        assert landed["coverage"]["matches_pending_net"] is None  # type: ignore[index]

    def test_confirmed_proposal_voids_the_instance(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        today = date.today()
        expected_id = self._expected(client, "-200.00", today - timedelta(days=3))
        posted_id = _import_row(
            migrated_engine,
            account_path="assets.checking",
            amount="-200.00",
            on=today - timedelta(days=5),
            payee="Cleaning Service Bill O",
            description="CLEANING SERVICE BILL O",
        )
        match_id = _propose(migrated_engine, expected_id, posted_id)
        assert client.post(f"/matching/{match_id}/confirm").status_code == 200
        assert client.get(f"/transactions/{expected_id}").json()["status"] == "void"
        forecast = self._forecast(client, "assets.checking")
        assert forecast["coverage"]["matches_pending"] == 0  # type: ignore[index]
        assert self._events(forecast) == []
        assert Decimal(str(forecast["days"][-1]["balance"])) == Decimal("-200.00")  # type: ignore[index]

    def test_future_instance_with_a_pending_proposal_is_left_out(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        """Not only overdue: the match window is ±5 days, so an instance due
        in three days can already hold a proposal against a future-dated
        pending row — which the opening balance includes."""
        today = date.today()
        expected_id = self._expected(client, "-150.00", today + timedelta(days=3))
        pending_id = _import_row(
            migrated_engine,
            account_path="assets.checking",
            amount="-150.00",
            on=today + timedelta(days=2),
            payee="Eversource",
            description="EVERSOURCE",
            status="pending",
        )
        _propose(migrated_engine, expected_id, pending_id)
        forecast = self._forecast(client, "assets.checking")
        assert Decimal(str(forecast["start_balance"])) == Decimal("-150.00")
        assert self._events(forecast) == []
        assert Decimal(str(forecast["days"][-1]["balance"])) == Decimal("-150.00")  # type: ignore[index]
        assert forecast["coverage"]["matches_pending"] == 1  # type: ignore[index]

    def test_variance_is_never_carried_as_a_remainder(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        today = date.today()
        expected_id = self._expected(client, "-550.00", today - timedelta(days=2))
        posted_id = _import_row(
            migrated_engine,
            account_path="assets.checking",
            amount="-500.00",
            on=today - timedelta(days=2),
            payee="B and B",
            description="B AND B",
        )
        _propose(migrated_engine, expected_id, posted_id, variance="50.00")
        forecast = self._forecast(client, "assets.checking")
        # The posted amount wins: -500 in the balance, no -50 remainder event.
        assert self._events(forecast) == []
        assert Decimal(str(forecast["days"][-1]["balance"])) == Decimal("-500.00")  # type: ignore[index]
        assert Decimal(str(forecast["coverage"]["matches_pending_net"])) == Decimal("-550.00")  # type: ignore[index]

    def test_transfer_proposal_on_the_card_row_settles_only_the_card_side(
        self, with_card: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        """A two-leg instance whose proposal names the one-sided card row:
        that row is in the card's balance, not checking's — so the card's
        forecast leaves the instance out and checking's still lands it."""
        schedule = client.post(
            "/schedules",
            json={
                "name": "Citi payment",
                "account": "assets.checking",
                "counterparty_account": "liabilities.citicard",
                "rrule": f"FREQ=MONTHLY;BYMONTHDAY={_near_term_bymonthday()}",
                "amount": "-500.00",
            },
        ).json()
        first = client.get(f"/schedules/{schedule['id']}").json()["instances"][0]
        card_row = _import_row(
            migrated_engine,
            account_path="liabilities.citicard",
            amount="500.00",
            on=date.fromisoformat(first["date"]),
            payee="PAYMENT THANK YOU",
            description="PAYMENT - THANK YOU",
        )
        _propose(migrated_engine, first["transaction_id"], card_row)

        card = self._forecast(client, "liabilities.citicard", days=10)
        assert first["transaction_id"] not in {e["transaction_id"] for e in self._events(card)}
        assert card["coverage"]["matches_pending"] == 1  # type: ignore[index]
        assert Decimal(str(card["coverage"]["matches_pending_net"])) == Decimal("500.00")  # type: ignore[index]

        checking = self._forecast(client, "assets.checking", days=10)
        assert first["transaction_id"] in {e["transaction_id"] for e in self._events(checking)}
        assert checking["coverage"]["matches_pending"] == 0  # type: ignore[index]

    def test_a_proposal_on_a_voided_bank_row_settles_nothing(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        from purser import db, matching
        from purser.ledger import resolve_account

        today = date.today()
        expected_id = self._expected(client, "-60.00", today + timedelta(days=1))
        pending_id = _import_row(
            migrated_engine,
            account_path="assets.checking",
            amount="-60.00",
            on=today,
            payee="Gym",
            description="GYM",
            status="pending",
        )
        _propose(migrated_engine, expected_id, pending_id)
        with migrated_engine.begin() as conn:
            conn.execute(
                text("UPDATE transactions SET status = 'void' WHERE id = :t"), {"t": pending_id}
            )
        with db.get_sessionmaker()() as session:
            checking = resolve_account(session, "assets.checking")
            ids = [uuid.UUID(expected_id)]
            assert (
                matching.proposed_settlements(session, account_id=checking.id, expected_ids=ids)
                == {}
            )
            assert matching.proposed_settlements(session, account_id=None, expected_ids=ids) == {}
        forecast = self._forecast(client, "assets.checking")
        assert [e["transaction_id"] for e in self._events(forecast)] == [expected_id]

    def test_valuation_account_keeps_the_instance(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        """A valuation replaces the postings basis (decision 42): the bank row
        moves nothing the forecast starts from, so the instance stays."""
        loan = client.post(
            "/accounts", json={"name": "Mortgage", "type": "loan", "path": "liabilities.mortgage"}
        ).json()
        today = date.today()
        valued = client.post(
            f"/accounts/{loan['id']}/valuations",
            json={"as_of": (today - timedelta(days=1)).isoformat(), "value": "-90000.00"},
        )
        assert valued.status_code == 201, valued.text
        expected_id = self._expected(
            client, "-400.00", today - timedelta(days=1), account="liabilities.mortgage"
        )
        posted_id = _import_row(
            migrated_engine,
            account_path="liabilities.mortgage",
            amount="-400.00",
            on=today - timedelta(days=1),
            payee="Mortgage Servicer",
            description="MORTGAGE SERVICER",
        )
        _propose(migrated_engine, expected_id, posted_id)
        forecast = self._forecast(client, "liabilities.mortgage")
        assert Decimal(str(forecast["start_balance"])) == Decimal("-90000.00")
        assert [e["transaction_id"] for e in self._events(forecast)] == [expected_id]
        assert forecast["coverage"]["matches_pending"] == 0  # type: ignore[index]

    def test_shortfall_scan_ignores_a_dip_made_only_by_the_double_count(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        resp = client.post(
            "/accounts",
            json={
                "name": "Bills",
                "type": "checking",
                "path": "assets.bills",
                "min_balance": "0.00",
            },
        )
        assert resp.status_code == 201, resp.text
        self._opening(client, "assets.bills", "300.00")
        today = date.today()
        expected_id = self._expected(
            client, "-250.00", today - timedelta(days=2), account="assets.bills"
        )
        posted_id = _import_row(
            migrated_engine,
            account_path="assets.bills",
            amount="-250.00",
            on=today - timedelta(days=3),
            payee="Lakeside Sewer",
            description="LAKESIDE SEWER",
        )
        _propose(migrated_engine, expected_id, posted_id)

        from purser import schedules as schedules_service

        schedules_service.shortfall_scan_job()
        with migrated_engine.connect() as conn:
            fired = conn.execute(
                text("SELECT COUNT(*) FROM events WHERE type = 'forecast.shortfall'")
            ).scalar_one()
        assert fired == 0  # 300 - 250 = 50 stays above the floor; -200 was the double count


# Instance specs for the property below: (amount cents, day offset, bank row
# kind, proposal state, bank-amount variance cents).
_SETTLEMENT_CASES = st.lists(
    st.tuples(
        st.integers(min_value=-50_000, max_value=50_000).filter(lambda c: c != 0),
        st.integers(min_value=-10, max_value=10),
        st.sampled_from(["none", "posted", "pending", "other_account", "void"]),
        st.sampled_from(["proposed", "rejected", "none"]),
        st.integers(min_value=-2_000, max_value=2_000),
    ),
    max_size=6,
)


@settings(
    max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(cases=_SETTLEMENT_CASES)
def test_every_bank_row_is_counted_exactly_once(
    clean_db: None, db_session: object, cases: list[tuple[int, int, str, str, int]]
) -> None:
    """Over generated instances, bank rows and proposals: the line ends at
    opening + Σ(instances without an account-side live proposal), where the
    opening already holds every live bank row on the account — so a bank row
    is counted once (in the balance) and the instance it settles never again
    (decision 76). Coverage counts exactly the left-out instances."""
    from sqlalchemy.orm import Session

    from purser import ledger, schedules
    from purser.core import ledger as core
    from purser.feed import importer
    from purser.models import Match, Transaction

    session = db_session
    assert isinstance(session, Session)
    today = date.today()
    try:
        suffix = uuid.uuid4().hex[:8]
        account = ledger.create_account(
            session, name="Prop", type_="checking", path=f"assets.p{suffix}", actor_token_id=None
        )
        other = ledger.create_account(
            session, name="Other", type_="savings", path=f"assets.o{suffix}", actor_token_id=None
        )
        category = importer._uncategorized(session, income=False)

        def row(
            acct: str, cents: int, on: date, status: core.Status, origin: core.Origin
        ) -> uuid.UUID:
            tx = ledger.create_flat_transaction(
                session,
                account_ref=acct,
                category_ref=str(category.id),
                amount=Decimal(cents) / 100,
                date=on,
                payee_name="Synthetic Payee",
                requested_status=status,
                origin=origin,
                token=None,
            )
            return tx.id

        opening = Decimal(0)
        unsettled = Decimal(0)
        settled_ids: set[uuid.UUID] = set()
        settled_net = Decimal(0)
        for cents, offset, bank, proposal, variance in cases:
            on = today + timedelta(days=offset)
            expected = row(str(account.id), cents, on, core.Status.EXPECTED, core.Origin.HUMAN)
            bank_cents = cents + variance or cents
            bank_id = None
            if bank in ("posted", "pending", "void"):
                status = core.Status.POSTED if bank == "posted" else core.Status.PENDING
                bank_id = row(
                    str(account.id), bank_cents, on - timedelta(days=1), status, core.Origin.IMPORT
                )
                if bank == "void":
                    session.get_one(Transaction, bank_id).status = "void"
                else:
                    opening += Decimal(bank_cents) / 100
            elif bank == "other_account":
                bank_id = row(str(other.id), bank_cents, on, core.Status.POSTED, core.Origin.IMPORT)
            if bank_id is not None and proposal != "none":
                session.add(
                    Match(
                        expected_id=expected,
                        posted_id=bank_id,
                        score=0.6,
                        variance=Decimal(bank_cents - cents) / 100,
                        state=proposal,
                    )
                )
            if proposal == "proposed" and bank in ("posted", "pending"):
                settled_ids.add(expected)
                settled_net += Decimal(cents) / 100
            else:
                unsettled += Decimal(cents) / 100
        session.flush()

        result = schedules.forecast_account(session, account_ref=str(account.id), horizon_days=30)
        fc = result["forecast"]
        coverage = result["coverage"]
        assert fc.start_balance == opening
        assert fc.days[-1].balance == opening + unsettled
        assert coverage["matches_pending"] == len(settled_ids)
        assert coverage.get("matches_pending_net", Decimal(0)) == settled_net
        assert coverage["instances"] == len(cases) - len(settled_ids)
        listed = {e.transaction_id for d in fc.days for e in d.events}
        assert not listed & settled_ids
    finally:
        session.rollback()
