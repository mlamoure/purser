import { screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { accountOut, counts, previewAccount } from "../../test/fixtures";
import { renderWithProviders } from "../../test/render";
import { AccountRow } from "./AccountRow";
import { CREATE, SKIP, type Mapping } from "./model";

const ACCOUNTS = [
  accountOut({ id: "a-1", path: "assets.checking" }),
  accountOut({ id: "a-2", path: "assets.feed_checking", connection_id: "c-1" }),
];

const mapping = (over: Partial<Mapping> = {}): Mapping => ({
  target: "",
  cutoff: "",
  invert: false,
  cutoffTouched: false,
  invertTouched: false,
  newName: "Everyday Checking",
  newType: "checking",
  newPath: "assets.everyday_checking",
  ...over,
});

function renderRow(over: Partial<Parameters<typeof AccountRow>[0]> = {}) {
  const props = {
    account: previewAccount(),
    mapping: mapping(),
    accounts: ACCOUNTS,
    selected: null,
    allowSkip: false,
    busy: false,
    onPatch: vi.fn(),
    onCreate: vi.fn(),
    ...over,
  };
  const view = renderWithProviders(
    <table>
      <tbody>
        <AccountRow {...props} />
      </tbody>
    </table>,
  );
  return { props, ...view };
}

const target = () => screen.getAllByRole("combobox")[0];

describe("AccountRow", () => {
  it("offers manual and SimpleFIN accounts, and patches the chosen target", async () => {
    const { props, user } = renderRow();

    expect(within(screen.getByRole("group", { name: "Accounts" })).getByText("assets.checking")).toBeInTheDocument();
    expect(
      within(screen.getByRole("group", { name: "SimpleFIN accounts" })).getByText("assets.feed_checking (SimpleFIN)"),
    ).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "Don't import" })).not.toBeInTheDocument();

    await user.selectOptions(target(), "a-2");

    expect(props.onPatch).toHaveBeenCalledWith({ target: "a-2" });
  });

  it("shows the live counts, uncleared and reminder rows included (#249)", () => {
    renderRow({
      account: previewAccount({
        counts: counts({ in_file: 5, would_import: 2, uncleared: 1, skipped_scheduled: 2 }),
      }),
    });

    expect(screen.getByText("in 5")).toBeInTheDocument();
    expect(screen.getByText("import 2")).toBeInTheDocument();
    expect(screen.getByText("uncleared 1")).toBeInTheDocument();
    expect(screen.getByText("reminder 2")).toBeInTheDocument();
  });

  it("counts transfer lines carved out of split rows (#274)", () => {
    renderRow({
      account: previewAccount({ counts: counts({ in_file: 1, would_import: 1, split_transfers: 1 }) }),
    });

    expect(screen.getByText("split→transfer 1")).toBeInTheDocument();
  });

  it("counts rows a cleared re-import will promote (#310)", () => {
    renderRow({
      account: previewAccount({ counts: counts({ in_file: 2, promoted: 1, skipped_dedup: 1 }) }),
    });

    expect(screen.getByText("promoted 1")).toBeInTheDocument();
    expect(screen.getByText("dedup 1")).toBeInTheDocument();
  });

  it("counts investment records and offers don't-import for an investment-only QIF account (#274)", () => {
    renderRow({
      account: previewAccount({
        name: "Brokerage",
        type: "brokerage",
        counts: counts({ in_file: 7, skipped_investment: 7 }),
      }),
    });

    expect(screen.getByText("investment 7")).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Don't import" })).toBeInTheDocument();
  });

  it("disables cutoff and invert for a CSV account marked don't-import", () => {
    renderRow({ allowSkip: true, mapping: mapping({ target: SKIP }) });

    expect(target()).toHaveValue(SKIP);
    expect(screen.getByText("not imported")).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "flip signs" })).toBeDisabled();
  });

  it("marks cutoff and invert edits as the user's own", async () => {
    const { props, user } = renderRow({ mapping: mapping({ target: "a-1" }) });

    await user.click(screen.getByRole("checkbox", { name: "flip signs" }));

    expect(props.onPatch).toHaveBeenCalledWith({ invert: true, invertTouched: true });
    expect(screen.getByText("importing everything")).toBeInTheDocument();
  });

  it("edits and creates a new account, re-suggesting the path for its type", async () => {
    const { props, user } = renderRow({ mapping: mapping({ target: CREATE }) });

    expect(screen.getByDisplayValue("Everyday Checking")).toBeInTheDocument();
    await user.selectOptions(screen.getAllByRole("combobox")[1], "loan");
    expect(props.onPatch).toHaveBeenCalledWith({ newType: "loan", newPath: "liabilities.everyday_checking" });

    await user.click(screen.getByRole("button", { name: "Create & select" }));
    expect(props.onCreate).toHaveBeenCalledOnce();
  });

  it("suggests inverting an income-positive account", () => {
    renderRow({
      account: previewAccount({
        type: "credit_card",
        sign: { positive: "120.00", negative: "0.00", net: "120.00", suspect: true },
      }),
    });

    expect(screen.getByText("Looks income-positive for a credit_card — invert suggested.")).toBeInTheDocument();
  });
});
