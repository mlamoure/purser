import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { components } from "../api/schema";
import { overrideBody } from "../lib/utils";
import { apiMock, mockApi, ok } from "../test/api";
import { renderWithProviders } from "../test/render";
import { ForecastChart, InstancesPanel } from "./Schedules";

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
  const row = (await screen.findAllByText("2026-11-05"))[0].closest("tr")!;
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
    const pinnedRow = (await screen.findByText("2026-11-03")).closest("tr")!;
    const freeRow = screen.getAllByText("2026-12-05")[0].closest("tr")!;
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
    const row = (await screen.findAllByText("2026-11-05"))[0].closest("tr")!;
    const stated = screen.getAllByText("2026-12-05")[0].closest("tr")!;
    expect(within(row).getByText("current balance")).toBeInTheDocument();
    expect(within(stated).getByText("statement · closed 2026-11-22")).toBeInTheDocument();
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
    const row = (await screen.findAllByText("2026-11-05"))[0].closest("tr")!;
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

  it("draws no band without one", async () => {
    mockApi({ "GET /forecast": ok(forecast(false)) });
    const { container } = renderWithProviders(<ForecastChart accountPath="assets.checking" />);
    await screen.findByText(/Low /);
    expect(container.querySelector('[data-testid="forecast-band"]')).toBeNull();
    expect(screen.queryByText(/Could dip to/)).not.toBeInTheDocument();
  });
});
