import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { apiMock, fail, mockApi, ok } from "../test/api";
import { accountOut } from "../test/fixtures";
import { renderWithProviders } from "../test/render";
import Accounts from "./Accounts";

const accountQueries = () =>
  apiMock.GET.mock.calls
    .filter(([path]) => path === "/accounts")
    .map(([, init]) => (init as { params: { query: Record<string, unknown> } }).params.query);

describe("Accounts page (#542)", () => {
  it("leads with the account name and a type in words, the path second (#541)", async () => {
    mockApi({
      "GET /accounts": ok([
        accountOut({ id: "a-cc", name: "Rewards Card", path: "liabilities.card", type: "credit_card", balance: "-380.0000" }),
      ]),
    });
    renderWithProviders(<Accounts />);

    const row = (await screen.findByRole("link", { name: "Rewards Card" })).closest("tr")!;
    const cells = within(row).getAllByRole("cell");
    // The first cell is the name (a link to the register), the path below it.
    expect(within(cells[0]).getByRole("link")).toHaveAttribute("href", "/accounts/a-cc");
    expect(cells[0].textContent).toBe("Rewards Cardliabilities.card");
    expect(within(row).getByText("Credit card")).toBeInTheDocument();
    expect(within(row).queryByText("credit_card")).not.toBeInTheDocument();
    expect(within(row).getByText("-$380.00")).toBeInTheDocument();
    const headers = screen.getAllByRole("columnheader").map((h) => h.textContent);
    expect(headers).toEqual(["Account", "Type", "Balance"]);
  });

  it("loads hidden and closed accounts only when asked, and badges them", async () => {
    mockApi({
      "GET /accounts": (init) => {
        const q = init.params?.query ?? {};
        return ok([
          accountOut({ id: "a-1", name: "Everyday Checking" }),
          ...(q.include_hidden ? [accountOut({ id: "a-2", name: "Old Wallet", hidden_at: "2026-10-01T00:00:00Z" })] : []),
          ...(q.include_closed ? [accountOut({ id: "a-3", name: "Closed Savings", closed_at: "2026-09-01T00:00:00Z" })] : []),
        ]);
      },
    });
    const { user } = renderWithProviders(<Accounts />);

    await screen.findByRole("link", { name: "Everyday Checking" });
    expect(accountQueries()[0]).toEqual({ include_balances: true, include_hidden: false, include_closed: false });
    expect(screen.queryByText("Old Wallet")).not.toBeInTheDocument();

    await user.click(screen.getByLabelText("Show hidden"));
    const hidden = (await screen.findByRole("link", { name: "Old Wallet" })).closest("tr")!;
    expect(within(hidden).getByText("hidden")).toBeInTheDocument();

    await user.click(screen.getByLabelText("Show closed"));
    const closed = (await screen.findByRole("link", { name: "Closed Savings" })).closest("tr")!;
    expect(within(closed).getByText("closed")).toBeInTheDocument();
    await waitFor(() =>
      expect(accountQueries()).toContainEqual({ include_balances: true, include_hidden: true, include_closed: true }),
    );
  });

  it("shows a failed load as an error and an empty ledger as such", async () => {
    mockApi({ "GET /accounts": fail(500, "database unavailable") });
    renderWithProviders(<Accounts />);
    expect(await screen.findByText("Could not load accounts: database unavailable")).toBeInTheDocument();
    expect(screen.queryByText(/No accounts yet/)).not.toBeInTheDocument();
  });

  it("says there are no accounts yet", async () => {
    mockApi({ "GET /accounts": ok([]) });
    renderWithProviders(<Accounts />);
    expect(await screen.findByText(/No accounts yet/)).toBeInTheDocument();
  });
});
