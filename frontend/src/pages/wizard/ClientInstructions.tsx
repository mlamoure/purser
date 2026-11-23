import { useState } from "react";
import { Button } from "../../components/ui";
import { CopyButton } from "./CopyButton";

/** The MCP clients Purser documents, in the order people most often use them. */
const CLIENTS = [
  { id: "claude-code", label: "Claude Code" },
  { id: "claude-desktop", label: "Claude Desktop" },
  { id: "codex", label: "Codex" },
  { id: "gemini", label: "Gemini" },
  { id: "openclaw", label: "OpenClaw" },
  { id: "other", label: "Other" },
];

function Snippet({ text }: { text: string }) {
  return (
    <div>
      {/* Focusable: a long command scrolls sideways, and a keyboard user
          must be able to scroll it (WCAG 2.1.1). */}
      <pre tabIndex={0} className="overflow-auto rounded bg-muted p-2 font-mono text-[11px]">
        {text}
      </pre>
      <div className="mt-2">
        <CopyButton text={text} />
      </div>
    </div>
  );
}

/** Whether an MCP URL is plain HTTP on a host other than this machine. The
 * `mcp-remote` bridge refuses those unless told the network is trusted. */
export function needsAllowHttp(mcpUrl: string): boolean {
  const url = new URL(mcpUrl);
  return url.protocol === "http:" && !["localhost", "127.0.0.1", "[::1]"].includes(url.hostname);
}

/** Claude Desktop's local-server entry: the `mcp-remote` stdio bridge. The
 * header travels in an environment variable because some launchers split
 * arguments on spaces. */
export function claudeDesktopConfig(mcpUrl: string, token: string): string {
  return JSON.stringify(
    {
      mcpServers: {
        purser: {
          command: "npx",
          args: [
            "-y",
            "mcp-remote",
            mcpUrl,
            ...(needsAllowHttp(mcpUrl) ? ["--allow-http"] : []),
            "--header",
            "Authorization:${AUTH_HEADER}",
          ],
          env: { AUTH_HEADER: `Bearer ${token}` },
        },
      },
    },
    null,
    2,
  );
}

/** Copy-paste setup for each supported MCP client, with the token filled in
 * and the server address taken from this browser's. Shown wherever a token
 * has just been minted: the wizard's last step and Settings -> Agents & API. */
export function ClientInstructions({ token }: { token: string }) {
  const [client, setClient] = useState("claude-code");
  const mcpUrl = `${window.location.origin}/mcp`;
  const genericConfig = JSON.stringify(
    {
      mcpServers: {
        purser: {
          type: "http",
          url: mcpUrl,
          headers: { Authorization: `Bearer ${token}` },
        },
      },
    },
    null,
    2,
  );
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap gap-2" role="group" aria-label="Your client">
        {CLIENTS.map((c) => (
          <Button key={c.id} type="button" size="sm"
            variant={client === c.id ? "default" : "outline"}
            aria-pressed={client === c.id}
            onClick={() => setClient(c.id)}>
            {c.label}
          </Button>
        ))}
      </div>

      {client === "claude-code" && (
        <div className="space-y-3 text-sm">
          <p>
            Run this in a terminal on the machine where you use <b>Claude Code</b>, then start a
            new session (<code>claude --continue</code> keeps the conversation):
          </p>
          <Snippet text={`claude mcp add --transport http --scope user purser ${mcpUrl} --header "Authorization: Bearer ${token}"`} />
          <p className="text-xs text-muted-foreground">
            <code>--scope user</code> makes Purser available in every folder, not only the one
            the command ran in.
          </p>
        </div>
      )}

      {client === "claude-desktop" && (
        <div className="space-y-3 text-sm">
          <p>
            <b>Claude Desktop</b> reaches a server on your own machine or home network through a
            small bridge program, which needs <a className="underline" href="https://nodejs.org/"
              target="_blank" rel="noreferrer">Node.js</a>. Open <b>Settings</b>, then{" "}
            <b>Developer</b>, then <b>Edit Config</b>, and put this in{" "}
            <code>claude_desktop_config.json</code> (if the file already has{" "}
            <code>mcpServers</code>, add <code>purser</code> beside the others). Then quit and
            reopen Claude Desktop.
          </p>
          <Snippet text={claudeDesktopConfig(mcpUrl, token)} />
          <p className="text-xs text-muted-foreground">
            The Connectors dialog in claude.ai and Claude Desktop, and the ChatGPT app, connect
            from the vendor's servers, so they cannot reach a Purser on your own machine or
            home network.
          </p>
        </div>
      )}

      {client === "codex" && (
        <div className="space-y-3 text-sm">
          <p>
            <b>Codex</b> (OpenAI's agent: the CLI, desktop app and IDE extension share one
            config). Add this to <code>~/.codex/config.toml</code>, then restart Codex or open a
            new thread:
          </p>
          <Snippet text={`[mcp_servers.purser]\nurl = "${mcpUrl}"\nhttp_headers = { Authorization = "Bearer ${token}" }`} />
          <p>The file now holds the token, so make it readable only by you:</p>
          <Snippet text="chmod 600 ~/.codex/config.toml" />
          <p className="text-xs text-muted-foreground">
            To keep the token out of the file, write{" "}
            <code>bearer_token_env_var = "PURSER_TOKEN"</code> in place of the{" "}
            <code>http_headers</code> line and set <code>PURSER_TOKEN</code> wherever Codex
            starts. Check with <code>codex mcp list</code>. The ChatGPT app cannot reach a
            Purser on your own machine or home network.
          </p>
        </div>
      )}

      {client === "gemini" && (
        <div className="space-y-3 text-sm">
          <p>
            <b>Gemini CLI</b> and Gemini Code Assist share their settings. One command:
          </p>
          <Snippet text={`gemini mcp add --scope user --transport http --header "Authorization: Bearer ${token}" purser ${mcpUrl}`} />
          <p className="text-xs text-muted-foreground">
            <code>--scope user</code> makes Purser available in every folder. Gemini CLI
            loads MCP servers only in folders you have marked as trusted. The consumer Gemini
            app does not support custom MCP servers.
          </p>
        </div>
      )}

      {client === "openclaw" && (
        <div className="space-y-3 text-sm">
          <p><b>OpenClaw</b>: one command on the gateway host:</p>
          <Snippet text={`openclaw mcp add purser --url ${mcpUrl} --transport streamable-http --header "Authorization: Bearer ${token}"`} />
          <p className="text-xs text-muted-foreground">
            Restart the gateway afterwards so the tools are picked up.
          </p>
        </div>
      )}

      {client === "other" && (
        <div className="space-y-3 text-sm">
          <p>Any MCP client that speaks streamable HTTP. The usual config shape:</p>
          <Snippet text={genericConfig} />
          <p className="text-xs text-muted-foreground">
            A client that only speaks stdio can use the bridge shown under Claude Desktop.
          </p>
        </div>
      )}

      <p className="text-xs text-muted-foreground">
        The address above is the one this browser used. If your agent runs on another machine,
        open Purser at the address that machine will use, or change the host in the snippet.
      </p>
    </div>
  );
}
