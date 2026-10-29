import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { apiMock, fail, mockApi, ok } from "../../test/api";
import { renderWithProviders } from "../../test/render";
import { ConnectAgentStep } from "./ConnectAgentStep";

describe("ConnectAgentStep", () => {
  it("mints a read + write token under the chosen name and shows it once", async () => {
    mockApi({ "POST /tokens": ok({ token: "synthetic-agent-token" }, 201) });
    const onExit = vi.fn();
    const { user } = renderWithProviders(<ConnectAgentStep onExit={onExit} />);

    const name = screen.getByRole("textbox");
    expect(name).toHaveValue("agent");
    await user.clear(name);
    await user.type(name, " home-agent ");
    await user.click(screen.getByRole("button", { name: "Create agent token" }));

    expect(await screen.findByText("synthetic-agent-token")).toBeInTheDocument();
    expect(apiMock.POST).toHaveBeenCalledWith("/tokens", {
      body: { name: "home-agent", scopes: ["read", "write"] },
    });

    await user.click(screen.getByRole("button", { name: "Finish" }));

    expect(onExit).toHaveBeenCalledOnce();
    expect(screen.queryByText("synthetic-agent-token")).not.toBeInTheDocument();
  });

  it("can be skipped without minting a token", async () => {
    const onExit = vi.fn();
    const { user } = renderWithProviders(<ConnectAgentStep onExit={onExit} />);

    await user.click(screen.getByRole("button", { name: "Skip for now" }));

    expect(onExit).toHaveBeenCalledOnce();
    expect(apiMock.POST).not.toHaveBeenCalled();
  });

  it("shows why minting failed", async () => {
    mockApi({ "POST /tokens": fail(409, "a token with this name already exists") });
    const { user } = renderWithProviders(<ConnectAgentStep onExit={vi.fn()} />);

    await user.click(screen.getByRole("button", { name: "Create agent token" }));

    expect(await screen.findByText("a token with this name already exists")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Finish" })).not.toBeInTheDocument();
  });
});
