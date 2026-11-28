import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { mockApi, ok } from "../test/api";
import { renderWithProviders } from "../test/render";
import Holdings from "./Holdings";

const line = (over: Record<string, unknown>) => ({
  symbol: "ACME",
  name: null,
  source: "feed",
  position_id: null,
  quantity: "10.000000",
  price: "50.000000",
  price_as_of: null,
  price_source: null,
  market_value: "500.0000",
  cost_basis: "400.0000",
  unrealized_gain: "100.0000",
  gain_pct: "25.00",
  weight_pct: "50.00",
  in_transit: null,
  stale_price: false,
  no_price: false,
  unpriceable: false,
  value_out_of_range: false,
  manual_price: false,
  ...over,
});

const PORTFOLIO = {
  as_of: "2026-10-14",
  totals: [{ currency: "USD", market_value: "740.0000", cash: "260.0000", total: "1000.0000" }],
  accounts: [
    {
      account_id: "a-1",
      name: "Synthetic Brokerage",
      path: "assets.synthetic",
      currency: "USD",
      as_of: "2026-10-13",
      source: "feed",
      cash: "260.0000",
      market_value: "740.0000",
      cost_basis: "400.0000",
      unrealized_gain: "100.0000",
      gain_pct: "25.00",
      total: "1000.0000",
      lines: [
        line({}),
        line({
          symbol: "PLANX",
          quantity: "4.000000",
          price: "60.000000",
          price_as_of: "2026-09-30",
          price_source: "manual",
          market_value: "240.0000",
          cost_basis: null,
          unrealized_gain: null,
          gain_pct: null,
          weight_pct: "24.00",
          stale_price: true,
          manual_price: true,
        }),
        line({
          symbol: "TRUSTX",
          price: null,
          market_value: null,
          cost_basis: null,
          unrealized_gain: null,
          gain_pct: null,
          weight_pct: null,
          no_price: true,
        }),
      ],
    },
  ],
  lines: 3,
  stale_price: 1,
  no_price: 1,
  unpriceable: 0,
};

describe("Holdings (#684)", () => {
  it("shows every account's lines, totals and the price warnings, tagging only manual prices", async () => {
    mockApi({ "GET /holdings/portfolio": ok(PORTFOLIO) });
    renderWithProviders(<Holdings />);

    const account = await screen.findByRole("region", { name: "Synthetic Brokerage" });
    const rows = within(account).getAllByRole("row");
    const acme = rows.find((r) => r.textContent?.includes("ACME"))!;
    expect(acme).toHaveTextContent("$500.00");
    expect(acme).not.toHaveTextContent("manual price");
    expect(within(account).getByText(/From SimpleFIN/)).toBeInTheDocument();
    const plan = rows.find((r) => r.textContent?.includes("PLANX"))!;
    expect(within(plan).getByText("manual price")).toBeInTheDocument();
    expect(within(plan).getByText("stale")).toBeInTheDocument();
    const trust = rows.find((r) => r.textContent?.includes("TRUSTX"))!;
    expect(within(trust).getByText("no price")).toBeInTheDocument();

    expect(screen.getByText("$1,000.00", { selector: "dd" })).toBeInTheDocument();
    expect(screen.getByRole("note")).toHaveTextContent(
      "1 holding(s) with a stale price. 1 holding(s) with no price.",
    );
    expect(screen.getByRole("link", { name: "Settings, Market data" })).toHaveAttribute(
      "href",
      "/settings/market-data",
    );
    expect(screen.getByRole("link", { name: "Synthetic Brokerage" })).toHaveAttribute(
      "href",
      "/accounts/a-1",
    );
  });

  it("says when there is nothing to show", async () => {
    mockApi({
      "GET /holdings/portfolio": ok({ ...PORTFOLIO, totals: [], accounts: [], lines: 0, stale_price: 0, no_price: 0 }),
    });
    renderWithProviders(<Holdings />);
    expect(await screen.findByText("No brokerage accounts yet.")).toBeInTheDocument();
  });
});
