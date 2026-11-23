import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { renderWithProviders } from "../../test/render";
import { claudeDesktopConfig, ClientInstructions, needsAllowHttp } from "./ClientInstructions";

const TOKEN = "synthetic-agent-token";

describe("ClientInstructions", () => {
  it("starts on Claude Code, registered for every folder at this origin's /mcp", () => {
    const mcpUrl = `${window.location.origin}/mcp`;
    renderWithProviders(<ClientInstructions token={TOKEN} />);

    expect(
      screen.getByText(
        `claude mcp add --transport http --scope user purser ${mcpUrl} --header "Authorization: Bearer ${TOKEN}"`,
      ),
    ).toBeInTheDocument();
  });

  it("lets the keyboard reach a command that scrolls sideways", () => {
    renderWithProviders(<ClientInstructions token={TOKEN} />);

    expect(screen.getByText(/^claude mcp add /)).toHaveAttribute("tabindex", "0");
  });

  it("has a tab for every supported client", () => {
    renderWithProviders(<ClientInstructions token={TOKEN} />);

    for (const name of ["Claude Code", "Claude Desktop", "Codex", "Gemini", "OpenClaw", "Other"]) {
      expect(screen.getByRole("button", { name })).toBeInTheDocument();
    }
  });

  it("gives Claude Desktop the bridge, and says the cloud connectors cannot reach a home server", async () => {
    const { user } = renderWithProviders(<ClientInstructions token={TOKEN} />);

    await user.click(screen.getByRole("button", { name: "Claude Desktop" }));

    expect(screen.getByText(/"mcp-remote"/)).toBeInTheDocument();
    expect(screen.getByText(/"AUTH_HEADER": "Bearer synthetic-agent-token"/)).toBeInTheDocument();
    expect(screen.getByText(/connect\s+from the vendor's servers/)).toBeInTheDocument();
    expect(screen.queryByText(/claude mcp add/)).not.toBeInTheDocument();
  });

  it("switches the instructions per client", async () => {
    const { user } = renderWithProviders(<ClientInstructions token={TOKEN} />);

    await user.click(screen.getByRole("button", { name: "Codex" }));
    expect(screen.getByText(/http_headers = \{ Authorization = "Bearer synthetic-agent-token" \}/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Gemini" }));
    expect(screen.getByText(/gemini mcp add --scope user --transport http/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Other" }));
    expect(screen.getByText(/"type": "http"/)).toBeInTheDocument();
  });
});

describe("the Claude Desktop bridge config", () => {
  it("needs --allow-http only for plain HTTP off this machine", () => {
    expect(needsAllowHttp("http://localhost:8000/mcp")).toBe(false);
    expect(needsAllowHttp("http://127.0.0.1:8010/mcp")).toBe(false);
    expect(needsAllowHttp("https://purser.example.com/mcp")).toBe(false);
    expect(needsAllowHttp("http://192.168.1.20:8000/mcp")).toBe(true);
    expect(needsAllowHttp("http://homeserver.local:8000/mcp")).toBe(true);
  });

  it("adds the flag for a home-network address and keeps the token out of the arguments", () => {
    const lan = JSON.parse(claudeDesktopConfig("http://192.168.1.20:8000/mcp", TOKEN));
    expect(lan.mcpServers.purser.args).toEqual([
      "-y", "mcp-remote", "http://192.168.1.20:8000/mcp", "--allow-http",
      "--header", "Authorization:${AUTH_HEADER}",
    ]);
    expect(lan.mcpServers.purser.env).toEqual({ AUTH_HEADER: `Bearer ${TOKEN}` });

    const local = JSON.parse(claudeDesktopConfig("http://localhost:8000/mcp", TOKEN));
    expect(local.mcpServers.purser.args).not.toContain("--allow-http");
  });
});
