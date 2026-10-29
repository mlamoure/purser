"""Opt-in browser test against a NEW disposable Docker instance (#427).

PURSER_E2E_URL must point at the instance created by run_docker.py. Never point
this test at an existing instance: the pristine-setup precondition refuses it.
A shared browser (PURSER_E2E_CDP_URL) keeps its existing context; the test
opens and closes only its own tab.
"""

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx2
import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("PURSER_E2E_URL"), reason="requires isolated Docker E2E instance"
)


async def exercise_agent(url: str, token: str) -> None:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async with (
        httpx2.AsyncClient(headers={"Authorization": "Bearer " + token}) as http,
        streamable_http_client(url + "/mcp", http_client=http) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        described = await session.call_tool("describe", {})
        assert not described.is_error
        written = await session.call_tool(
            "create_transaction",
            {
                "account": "assets.e2e_checking",
                "category": "expenses.e2e_groceries",
                "amount": "-2.50",
                "date_": "2026-07-16",
                "status": "posted",
                "description": "Synthetic MCP write",
            },
        )
        # The agent acts with the user's authority (#436): the requested
        # status is honoured and the write is attributed to its token.
        assert not written.is_error
        assert written.structured_content["status"] == "posted"
        drafted = await session.call_tool(
            "propose_rule",
            {
                "type": "categorize",
                "category": "expenses.e2e_groceries",
                "match_field": "description",
                "match_mode": "contains",
                "match_text": "Synthetic",
            },
        )
        assert not drafted.is_error
        assert drafted.structured_content["state"] == "draft"


AXE = Path(__file__).resolve().parents[2] / "frontend" / "node_modules" / "axe-core" / "axe.min.js"


def audit(page: Any, where: str, findings: list[str]) -> None:
    """Accessibility at one onboarding step (#427): axe-core's WCAG 2 A/AA
    rules, keeping the serious and critical violations. Needs
    `cd frontend && npm ci` (axe-core is a pinned devDependency)."""
    page.add_script_tag(path=str(AXE))
    result = page.evaluate(
        "async () => (await axe.run(document, {runOnly: ['wcag2a', 'wcag2aa']})).violations"
        ".filter(v => ['serious', 'critical'].includes(v.impact))"
        ".map(v => `${v.id}: ${v.help} (${v.nodes.length})`)"
    )
    findings.extend(f"{where}: {line}" for line in result)


def focus_is_visible(page: Any) -> bool:
    return bool(
        page.evaluate(
            "() => { const el = document.activeElement;"
            " if (!el || el === document.body) return false;"
            " const s = getComputedStyle(el);"
            " return s.outlineStyle !== 'none' || s.boxShadow !== 'none'; }"
        )
    )


def no_horizontal_overflow(page: Any) -> bool:
    return bool(
        page.evaluate(
            "() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1"
        )
    )


def overflow_culprits(page: Any) -> str:
    """The widest elements past the viewport, for a fixable report."""
    return str(
        page.evaluate(
            "() => [...document.querySelectorAll('body *')]"
            ".filter(e => e.getBoundingClientRect().right >"
            " document.documentElement.clientWidth + 1)"
            ".slice(0, 5).map(e => e.tagName.toLowerCase() + (e.className ? '.' +"
            " String(e.className).split(' ').slice(0, 2).join('.') : '') + ' ' +"
            " Math.round(e.getBoundingClientRect().width) + 'px')"
        )
    )


def test_first_run_agent_access_and_logout() -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    url = os.environ["PURSER_E2E_URL"].rstrip("/")
    with playwright.sync_playwright() as pw:
        cdp = os.environ.get("PURSER_E2E_CDP_URL")
        browser = pw.chromium.connect_over_cdp(cdp) if cdp else pw.chromium.launch()
        context = browser.contexts[0] if cdp else browser.new_context()
        page = context.new_page()
        page.set_default_timeout(20000)
        try:
            status = context.request.get(url + "/auth/setup/status")
            assert status.ok and status.json() == {"setup_needed": True}, (
                "E2E requires a pristine disposable instance; refusing existing data"
            )
            page.goto(url)
            playwright.expect(page.get_by_role("heading", name="Welcome to Purser")).to_be_visible()
            findings: list[str] = []
            audit(page, "create admin", findings)
            # Keyboard: the first Tab lands on a control and its focus shows.
            page.keyboard.press("Tab")
            assert focus_is_visible(page), "the first focused control shows no focus indicator"
            # Labels must be associated with their inputs, not merely adjacent.
            page.get_by_label("Email", exact=True).fill("onboarding@example.com")
            page.get_by_label("Password (8+ characters)").fill("synthetic-onboarding-password")
            page.get_by_role("button", name="Create admin", exact=True).click()
            playwright.expect(
                page.get_by_text("Record your encryption key", exact=True)
            ).to_be_visible()
            audit(page, "encryption key", findings)
            page.get_by_label("I have stored this key somewhere safe").check()
            page.get_by_role("button", name="Continue", exact=True).click()
            page.get_by_role("button", name="Skip for now", exact=True).click()
            playwright.expect(
                page.get_by_text("Import bank history (optional)", exact=True)
            ).to_be_visible()
            audit(page, "import history", findings)
            page.get_by_role("button", name="Skip for now", exact=True).click()
            audit(page, "connect agent", findings)
            page.get_by_label("Token name", exact=True).fill("e2e-agent")
            with page.expect_response(
                lambda r: r.url == url + "/tokens" and r.request.method == "POST"
            ) as minted:
                page.get_by_role("button", name="Create agent token", exact=True).click()
            token_result = minted.value.json()
            token = token_result["token"]
            assert set(token_result["scopes"]) == {"read", "write"}
            headers = {"Authorization": "Bearer " + token}
            # Agent permissions are exercised against the real server.
            assert context.request.get(url + "/accounts", headers=headers).ok
            denied = context.request.post(
                url + "/tokens", headers=headers, data={"name": "escalate", "scopes": ["admin"]}
            )
            assert denied.status == 403
            page.get_by_role("button", name="Finish", exact=True).click()
            playwright.expect(
                page.get_by_role("link", name="Dashboard", exact=True)
            ).to_be_visible()
            assert context.request.get(url + "/auth/encryption-key").status == 410
            # Real ledger writes make the subsequent restore drill meaningful.
            for endpoint, body in [
                (
                    "/accounts",
                    {"name": "E2E Checking", "type": "checking", "path": "assets.e2e_checking"},
                ),
                (
                    "/categories",
                    {
                        "name": "E2E Groceries",
                        "type": "category_expense",
                        "path": "expenses.e2e_groceries",
                    },
                ),
            ]:
                response = context.request.post(url + endpoint, data=body)
                assert response.status == 201
            response = context.request.post(
                url + "/transactions",
                data={
                    "account": "assets.e2e_checking",
                    "category": "expenses.e2e_groceries",
                    "amount": "-12.34",
                    "date": "2026-07-15",
                    "status": "posted",
                    "payee": "Synthetic Shop",
                    "description": "E2E encrypted narrative",
                },
            )
            assert response.status == 201
            transaction = response.json()
            assert (
                context.request.get(url + "/transactions/" + transaction["id"]).json()[
                    "description"
                ]
                == "E2E encrypted narrative"
            )
            assert (
                context.request.post(
                    url + "/auth/setup",
                    data={"email": "second@example.com", "password": "synthetic-other-password"},
                ).status
                == 409
            )
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(lambda: asyncio.run(exercise_agent(url, token))).result(timeout=30)
            audit(page, "dashboard", findings)
            page.set_viewport_size({"width": 375, "height": 740})
            if not no_horizontal_overflow(page):
                findings.append("dashboard: scrolls sideways at 375px " + overflow_culprits(page))
            page.get_by_role("button", name="Log out", exact=True).click()
            playwright.expect(
                page.get_by_role("button", name="Sign in", exact=True)
            ).to_be_visible()
            audit(page, "sign in", findings)
            if not no_horizontal_overflow(page):
                findings.append("sign in: scrolls sideways at 375px " + overflow_culprits(page))
            assert not findings, "accessibility violations:\n" + "\n".join(findings)
            page.get_by_label("Email", exact=True).fill("onboarding@example.com")
            page.get_by_label("Password", exact=True).fill("synthetic-onboarding-password")
            page.get_by_role("button", name="Sign in", exact=True).click()
            playwright.expect(
                page.get_by_role("link", name="Dashboard", exact=True)
            ).to_be_visible()
            revoked = context.request.delete(url + "/tokens/" + token_result["id"])
            assert revoked.ok
            assert context.request.get(url + "/accounts", headers=headers).status == 401
        finally:
            page.close()
            if not cdp:
                context.close()
            browser.close()  # CDP: disconnect only; never close other tabs.
