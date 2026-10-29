"""readmodel views: public contract pins, correctness against seed-demo,
read-only role, and dashboard queries actually executing."""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

DASHBOARDS = sorted((Path(__file__).parent.parent / "grafana" / "dashboards").glob("*.json"))

# The public contract: these columns may grow but must never change or vanish
# within v1 (readmodel views are versioned).
CONTRACT = {
    # v2 replaces v1_transactions_flat (#137): payee + description were
    # encrypted, so they left the plaintext direct-SQL/Grafana contract.
    "v2_transactions_flat": [
        "id",
        "date",
        "status",
        "origin",
        "amount",
        "account_path",
        "category_path",
        "is_transfer",
        "created_at",
    ],
    "v1_spending_by_category_month": ["month", "category_path", "total", "transaction_count"],
    "v1_net_worth_daily": [
        "as_of",
        "currency",
        "assets",
        "liabilities",
        "net_worth",
        "computed_at",
    ],
    "v1_forecast_daily": ["day", "account_path", "projected_balance"],
    "v1_holdings_latest": [
        "account_path",
        "symbol",
        "as_of",
        "quantity",
        "cost_basis",
        "market_value",
    ],
}


@pytest.fixture
def demo(clean_db: None, migrated_engine: Engine) -> None:
    from purser.db import get_sessionmaker
    from purser.networth import materialize_day
    from purser.seed import seed_demo

    session: Session
    with get_sessionmaker()() as session:
        seed_demo(session)
        materialize_day(session, date.today())
        session.commit()


class TestContract:
    def test_view_columns_are_pinned(self, migrated_engine: Engine) -> None:
        with migrated_engine.connect() as conn:
            for view, expected_columns in CONTRACT.items():
                rows = conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = 'readmodel' AND table_name = :v "
                        "ORDER BY ordinal_position"
                    ),
                    {"v": view},
                ).scalars()
                assert list(rows) == expected_columns, f"contract broken for {view}"

    def test_read_role_has_select_on_every_view(self, migrated_engine: Engine) -> None:
        with migrated_engine.connect() as conn:
            for view in CONTRACT:
                ok = conn.execute(
                    text("SELECT has_table_privilege('purser_read', :v, 'SELECT')"),
                    {"v": f"readmodel.{view}"},
                ).scalar_one()
                assert ok, f"purser_read cannot read {view}"


class TestAgainstSeedDemo:
    def test_flat_view_matches_ledger_semantics(self, demo: None, migrated_engine: Engine) -> None:
        with migrated_engine.connect() as conn:
            statuses = set(
                conn.execute(
                    text("SELECT DISTINCT status FROM readmodel.v2_transactions_flat")
                ).scalars()
            )
            assert statuses <= {"pending", "posted"}
            transfers = conn.execute(
                text("SELECT COUNT(*) FROM readmodel.v2_transactions_flat WHERE is_transfer")
            ).scalar_one()
            assert transfers == 2  # one per seeded month

    def test_spending_by_month_sums_are_right(self, demo: None, migrated_engine: Engine) -> None:
        with migrated_engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT month, SUM(total) FROM readmodel.v1_spending_by_category_month "
                    "GROUP BY month ORDER BY month"
                )
            ).all()
        assert len(rows) == 2  # two seeded months
        # Each month's expense legs: 84.12+23.40+118.55+56.80+102.33+6.25+1850+64.90
        for _, total in rows:
            assert Decimal(total) == Decimal("2306.35")

    def test_net_worth_daily_materialized(self, demo: None, migrated_engine: Engine) -> None:
        with migrated_engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT assets, liabilities, net_worth FROM readmodel.v1_net_worth_daily "
                    "ORDER BY as_of DESC LIMIT 1"
                )
            ).one()
        assert Decimal(row.net_worth) == Decimal(row.assets) - Decimal(row.liabilities)

    def test_forecast_daily_reflects_rent_instances(
        self, demo: None, migrated_engine: Engine
    ) -> None:
        with migrated_engine.connect() as conn:
            first, last = conn.execute(
                text(
                    "SELECT MIN(projected_balance), MAX(projected_balance) "
                    "FROM (SELECT projected_balance FROM readmodel.v1_forecast_daily "
                    "WHERE account_path = 'assets.checking' ORDER BY day) sub"
                )
            ).one()
        # Rent instances (-1850) land inside the horizon: projection must dip.
        assert Decimal(first) < Decimal(last)

    def test_dashboard_queries_execute_against_the_schema(
        self, demo: None, migrated_engine: Engine
    ) -> None:
        """The 'dashboards import and render' proxy: every panel query runs."""
        assert len(DASHBOARDS) == 3
        with migrated_engine.connect() as conn:
            for path in DASHBOARDS:
                spec = json.loads(path.read_text())
                assert spec["__inputs"][0]["pluginId"] == "postgres"
                assert spec["panels"], f"{path.name} has no panels"
                for panel in spec["panels"]:
                    for target in panel.get("targets", []):
                        sql = target["rawSql"].replace("$__timeFilter(as_of)", "as_of IS NOT NULL")
                        conn.execute(text(sql)).fetchall()  # must not raise
