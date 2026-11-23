import { screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { components } from "../api/schema";
import { apiMock, fail, mockApi, ok } from "../test/api";
import { renderWithProviders } from "../test/render";
import PositionsPanel, { fmtQuantity } from "./Positions";

type PositionOut = components["schemas"]["PositionOut"];

// The server's ledger day (#471): a household zone's date, not UTC's or the
// browser's, so the forms must send this one.
const LEDGER_DAY = "2026-11-09";
const CLOCK = { "GET /clock": ok({ ledger_today: LEDGER_DAY, timezone: "America/New_York" }) };

const position = (over: Partial<PositionOut> = {}): PositionOut => ({
  id: "pos-1",
  account_id: "acct-1",
  symbol: "ACME",
  name: null,
  opened_on: "2026-09-01",
  closed_at: null,
  quantity: "412.500000",
  cost_basis: "1000.0000",
  price: "12.340000",
  price_as_of: "2026-10-14",
  price_source: "tiingo",
  market_value: "5090.2500",
  value_out_of_range: false,
  unrealized_gain: "4090.2500",
  stale_price: false,
  events: 1,
  tracks_lots: false,
  open_lots: 0,
  ...over,
});

describe("PositionsPanel (#390)", () => {
  it("lists positions, flags an unpriced one, and never shows it as zero", async () => {
    mockApi({
      ...CLOCK,
      "GET /accounts/{account_id}/positions": ok([
        position(),
        position({
          id: "pos-2",
          symbol: "TEST",
          quantity: "3.000000",
          price: null,
          price_as_of: null,
          price_source: null,
          market_value: null,
          unrealized_gain: null,
        }),
      ]),
    });
    renderWithProviders(<PositionsPanel accountId="acct-1" />);
    expect(await screen.findByText("ACME")).toBeInTheDocument();
    expect(screen.getByText("412.5")).toBeInTheDocument();
    expect(screen.getByText("no price")).toBeInTheDocument();
    expect(screen.queryByText("$0.00")).not.toBeInTheDocument();
  });

  it("says to check the share count when the value does not fit, not that the price is missing", async () => {
    mockApi({
      ...CLOCK,
      "GET /accounts/{account_id}/positions": ok([
        position({
          quantity: "1000000000000.000000",
          price: "25000.000000",
          market_value: null,
          value_out_of_range: true,
          unrealized_gain: null,
        }),
      ]),
    });
    renderWithProviders(<PositionsPanel accountId="acct-1" />);
    expect(await screen.findByText("check the share count")).toBeInTheDocument();
    expect(screen.queryByText("no price")).not.toBeInTheDocument();
  });

  it("sends no cost basis unless one is typed; the current one is a hint", async () => {
    // Prefilled, the basis would ride along with a sell-out (refused: 0 shares
    // have no basis) or a backdated count (today's basis on an older date).
    mockApi({
      ...CLOCK,
      "GET /accounts/{account_id}/positions": ok([position()]),
      "POST /positions/{position_id}/events": ok({}),
    });
    const { user } = renderWithProviders(<PositionsPanel accountId="acct-1" />);
    await user.click(await screen.findByRole("button", { name: "Set quantity" }));
    const basis = screen.getByLabelText("Cost basis of ACME (total, optional)");
    expect(basis).toHaveValue("");
    expect(basis).toHaveAttribute("placeholder", "Basis (now 1000)");
    const input = screen.getByLabelText("Quantity of ACME");
    await user.clear(input);
    await user.type(input, "0");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(apiMock.POST).toHaveBeenCalledWith("/positions/{position_id}/events", {
        params: { path: { position_id: "pos-1" } },
        body: { kind: "set_quantity", occurred_on: LEDGER_DAY, quantity: "0" },
      }),
    );
  });

  it("sends the cost basis the user types", async () => {
    mockApi({
      ...CLOCK,
      "GET /accounts/{account_id}/positions": ok([position()]),
      "POST /positions/{position_id}/events": ok({}),
    });
    const { user } = renderWithProviders(<PositionsPanel accountId="acct-1" />);
    await user.click(await screen.findByRole("button", { name: "Set quantity" }));
    const input = screen.getByLabelText("Quantity of ACME");
    await user.clear(input);
    await user.type(input, "420");
    await user.type(screen.getByLabelText("Cost basis of ACME (total, optional)"), "1100.5");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(apiMock.POST).toHaveBeenCalledWith("/positions/{position_id}/events", {
        params: { path: { position_id: "pos-1" } },
        body: {
          kind: "set_quantity",
          occurred_on: LEDGER_DAY,
          quantity: "420",
          cost_basis: "1100.5",
        },
      }),
    );
  });

  it("offers Close on a position that holds nothing, and only there", async () => {
    mockApi({
      ...CLOCK,
      "GET /accounts/{account_id}/positions": ok([
        position(),
        position({ id: "pos-0", symbol: "SOLD", quantity: "0.000000", market_value: "0.0000" }),
      ]),
      "PATCH /positions/{position_id}": ok({}),
    });
    const { user } = renderWithProviders(<PositionsPanel accountId="acct-1" />);
    const close = await screen.findByRole("button", { name: "Close SOLD" });
    expect(screen.queryByRole("button", { name: "Close ACME" })).not.toBeInTheDocument();
    await user.click(close);
    await waitFor(() =>
      expect(apiMock.PATCH).toHaveBeenCalledWith("/positions/{position_id}", {
        params: { path: { position_id: "pos-0" } },
        body: { closed: true },
      }),
    );
  });

  it("records a split: new for old shares, and cash in lieu with its lots", async () => {
    mockApi({
      ...CLOCK,
      "GET /accounts/{account_id}/positions": ok([position({ tracks_lots: true, open_lots: 2 })]),
      "POST /positions/{position_id}/events": ok({}),
    });
    const { user } = renderWithProviders(<PositionsPanel accountId="acct-1" />);
    await user.click(await screen.findByRole("button", { name: "Record split of ACME" }));
    const record = screen.getByRole("button", { name: "Record split" });
    expect(record).toBeDisabled(); // a ratio of whole numbers first
    await user.type(screen.getByLabelText("New shares for ACME"), "1");
    const old = screen.getByLabelText("Old shares for ACME");
    await user.clear(old);
    await user.type(old, "10");
    expect(screen.queryByLabelText("Lots the fractional share comes from")).not.toBeInTheDocument();
    await user.type(screen.getByLabelText("Cash in lieu for ACME (optional)"), "4.25");
    await user.selectOptions(screen.getByLabelText("Lots the fractional share comes from"), "hifo");
    await user.click(record);
    await waitFor(() =>
      expect(apiMock.POST).toHaveBeenCalledWith("/positions/{position_id}/events", {
        params: { path: { position_id: "pos-1" } },
        body: {
          kind: "split",
          occurred_on: LEDGER_DAY,
          split_numerator: 1,
          split_denominator: 10,
          cash_in_lieu: "4.25",
          lot_method: "hifo",
        },
      }),
    );
  });

  it("offers no split on a position that holds nothing", async () => {
    mockApi({
      ...CLOCK,
      "GET /accounts/{account_id}/positions": ok([
        position({ id: "pos-0", symbol: "SOLD", quantity: "0.000000", market_value: "0.0000" }),
      ]),
    });
    renderWithProviders(<PositionsPanel accountId="acct-1" />);
    expect(await screen.findByRole("button", { name: "Close SOLD" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Record split of SOLD" })).not.toBeInTheDocument();
  });

  it("adds a position and shows the server's refusal", async () => {
    mockApi({
      ...CLOCK,
      "GET /accounts/{account_id}/positions": ok([]),
      "POST /accounts/{account_id}/positions": fail(
        409,
        "assets.broker already has an open ACME position; record an event on it",
      ),
    });
    const { user } = renderWithProviders(<PositionsPanel accountId="acct-1" />);
    expect(await screen.findByText(/No positions yet/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "+ position" }));
    await user.type(screen.getByLabelText("Symbol"), "acme");
    await user.type(screen.getByLabelText("Quantity"), "10");
    await user.type(screen.getByLabelText("Cost basis (total, optional)"), "250.00");
    await user.click(screen.getByRole("button", { name: "Add" }));
    expect(await screen.findByText(/already has an open ACME position/)).toBeInTheDocument();
    expect(apiMock.POST).toHaveBeenCalledWith("/accounts/{account_id}/positions", {
      params: { path: { account_id: "acct-1" } },
      body: { symbol: "acme", quantity: "10", as_of: LEDGER_DAY, cost_basis: "250.00" },
    });
  });
});

describe("fmtQuantity", () => {
  it("drops trailing zeros without going through a float", () => {
    expect(fmtQuantity("412.500000")).toBe("412.5");
    expect(fmtQuantity("100.000000")).toBe("100");
    expect(fmtQuantity("1234567890123.123456")).toBe("1234567890123.123456");
    expect(fmtQuantity(null)).toBe("—");
  });
});
