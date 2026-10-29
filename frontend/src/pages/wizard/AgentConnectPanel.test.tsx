import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { renderWithProviders } from "../../test/render";
import { AgentConnectPanel } from "./AgentConnectPanel";

const TOKEN = "synthetic-agent-token";

describe("AgentConnectPanel", () => {
  it("starts on the Claude setup, pointed at this origin's /mcp", () => {
    const mcpUrl = `${window.location.origin}/mcp`;
    renderWithProviders(<AgentConnectPanel token={TOKEN} onFinish={vi.fn()} />);

    expect(screen.getByText(TOKEN)).toBeInTheDocument();
    expect(screen.getByText(mcpUrl)).toBeInTheDocument();
    expect(screen.getByText(`Bearer ${TOKEN}`)).toBeInTheDocument();
    expect(
      screen.getByText(
        `claude mcp add --transport http purser ${mcpUrl} --header "Authorization: Bearer ${TOKEN}"`,
      ),
    ).toBeInTheDocument();
  });

  it("switches the instructions per client", async () => {
    const { user } = renderWithProviders(<AgentConnectPanel token={TOKEN} onFinish={vi.fn()} />);

    await user.click(screen.getByRole("button", { name: "Codex" }));
    expect(screen.getByText(/bearer_token_env_var = "PURSER_TOKEN"/)).toBeInTheDocument();
    expect(screen.getByText(`export PURSER_TOKEN="${TOKEN}"`)).toBeInTheDocument();
    expect(screen.queryByText(/claude mcp add/)).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Other" }));
    expect(screen.getByText(/"mcpServers"/)).toBeInTheDocument();
  });

  it("copies the token", async () => {
    const { user } = renderWithProviders(<AgentConnectPanel token={TOKEN} onFinish={vi.fn()} />);

    await user.click(screen.getAllByRole("button", { name: "Copy" })[0]);

    expect(await navigator.clipboard.readText()).toBe(TOKEN);
  });

  it("finishes on request", async () => {
    const onFinish = vi.fn();
    const { user } = renderWithProviders(<AgentConnectPanel token={TOKEN} onFinish={onFinish} />);

    await user.click(screen.getByRole("button", { name: "Finish" }));

    expect(onFinish).toHaveBeenCalledOnce();
  });
});
