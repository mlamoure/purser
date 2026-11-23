import { screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { apiMock, fail, mockApi, ok } from "../test/api";
import { accountOut } from "../test/fixtures";
import { renderWithProviders } from "../test/render";
import SplitEditor, { fromUnits, toUnits } from "./SplitEditor";

const category = (name: string, path: string, type = "category_expense") =>
  accountOut({ id: `c-${path}`, name, path, type, is_virtual: true });

function names() {
  mockApi({
    "GET /categories": ok([
      category("Salary", "income.salary", "category_income"),
      category("Taxes", "expenses.taxes"),
      category("Uncategorized", "expenses.uncategorized"),
      category("Uncategorized income", "income.uncategorized", "category_income"),
    ]),
    "GET /accounts": ok([
      accountOut({ id: "a-1", name: "Checking", path: "assets.checking" }),
      accountOut({ id: "a-2", name: "Example 401(k)", path: "assets.retirement", type: "brokerage" }),
    ]),
  });
}

function renderFlat(over: Partial<Parameters<typeof SplitEditor>[0]> = {}) {
  names();
  const onClose = vi.fn();
  const view = renderWithProviders(
    <SplitEditor
      txId="tx-1"
      isSplit={false}
      amount="2500.0000"
      accountPath="assets.checking"
      category="income.salary"
      onClose={onClose}
      {...over}
    />,
  );
  return { ...view, onClose };
}

const amountBox = (n: number) => screen.getByRole("textbox", { name: `Line ${n} amount` });
const pick = (n: number) => screen.getByRole("combobox", { name: `Line ${n} category or account` });
const save = () => screen.getByRole("button", { name: "Save split" });

describe("split money arithmetic", () => {
  it("adds and subtracts exactly, in ten-thousandths", () => {
    expect(toUnits("0.1")! + toUnits("0.2")!).toBe(toUnits("0.3"));
    expect(fromUnits(toUnits("-600")!)).toBe("-600.00");
    expect(fromUnits(toUnits("1.2345")!)).toBe("1.2345");
    expect(toUnits("1.23456")).toBeNull();
    expect(toUnits("abc")).toBeNull();
  });
});

describe("SplitEditor (#590)", () => {
  it("shows what is left to assign and saves only when it is zero", async () => {
    const { user } = renderFlat();
    await screen.findAllByRole("option", { name: "Taxes" });
    // The first line starts with the row's category and amount.
    expect(amountBox(1)).toHaveValue("2500.00");
    expect(save()).toBeDisabled();

    await user.clear(amountBox(1));
    await user.type(amountBox(1), "3400");
    await user.selectOptions(pick(2), "expenses.taxes");
    await user.type(amountBox(2), "-900");
    expect(screen.getByText("Left to assign: $0.00")).toBeInTheDocument();
    expect(save()).toBeEnabled();

    await user.clear(amountBox(2));
    await user.type(amountBox(2), "-800");
    expect(screen.getByText("Left to assign: -$100.00")).toBeInTheDocument();
    expect(save()).toBeDisabled();
  });

  it("puts the rest on the uncategorized placeholder of its sign", async () => {
    mockApi({ "PUT /transactions/{tx_id}/split": ok({}) });
    const { user, onClose } = renderFlat();
    await screen.findAllByRole("option", { name: "Taxes" });
    await user.clear(amountBox(1));
    await user.type(amountBox(1), "3400");
    await user.selectOptions(pick(2), "expenses.taxes");
    await user.type(amountBox(2), "-700");
    await user.click(screen.getByRole("button", { name: "Put the rest on Uncategorized" }));
    expect(amountBox(3)).toHaveValue("-200.00");
    expect(pick(3)).toHaveValue("expenses.uncategorized");
    await user.click(save());
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(apiMock.PUT).toHaveBeenCalledWith("/transactions/{tx_id}/split", {
      params: { path: { tx_id: "tx-1" } },
      body: {
        dry_run: false,
        lines: [
          { category: "income.salary", amount: "3400.00", backing_ids: [] },
          { category: "expenses.taxes", amount: "-700.00", backing_ids: [] },
          { category: "expenses.uncategorized", amount: "-200.00", backing_ids: [] },
        ],
      },
    });
  });

  it("lists bank rows for an account line and sends the checked ones as backing", async () => {
    mockApi({
      "GET /transactions/{tx_id}/split/candidates": (init) => {
        expect(init.params?.query).toEqual({ account: "assets.retirement", amount: "600.00" });
        return ok({
          account: "assets.retirement",
          window_days: 7,
          amount: "600.00",
          candidates: [
            {
              id: "bank-1",
              date: "2026-07-16",
              amount: "600.0000",
              status: "posted",
              payee: "CONTRIBUTION",
              description: null,
              backing: false,
              exact: true,
            },
          ],
        });
      },
      "PUT /transactions/{tx_id}/split": fail(422, "the rows backing [assets.retirement] sum to 1"),
    });
    const { user } = renderFlat();
    await screen.findAllByRole("option", { name: "Taxes" });
    await user.clear(amountBox(1));
    await user.type(amountBox(1), "3100");
    await user.selectOptions(pick(2), "[assets.retirement]");
    await user.type(amountBox(2), "-600");
    await user.click(await screen.findByRole("checkbox"));
    await user.click(save());
    expect(await screen.findByRole("alert")).toHaveTextContent("sum to 1");
    expect(apiMock.PUT).toHaveBeenCalledWith("/transactions/{tx_id}/split", {
      params: { path: { tx_id: "tx-1" } },
      body: {
        dry_run: false,
        lines: [
          { category: "income.salary", amount: "3100.00", backing_ids: [] },
          { category: "[assets.retirement]", amount: "-600.00", backing_ids: ["bank-1"] },
        ],
      },
    });
  });

  it("prefills a split from its detail and removes it", async () => {
    names();
    mockApi({
      "GET /transactions/{tx_id}": ok({
        id: "tx-s",
        amount: "2500.0000",
        split: {
          account_path: "assets.checking",
          lines: [
            { target: "income.salary", kind: "category", amount: "3400.0000", memo: null, backing_ids: [] },
            { target: "expenses.taxes", kind: "category", amount: "-900.0000", memo: "withholding", backing_ids: [] },
          ],
        },
      }),
      "DELETE /transactions/{tx_id}/split": ok({}),
    });
    const onClose = vi.fn();
    const { user } = renderWithProviders(
      <SplitEditor txId="tx-s" isSplit accountPath="assets.checking" onClose={onClose} />,
    );
    expect(await screen.findByDisplayValue("withholding")).toBeInTheDocument();
    expect(amountBox(1)).toHaveValue("3400.00");
    expect(screen.getByText("Left to assign: $0.00")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Remove split" }));
    // It asks first, and removes only on the confirmation.
    expect(screen.getByRole("group", { name: /Remove this split\?/ })).toBeInTheDocument();
    expect(apiMock.DELETE).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Remove split" }));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(apiMock.DELETE).toHaveBeenCalledWith("/transactions/{tx_id}/split", {
      params: { path: { tx_id: "tx-s" } },
    });
  });

  it("asks before removing a split, by keyboard, and keeps it on Escape or Keep split", async () => {
    names();
    mockApi({
      "GET /transactions/{tx_id}": ok({
        id: "tx-s",
        amount: "2500.0000",
        split: {
          account_path: "assets.checking",
          lines: [
            { target: "income.salary", kind: "category", amount: "3400.0000", memo: null, backing_ids: [] },
            { target: "expenses.taxes", kind: "category", amount: "-900.0000", memo: null, backing_ids: [] },
          ],
        },
      }),
      "DELETE /transactions/{tx_id}/split": ok({}),
    });
    const onClose = vi.fn();
    const { user } = renderWithProviders(
      <SplitEditor txId="tx-s" isSplit accountPath="assets.checking" onClose={onClose} />,
    );
    const removeButton = await screen.findByRole("button", { name: "Remove split" });
    removeButton.focus();
    await user.keyboard("{Enter}");
    // The question opens with focus on the safe choice.
    const question = screen.getByRole("group", { name: /Remove this split\?/ });
    expect(question).toHaveTextContent("the bank rows that backed its account lines come back");
    expect(screen.getByRole("button", { name: "Keep split" })).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("group", { name: /Remove this split\?/ })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Remove split" })).toHaveFocus();

    await user.keyboard("{Enter}");
    await user.click(screen.getByRole("button", { name: "Keep split" }));
    expect(screen.getByRole("button", { name: "Remove split" })).toHaveFocus();

    // Confirmed from the keyboard: Enter opens it, Shift+Tab reaches the
    // destructive choice, Enter removes.
    await user.keyboard("{Enter}");
    await user.keyboard("{Shift>}{Tab}{/Shift}");
    expect(screen.getByRole("button", { name: "Remove split" })).toHaveFocus();
    await user.keyboard("{Enter}");
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(apiMock.DELETE).toHaveBeenCalledTimes(1);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("saves through onSave instead of PUT when given one", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    const { user } = renderFlat({
      initialLines: [
        { category: "income.salary", amount: "3400.00", memo: "", backing: [] },
        { category: "expenses.taxes", amount: "-900.00", memo: "", backing: [] },
      ],
      onSave,
    });
    await screen.findAllByRole("option", { name: "Taxes" });
    expect(screen.queryByRole("button", { name: "Remove split" })).not.toBeInTheDocument();
    await user.click(save());
    await waitFor(() => expect(onSave).toHaveBeenCalled());
    expect(onSave.mock.calls[0][0]).toEqual([
      { category: "income.salary", amount: "3400.00", backing_ids: [] },
      { category: "expenses.taxes", amount: "-900.00", backing_ids: [] },
    ]);
    expect(apiMock.PUT).not.toHaveBeenCalled();
  });
});
