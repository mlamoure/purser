"""README screenshots of the admin UI from a throwaway, demo-seeded instance (#543).

Run against a fresh instance only; this script creates its admin and adds
synthetic data. One way to get one (a project name and a spare host port keep
it apart from anything else running):

    docker build -t purser-shots:local .
    # a compose file with image purser-shots:local, port 127.0.0.1:18543:8000
    # and a postgres service, then:
    docker compose -p purser-shots up -d
    docker compose -p purser-shots exec purser purser seed-demo
    uv run --group e2e --with pillow python tests/e2e/readme_screenshots.py
    docker compose -p purser-shots down -v

PURSER_SHOTS_URL overrides the instance URL (default http://127.0.0.1:18543).
Everything is synthetic: the built-in demo data, an @example.com admin, a few
example schedules, a detected subscription and one MCP read by an example
agent token so the audit log has a sharing entry. The PNGs are written to
docs/public/screenshots/ in the light scheme, palette-quantized to keep the
repository small; `--dark DIR` also writes dark-scheme copies to DIR for
review (they are not committed).
"""

import argparse
import json
import os
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

from PIL import Image
from playwright.sync_api import Page, expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "docs/public/screenshots"
BASE = os.getenv("PURSER_SHOTS_URL", "http://127.0.0.1:18543").rstrip("/")
ADMIN = {"email": "owner@example.com", "password": "demo-password-1", "display_name": "Demo owner"}
SCHEDULES = [
    {
        "account": "assets.checking",
        "payee": "Example Employer",
        "memo": "Paycheck",
        "amount": "2400.00",
        "amount_type": "fixed",
        "category": "income.salary",
        "rrule": "FREQ=MONTHLY;BYMONTHDAY=1,15",
    },
    {
        "account": "assets.checking",
        "payee": "City Power & Light",
        "memo": "Electric",
        "amount": "-95.00",
        "amount_type": "estimated",
        "category": "expenses.utilities.electric_gas",
        "rrule": "FREQ=MONTHLY;BYMONTHDAY=22",
    },
    {
        "account": "liabilities.citicard",
        "payee": "Example Streaming",
        "memo": "Family plan",
        "amount": "-15.99",
        "amount_type": "fixed",
        "category": "expenses.entertainment.streaming",
        "rrule": "FREQ=MONTHLY;BYMONTHDAY=8",
    },
    {
        "account": "assets.checking",
        "counterparty_account": "assets.savings",
        "memo": "Savings transfer",
        "amount": "-200.00",
        "amount_type": "fixed",
        "rrule": "FREQ=MONTHLY;BYMONTHDAY=2",
    },
]
# Four monthly charges the schedule detector turns into a suggestion.
SUBSCRIPTION_DATES = ["2026-07-24", "2026-08-24", "2026-09-24", "2026-10-24"]


class Client:
    """Minimal JSON client: a cookie session, or a Bearer token for /mcp."""

    def __init__(self, bearer: str | None = None) -> None:
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
        self.headers = {"content-type": "application/json"}
        if bearer:
            self.headers |= {
                "authorization": f"Bearer {bearer}",
                "accept": "application/json, text/event-stream",
            }

    def call(self, method: str, path: str, body: object = None) -> tuple[dict[str, str], str]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(BASE + path, data=data, method=method, headers=self.headers)
        with self.opener.open(req, timeout=30) as resp:
            return dict(resp.headers), resp.read().decode()

    def json(self, method: str, path: str, body: object = None) -> object:
        return json.loads(self.call(method, path, body)[1] or "null")


def seed() -> None:
    """Admin, schedules, a detected suggestion and one agent read."""
    session = Client()
    status = session.json("GET", "/auth/setup/status")
    assert isinstance(status, dict) and status["setup_needed"], "Use a fresh, demo-seeded instance"
    session.json("POST", "/auth/setup", ADMIN)
    session.json("POST", "/auth/login", {k: ADMIN[k] for k in ("email", "password")})
    accounts = session.json("GET", "/accounts")
    assert isinstance(accounts, list) and accounts, "Run `purser seed-demo` first"
    today = session.json("GET", "/clock")
    assert isinstance(today, dict)
    for schedule in SCHEDULES:
        session.json("POST", "/schedules", {**schedule, "dtstart": today["ledger_today"]})
    for date in SUBSCRIPTION_DATES:
        session.json(
            "POST",
            "/transactions",
            {
                "account": "liabilities.citicard",
                "category": "expenses.services.subscriptions",
                "amount": "-11.99",
                "date": date,
                "payee": "Example Music",
                "description": "EXAMPLE MUSIC MONTHLY",
                "status": "posted",
            },
        )
    session.json("POST", "/schedules/detect")
    minted = session.json("POST", "/tokens", {"name": "Budget Assistant", "scopes": ["read"]})
    session.json("POST", "/tokens", {"name": "Household dashboard", "scopes": ["read"]})
    assert isinstance(minted, dict)
    agent = Client(bearer=str(minted["token"]))
    headers, _ = agent.call(
        "POST",
        "/mcp/",
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "budget-assistant", "version": "1.0"},
            },
        },
    )
    sid = {k.lower(): v for k, v in headers.items()}.get("mcp-session-id")
    if sid:
        agent.headers |= {"mcp-session-id": sid, "mcp-protocol-version": "2025-06-18"}
    agent.call("POST", "/mcp/", {"jsonrpc": "2.0", "method": "notifications/initialized"})
    _, body = agent.call(
        "POST",
        "/mcp/",
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "search_transactions", "arguments": {"account": "assets.checking"}},
        },
    )
    assert '"isError":true' not in body.replace(" ", ""), body[:300]


def quantize(path: Path) -> None:
    """256-color palette PNG: UI screenshots have few colors, and this keeps
    each file a fraction of its RGB size without visible banding."""
    with Image.open(path) as im:
        rgb = im.convert("RGB")
    palette = rgb.quantize(256, method=Image.Quantize.FASTOCTREE, dither=Image.Dither.NONE)
    palette.save(path, optimize=True)


def settle(page: Page, route: str) -> None:
    page.goto(BASE + "/#" + route)
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(300)


def shoot(page: Page, out: Path, quantized: bool) -> list[Path]:
    """Every README screenshot of the current scheme into `out`."""
    accounts = page.request.get(BASE + "/accounts").json()
    checking = next(a["id"] for a in accounts if a["path"] == "assets.checking")
    written: list[Path] = []

    def save(name: str, **kwargs: object) -> None:
        path = out / name
        target = kwargs.pop("locator", None)
        if target is not None:
            target.screenshot(path=str(path))  # type: ignore[attr-defined]
        else:
            page.screenshot(path=str(path), **kwargs)  # type: ignore[arg-type]
        written.append(path)

    # Dashboard: the setup checklist dismissed (per browser), a suggestion
    # group and the review summary open.
    settle(page, "/")
    dismiss = page.get_by_role("button", name="Dismiss", exact=True)
    if dismiss.count():
        dismiss.click()
    page.get_by_role("button", name="Scheduled transactions", exact=False).first.click()
    expect(page.get_by_text("Example Music", exact=False).first).to_be_visible()
    page.get_by_text("Transaction review", exact=False).first.click()
    save("dashboard.png", full_page=True)

    settle(page, f"/accounts/{checking}")
    save("register.png")

    settle(page, "/schedules")
    expect(page.get_by_role("img", name="Forecast", exact=False)).to_be_visible()
    # The forecast and the first account's table, not the whole page.
    save("schedules.png", full_page=True, clip={"x": 0, "y": 0, "width": 1280, "height": 1150})

    settle(page, "/settings/agents")
    expect(page.get_by_text("Budget Assistant", exact=True)).to_be_visible()
    # The create form and the token list, without the login sessions below.
    save("tokens.png", full_page=True, clip={"x": 0, "y": 0, "width": 1280, "height": 990})
    privacy = (
        page.locator("div")
        .filter(has=page.get_by_role("heading", name="Privacy with agents", exact=True))
        .last
    )
    save("privacy.png", locator=privacy)

    settle(page, "/settings/audit")
    page.get_by_text("Budget Assistant", exact=True).first.click()
    page.wait_for_timeout(300)
    save("audit.png")

    if quantized:
        for path in written:
            quantize(path)
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dark", type=Path, help="also write dark-scheme copies here")
    parser.add_argument("--no-seed", action="store_true", help="the instance is already set up")
    args = parser.parse_args()
    if not args.no_seed:
        seed()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        runs = [("light", OUT)] + ([("dark", args.dark)] if args.dark else [])
        for scheme, out in runs:
            out.mkdir(parents=True, exist_ok=True)
            context = browser.new_context(
                viewport={"width": 1280, "height": 860}, device_scale_factor=2, color_scheme=scheme
            )
            login = {k: ADMIN[k] for k in ("email", "password")}
            context.request.post(BASE + "/auth/login", data=login)
            page = context.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda error, errors=errors: errors.append(str(error)))
            written = shoot(page, out, quantized=scheme == "light")
            assert not errors, errors
            print(json.dumps({"scheme": scheme, "files": [str(p) for p in written]}))
            context.close()
        browser.close()


if __name__ == "__main__":
    main()
