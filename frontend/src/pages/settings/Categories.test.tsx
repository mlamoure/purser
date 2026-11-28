import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { meQuery } from "../../lib/me";
import { apiMock, fail, mockApi, ok } from "../../test/api";
import { accountOut, configOut } from "../../test/fixtures";
import { renderWithProviders } from "../../test/render";
import Categories from "./Categories";

describe("Settings, Categories", () => {
  it("shows a failed load as an error instead of loading forever", async () => {
    mockApi({ "GET /categories": fail(500, "database unavailable") });
    renderWithProviders(<Categories />);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not load categories: database unavailable",
    );
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("names each row's inputs after the category path", async () => {
    mockApi({
      "GET /categories": ok([
        accountOut({ id: "c-1", path: "expense", name: "Expenses", type: "category_expense" }),
        accountOut({ id: "c-2", path: "expense.food", name: "Food", type: "category_expense" }),
      ]),
    });
    renderWithProviders(<Categories />);

    expect(await screen.findByLabelText("Name of expense.food")).toHaveValue("Food");
    expect(screen.getByLabelText("Description of expense.food")).toBeInTheDocument();
  });

  it("lets an admin turn off flagging bank-sync rows with no category, with its own Save (#694)", async () => {
    const config = configOut();
    mockApi({ "GET /categories": ok([]), "GET /config": ok(config), "PUT /config": ok(config) });
    const view = renderWithProviders(<Categories />);
    view.queryClient.setQueryData(meQuery.queryKey, { scopes: ["read", "write", "admin"] } as never);

    const card = (await screen.findByRole("heading", { name: "Transactions that need review" })).closest("div")!;
    const flag = within(card).getByLabelText("Flag bank-sync transactions with no category for review");
    expect(flag).toBeChecked();
    expect(card).toHaveTextContent("Reconciliation adjustments are flagged whatever this says.");
    await view.user.click(flag);
    await view.user.click(within(card).getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(apiMock.PUT).toHaveBeenCalledWith("/config", { body: { review_uncategorized: false } }),
    );
    expect(await within(card).findByText("Saved.")).toBeInTheDocument();
    // The provider moved to its own tab (#694).
    expect(screen.queryByRole("heading", { name: "AI categorization" })).not.toBeInTheDocument();
  });

  it("shows the setting as saved: off stays off", async () => {
    mockApi({ "GET /categories": ok([]), "GET /config": ok(configOut({ review_uncategorized: false })) });
    const view = renderWithProviders(<Categories />);
    view.queryClient.setQueryData(meQuery.queryKey, { scopes: ["read", "write", "admin"] } as never);

    expect(
      await screen.findByLabelText("Flag bank-sync transactions with no category for review"),
    ).not.toBeChecked();
  });

  it("never reads the admin-only review setting for a member session", async () => {
    mockApi({ "GET /categories": ok([]) });
    const view = renderWithProviders(<Categories />);
    view.queryClient.setQueryData(meQuery.queryKey, { scopes: ["read", "write"] } as never);

    await screen.findByText(/Categories are a curated, finite set/);
    expect(screen.queryByText("Transactions that need review")).not.toBeInTheDocument();
    expect(
      screen.queryByLabelText("Flag bank-sync transactions with no category for review"),
    ).not.toBeInTheDocument();
    expect(apiMock.GET).not.toHaveBeenCalledWith("/config");
  });
});
