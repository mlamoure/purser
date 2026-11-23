import { fireEvent, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { apiMock, mockApi, ok, fail } from "../test/api";
import { accountOut, balanceHistoryOut } from "../test/fixtures";
import { renderWithProviders } from "../test/render";
import { BalanceHistoryChart, trimZeros } from "./BalanceHistoryChart";

const historyCalls = () =>
  apiMock.GET.mock.calls
    .filter(([path]) => path === "/accounts/{account_id}/balance/history")
    .map(([, init]) => (init as { params: { query: { range: string } } }).params.query.range);

describe("BalanceHistoryChart (#594)", () => {
  it("shows the change over the default year, in green when it rose", async () => {
    mockApi({ "GET /accounts/{account_id}/balance/history": ok(balanceHistoryOut()) });
    renderWithProviders(<BalanceHistoryChart account={accountOut()} />);

    const pill = await screen.findByText("+$100.00 (+100%)");
    expect(pill.className).toContain("text-success");
    expect(screen.getByText("past year")).toBeInTheDocument();
    expect(historyCalls()).toEqual(["1y"]);
    expect(screen.getByRole("img").getAttribute("aria-label")).toMatch(
      /^Balance past year: \$100\.00 on Nov 1, 2026 to \$200\.00 on Nov 3, 2026, low \$100\.00 on Nov 1, 2026, high \$250\.00 on Nov 2, 2026/,
    );
    expect(screen.getByTestId("chart-line")).toHaveClass("chart-draw");
  });

  it("asks for the range a chip names", async () => {
    mockApi({
      "GET /accounts/{account_id}/balance/history": (init) =>
        ok(balanceHistoryOut({ range: String(init.params?.query?.range) })),
    });
    renderWithProviders(<BalanceHistoryChart account={accountOut()} />);
    await screen.findByText("past year");

    await userEvent.click(screen.getByRole("button", { name: "3M" }));
    expect(await screen.findByText("past 3 months")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "3M" })).toHaveAttribute("aria-pressed", "true");
    expect(historyCalls()).toEqual(["1y", "3m"]);
  });

  it("reads each day from the keyboard and opens it on Enter", async () => {
    mockApi({ "GET /accounts/{account_id}/balance/history": ok(balanceHistoryOut()) });
    const onPickDate = vi.fn();
    renderWithProviders(<BalanceHistoryChart account={accountOut()} onPickDate={onPickDate} />);

    const chart = await screen.findByRole("img");
    chart.focus();
    fireEvent.keyDown(chart, { key: "ArrowLeft" });
    const region = screen.getByRole("region", { name: "Balance history" });
    expect(within(region).getByText("$250.00")).toBeInTheDocument();
    expect(within(region).getByText("Nov 2, 2026")).toBeInTheDocument();
    expect(screen.getByTestId("chart-crosshair")).toBeInTheDocument();
    fireEvent.keyDown(chart, { key: "Enter" });
    expect(onPickDate).toHaveBeenCalledWith("2026-11-02");
    fireEvent.keyDown(chart, { key: "Escape" });
    expect(screen.queryByTestId("chart-crosshair")).not.toBeInTheDocument();
  });

  it("charts a loan as the amount owed: paid down is good news", async () => {
    mockApi({
      "GET /accounts/{account_id}/balance/history": ok(
        balanceHistoryOut({
          method: "valuation",
          points: [
            { date: "2026-11-01", balance: "-300000.0000" },
            { date: "2026-11-03", balance: "-299000.0000" },
          ],
          current: "-299000.0000",
          change: "1000.0000",
          change_pct: "0.33",
          low: { date: "2026-11-01", balance: "-300000.0000" },
          high: { date: "2026-11-03", balance: "-299000.0000" },
        }),
      ),
    });
    renderWithProviders(
      <BalanceHistoryChart account={accountOut({ type: "loan", path: "liabilities.mortgage", min_balance: "5.00" })} />,
    );

    const pill = await screen.findByText("-$1,000.00 (-0.33%)");
    expect(pill.className).toContain("text-success");
    expect(screen.getByText("Amount owed")).toBeInTheDocument();
    expect(screen.getByText(/From valuations/)).toBeInTheDocument();
    expect(screen.getByRole("img").getAttribute("aria-label")).toContain("$300,000.00 on Nov 1, 2026 to $299,000.00");
    // A floor is a cash-account idea; a liability never draws one.
    expect(screen.queryByTestId("chart-floor")).not.toBeInTheDocument();
  });

  it("draws the account's floor and names brokerage's cash-and-holdings basis", async () => {
    mockApi({
      "GET /accounts/{account_id}/balance/history": ok(balanceHistoryOut({ method: "postings+holdings" })),
    });
    renderWithProviders(<BalanceHistoryChart account={accountOut({ type: "brokerage", min_balance: "150.00" })} />);

    expect(await screen.findByTestId("chart-floor")).toHaveTextContent("Floor $150");
    expect(screen.getByText(/Cash and holdings/)).toBeInTheDocument();
  });

  it("says so when there is no history, and reports a failed load", async () => {
    mockApi({
      "GET /accounts/{account_id}/balance/history": ok(
        balanceHistoryOut({ points: [{ date: "2026-11-03", balance: "0" }], first_activity: null, change_pct: null }),
      ),
    });
    const { unmount } = renderWithProviders(<BalanceHistoryChart account={accountOut()} />);
    expect(await screen.findByText("No balance history yet.")).toBeInTheDocument();
    unmount();

    mockApi({ "GET /accounts/{account_id}/balance/history": fail(500, "database unavailable") });
    renderWithProviders(<BalanceHistoryChart account={accountOut()} />);
    expect(
      await screen.findByText("Could not load the balance history: database unavailable"),
    ).toBeInTheDocument();
  });

  it("trims a percent's trailing zeros", () => {
    expect(trimZeros("12.50")).toBe("12.5");
    expect(trimZeros("10.00")).toBe("10");
    expect(trimZeros("0.00")).toBe("0");
    expect(trimZeros("-0.33")).toBe("-0.33");
    expect(trimZeros("100")).toBe("100");
  });
});

describe("BalanceHistoryChart percent (#594)", () => {
  it("leaves out a percent past 1,000% (a window opening near zero)", async () => {
    mockApi({
      "GET /accounts/{account_id}/balance/history": ok(balanceHistoryOut({ change_pct: "31864.73" })),
    });
    renderWithProviders(<BalanceHistoryChart account={accountOut()} />);
    expect(await screen.findByText("+$100.00")).toBeInTheDocument();
  });
});
