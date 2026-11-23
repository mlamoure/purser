import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { mockApi, ok, fail } from "../test/api";
import { holdingLine, holdingsSummaryOut } from "../test/fixtures";
import { renderWithProviders } from "../test/render";
import { HoldingsCard, pctText, sortLines } from "./HoldingsCard";

const symbols = () =>
  within(screen.getAllByRole("rowgroup")[1])
    .getAllByRole("row")
    .map((r) => within(r).getAllByRole("cell")[0].textContent?.slice(0, 4));

describe("HoldingsCard (#595)", () => {
  it("shows the totals, the lines largest first, and cash plus total rows", async () => {
    mockApi({ "GET /accounts/{account_id}/holdings/summary": ok(holdingsSummaryOut()) });
    renderWithProviders(<HoldingsCard accountId="a-1" currency="USD" />);

    const card = await screen.findByRole("region", { name: "Holdings" });
    expect(await within(card).findByText(/As of Nov 20, 2026 · from the bank feed/)).toBeInTheDocument();
    expect(within(card).getByText("$1,900.00")).toBeInTheDocument(); // securities
    expect(within(card).getAllByText("+$400.00").length).toBeGreaterThan(0); // gain
    expect(symbols()).toEqual(["AAAA", "BBBB"]);
    const aaaa = within(card).getByText("Example Growth Fund").closest("tr")!;
    expect(within(aaaa).getByText("$1,500.00")).toBeInTheDocument();
    expect(within(aaaa).getByText("+50%")).toBeInTheDocument();
    expect(within(aaaa).getByText("62.5%")).toBeInTheDocument();
    const bbbb = within(card).getByText("BBBB").closest("tr")!;
    expect(within(bbbb).getByText("-$100.00")).toHaveClass("text-destructive");
    const [cash, total] = within(screen.getAllByRole("rowgroup")[2]).getAllByRole("row");
    expect(within(cash).getByText("$500.00")).toBeInTheDocument();
    expect(within(cash).getByText("20.83%")).toBeInTheDocument();
    expect(within(total).getByText("$2,400.00")).toBeInTheDocument();
  });

  it("sorts by a column and flips on a second click", async () => {
    mockApi({ "GET /accounts/{account_id}/holdings/summary": ok(holdingsSummaryOut()) });
    renderWithProviders(<HoldingsCard accountId="a-1" currency="USD" />);
    await screen.findByText("Example Growth Fund");

    const gain = screen.getByRole("button", { name: "Gain" });
    await userEvent.click(gain);
    expect(gain.closest("th")).toHaveAttribute("aria-sort", "descending");
    expect(symbols()).toEqual(["AAAA", "BBBB"]);
    await userEvent.click(gain);
    expect(gain.closest("th")).toHaveAttribute("aria-sort", "ascending");
    expect(symbols()).toEqual(["BBBB", "AAAA"]);
    await userEvent.click(screen.getByRole("button", { name: "Holding" }));
    expect(symbols()).toEqual(["AAAA", "BBBB"]);
  });

  it("flags lines the server marks and offers Edit positions when given", async () => {
    mockApi({
      "GET /accounts/{account_id}/holdings/summary": ok(
        holdingsSummaryOut({
          source: "manual",
          lines: [
            holdingLine({ symbol: "OLDP", stale_price: true }),
            holdingLine({ symbol: "NOPX", market_value: null, price: null, no_price: true }),
            holdingLine({ symbol: "Plan fund", unpriceable: true, market_value: null }),
            holdingLine({ symbol: "BIGQ", value_out_of_range: true, market_value: null }),
            holdingLine({ symbol: "SOLD", quantity: "0", in_transit: "250.0000", market_value: "250.0000" }),
          ],
        }),
      ),
    });
    const onEdit = vi.fn();
    renderWithProviders(<HoldingsCard accountId="a-1" currency="USD" onEditPositions={onEdit} />);

    expect(await screen.findByText("stale price")).toBeInTheDocument();
    expect(screen.getByText(/tracked by hand/)).toBeInTheDocument();
    expect(screen.getByText("no price")).toBeInTheDocument();
    expect(screen.getByText("no ticker")).toBeInTheDocument();
    expect(screen.getByText("value too large: check shares")).toBeInTheDocument();
    expect(screen.getByText("$250.00 in transit")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Edit positions" }));
    expect(onEdit).toHaveBeenCalled();
  });

  it("says when there is nothing yet, and reports a failed load", async () => {
    mockApi({
      "GET /accounts/{account_id}/holdings/summary": ok(holdingsSummaryOut({ lines: [], as_of: null })),
    });
    const { unmount } = renderWithProviders(<HoldingsCard accountId="a-1" currency="USD" />);
    expect(await screen.findByText("No holdings reported for this account yet.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Edit positions" })).not.toBeInTheDocument();
    unmount();

    mockApi({ "GET /accounts/{account_id}/holdings/summary": fail(500, "database unavailable") });
    renderWithProviders(<HoldingsCard accountId="a-1" currency="USD" />);
    expect(await screen.findByText("Could not load holdings: database unavailable")).toBeInTheDocument();
  });
});

describe("HoldingsCard helpers", () => {
  it("pctText trims and signs", () => {
    expect(pctText("12.50")).toBe("12.5%");
    expect(pctText("26.67", { sign: true })).toBe("+26.67%");
    expect(pctText("-3.00", { sign: true })).toBe("-3%");
    expect(pctText(null)).toBe("—");
  });

  it("sortLines puts a missing value last both ways", () => {
    const lines = [
      holdingLine({ symbol: "A", market_value: "5" }),
      holdingLine({ symbol: "B", market_value: null }),
      holdingLine({ symbol: "C", market_value: "10" }),
    ];
    expect(sortLines(lines, "market_value", true).map((l) => l.symbol)).toEqual(["C", "A", "B"]);
    expect(sortLines(lines, "market_value", false).map((l) => l.symbol)).toEqual(["A", "C", "B"]);
  });
});
