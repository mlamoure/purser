import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { meQuery } from "../../lib/me";
import { apiMock, mockApi, ok } from "../../test/api";
import { renderWithProviders } from "../../test/render";
import AgentsApi from "./AgentsApi";

function renderAs(scopes: string[]) {
  const view = renderWithProviders(<AgentsApi />);
  view.queryClient.setQueryData(meQuery.queryKey, { scopes } as never);
  return view;
}

describe("Settings, Agents & API (#542)", () => {
  it("shows an admin the tokens and sign-ins, and no privacy settings (#613)", async () => {
    mockApi({
      "GET /tokens": ok([]),
      "GET /config/network": ok({
        reach: "this_machine",
        public_url: "http://localhost:8000",
        mcp_url: "http://localhost:8000/mcp",
        allowed_hosts: [],
        bind_address: "127.0.0.1",
        public_url_is_local: true,
        request_host: "localhost:8000",
        request_host_accepted: true,
        guide_url: "https://docs.example.com/tutorials/ai-setup/",
      }),
    });
    renderAs(["read", "write", "admin"]);

    expect(await screen.findByRole("heading", { name: "API tokens" })).toBeInTheDocument();
    // How Purser is reached, next to the connection steps (#632).
    expect(await screen.findByRole("heading", { name: "How Purser is reached" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Login sessions" })).toBeInTheDocument();
    // Session lifetime is an admin setting under Security; the list says where.
    expect(screen.getByRole("link", { name: "Security" })).toHaveAttribute("href", "/settings/security");
    // Privacy is the agent's per-call choice now: nothing to set, no /config read.
    expect(screen.queryByText("Privacy with agents")).not.toBeInTheDocument();
    expect(apiMock.GET).not.toHaveBeenCalledWith("/config");
  });

  it("gives a member their own tokens and sign-ins without reading GET /config", async () => {
    mockApi({ "GET /tokens": ok([]) });
    renderAs(["read", "write"]);

    expect(await screen.findByRole("heading", { name: "Login sessions" })).toBeInTheDocument();
    expect(screen.queryByText("Privacy with agents")).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Security" })).not.toBeInTheDocument();
    expect(apiMock.GET).not.toHaveBeenCalledWith("/config");
    // The network report is admin configuration (#632).
    expect(screen.queryByRole("heading", { name: "How Purser is reached" })).not.toBeInTheDocument();
    expect(apiMock.GET).not.toHaveBeenCalledWith("/config/network");
  });
});
