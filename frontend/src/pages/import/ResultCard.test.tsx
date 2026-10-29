import { screen, within } from "@testing-library/react";
import { Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { counts, importResult } from "../../test/fixtures";
import { renderWithProviders } from "../../test/render";
import { ResultCard } from "./ResultCard";

/** The number on a stat tile (labels also head table columns, hence `div`). */
const stat = (label: string) =>
  screen.getByText(label, { selector: "div" }).previousElementSibling?.textContent;

describe("ResultCard", () => {
  it("reports the totals and each file account, reminders and uncleared included (#249)", () => {
    const result = importResult({
      imported: 4,
      transfers_created: 1,
      uncleared: 2,
      skipped: 3,
      skipped_scheduled: 1,
      per_account: {
        __default__: counts({ would_import: 4, transfers: 1, uncleared: 2, skipped_dedup: 3, skipped_scheduled: 1 }),
      },
    });
    renderWithProviders(<ResultCard result={result} onDone={vi.fn()} />);

    expect(stat("Imported")).toBe("4");
    expect(stat("Transfers")).toBe("1");
    expect(stat("Uncleared (expected)")).toBe("2");
    expect(stat("Skipped (dedup)")).toBe("3");
    expect(stat("Skipped (reminders)")).toBe("1");
    const row = screen.getByText("(single account)").closest("tr")!;
    expect(within(row).getAllByRole("cell").map((c) => c.textContent)).toEqual([
      "(single account)", "4", "1", "0", "2", "0", "3", "0", "0", "1", "0",
    ]);
  });

  it("reports rows a cleared re-import promoted (#310)", () => {
    const result = importResult({
      promoted: 2,
      skipped: 1,
      per_account: { Checking: counts({ promoted: 2, skipped_dedup: 1 }) },
    });
    renderWithProviders(<ResultCard result={result} onDone={vi.fn()} />);

    expect(stat("Promoted (posted)")).toBe("2");
    const row = screen.getByText("Checking").closest("tr")!;
    expect(within(row).getAllByRole("cell").map((c) => c.textContent)).toEqual([
      "Checking", "0", "0", "0", "0", "2", "1", "0", "0", "0", "0",
    ]);
  });

  it("reports skipped QIF investment records (#274)", () => {
    const result = importResult({
      skipped_investment: 7,
      per_account: { Brokerage: counts({ in_file: 7, skipped_investment: 7 }) },
    });
    renderWithProviders(<ResultCard result={result} onDone={vi.fn()} />);

    expect(stat("Skipped (investment)")).toBe("7");
    const row = screen.getByText("Brokerage").closest("tr")!;
    expect(within(row).getAllByRole("cell").map((c) => c.textContent)).toEqual([
      "Brokerage", "0", "0", "0", "0", "0", "0", "0", "0", "0", "7",
    ]);
  });

  it("leaves out the per-account table when there is nothing per account", () => {
    renderWithProviders(<ResultCard result={importResult()} onDone={vi.fn()} />);

    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("hands back to the wizard when it has a caller", async () => {
    const onDone = vi.fn();
    const { user } = renderWithProviders(<ResultCard result={importResult()} onDone={onDone} />);

    await user.click(screen.getByRole("button", { name: "Done" }));

    expect(onDone).toHaveBeenCalledOnce();
  });

  it("goes to the dashboard otherwise", async () => {
    const { user } = renderWithProviders(
      <Routes>
        <Route path="/import" element={<ResultCard result={importResult()} />} />
        <Route path="/" element={<p>dashboard</p>} />
      </Routes>,
      { route: "/import" },
    );

    await user.click(screen.getByRole("button", { name: "Done" }));

    expect(screen.getByText("dashboard")).toBeInTheDocument();
  });
});
