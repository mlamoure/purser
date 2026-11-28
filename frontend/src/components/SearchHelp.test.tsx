import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { SearchHelp } from "./SearchHelp";

describe("SearchHelp", () => {
  it("opens the syntax from its button", async () => {
    const user = userEvent.setup();
    render(<SearchHelp />);
    const toggle = screen.getByLabelText("Search syntax");
    expect(toggle.closest("details")).not.toHaveAttribute("open");
    await user.click(toggle);
    expect(toggle.closest("details")).toHaveAttribute("open");
    expect(screen.getByText("payee:costco")).toBeVisible();
    expect(screen.getByText("amount:>100")).toBeVisible();
    expect(screen.getByText(/Every part must match/)).toBeVisible();
  });

  it("documents is:needs-review, not the retired review terms (#694)", async () => {
    const user = userEvent.setup();
    render(<SearchHelp />);
    await user.click(screen.getByLabelText("Search syntax"));
    expect(screen.getByText("is:needs-review")).toBeVisible();
    expect(screen.getByText(/no category, or a reconciliation adjustment/)).toBeVisible();
    expect(screen.queryByText(/is:unreviewed|is:reviewed/)).not.toBeInTheDocument();
  });
});
