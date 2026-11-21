import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { components } from "../api/schema";
import { overrideBody } from "../lib/utils";
import { apiMock, fail, mockApi, ok } from "../test/api";
import { renderWithProviders } from "../test/render";
import Schedules, { cadenceLabel, ForecastChart, InstancesPanel, negateAmount } from "./Schedules";

type ScheduleDetail = components["schemas"]["ScheduleDetail"];
type InstanceOut = components["schemas"]["InstanceOut"];
type ForecastOut = components["schemas"]["ForecastOut"];

const instance = (over: Partial<InstanceOut> = {}): InstanceOut => ({
  transaction_id: "tx-1",
  date: "2026-11-05",
  occurrence_date: "2026-11-05",
  amount: "-340.0000",
  pinned: false,
  overdue: false,
  basis: "counterparty_balance",
  ...over,
});

// A card payment from checking: a transfer schedule whose instances carry two legs.
const schedule = (instances: InstanceOut[]): ScheduleDetail => ({
  id: "s-1",
  name: "Card payment",
  account_id: "a-1",
  counterparty_account_id: "a-2",
  category_path: null,
  rrule: "FREQ=MONTHLY;BYMONTHDAY=5",
  dtstart: "2026-10-05",
  date_type: "fixed",
  amount_type: "estimated",
  amount: null,
  estimate_basis: "counterparty_balance",
  currency: "USD",
  next_due: "2026-11-05",
  autopost_policy: "wait_for_match",
  source: "manual",
  status: "active",
  ended_at: null,
  instances,
});

async function openOverride() {
  const view = renderWithProviders(<InstancesPanel scheduleId="s-1" />);
  const row = (await screen.findAllByText("Nov 5, 2026"))[0].closest("tr")!;
  await view.user.click(within(row).getByRole("button", { name: "Override" }));
  return { ...view, row };
}

describe("InstancesPanel override (#350)", () => {
  it("sends only the date when only the date changed", async () => {
    mockApi({
      "GET /schedules/{schedule_id}": ok(schedule([instance()])),
      "PATCH /transactions/{tx_id}": ok({}),
    });
    const { user, row } = await openOverride();
    const dateInput = row.querySelector<HTMLInputElement>('input[type="date"]')!;
    await user.clear(dateInput);
    await user.type(dateInput, "2026-11-03");
    await user.click(within(row).getByRole("button", { name: "Save" }));

    expect(apiMock.PATCH).toHaveBeenCalledWith("/transactions/{tx_id}", {
      params: { path: { tx_id: "tx-1" } },
      body: { date: "2026-11-03" },
    });
  });

  it("sends only the amount when only the amount changed", async () => {
    mockApi({
      "GET /schedules/{schedule_id}": ok(schedule([instance()])),
      "PATCH /transactions/{tx_id}": ok({}),
    });
    const { user, row } = await openOverride();
    const amountInput = within(row).getByPlaceholderText("amount");
    await user.clear(amountInput);
    await user.type(amountInput, "-8347.00");
    await user.click(within(row).getByRole("button", { name: "Save" }));

    expect(apiMock.PATCH).toHaveBeenCalledWith("/transactions/{tx_id}", {
      params: { path: { tx_id: "tx-1" } },
      body: { amount: "-8347.00" },
    });
  });

  it("makes no edit — so pins nothing — when nothing changed", async () => {
    mockApi({ "GET /schedules/{schedule_id}": ok(schedule([instance()])) });
    const { user, row } = await openOverride();
    await user.click(within(row).getByRole("button", { name: "Save" }));

    expect(apiMock.PATCH).not.toHaveBeenCalled();
    expect(await screen.findByRole("button", { name: "Override" })).toBeInTheDocument();
  });
});

describe("overrideBody", () => {
  it("compares as typed and drops blanks", () => {
    const original = { amount: "-340.0000", date: "2026-11-05" };
    expect(overrideBody(original, { amount: "-340.0000", date: "2026-11-05" })).toEqual({});
    expect(overrideBody(original, { amount: " -350.00 ", date: "2026-11-05" })).toEqual({ amount: "-350.00" });
    expect(overrideBody(original, { amount: "", date: "" })).toEqual({});
    expect(overrideBody({ amount: null, date: "2026-11-05" }, { amount: "-1", date: "2026-11-06" })).toEqual({
      amount: "-1",
      date: "2026-11-06",
    });
  });
});

describe("InstancesPanel reset (#353)", () => {
  it("offers Reset on pinned rows only and posts it", async () => {
    mockApi({
      "GET /schedules/{schedule_id}": ok(
        schedule([
          instance({ transaction_id: "tx-pinned", date: "2026-11-03", pinned: true }),
          instance({ transaction_id: "tx-free", date: "2026-12-05", occurrence_date: "2026-12-05" }),
        ]),
      ),
      "POST /transactions/{tx_id}/reset-instance": ok({}),
    });
    const { user } = renderWithProviders(<InstancesPanel scheduleId="s-1" />);
    const pinnedRow = (await screen.findByText("Nov 3, 2026")).closest("tr")!;
    const freeRow = screen.getAllByText("Dec 5, 2026")[0].closest("tr")!;
    expect(within(freeRow).queryByRole("button", { name: "Reset" })).not.toBeInTheDocument();

    await user.click(within(pinnedRow).getByRole("button", { name: "Reset" }));
    expect(apiMock.POST).toHaveBeenCalledWith("/transactions/{tx_id}/reset-instance", {
      params: { path: { tx_id: "tx-pinned" } },
    });
  });
});

describe("InstancesPanel card statement (#351)", () => {
  it("records a statement balance on a card payment and shows its basis", async () => {
    mockApi({
      "GET /schedules/{schedule_id}": ok(
        schedule([
          instance(),
          instance({
            transaction_id: "tx-stated",
            date: "2026-12-05",
            occurrence_date: "2026-12-05",
            amount: "-8347.0000",
            pinned: true,
            basis: "statement",
            statement_balance: "8347.0000",
            statement_date: "2026-11-22",
            statement_source: "agent",
          }),
        ]),
      ),
      "POST /schedules/statement-balance": ok({}),
    });
    const { user } = renderWithProviders(<InstancesPanel scheduleId="s-1" cardPayment />);
    const row = (await screen.findAllByText("Nov 5, 2026"))[0].closest("tr")!;
    const stated = screen.getAllByText("Dec 5, 2026")[0].closest("tr")!;
    expect(within(row).getByText("current balance")).toBeInTheDocument();
    expect(within(stated).getByText("statement · closed Nov 22, 2026")).toBeInTheDocument();
    expect(within(stated).queryByText("pinned")).not.toBeInTheDocument();

    await user.click(within(row).getByRole("button", { name: "Statement" }));
    await user.type(within(row).getByLabelText("Statement balance (as printed)"), "8347.00");
    await user.type(within(row).getByLabelText("Statement closing date"), "2026-10-22");
    await user.click(within(row).getByRole("button", { name: "Save" }));

    expect(apiMock.POST).toHaveBeenCalledWith("/schedules/statement-balance", {
      body: { transaction_id: "tx-1", statement_balance: "8347.00", statement_date: "2026-10-22" },
    });
  });

  it("offers Statement only on a card payment", async () => {
    mockApi({ "GET /schedules/{schedule_id}": ok(schedule([instance()])) });
    renderWithProviders(<InstancesPanel scheduleId="s-1" />);
    const row = (await screen.findAllByText("Nov 5, 2026"))[0].closest("tr")!;
    expect(within(row).getByRole("button", { name: "Override" })).toBeInTheDocument();
    expect(within(row).queryByRole("button", { name: "Statement" })).not.toBeInTheDocument();
  });
});

const forecast = (banded: boolean): ForecastOut => {
  const day = (date: string, balance: string, low: string, high: string) => ({
    date,
    balance,
    events: [],
    ...(banded ? { low, high } : { low: null, high: null }),
  });
  return {
    account_id: "a-1",
    account_path: "assets.checking",
    start_balance: "1300.0000",
    min_balance: null,
    first_shortfall: null,
    coverage: { schedules_active: 1, instances: 1, projected: 0, suggestions_open: 0, matches_pending: 0, matches_pending_net: null },
    days: [
      day("2026-11-01", "1300.0000", "1300.0000", "1300.0000"),
      {
        ...day("2026-11-02", "100.0000", "-200.0000", "400.0000"),
        events: [
          {
            label: "Citi payment",
            amount: "-1200.0000",
            transaction_id: "tx-1",
            projected: false,
            overdue: false,
            basis: "rolling_avg_3",
            range: banded ? { low: "-1500.0000", high: "-900.0000" } : null,
          },
        ],
      },
      day("2026-11-03", "100.0000", "-200.0000", "400.0000"),
    ],
    pessimistic_low: banded ? { date: "2026-11-02", balance: "-200.0000" } : null,
    optimistic_low: banded ? { date: "2026-11-02", balance: "400.0000" } : null,
  };
};

describe("ForecastChart range band (#247)", () => {
  it("shades the band and says how low it could go", async () => {
    mockApi({ "GET /forecast": ok(forecast(true)) });
    const { container } = renderWithProviders(<ForecastChart accountPath="assets.checking" />);
    expect(await screen.findByText(/Could dip to/)).toBeInTheDocument();
    const band = container.querySelector('[data-testid="forecast-band"]');
    expect(band?.getAttribute("fill")).toBe("var(--chart-band)");
    expect(container.querySelector("circle title")?.textContent).toContain("3-payment average");
  });

  it("labels the axis and the low with the forecast's own decimals (#540)", async () => {
    mockApi({ "GET /forecast": ok(forecast(true)) });
    const { container } = renderWithProviders(
      <ForecastChart accountPath="assets.checking" currency="USD" />,
    );
    expect(await screen.findByText(/Low \$100\.00 on Nov 2, 2026\./)).toBeInTheDocument();
    expect(screen.getByText(/Could dip to -\$200\.00 on Nov 2, 2026/)).toBeInTheDocument();
    // The extremes are picked from the strings (band high 1300, band low
    // -200) and shown as they are, never as a float turned back into money.
    const labels = [...container.querySelectorAll("svg text")].map((t) => t.textContent);
    expect(labels).toEqual(["$1,300.00", "-$200.00", "Nov 1, 2026", "Nov 3, 2026"]);
  });

  it("draws no band without one", async () => {
    mockApi({ "GET /forecast": ok(forecast(false)) });
    const { container } = renderWithProviders(<ForecastChart accountPath="assets.checking" />);
    await screen.findByText(/Low /);
    expect(container.querySelector('[data-testid="forecast-band"]')).toBeNull();
    expect(screen.queryByText(/Could dip to/)).not.toBeInTheDocument();
  });
});

describe("Schedules page — suggestions awaiting a decision (#386)", () => {
  it("counts every open suggestion, not one capped inbox page", async () => {
    mockApi({
      "GET /accounts": ok([]),
      "GET /categories": ok([]),
      "GET /schedules": ok([]),
      "GET /suggestions/counts": ok({
        total: 31,
        match: 0,
        schedule: 30,
        transfer_link: 0,
        transfer_pair: 1,
      }),
    });

    renderWithProviders(<Schedules />);

    expect(await screen.findByText(/31 suggestions await a decision/)).toBeInTheDocument();
    const paths = apiMock.GET.mock.calls.map(([path]) => path);
    expect(paths).not.toContain("/suggestions");
  });
});

/* -------------------------------------------------------------------------
 * The page (#535): forecast first, grouped by account, payee + memo (#534)
 * ----------------------------------------------------------------------- */

const acct = (id: string, name: string, path: string, type = "checking") => ({
  id,
  name,
  path,
  type,
  is_virtual: false,
  closed_at: null,
});

const listed = (over: Partial<ScheduleDetail>): ScheduleDetail => ({ ...schedule([]), ...over });

const pageRoutes = (schedules: ScheduleDetail[]) => ({
  "GET /accounts": ok([
    acct("a-1", "Checking", "assets.checking"),
    acct("a-2", "Citi Card", "liabilities.citi", "credit_card"),
  ]),
  "GET /schedules": ok(schedules),
  "GET /suggestions/counts": ok({ total: 0, match: 0, schedule: 0, transfer_link: 0, transfer_pair: 0 }),
  "GET /forecast": ok(forecast(false)),
  "GET /categories": ok([{ path: "expenses.fitness", name: "Fitness" }]),
});

describe("Scheduled transactions page (#535)", () => {
  it("lists a transfer under both accounts, in each account's sign", async () => {
    mockApi(
      pageRoutes([
        listed({ amount_type: "fixed", amount: "-250.0000", estimate_basis: null, memo: "Autopay" }),
        listed({
          id: "s-2",
          name: "Acme Gym",
          payee: "Acme Gym",
          memo: null,
          counterparty_account_id: null,
          category_path: "expenses.fitness",
          amount_type: "fixed",
          amount: "-60.0000",
          estimate_basis: null,
        }),
      ]),
    );
    renderWithProviders(<Schedules />);

    expect(await screen.findByRole("heading", { name: "Scheduled transactions" })).toBeInTheDocument();
    // Scoped to the desktop table: the phone list renders the same rows.
    const checking = within(await screen.findByRole("region", { name: "Checking" })).getByRole("table");
    const card = within(screen.getByRole("region", { name: "Citi Card" })).getByRole("table");
    expect(within(checking).getByText("Transfer to Citi Card")).toBeInTheDocument();
    expect(within(checking).getByText("Acme Gym")).toBeInTheDocument();
    // The category by name, never its path (#541).
    expect(within(checking).getByText("Fitness")).toBeInTheDocument();
    expect(within(checking).queryByText("expenses.fitness")).not.toBeInTheDocument();
    expect(within(checking).getByText("-$250.00")).toBeInTheDocument();
    expect(within(card).getByText("Transfer from Checking")).toBeInTheDocument();
    expect(within(card).getByText("$250.00")).toBeInTheDocument();
    expect(within(card).queryByText("Acme Gym")).not.toBeInTheDocument();
    expect(within(card).getByText("Autopay")).toBeInTheDocument();
  });

  it("renders a stacked list for phones beside the table", async () => {
    mockApi(pageRoutes([listed({ amount_type: "fixed", amount: "-250.0000", estimate_basis: null, memo: "Autopay" })]));
    renderWithProviders(<Schedules />);
    const card = await screen.findByRole("region", { name: "Citi Card" });
    const item = within(card).getByRole("listitem");
    expect(within(item).getByText("Transfer from Checking")).toBeInTheDocument();
    expect(within(item).getByText("$250.00")).toBeInTheDocument();
    expect(within(item).getByText(/Autopay · monthly on the 5/)).toBeInTheDocument();
  });

  it("puts the forecast first and follows a group's Show forecast", async () => {
    mockApi(pageRoutes([listed({ amount_type: "fixed", amount: "-250.0000", estimate_basis: null })]));
    const { user } = renderWithProviders(<Schedules />);

    const picker = await screen.findByRole("combobox", { name: "Forecast account" });
    const card = await screen.findByRole("region", { name: "Citi Card" });
    // The forecast precedes the groups in document order.
    expect(picker.compareDocumentPosition(card) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(picker).toHaveValue("assets.checking");
    await user.click(within(card).getByRole("button", { name: "Show forecast" }));
    expect(picker).toHaveValue("liabilities.citi");
    expect(within(card).getByText("Shown in the forecast")).toBeInTheDocument();
  });

  it("creates with payee and memo, never a name", async () => {
    mockApi({ ...pageRoutes([]), "POST /schedules": ok({}, 201) });
    const { user } = renderWithProviders(<Schedules />);

    await user.click(await screen.findByRole("button", { name: "New scheduled transaction" }));
    const create = screen.getByRole("button", { name: "Create" });
    expect(create).toBeDisabled();
    expect(screen.getByText("Enter a payee (or a memo) to save.")).toBeInTheDocument();
    await user.type(screen.getByLabelText("Payee"), "Acme Gym");
    await user.type(screen.getByLabelText("Memo"), "Family plan");
    await user.type(screen.getByLabelText("Amount (negative = money out)"), "-60.00");
    await user.click(create);

    const post = apiMock.POST.mock.calls.find(([path]) => path === "/schedules");
    const body = (post?.[1] as { body: Record<string, unknown> }).body;
    expect(body).toMatchObject({ payee: "Acme Gym", memo: "Family plan", account: "assets.checking" });
    expect(body).not.toHaveProperty("name");
  });

  it("edits a schedule's existing payee and clears its memo", async () => {
    mockApi({
      ...pageRoutes([
        listed({
          id: "s-2",
          name: "Family plan",
          payee: "Acme Gym",
          memo: "Family plan",
          counterparty_account_id: null,
          category_path: "expenses.fitness",
          amount_type: "fixed",
          amount: "-60.0000",
          estimate_basis: null,
        }),
      ]),
      "PATCH /schedules/{schedule_id}": ok(listed({})),
    });
    const { user } = renderWithProviders(<Schedules />);

    const checking = within(await screen.findByRole("region", { name: "Checking" })).getByRole("table");
    await user.click(within(checking).getByRole("button", { name: "Edit" }));
    expect(screen.getByLabelText("Payee")).toHaveValue("Acme Gym");
    await user.clear(screen.getByLabelText("Memo"));
    await user.click(screen.getByRole("button", { name: "Save" }));

    const patch = apiMock.PATCH.mock.calls.find(([path]) => path === "/schedules/{schedule_id}");
    expect((patch?.[1] as { body: Record<string, unknown> }).body).toMatchObject({
      payee: "Acme Gym",
      memo: "",
    });
  });
});

describe("Scheduled transactions page — load failures (#540)", () => {
  it("shows a failed schedule load as an error, not as no schedules", async () => {
    mockApi({
      "GET /accounts": ok([]),
      "GET /categories": ok([]),
      "GET /schedules": fail(500, "database unavailable"),
      "GET /suggestions/counts": ok({ total: 0, match: 0, schedule: 0, transfer_link: 0, transfer_pair: 0 }),
    });
    renderWithProviders(<Schedules />);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not load scheduled transactions: database unavailable",
    );
    expect(screen.queryByText(/scheduled transactions yet/)).not.toBeInTheDocument();
  });
});

describe("negateAmount", () => {
  it("flips the sign of a decimal string without floats", () => {
    expect(negateAmount("-250.0000")).toBe("250.0000");
    expect(negateAmount("12.34")).toBe("-12.34");
    expect(negateAmount("0.00")).toBe("0.00");
    expect(negateAmount("-0.00")).toBe("0.00");
    expect(negateAmount(null)).toBeNull();
  });
});

describe("cadenceLabel (#541)", () => {
  it("says a cadence in words and never shows a raw rule", () => {
    expect(cadenceLabel("FREQ=YEARLY;BYMONTH=3;BYMONTHDAY=15")).toBe("yearly in March on the 15");
    expect(cadenceLabel("FREQ=MONTHLY;BYMONTHDAY=6,20")).toBe("semi-monthly on the 6 & 20");
    expect(cadenceLabel("FREQ=HOURLY;INTERVAL=6")).toBe("custom schedule");
  });
});
