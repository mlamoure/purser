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
example schedules, a detected subscription, two MCP reads by an example
agent token so the audit log shows one identifiable answer and one that
withheld payees, descriptions and amounts on request (#613), and (#596) a hand-tracked
brokerage account with a year of weekly closes, a house and the mortgage it
secures, so the Dashboard shows its account groups and the account page its
balance chart and holdings table. The PNGs are written to
docs/public/screenshots/ in the light scheme, palette-quantized to keep the
repository small; `--dark DIR` also writes dark-scheme copies to DIR for
review (they are not committed).

Each scheme is then checked on the same pages (#556, which retired the
mocked attention and audit scripts): no register badge is cut off at the
table's edge, at phone width no page scrolls sideways (the Dashboard with
its schedule suggestion open for editing included), and axe-core finds no
serious or critical WCAG 2 A/AA violation. axe-core is the
frontend's pinned devDependency: run `cd frontend && npm ci` first.
"""

import argparse
import json
import os
import urllib.request
from datetime import date as Day
from datetime import timedelta
from decimal import Decimal
from http.cookiejar import CookieJar
from pathlib import Path

from PIL import Image
from playwright.sync_api import Page, expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "docs/public/screenshots"
AXE = ROOT / "frontend/node_modules/axe-core/axe.min.js"
PHONE = {"width": 390, "height": 844}
# The register's badges (status, auto-linked, scheduled …) that end past the
# right edge of the table's visible area (#556: "auto-linked" was cut off).
CLIPPED_BADGES = """() => {
  const table = [...document.querySelectorAll('table')].find((t) =>
    [...t.querySelectorAll('thead th')].some((th) => th.textContent.trim() === 'Status'));
  if (!table) return ['no register table'];
  const edge = table.parentElement.getBoundingClientRect().right;
  return [...table.querySelectorAll('tbody .rounded-full')]
    .filter((badge) => badge.getBoundingClientRect().right > edge + 0.5)
    .map((badge) => badge.textContent);
}"""
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
    # One reconciliation adjustment (#694, decision 141): the demo's rows are
    # typed in, not from bank sync, so this is what the Dashboard's review
    # summary and the register's review badge show.
    checking = next(a for a in accounts if a["path"] == "assets.checking")
    account = session.json("GET", f"/accounts/{checking['id']}")
    assert isinstance(account, dict)
    session.json(
        "POST",
        f"/accounts/{checking['id']}/reconcile",
        {
            "as_of": today["ledger_today"],
            "statement_balance": str(Decimal(account["balance"]) - Decimal("4.17")),
        },
    )
    seed_investments_and_property(session, Day.fromisoformat(today["ledger_today"]))
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
    # A count needs no payee names or single amounts: the agent withholds
    # them (#613), and the audit log marks this read "Privacy used".
    _, body = agent.call(
        "POST",
        "/mcp/",
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "search_transactions",
                "arguments": {
                    "account": "assets.checking",
                    "withhold": ["payee", "description", "amount"],
                },
            },
        },
    )
    assert '"isError":true' not in body.replace(" ", ""), body[:300]


def seed_investments_and_property(session: Client, today: Day) -> None:
    """A brokerage account tracked by hand with a year of weekly closes, a
    house and the mortgage it secures (#596). Prices are whole cents built
    from integers, so no amount passes through a float."""
    broker = session.json(
        "POST",
        "/accounts",
        {"name": "Index Brokerage", "type": "brokerage", "path": "assets.brokerage"},
    )
    assert isinstance(broker, dict)
    session.json("PATCH", f"/accounts/{broker['id']}", {"holdings_source": "manual"})
    opened = today - timedelta(days=371)
    for symbol, name, quantity, basis in (
        ("EXTM", "Example Total Market Fund", "40", "8400.00"),
        ("EXIN", "Example International Fund", "120", "6600.00"),
    ):
        session.json(
            "POST",
            f"/accounts/{broker['id']}/positions",
            {
                "symbol": symbol,
                "name": name,
                "quantity": quantity,
                "cost_basis": basis,
                "as_of": opened.isoformat(),
            },
        )
    for week in range(54):
        day = opened + timedelta(days=7 * week)
        if day > today:
            break
        # A gentle climb with a wobble and one dip, in cents.
        wobble = ((week * 37) % 11 - 5) * 90
        dip = -1500 if 20 <= week <= 26 else 0
        closes = {
            "EXTM": 21000 + week * 95 + wobble + dip,
            "EXIN": 5500 + week * 22 + wobble // 3,
        }
        for symbol, cents in closes.items():
            session.json(
                "POST",
                "/prices",
                {"symbol": symbol, "date": day.isoformat(), "close": str(Decimal(cents) / 100)},
            )

    house = session.json(
        "POST",
        "/accounts",
        {
            "name": "Home",
            "type": "property",
            "path": "assets.home",
            "asset_kind": "house",
            "acquired_on": (today - timedelta(days=2000)).isoformat(),
        },
    )
    assert isinstance(house, dict)
    session.json(
        "POST",
        f"/accounts/{house['id']}/value-changes",
        {
            "as_of": (today - timedelta(days=400)).isoformat(),
            "amount": "450000.00",
            "kind": "capital",
        },
    )
    session.json(
        "POST",
        f"/accounts/{house['id']}/value-changes",
        {"as_of": (today - timedelta(days=120)).isoformat(), "amount": "12500.00"},
    )
    mortgage = session.json(
        "POST", "/accounts", {"name": "Mortgage", "type": "loan", "path": "liabilities.mortgage"}
    )
    assert isinstance(mortgage, dict)
    for month in range(13, -1, -1):
        owed = Decimal(318000) - Decimal(910) * (13 - month)
        session.json(
            "POST",
            f"/accounts/{mortgage['id']}/valuations",
            {"as_of": (today - timedelta(days=30 * month)).isoformat(), "value": str(-owed)},
        )
    session.json("PATCH", f"/accounts/{mortgage['id']}", {"secured_by_id": house["id"]})


# Shots holding the balance chart's gradient fill (#594), which bands into
# visible steps at 256 colors unless dithered.
GRADIENT_SHOTS = {"account.png", "register.png"}


def quantize(path: Path) -> None:
    """256-color palette PNG: UI screenshots have few colors, and this keeps
    each file a fraction of its RGB size without visible banding. A shot with
    a gradient is dithered against that palette instead, which costs size
    (the noise compresses worse) but keeps the fill smooth."""
    with Image.open(path) as im:
        rgb = im.convert("RGB")
    palette = rgb.quantize(256, method=Image.Quantize.FASTOCTREE, dither=Image.Dither.NONE)
    if path.name in GRADIENT_SHOTS:
        palette = rgb.quantize(palette=palette, dither=Image.Dither.FLOYDSTEINBERG)
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
    page.get_by_text("Transactions that need review", exact=False).first.click()
    save("dashboard.png", full_page=True)

    settle(page, f"/accounts/{checking}")
    page.wait_for_timeout(2600)  # the balance chart's draw-in (#594)
    save("register.png")

    # An account page (#594, #595): the balance chart, drawn in, above the
    # holdings table, without the register below.
    broker = next(a["id"] for a in accounts if a["path"] == "assets.brokerage")
    settle(page, f"/accounts/{broker}")
    expect(
        page.get_by_role("region", name="Holdings").get_by_text("Unrealized gain")
    ).to_be_visible()
    page.wait_for_timeout(2600)
    bottom = page.get_by_role("region", name="Holdings").bounding_box()
    assert bottom is not None
    save(
        "account.png",
        full_page=True,
        clip={"x": 0, "y": 0, "width": 1280, "height": bottom["y"] + bottom["height"] + 24},
    )

    settle(page, "/schedules")
    expect(page.get_by_role("img", name="Forecast", exact=False)).to_be_visible()
    # The forecast and the first account's table, not the whole page.
    save("schedules.png", full_page=True, clip={"x": 0, "y": 0, "width": 1280, "height": 1150})

    settle(page, "/settings/agents")
    expect(page.get_by_text("Budget Assistant", exact=True)).to_be_visible()
    # How Purser is reached (#632), the create form and the token list, without
    # the login sessions below: the clip ends under the token table's card.
    tokens = page.get_by_role("table").filter(has_text="Budget Assistant").bounding_box()
    assert tokens is not None
    save(
        "tokens.png",
        full_page=True,
        clip={"x": 0, "y": 0, "width": 1280, "height": tokens["y"] + tokens["height"] + 48},
    )
    settle(page, "/settings/audit")
    expect(page.get_by_text("Privacy used", exact=True)).to_be_visible()
    expect(page.get_by_text("Identifiable", exact=True)).to_be_visible()
    # Both reads, collapsed: the one that withheld fields on request (newest,
    # "Privacy used") above the one that returned identifiable data.
    save("audit.png")

    if quantized:
        for path in written:
            quantize(path)
    return written


def open_schedule_editor(page: Page) -> None:
    """The Dashboard's detected subscription, open in its editor."""
    page.get_by_role("button", name="Scheduled transactions", exact=False).first.click()
    page.get_by_role("button", name="Edit first", exact=True).first.click()
    expect(page.get_by_label("Repeats", exact=True).first).to_be_visible()


def check(page: Page) -> list[str]:
    """What each README page must keep: the register's badges inside the
    table at the screenshot width, and at phone width no sideways scroll and
    no serious or critical axe-core WCAG 2 A/AA violation."""
    accounts = page.request.get(BASE + "/accounts").json()
    checking = next(a["id"] for a in accounts if a["path"] == "assets.checking")
    broker = next(a["id"] for a in accounts if a["path"] == "assets.brokerage")
    pages = [
        ("/", open_schedule_editor),
        ("/accounts", None),
        (f"/accounts/{checking}", None),
        (f"/accounts/{broker}", None),
        ("/schedules", None),
        ("/settings/agents", None),
        ("/settings/audit", None),
    ]
    problems: list[str] = []
    settle(page, f"/accounts/{checking}")
    clipped = page.evaluate(CLIPPED_BADGES)
    if clipped:
        problems.append(f"register: badges cut off at the table's edge: {clipped}")
    page.set_viewport_size(PHONE)
    for route, prepare in pages:
        settle(page, route)
        if prepare:
            prepare(page)
        if not page.evaluate("document.documentElement.scrollWidth <= innerWidth"):
            problems.append(f"{route}: scrolls sideways at {PHONE['width']}px")
        page.add_script_tag(path=str(AXE))
        violations = page.evaluate(
            "async () => (await axe.run(document, {runOnly: ['wcag2a', 'wcag2aa']}))"
            ".violations.filter(v => ['serious', 'critical'].includes(v.impact))"
            ".map(v => v.id + ': ' + v.help + ' (' + v.nodes.length + ')')"
        )
        problems += [f"{route}: {v}" for v in violations]
    return problems


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
            problems = check(page)
            assert not problems, f"{scheme}: {problems}"
            assert not errors, errors
            print(json.dumps({"scheme": scheme, "files": [str(p) for p in written]}))
            context.close()
        browser.close()


if __name__ == "__main__":
    main()
