import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { meQuery } from "../../lib/me";
import { apiMock, mockApi, ok } from "../../test/api";
import { configOut } from "../../test/fixtures";
import { renderWithProviders } from "../../test/render";
import AgentsApi from "./AgentsApi";

const CONFIG = configOut();

function renderAs(scopes: string[]) {
  const view = renderWithProviders(<AgentsApi />);
  view.queryClient.setQueryData(meQuery.queryKey, { scopes } as never);
  return view;
}

describe("Settings, Agents & API (#542)", () => {
  it("shows an admin the tokens, sign-ins and the privacy defaults (#437)", async () => {
    mockApi({ "GET /tokens": ok([]), "GET /config": ok(CONFIG), "PUT /config": ok(CONFIG) });
    const { user } = renderAs(["read", "write", "admin"]);

    expect(await screen.findByRole("heading", { name: "API tokens" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Login sessions" })).toBeInTheDocument();
    // Session lifetime is an admin setting under Security; the list says where.
    expect(screen.getByRole("link", { name: "Security" })).toHaveAttribute("href", "/settings/security");

    await user.click(await screen.findByLabelText(/Privatize the purchase/));
    await user.click(screen.getByLabelText(/Privatize the amount/));
    const card = screen.getByRole("heading", { name: "Privacy with agents" }).closest("div")!;
    await user.click(within(card).getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(apiMock.PUT).toHaveBeenCalledWith("/config", {
        body: { privacy_purchase: true, privacy_amount: true },
      }),
    );
  });

  it("gives a member their own tokens and sign-ins without reading GET /config", async () => {
    mockApi({ "GET /tokens": ok([]) });
    renderAs(["read", "write"]);

    expect(await screen.findByRole("heading", { name: "Login sessions" })).toBeInTheDocument();
    expect(screen.queryByText("Privacy with agents")).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Security" })).not.toBeInTheDocument();
    expect(apiMock.GET).not.toHaveBeenCalledWith("/config");
  });
});
