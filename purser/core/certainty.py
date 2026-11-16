"""Independent occurrence certainty; pure rules for issue #512."""

from datetime import date
from decimal import Decimal

STATES = ("estimated", "confirmed")
SOURCES = ("history", "statement", "payroll", "invoice", "approval", "user", "agent", "other")


def status(amount: str, date: str) -> str:
    return "estimated" if "estimated" in (amount, date) else "confirmed"


def variance(
    original_amount: Decimal, original_date: date, amount: Decimal, date_: date
) -> dict[str, str | int]:
    return {"amount": str(amount - original_amount), "days": (date_ - original_date).days}
