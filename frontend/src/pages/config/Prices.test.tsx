import { screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { apiMock, mockApi, ok } from "../../test/api";
import { renderWithProviders } from "../../test/render";
import Prices from "./Prices";

const CONFIG = {
  values: { price_provider: "none", price_stale_days: 7 },
  overridden: [],
  ai_api_key_set: false,
  oidc_client_secret_set: false,
  price_api_key_set: false,
  sync_reschedule_live: true,
  encryption: {},
};

const STATUS = {
  provider: "none",
  stale_days: 7,
  last_fetched_at: null,
  symbols: [
    { symbol: "ACME", close: "12.500000", price_as_of: "2026-10-01", source: "manual", stale: true },
    { symbol: "TEST", close: null, price_as_of: null, source: null, stale: true },
  ],
  last_failure: { at: "2026-10-14T06:40:00Z", symbol: "ACME", status: 404, reason: "not found" },
};

describe("Config, Prices (#390)", () => {
  it("saves the provider, a write-only key and the staleness threshold", async () => {
    mockApi({
      "GET /config": ok(CONFIG),
      "GET /prices/status": ok(STATUS),
      "PUT /config": ok(CONFIG),
    });
    const { user } = renderWithProviders(<Prices />);
    await user.selectOptions(await screen.findByLabelText("Provider"), "tiingo");
    await user.type(screen.getByLabelText(/API key/), "synthetic-key");
    const stale = screen.getByLabelText("Stale after (days)");
    await user.clear(stale);
    await user.type(stale, "10");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(apiMock.PUT).toHaveBeenCalledWith("/config", {
        body: { price_provider: "tiingo", price_stale_days: 10, price_api_key: "synthetic-key" },
      }),
    );
  });

  it("shows each symbol's close, the last failure, and runs fetch now", async () => {
    mockApi({
      "GET /config": ok(CONFIG),
      "GET /prices/status": ok(STATUS),
      "POST /prices/fetch": ok({
        provider: "none",
        skipped: "no price provider is configured",
        symbols: 2,
        fetched: 0,
        failed: [{ symbol: "TEST", status: 200, reason: "1 close(s) out of range dropped" }],
        accounts_projected: 1,
        accounts_failed: 0,
        stale: [],
      }),
    });
    const { user } = renderWithProviders(<Prices />);
    expect(await screen.findByText("ACME")).toBeInTheDocument();
    expect(screen.getByText("stale")).toBeInTheDocument();
    expect(screen.getByText("no price")).toBeInTheDocument();
    // The reason travels with the status: what went wrong, not only where.
    expect(screen.getByText(/Last failure: ACME \(HTTP 404, not found\)/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Fetch now" }));
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Nothing fetched: no price provider is configured.",
    );
    expect(screen.getByRole("status")).toHaveTextContent(
      "1 symbol(s) failed: TEST (HTTP 200, 1 close(s) out of range dropped).",
    );
    expect(apiMock.POST).toHaveBeenCalledWith("/prices/fetch");
  });

  it.each(["0", "7.5", "10abc", "366", "1e2", "0x10", "+7"])(
    "refuses a staleness threshold of %s without calling the API",
    async (typed) => {
      // A whole number of days only: "7.5" and "10abc" are refused, not
      // quietly read as 7 and 10.
      mockApi({ "GET /config": ok(CONFIG), "GET /prices/status": ok(STATUS) });
      const { user } = renderWithProviders(<Prices />);
      const stale = await screen.findByLabelText("Stale after (days)");
      await user.clear(stale);
      await user.type(stale, typed);
      await user.click(screen.getByRole("button", { name: "Save" }));
      expect(await screen.findByText(/1 to 365/)).toBeInTheDocument();
      expect(apiMock.PUT).not.toHaveBeenCalled();
    },
  );
});
