"""`purser seed-demo` — a small realistic dataset (a few accounts,
two months of transactions, one transfer pair, one recurring bill) so
matching, forecasting, and the Grafana views have something to show."""

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from purser import audit, matching, schedules
from purser import ledger as ledger_service
from purser.core import ledger as core
from purser.models import Account, Transaction

DEMO_SPEND = [
    # (day offset, account, category, amount, payee)
    (3, "assets.checking", "expenses.groceries", "-84.12", "Safeway"),
    (5, "liabilities.citicard", "expenses.dining", "-23.40", "Chipotle"),
    (8, "assets.checking", "expenses.utilities", "-118.55", "City Utilities"),
    (11, "liabilities.citicard", "expenses.dining", "-56.80", "Olive Garden"),
    (12, "assets.checking", "expenses.groceries", "-102.33", "Safeway"),
    (17, "liabilities.citicard", "expenses.dining.coffee", "-6.25", "Blue Bottle"),
    (19, "assets.checking", "expenses.housing", "-1850.00", "Landlord LLC"),
    (24, "liabilities.citicard", "expenses.groceries", "-64.90", "Trader Joes"),
]


def _has_user_data(session: Session) -> bool:
    """Anything beyond the migration-seeded category tree counts as user data:
    a real (non-category) account, or any transaction. (Checking a specific
    demo path is not enough — a live install whose accounts happen to use
    other paths must never be seeded over, #217. Connections are deliberately
    not consulted: an unmapped connection carries no ledger state.)"""
    real_account = session.execute(
        select(Account.id).where(Account.type.not_in(ledger_service.CATEGORY_TYPES)).limit(1)
    ).scalar_one_or_none()
    if real_account is not None:
        return True
    return session.execute(select(Transaction.id).limit(1)).scalar_one_or_none() is not None


def seed_demo(session: Session, *, force: bool = False) -> dict[str, int]:
    if _has_user_data(session):
        if not force:
            return {"skipped": 1}
        # Guardrail 5: a forced seed over user data leaves an explicit trail.
        audit.record(
            session,
            actor_token_id=None,
            action="seed.force",
            entity_type="system",
            after={"reason": "seed-demo --force over existing user data"},
        )

    def acct(name: str, type_: str, path: str) -> Account:
        # Get-or-create: migration 0012 ships the default category tree, so
        # demo paths like 'expenses' and 'income.salary' usually exist already.
        existing = session.execute(select(Account).where(Account.path == path)).scalar_one_or_none()
        if existing is not None:
            return existing
        return ledger_service.create_account(
            session, name=name, type_=type_, path=path, actor_token_id=None
        )

    acct("Checking", "checking", "assets.checking")
    acct("Savings", "savings", "assets.savings")
    acct("Citi Card", "credit_card", "liabilities.citicard")
    for name, path in [
        ("Expenses", "expenses"),
        ("Groceries", "expenses.groceries"),
        ("Dining", "expenses.dining"),
        ("Coffee", "expenses.dining.coffee"),
        ("Utilities", "expenses.utilities"),
        ("Housing", "expenses.housing"),
    ]:
        acct(name, "category_expense", path)
    acct("Income", "category_income", "income")
    acct("Salary", "category_income", "income.salary")

    today = date.today()
    created = 0
    for month_offset in (2, 1):
        month_start = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
        for _ in range(month_offset - 1):
            month_start = (month_start - timedelta(days=1)).replace(day=1)
        # Paychecks on the 1st and 15th.
        for payday in (1, 15):
            ledger_service.create_flat_transaction(
                session,
                account_ref="assets.checking",
                category_ref="income.salary",
                amount=Decimal("2400.00"),
                date=month_start.replace(day=payday),
                payee_name="Acme Corp Payroll",
                requested_status=core.Status.POSTED,
                origin=core.Origin.IMPORT,
                token=None,
            )
            created += 1
        for day, account, category, amount, payee in DEMO_SPEND:
            ledger_service.create_flat_transaction(
                session,
                account_ref=account,
                category_ref=category,
                amount=Decimal(amount),
                date=month_start.replace(day=min(day, 28)),
                payee_name=payee,
                requested_status=core.Status.POSTED,
                origin=core.Origin.IMPORT,
                token=None,
            )
            created += 1
        # One transfer pair per month: checking -> savings.
        ledger_service.create_transfer(
            session,
            from_account_ref="assets.checking",
            to_account_ref="assets.savings",
            amount=Decimal("400.00"),
            date=month_start.replace(day=16),
            description="Monthly savings transfer",
            requested_status=core.Status.POSTED,
            token=None,
        )
        created += 1

    # One recurring bill: rent on the 19th, materialized as expected instances.
    schedules.create_schedule(
        session,
        name="Rent",
        account_ref="assets.checking",
        rrule="FREQ=MONTHLY;BYMONTHDAY=19",
        amount_type="fixed",
        amount=Decimal("-1850.00"),
        payee_name="Landlord LLC",
        category_path="expenses.housing",
        token=None,
    )
    # One recurring transfer (#232): the card is paid in full from checking on
    # the 5th — instances are two-leg expected transfers sized from the
    # card's reported balance.
    schedules.create_schedule(
        session,
        name="Citi Card payment",
        account_ref="assets.checking",
        counterparty_account_ref="liabilities.citicard",
        rrule="FREQ=MONTHLY;BYMONTHDAY=5",
        amount_type="estimated",
        estimate_basis="counterparty_balance",
        amount=None,
        token=None,
    )
    matching.run_matching(session)
    return {"accounts": 12, "transactions": created, "schedules": 2}


def seed_demo_command(*, force: bool = False) -> None:
    from purser.cli import run_migrations
    from purser.db import get_sessionmaker

    run_migrations()
    with get_sessionmaker()() as session:
        stats = seed_demo(session, force=force)
        session.commit()
    print(f"seed-demo: {stats}")
