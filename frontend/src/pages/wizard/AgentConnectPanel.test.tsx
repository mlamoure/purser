import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { renderWithProviders } from "../../test/render";
import { AgentConnectPanel } from "./AgentConnectPanel";

const TOKEN = "synthetic-agent-token";

describe("AgentConnectPanel", () => {
  it("shows the token and the client instructions", () => {
    renderWithProviders(<AgentConnectPanel token={TOKEN} onFinish={vi.fn()} />);

    expect(screen.getByText(TOKEN)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Claude Code" })).toBeInTheDocument();
    // Both ways to hand the token over are offered as equals (#582).
    expect(screen.getByText(/follow its steps on the machine where your agent runs, or paste the\s+token to your agent/)).toBeInTheDocument();
  });

  it("copies the token and says so", async () => {
    const { user } = renderWithProviders(<AgentConnectPanel token={TOKEN} onFinish={vi.fn()} />);

    await user.click(screen.getAllByRole("button", { name: "Copy" })[0]);

    expect(await navigator.clipboard.readText()).toBe(TOKEN);
    expect(await screen.findByRole("button", { name: "Copied" })).toBeInTheDocument();
  });

  it("finishes on request", async () => {
    const onFinish = vi.fn();
    const { user } = renderWithProviders(<AgentConnectPanel token={TOKEN} onFinish={onFinish} />);

    await user.click(screen.getByRole("button", { name: "Finish" }));

    expect(onFinish).toHaveBeenCalledOnce();
  });
});
