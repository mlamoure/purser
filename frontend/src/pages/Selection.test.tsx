import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { apiMock, mockApi, ok } from "../test/api";
import { renderWithProviders } from "../test/render";
import Selection from "./Selection";

const ROW = {
  id: "11111111-1111-4111-8111-111111111111",
  date: "2026-10-20",
  payee: "Example Bakery",
  description: "SYNTHETIC CARD 1111",
  status: "posted",
  origin: "import",
  amount: "-12.50",
  account_path: "assets.checking",
  category_path: "expenses.dining",
};

describe("Selection (#437 present)", () => {
  it("loads the presented rows by id and shows the agent's note", async () => {
    mockApi({ "GET /transactions/selection": ok([ROW]) });
    renderWithProviders(<Selection />, {
      route: `/selection?id=${ROW.id}&note=largest+this+week`,
    });
    expect(await screen.findByText("Example Bakery")).toBeInTheDocument();
    expect(screen.getByText("largest this week")).toBeInTheDocument();
    expect(screen.getByText("-$12.50")).toBeInTheDocument();
    expect(apiMock.GET).toHaveBeenCalledWith("/transactions/selection", {
      params: { query: { id: [ROW.id] } },
    });
  });

  it("says so when the link names nothing", () => {
    mockApi({});
    renderWithProviders(<Selection />, { route: "/selection" });
    expect(screen.getByText("This link names no transactions.")).toBeInTheDocument();
  });
});
