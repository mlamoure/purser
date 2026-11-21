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

  it("shows an admin the AI categorization provider, saved with its own Save (#542)", async () => {
    const config = configOut();
    mockApi({ "GET /categories": ok([]), "GET /config": ok(config), "PUT /config": ok(config) });
    const view = renderWithProviders(<Categories />);
    view.queryClient.setQueryData(meQuery.queryKey, { scopes: ["read", "write", "admin"] } as never);

    const card = (await screen.findByRole("heading", { name: "AI categorization" })).closest("div")!;
    expect(within(card).getByLabelText("Provider")).toHaveValue("none");
    await view.user.click(within(card).getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(apiMock.PUT).toHaveBeenCalledWith("/config", { body: { ai_provider: "none" } }),
    );
  });

  it("never reads the admin-only AI setting for a member session", async () => {
    mockApi({ "GET /categories": ok([]) });
    const view = renderWithProviders(<Categories />);
    view.queryClient.setQueryData(meQuery.queryKey, { scopes: ["read", "write"] } as never);

    await screen.findByText(/Categories are a curated, finite set/);
    expect(screen.queryByText("AI categorization")).not.toBeInTheDocument();
    expect(apiMock.GET).not.toHaveBeenCalledWith("/config");
  });
});
