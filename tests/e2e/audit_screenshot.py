"""Synthetic audit-page browser checks and documentation screenshot (#525).

Build frontend first. PURSER_E2E_CDP_URL attaches to the lab browser using
only a new tab. Every request is fulfilled locally; no real app data is used.
"""

import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
ORIGIN = "http://purser-audit-demo.invalid"
ENTRY = {
    "id": "synthetic-event",
    "at": "2026-11-15T14:42:00Z",
    "kind": "read",
    "action": "mcp.search_transactions",
    "entity_type": "transaction",
    "entity_id": None,
    "request_id": "synthetic-request",
    "channel": "mcp",
    "outcome": "shared",
    "identity": {
        "token_id": "synthetic-credential",
        "name": "Budget Assistant",
        "kind": "api",
        "integration_id": "synthetic-integration",
        "user_name": "Demo owner",
        "scopes": ["read"],
        "historical": True,
        "authorization": {"granted_by": "Demo owner", "granted_at": "2026-11-01T12:00:00Z"},
    },
    "sharing": {
        "fields": ["items[].date", "items[].amount", "items[].category"],
        "data_types": ["Dates", "Categories", "Individual amounts"],
        "withheld": ["payee"],
        "record_count": 24,
        "count_unit": "transactions",
        "accounts": ["Everyday checking"],
        "requested_coverage": {"period": "2026-11-01 to 2026-11-15"},
        "returned_dates": {"from": "2026-11-01", "to": "2026-11-15"},
        "response_has_more": False,
        "partial": False,
    },
    "extra_fields": [],
    "requested_extra": [],
    "before_after": None,
    "technical": {"tool": "search_transactions", "ok": True},
    "completion_point": "tool_return",
}


def main() -> None:
    dist = ROOT / "frontend/dist"
    errors: list[str] = []
    requests: list[dict[str, list[str]]] = []
    with sync_playwright() as p:
        cdp = os.getenv("PURSER_E2E_CDP_URL")
        browser = p.chromium.connect_over_cdp(cdp) if cdp else p.chromium.launch()
        page = browser.contexts[0].new_page() if cdp else browser.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))

        def route(r):
            url = urlparse(r.request.url)
            path = url.path
            if r.request.resource_type == "document":
                r.fulfill(path=str(dist / "index.html"), content_type="text/html")
            elif path.startswith("/assets/"):
                r.fulfill(path=str(dist / path.lstrip("/")))
            elif path == "/auth/setup/status":
                r.fulfill(json={"setup_needed": False})
            elif path == "/auth/me":
                r.fulfill(
                    json={"id": "demo", "scopes": ["read", "write", "admin"], "is_admin": True}
                )
            elif path == "/audit/options":
                r.fulfill(
                    json={
                        "integrations": [
                            {"id": "synthetic-integration", "name": "Budget Assistant"}
                        ],
                        "accounts": [{"id": "synthetic-account", "name": "Everyday checking"}],
                        "data_types": ["Dates", "Individual amounts"],
                        "retention_days": 90,
                        "retained_since": "2026-08-17T12:00:00Z",
                        "capture_health": {"failures": 0},
                    }
                )
            elif path == "/audit/activity":
                query = parse_qs(url.query)
                requests.append(query)
                entries = [
                    ENTRY,
                    {
                        **ENTRY,
                        "id": "synthetic-second",
                        "entity_type": "networth",
                        "identity": {**ENTRY["identity"], "name": "Planning Assistant"},
                        "sharing": {
                            **ENTRY["sharing"],
                            "data_types": ["Totals", "Balances"],
                            "record_count": None,
                            "withheld": [],
                            "accounts": ["All accounts"],
                        },
                    },
                    {**ENTRY, "id": "synthetic-third", "outcome": "denied", "sharing": None},
                ]
                if query.get("integration_id"):
                    entries = [ENTRY]
                r.fulfill(
                    json={"items": entries, "next_cursor": None, "until": "2026-11-15T20:00:00Z"}
                )
            elif path.endswith(".ico"):
                r.fulfill(status=204)
            else:
                errors.append(f"Unexpected route: {path}")
                r.fulfill(status=404)

        try:
            page.route(ORIGIN + "/**", route)
            page.set_viewport_size({"width": 1440, "height": 1080})
            page.goto(ORIGIN + "/#/audit")
            expect(
                page.get_by_text("24 transactions · Dates, Categories, Individual amounts").first
            ).to_be_visible()
            page.screenshot(path=str(ROOT / "docs/public/screenshots/audit.png"), full_page=True)
            page.locator("details > summary").first.click()
            expect(page.get_by_text("Withheld: Merchant names").first).to_be_visible()
            page.add_script_tag(path=str(ROOT / "frontend/node_modules/axe-core/axe.min.js"))
            violations = page.evaluate(
                "async () => (await axe.run(document, {runOnly: ['wcag2a','wcag2aa']}))"
                ".violations.filter(v => ['serious','critical'].includes(v.impact))"
                ".map(v => v.id + ': ' + v.help)"
            )
            assert not violations, violations
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (
                "mobile overflow"
            )
            page.get_by_label("Integrator", exact=True).select_option("synthetic-integration")
            expect(
                page.get_by_text("1 entries loaded · all matching entries loaded")
            ).to_be_visible()
            assert requests[-1]["integration_id"] == ["synthetic-integration"]
            page.emulate_media(color_scheme="dark")
            page.screenshot(path="/tmp/purser-audit-mobile-dark.png", full_page=True)
            assert not errors, errors
            print("Audit desktop/mobile, expanded evidence, filters and accessibility passed.")
        except Exception:
            print("Synthetic page diagnostics:", page.locator("body").inner_text(), errors)
            page.screenshot(path="/tmp/purser-audit-browser-failure.png", full_page=True)
            raise
        finally:
            page.close()
            browser.close()


if __name__ == "__main__":
    main()
