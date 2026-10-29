import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { accountOut, counts, previewAccount, previewResponse } from "../../test/fixtures";
import { renderWithProviders } from "../../test/render";
import { MappingTable } from "./MappingTable";
import { mergePreview, type Mapping } from "./model";

function renderTable(
  accounts = [previewAccount()],
  patch: (m: Record<string, Mapping>) => Record<string, Mapping> = (m) => m,
) {
  const preview = previewResponse(accounts);
  const props = {
    preview,
    mappings: patch(mergePreview({}, preview.accounts)),
    accounts: [accountOut()],
    allowSkip: false,
    busy: false,
    error: null,
    onPatch: vi.fn(),
    onCreate: vi.fn(),
    onImport: vi.fn(),
  };
  return { props, ...renderWithProviders(<MappingTable {...props} />) };
}

describe("MappingTable", () => {
  it("summarises the file and holds Import until every account is mapped", () => {
    renderTable([previewAccount({ name: "A" }), previewAccount({ name: "B", transactions: 1 })]);

    expect(screen.getByText(/4 transactions across 2 accounts\./)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Import" })).toBeDisabled();
    expect(screen.getByText("Map or skip every account to enable import.")).toBeInTheDocument();
  });

  it("patches the row's own file account", async () => {
    const { props, user } = renderTable();

    await user.selectOptions(screen.getAllByRole("combobox")[0], "a-1");

    expect(props.onPatch).toHaveBeenCalledWith("Everyday Checking", { target: "a-1" });
  });

  it("imports once every account is mapped", async () => {
    const { props, user } = renderTable(undefined, (m) => ({
      ...m,
      "Everyday Checking": { ...m["Everyday Checking"], target: "a-1" },
    }));

    expect(screen.queryByText("Map or skip every account to enable import.")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Import" }));

    expect(props.onImport).toHaveBeenCalledOnce();
  });

  it("warns about reminder and uncleared rows before the commit (#249)", () => {
    renderTable([
      previewAccount({ name: "A", counts: counts({ skipped_scheduled: 2, uncleared: 1 }) }),
      previewAccount({ name: "B", counts: counts({ uncleared: 2 }) }),
    ]);

    expect(
      screen.getByText("2 Quicken reminder rows (Scheduled) are not history and will be skipped."),
    ).toBeInTheDocument();
    expect(screen.getByText(/3 uncleared rows will land as/)).toBeInTheDocument();
  });

  it("says which expected rows a cleared re-import will post (#310)", () => {
    renderTable([
      previewAccount({ name: "A", counts: counts({ promoted: 1 }) }),
      previewAccount({ name: "B", counts: counts({ promoted: 2 }) }),
    ]);

    expect(
      screen.getByText(/3 rows an earlier import left expected are now marked cleared/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/uncleared row/)).not.toBeInTheDocument();
  });

  it("says investment records will be skipped (#274)", () => {
    renderTable([
      previewAccount({
        name: "Brokerage",
        type: "brokerage",
        counts: counts({ in_file: 7, skipped_investment: 7 }),
      }),
    ]);

    expect(
      screen.getByText(/7 investment records \(buys, sells, dividends\) are not bank activity/),
    ).toBeInTheDocument();
    // An investment-only account needs no target: import is already allowed.
    expect(screen.queryByText("Map or skip every account to enable import.")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Import" })).toBeEnabled();
  });

  it("says nothing about status when there is nothing to say", () => {
    renderTable([previewAccount({ counts: counts({ in_file: 3, would_import: 3 }) })]);

    expect(screen.queryByText(/Quicken reminder/)).not.toBeInTheDocument();
    expect(screen.queryByText(/uncleared row/)).not.toBeInTheDocument();
    expect(screen.queryByText(/earlier import left expected/)).not.toBeInTheDocument();
    expect(screen.queryByText(/investment record/)).not.toBeInTheDocument();
  });
});
