import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { apiMock, mockApi, ok } from "../../test/api";
import { renderWithProviders } from "../../test/render";
import { meQuery } from "../../lib/me";
import MarketData from "./MarketData";

/** The provider and Fetch now are admin-only (#542). */
function renderMarketData(scopes = ["read", "write", "admin"]) {
  const view = renderWithProviders(<MarketData />);
  view.queryClient.setQueryData(meQuery.queryKey, { scopes } as never);
  return view;
}

const CONFIG = {
  values: { price_provider: "none", price_fallback_provider: "none", price_stale_days: 7 },
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
  last_run_at: "2026-10-14T06:40:00Z",
  next_run_at: "2026-10-15T06:40:00Z",
  symbols: [
    { symbol: "ACME", close: "12.500000", price_as_of: "2026-10-01", source: "manual", stale: true },
    { symbol: "TEST", close: null, price_as_of: null, source: null, stale: true },
  ],
  last_failure: { at: "2026-10-14T06:40:00Z", symbol: "ACME", status: 404, reason: "not found" },
};

describe("Settings, Market data (#390, #678)", () => {
  it("saves the provider, a write-only key and the staleness threshold", async () => {
    mockApi({
      "GET /config": ok(CONFIG),
      "GET /prices/status": ok(STATUS),
      "GET /prices/symbols": ok([]),
      "POST /prices/test": ok({ ok: true, provider: "tiingo", status: null, reason: null }),
      "PUT /config": ok(CONFIG),
    });
    const { user } = renderMarketData();
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
    // The new key was tested first (#682).
    expect(apiMock.POST).toHaveBeenCalledWith("/prices/test", {
      body: { provider: "tiingo", api_key: "synthetic-key" },
    });
  });

  it("never saves a key the provider refuses, and says why (#682)", async () => {
    mockApi({
      "GET /config": ok(CONFIG),
      "GET /prices/status": ok(STATUS),
      "GET /prices/symbols": ok([]),
      "POST /prices/test": ok({ ok: false, provider: "tiingo", status: 401, reason: "refused" }),
      "PUT /config": ok(CONFIG),
    });
    const { user } = renderMarketData();
    await user.selectOptions(await screen.findByLabelText("Provider"), "tiingo");
    await user.type(screen.getByLabelText(/API key/), "synthetic-bad-key");
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(
      await screen.findByText("The key was not saved: the test failed, refused (HTTP 401)."),
    ).toBeInTheDocument();
    expect(apiMock.PUT).not.toHaveBeenCalled();
  });

  it("tests the saved key with Test connection (#682)", async () => {
    const saved = {
      ...CONFIG,
      values: { price_provider: "tiingo", price_fallback_provider: "none", price_stale_days: 7 },
      price_api_key_set: true,
    };
    mockApi({
      "GET /config": ok(saved),
      "GET /prices/status": ok(STATUS),
      "GET /prices/symbols": ok([]),
      "POST /prices/test": ok({ ok: true, provider: "tiingo", status: null, reason: null }),
    });
    const { user } = renderMarketData();
    await user.click(await screen.findByRole("button", { name: "Test connection" }));
    expect(
      await screen.findByText("Connection OK: tiingo answered with the saved key."),
    ).toBeInTheDocument();
    expect(apiMock.POST).toHaveBeenCalledWith("/prices/test", { body: { provider: "tiingo" } });
  });

  it("sums up the symbols it prices, links to Holdings, and runs fetch now (#684)", async () => {
    mockApi({
      "GET /config": ok(CONFIG),
      "GET /prices/status": ok(STATUS),
      "GET /prices/symbols": ok([]),
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
    const { user } = renderMarketData();
    expect(await screen.findByText(/Purser prices 2 symbols/)).toHaveTextContent(
      "Purser prices 2 symbols. Stale: ACME. No price: TEST. Each holding and where its price comes from is on the Holdings page.",
    );
    expect(screen.getByRole("link", { name: "Holdings" })).toHaveAttribute("href", "/holdings");
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
      mockApi({ "GET /config": ok(CONFIG), "GET /prices/status": ok(STATUS), "GET /prices/symbols": ok([]) });
      const { user } = renderMarketData();
      const stale = await screen.findByLabelText("Stale after (days)");
      await user.clear(stale);
      await user.type(stale, typed);
      await user.click(screen.getByRole("button", { name: "Save" }));
      expect(await screen.findByText(/1 to 365/)).toBeInTheDocument();
      expect(apiMock.PUT).not.toHaveBeenCalled();
    },
  );

  it("shows a member the price status only, without reading GET /config (#542)", async () => {
    mockApi({ "GET /prices/status": ok(STATUS), "GET /prices/symbols": ok([]) });
    renderMarketData(["read", "write"]);
    expect(await screen.findByText(/Purser prices 2 symbols/)).toBeInTheDocument();
    expect(screen.queryByText("Price provider")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Fetch now" })).not.toBeInTheDocument();
    expect(apiMock.GET).not.toHaveBeenCalledWith("/config");
  });

  it("says, to every session, what Market data prices (#678, #689)", async () => {
    mockApi({ "GET /prices/status": ok(STATUS), "GET /prices/symbols": ok([]) });
    renderMarketData(["read", "write"]);
    expect(await screen.findByText(/Purser prices 2 symbols/)).toBeInTheDocument();
    expect(screen.getByText(/isn.t connected through SimpleFIN/)).toHaveTextContent(
      "Market data prices the holdings of every account that isn't connected through SimpleFIN. SimpleFIN accounts are valued by your bank.",
    );
    expect(screen.queryByText(/by hand/)).not.toBeInTheDocument();
    expect(screen.getByText(/Last run:/)).toHaveTextContent("Next run:");
  });

  it("saves a fallback provider and tests its new key first (#686)", async () => {
    mockApi({
      "GET /config": ok(CONFIG),
      "GET /prices/status": ok(STATUS),
      "GET /prices/symbols": ok([]),
      "POST /prices/test": ok({ ok: true, provider: "eodhd", status: null, reason: null }),
      "PUT /config": ok(CONFIG),
    });
    const { user } = renderMarketData();
    await user.selectOptions(await screen.findByLabelText("Provider"), "tiingo");
    await user.type(screen.getByLabelText(/^API key/), "synthetic-key");
    await user.selectOptions(screen.getByLabelText("Fallback provider"), "eodhd");
    expect(screen.getByText(/Exchanges worldwide/)).toBeInTheDocument();
    await user.type(screen.getByLabelText(/Fallback API key/), "synthetic-eod-key");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(apiMock.PUT).toHaveBeenCalledWith("/config", {
        body: {
          price_provider: "tiingo",
          price_stale_days: 7,
          price_fallback_provider: "eodhd",
          price_api_key: "synthetic-key",
          price_fallback_api_key: "synthetic-eod-key",
        },
      }),
    );
    expect(apiMock.POST).toHaveBeenCalledWith("/prices/test", {
      body: { provider: "eodhd", api_key: "synthetic-eod-key" },
    });
  });

  it("lists, saves and removes symbol settings (#686)", async () => {
    mockApi({
      "GET /config": ok(CONFIG),
      "GET /prices/status": ok(STATUS),
      "GET /prices/symbols": ok([
        { symbol: "ACME", skip_providers: true, aliases: {} },
        { symbol: "VOD", skip_providers: false, aliases: { eodhd: "VOD.LSE" } },
      ]),
      "PUT /prices/symbols/{symbol}": ok({ symbol: "TEST", skip_providers: false, aliases: { eodhd: "TEST.TO" } }),
      "DELETE /prices/symbols/{symbol}": ok({ symbol: "ACME", skip_providers: false, aliases: {} }),
    });
    const { user } = renderMarketData(["read", "write"]);
    expect(await screen.findByText("VOD.LSE")).toBeInTheDocument();
    const acme = screen.getByRole("row", { name: /ACME/ });
    expect(within(acme).getByText("no")).toBeInTheDocument(); // not fetched
    await user.type(screen.getByLabelText("Symbol"), "TEST");
    await user.type(screen.getByLabelText("EODHD ticker"), "TEST.TO");
    await user.click(screen.getByRole("button", { name: "Save symbol" }));
    await waitFor(() =>
      expect(apiMock.PUT).toHaveBeenCalledWith("/prices/symbols/{symbol}", {
        params: { path: { symbol: "TEST" } },
        body: { skip_providers: false, aliases: { tiingo: "", eodhd: "TEST.TO" } },
      }),
    );
    await user.click(screen.getByRole("button", { name: "Remove ACME" }));
    await waitFor(() =>
      expect(apiMock.DELETE).toHaveBeenCalledWith("/prices/symbols/{symbol}", {
        params: { path: { symbol: "ACME" } },
      }),
    );
  });
});
