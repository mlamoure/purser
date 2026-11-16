"""Small, value-free inventories of the response that actually leaves Purser.

Account labels and request coverage are encrypted with the audit detail. Never
store the body, financial values, arbitrary request text, or credentials here.
Unknown shapes stay partial rather than being mistaken for no disclosure.
"""

import json
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from purser.models import Account, ApiToken, User

MAX_BODY = 16 * 1024 * 1024
MAX_FIELDS = 128
MAX_NODES = 100_000
_SAFE_KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SECRET_KEYS = {"token", "password", "secret", "authorization", "access_url", "encryption_key"}
_LIST_KEYS = (
    "items",
    "results",
    "result",
    "transactions",
    "accounts",
    "holdings",
    "lots",
    "awards",
    "days",
    "points",
    "series",
)
_ACCOUNT_KEYS = {"account", "account_path", "account_id", "account_ref"}
_DATE_KEYS = {"date", "day", "start", "end", "start_date", "end_date", "from_date", "to_date"}
_FIELDS = {
    "payee": "Merchant names",
    "payee_name": "Merchant names",
    "description": "Descriptions",
    "memo": "Descriptions",
    "note": "Descriptions",
    "amount": "Individual amounts",
    "net_amount": "Individual amounts",
    "balance": "Balances",
    "ledger_balance": "Balances",
    "net_worth": "Totals",
    "total": "Totals",
    "total_amount": "Totals",
    "start_balance": "Balances",
    "end_balance": "Balances",
    "low_balance": "Balances",
    "category": "Categories",
    "category_path": "Categories",
    "date": "Dates",
    "day": "Dates",
    "currency": "Currencies",
    "quantity": "Quantities",
    "shares": "Quantities",
    "symbol": "Securities",
    "external_id": "External identifiers",
    "external_ids": "External identifiers",
    "account": "Accounts",
    "account_id": "Accounts",
    "account_path": "Accounts",
}


def identity(session: Session, token: ApiToken) -> dict[str, Any]:
    owner = session.get(User, token.user_id)
    return {
        "token_id": str(token.id),
        "name": token.name,
        "kind": token.kind,
        "integration_id": str(token.integration_id) if token.integration_id else None,
        "user_id": str(token.user_id),
        "user_name": (owner.display_name or owner.email) if owner else None,
        "scopes": list(token.scopes),
        "created_at": token.created_at.isoformat(),
        "authorization": token.authorization,
    }


def payload(value: Any) -> tuple[Any, bool]:
    """Unwrap MCP structured/text results without counting duplicate tiers."""
    structured = getattr(value, "structured_content", None)
    content = getattr(value, "content", None)
    if structured is not None:
        # JSON text often duplicates structuredContent, including the SDK's
        # {result: []} envelope. Free text may carry additional narrative.
        extra_text = False
        for block in content or []:
            try:
                text_body = json.loads(getattr(block, "text", ""))
                if text_body != structured and not (
                    isinstance(structured, dict) and text_body == structured.get("result")
                ):
                    extra_text = True
            except (ValueError, TypeError):
                extra_text = True
        return structured, extra_text
    if content is not None:
        texts = [getattr(block, "text", None) for block in content]
        if len(texts) == 1 and isinstance(texts[0], str):
            return payload(texts[0])
        return None, True
    if isinstance(value, (str, bytes)):
        try:
            return json.loads(value), False
        except (ValueError, UnicodeDecodeError):
            return None, bool(value)
    return value, False


def summarize(value: Any, *, requested: dict[str, Any] | None = None) -> dict[str, Any]:
    body, has_text = payload(value)
    fields: set[str] = set()
    types: set[str] = set()
    withheld: set[str] = set()
    accounts: set[str] = set()
    dates: set[str] = set()
    sensitive: set[str] = set()
    nodes = 0
    partial = has_text
    truncated = False

    def walk(obj: Any, path: str = "", key: str = "") -> None:
        nonlocal nodes, partial, truncated
        nodes += 1
        if nodes > MAX_NODES:
            partial = True
            return
        if isinstance(obj, dict):
            if isinstance(obj.get("path"), str) and obj.get("type") in {
                "checking",
                "savings",
                "cash",
                "credit_card",
                "brokerage",
                "property",
                "loan",
                "asset",
                "liability",
            }:
                accounts.add(obj["path"])
            for k, v in obj.items():
                if k in _SECRET_KEYS or any(
                    word in str(k).lower() for word in ("password", "secret", "token_hash")
                ):
                    if v is not None:
                        types.add("Credential material")
                    continue
                if k == "withheld" and isinstance(v, list):
                    withheld.update(
                        x for x in v if x in {"payee", "description", "amount", "external_ids"}
                    )
                    continue
                if k in {"next_cursor", "next", "has_more", "truncated"}:
                    truncated = truncated or bool(v)
                    continue
                if k in _ACCOUNT_KEYS and isinstance(v, str):
                    accounts.add(v)
                if k in _DATE_KEYS and isinstance(v, str) and _DATE.fullmatch(v):
                    dates.add(v)
                label = k if isinstance(k, str) and _SAFE_KEY.fullmatch(k) else "{group}"
                walk(v, f"{path}.{label}" if path else label, str(k))
        elif isinstance(obj, list):
            for item in obj:
                walk(item, f"{path}[]", key)
        elif obj is not None:
            if len(fields) < MAX_FIELDS:
                fields.add(path or "value")
            else:
                partial = True
            if obj == "[withheld]":
                return
            if key in {"payee", "payee_name"} and isinstance(obj, str) and obj.startswith("py_"):
                types.add("Merchant references")
            elif key in _FIELDS:
                if key == "total" and isinstance(obj, int):
                    return  # paginated result count, not a monetary total
                types.add(_FIELDS[key])
                if key in {"payee", "payee_name"}:
                    sensitive.add("payee")
                if key in {"description", "memo", "note"}:
                    sensitive.add("description")
                if key in {"amount", "net_amount"}:
                    sensitive.add("amount")
                if key in {"external_id", "external_ids"}:
                    sensitive.add("external_ids")

    walk(body)
    if has_text:
        types.add("Text response")
    count = len(body) if isinstance(body, list) else None
    unit = "records"
    if isinstance(body, dict):
        lists = [(key, body[key]) for key in _LIST_KEYS if isinstance(body.get(key), list)]
        if len(lists) == 1:
            unit, values = lists[0]
            count = len(values)
        # An aggregate is an answer, not an arbitrary count of its JSON keys.
    coverage = {}
    requested_accounts: set[str] = set()
    for k, v in (requested or {}).items():
        if k in {
            "since",
            "until",
            "start",
            "end",
            "period",
            "horizon",
            "horizon_days",
            "date",
            "as_of",
        } and isinstance(v, (str, int)):
            coverage[k] = str(v)[:100]
        if k in _ACCOUNT_KEYS and isinstance(v, str):
            requested_accounts.add(v)
    return {
        "version": 1,
        "fields": sorted(fields),
        "data_types": sorted(types),
        "withheld": sorted(withheld),
        "sensitive_fields": sorted(sensitive),
        "record_count": count,
        "count_unit": unit,
        "accounts": sorted(accounts)[:100],
        "requested_accounts": sorted(requested_accounts)[:100],
        "requested_coverage": coverage,
        "returned_dates": {"from": min(dates), "to": max(dates)} if dates else None,
        "response_has_more": truncated,
        "partial": partial or len(accounts) > 100,
        "empty": count == 0 and not partial and types <= {"Accounts", "Dates", "Currencies"},
        "captured_at": datetime.now(UTC).isoformat(),
    }


def account_ids(session: Session, summary: dict[str, Any]) -> list[uuid.UUID]:
    refs = (summary.get("accounts") or []) + (summary.get("requested_accounts") or [])
    if not refs:
        return []
    # Resolve only existing account references; never put personal labels in an index.
    from sqlalchemy import String, cast, or_

    return list(
        session.scalars(
            select(Account.id).where(
                or_(cast(Account.id, String).in_(refs), cast(Account.path, String).in_(refs))
            )
        )
    )
