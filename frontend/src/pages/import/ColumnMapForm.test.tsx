import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { renderWithProviders } from "../../test/render";
import { ColumnMapForm } from "./ColumnMapForm";

const COLUMNS = ["Day", "Description", "Value", "Clr", "Sched"];

// Selects in field order: date, amount, payee, memo, category, account,
// cleared, scheduled.
const selects = () => screen.getAllByRole("combobox");

describe("ColumnMapForm", () => {
  it("needs date, amount and payee before it applies", async () => {
    const onApply = vi.fn();
    const { user } = renderWithProviders(
      <ColumnMapForm columns={COLUMNS} initial={null} busy={false} onApply={onApply} />,
    );
    const apply = screen.getByRole("button", { name: "Use these columns" });
    const [date, amount, payee] = selects();

    await user.selectOptions(date, "Day");
    await user.selectOptions(amount, "Value");
    expect(apply).toBeDisabled();
    await user.selectOptions(payee, "Description");
    expect(apply).toBeEnabled();
  });

  it("applies only the columns chosen, including cleared and scheduled (#249)", async () => {
    const onApply = vi.fn();
    const { user } = renderWithProviders(
      <ColumnMapForm
        columns={COLUMNS}
        initial={{ date: "Day", amount: "Value", payee: "Description" }}
        busy={false}
        onApply={onApply}
      />,
    );
    const s = selects();
    expect(s[0]).toHaveValue("Day"); // seeded from the last map

    await user.selectOptions(s[6], "Clr");
    await user.selectOptions(s[7], "Sched");
    await user.click(screen.getByRole("button", { name: "Use these columns" }));

    expect(onApply).toHaveBeenCalledExactlyOnceWith({
      date: "Day",
      amount: "Value",
      payee: "Description",
      cleared: "Clr",
      scheduled: "Sched",
    });
  });

  it("waits while a preview is running", () => {
    renderWithProviders(
      <ColumnMapForm
        columns={COLUMNS}
        initial={{ date: "Day", amount: "Value", payee: "Description" }}
        busy
        onApply={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: "Use these columns" })).toBeDisabled();
  });
});
