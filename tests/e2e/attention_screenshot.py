"""Synthetic Needs attention browser checks and README screenshot.

Build frontend first, then run with `uv run --group e2e python
 tests/e2e/attention_screenshot.py`. PURSER_E2E_CDP_URL attaches to an existing
browser; only this script's tab is used and closed. All requests on the demo
origin are fulfilled from fixtures or the local build. No bank data or app
credentials are used. Without CDP, CI may use its installed Chromium.
"""

import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
ORIGIN = "http://purser-attention-demo.invalid"
ACCOUNTS = [
    {
        "id": "checking",
        "name": "Everyday checking",
        "path": "assets.checking",
        "type": "checking",
        "balance": "4250.00",
    },
    {
        "id": "savings",
        "name": "Rainy day savings",
        "path": "assets.savings",
        "type": "savings",
        "balance": "10000.00",
    },
    {
        "id": "card",
        "name": "Everyday card",
        "path": "liabilities.card",
        "type": "credit_card",
        "balance": "-380.00",
    },
]
SCHEDULE = {
    "id": "example-schedule",
    "kind": "schedule",
    "status": "open",
    "source": "detected",
    "confidence": "0.825",
    "headline": "Example subscription: every 4 weeks ~-24.00 on assets.checking",
    "draft": {
        "name": "Example subscription",
        "account": "assets.checking",
        "rrule": "FREQ=WEEKLY;INTERVAL=4;BYDAY=MO",
        "dtstart": "2026-11-09",
        "amount": "-24.00",
        "amount_type": "fixed",
        "payee": "Example subscription",
        "category": "expenses.subscriptions",
    },
    "evidence_summary": {
        "count": 5,
        "first": "2026-07-20",
        "last": "2026-11-09",
        "cadence": "every 4 weeks",
        "amount_range": {"low": "-24.00", "high": "-8.00"},
        "price_changed": True,
        "latest_amount": "-24.00",
    },
    "seen_count": 3,
    "snooze_until": None,
}


def main() -> None:
    dist = Path(os.getenv("PURSER_E2E_DIST", str(ROOT / "frontend/dist")))
    assert (dist / "index.html").exists(), "Build frontend first"
    failures: list[str] = []
    state = {"fail": False}
    with sync_playwright() as p:
        cdp = os.getenv("PURSER_E2E_CDP_URL")
        browser = p.chromium.connect_over_cdp(cdp) if cdp else p.chromium.launch()
        page = browser.contexts[0].new_page() if cdp else browser.new_page()
        page.on("pageerror", lambda error: failures.append(str(error)))

        def route(request_route):
            parsed = urlparse(request_route.request.url)
            path, query = parsed.path, parse_qs(parsed.query)
            responses = {
                "/auth/setup/status": {"setup_needed": False},
                "/auth/me": {
                    "id": "demo",
                    "username": "demo",
                    "scopes": ["read", "write", "admin"],
                    "is_admin": True,
                },
                "/accounts": ACCOUNTS,
                "/networth": {
                    "net_worth": "13870.00",
                    "assets": "14250.00",
                    "liabilities": "380.00",
                },
                "/networth/potential": {
                    "potential_net_worth": "13870.00",
                    "potential_assets": "14250.00",
                    "unvested_value": "0.00",
                    "complete": True,
                    "unvested_awards": 0,
                    "unpriced_symbols": [],
                    "stale_price_symbols": [],
                    "unknown_quantity_awards": 0,
                    "unsupported_currency_awards": 0,
                },
                "/suggestions/counts": {
                    "total": 6,
                    "match": 1,
                    "transfer_pair": 2,
                    "schedule": 3,
                    "transfer_link": 0,
                },
                "/suggestions": [SCHEDULE] if query.get("kind") == ["schedule"] else [],
                "/matching/unmatched": [
                    {
                        "transaction_id": "utility",
                        "account_id": "checking",
                        "account_name": "Everyday checking",
                        "amount": "-86.00",
                        "date": "2026-11-12",
                        "aging_days": 3,
                        "has_proposed_match": False,
                        "amount_estimated": True,
                        "date_estimated": True,
                        "description": "Example electric bill",
                        "schedule_status": "active",
                    }
                ],
                "/transactions/review-summary": {
                    "total": 12,
                    "pending": 2,
                    "agent_pending": 0,
                    "oldest_date": "2026-11-01",
                    "accounts": [
                        {
                            "account_id": "checking",
                            "name": "Everyday checking",
                            "total": 8,
                            "pending": 1,
                            "agent_pending": 0,
                        },
                        {
                            "account_id": "card",
                            "name": "Everyday card",
                            "total": 4,
                            "pending": 1,
                            "agent_pending": 0,
                        },
                    ],
                },
            }
            if path == "/matching/unmatched" and state["fail"]:
                request_route.fulfill(status=503, json={"detail": "Example connection unavailable"})
            elif path in responses:
                request_route.fulfill(json=responses[path])
            elif path == "/":
                request_route.fulfill(path=str(dist / "index.html"), content_type="text/html")
            elif path.startswith("/assets/") and (dist / path.lstrip("/")).is_file():
                request_route.fulfill(path=str(dist / path.lstrip("/")))
            elif path.endswith(".ico"):
                request_route.fulfill(status=204)
            else:
                failures.append(f"Unexpected request: {path}")
                request_route.fulfill(status=404, json={"detail": "Fixture not found"})

        try:
            page.route(ORIGIN + "/**", route)
            page.set_viewport_size({"width": 1440, "height": 1080})
            page.goto(ORIGIN)
            expect(
                page.get_by_role("button", name="Recurring schedules", exact=False)
            ).to_be_visible()
            page.get_by_text("Transaction review", exact=False).click()
            page.screenshot(
                path=str(ROOT / "docs/public/screenshots/dashboard.png"), full_page=True
            )
            page.set_viewport_size({"width": 390, "height": 844})
            page.get_by_role("button", name="Recurring schedules", exact=False).click()
            expect(page.get_by_role("button", name="Create schedule", exact=True)).to_be_visible()
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), (
                "Mobile horizontal overflow"
            )
            page.get_by_role("button", name="Edit schedule", exact=True).click()
            expect(page.get_by_label("Repeats", exact=True)).to_have_value("weeks-4")
            page.get_by_label("Repeats", exact=True).select_option("monthly")
            expect(page.get_by_label("Day of month")).to_be_visible()
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), (
                "Editor horizontal overflow"
            )
            state["fail"] = True
            page.reload()
            expect(
                page.get_by_text("Some attention items could not be checked.", exact=False)
            ).to_be_visible(timeout=20000)
            expect(page.get_by_text("All clear.", exact=True)).to_have_count(0)
            assert not failures, failures
            print(
                json.dumps(
                    {
                        "desktop_screenshot": "docs/public/screenshots/dashboard.png",
                        "mobile_width": 390,
                        "checks": [
                            "group expansion",
                            "four-week editor",
                            "no horizontal overflow",
                            "partial failure",
                        ],
                        "data": "synthetic",
                    }
                )
            )
        finally:
            page.close()
            browser.close()


if __name__ == "__main__":
    main()
