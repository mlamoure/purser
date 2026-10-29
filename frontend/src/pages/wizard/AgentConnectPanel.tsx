import { useState } from "react";
import { Button } from "../../components/ui";
import { CopyButton } from "./CopyButton";

const CLIENTS = [
  { id: "claude", label: "Claude" },
  { id: "codex", label: "Codex" },
  { id: "openclaw", label: "OpenClaw" },
  { id: "gemini", label: "Gemini" },
  { id: "other", label: "Other" },
];

function Snippet({ text }: { text: string }) {
  return (
    <div>
      <pre className="overflow-auto rounded bg-muted p-2 font-mono text-[11px]">{text}</pre>
      <div className="mt-2">
        <CopyButton text={text} />
      </div>
    </div>
  );
}

/** The freshly minted agent token (shown once) plus copy-paste MCP setup for
 * the chosen client, pointed at this browser's origin. */
export function AgentConnectPanel({ token, onFinish }: { token: string; onFinish: () => void }) {
  const [client, setClient] = useState("claude");
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
      <p className="text-sm text-muted-foreground">
        The token is shown <b>once</b>. Connect your client now — if you lose the token,
        mint a new one from the Tokens page.
      </p>
      <div className="flex items-center gap-2">
        <code className="flex-1 break-all rounded-md border border-border bg-card p-3 font-mono text-xs">
          {token}
        </code>
        <CopyButton text={token} />
      </div>
      <div className="flex flex-wrap gap-2">
        {CLIENTS.map((c) => (
          <Button key={c.id} type="button" size="sm"
            variant={client === c.id ? "default" : "outline"}
            onClick={() => setClient(c.id)}>
            {c.label}
          </Button>
        ))}
      </div>

      {client === "claude" && (
        <div className="space-y-3 text-sm">
          <p>
            On <b>claude.ai / Claude Desktop</b>: open{" "}
            <a className="underline" href="https://claude.ai/settings/connectors?modal=add-custom-connector"
              target="_blank" rel="noreferrer">
              Settings &gt; Connectors &gt; Add custom connector
            </a>{" "}
            and paste the server URL, plus the header value below under <b>Request headers</b>{" "}
            (name: <code>Authorization</code>).
          </p>
          <div className="flex items-center gap-2">
            <code className="flex-1 break-all rounded bg-muted p-2 text-xs">{mcpUrl}</code>
            <CopyButton text={mcpUrl} label="Copy URL" />
          </div>
          <div className="flex items-center gap-2">
            <code className="flex-1 break-all rounded bg-muted p-2 text-xs">{`Bearer ${token}`}</code>
            <CopyButton text={`Bearer ${token}`} label="Copy header" />
          </div>
          <p className="text-xs text-muted-foreground">
            The Request headers section is a beta feature still rolling out — if your account
            does not show it, the dialog only offers OAuth (which Purser does not support yet).
          </p>
          <p><b>Claude Code</b> — one command:</p>
          <Snippet text={`claude mcp add --transport http purser ${mcpUrl} --header "Authorization: Bearer ${token}"`} />
        </div>
      )}

      {client === "codex" && (
        <div className="space-y-3 text-sm">
          <p>
            <b>Codex</b> (OpenAI's agent — CLI, desktop app, and IDE extension share one
            config). Add to <code>~/.codex/config.toml</code>:
          </p>
          <Snippet text={`[mcp_servers.purser]\nurl = "${mcpUrl}"\nbearer_token_env_var = "PURSER_TOKEN"`} />
          <p>Then make the token visible to the Codex process:</p>
          <Snippet text={`export PURSER_TOKEN="${token}"`} />
          <p className="text-xs text-muted-foreground">
            The env var must be visible to the Codex process — GUI apps may not inherit your
            shell config. Restart the app or open a new thread, then verify with{" "}
            <code>codex mcp list</code>. ChatGPT web-chat connectors require OAuth and are not
            supported yet.
          </p>
        </div>
      )}

      {client === "openclaw" && (
        <div className="space-y-3 text-sm">
          <p><b>OpenClaw</b> — one command on the gateway host:</p>
          <Snippet text={`openclaw mcp add purser --url ${mcpUrl} --transport streamable-http --header "Authorization: Bearer ${token}"`} />
          <p className="text-xs text-muted-foreground">
            Restart the gateway afterwards so the tools are picked up.
          </p>
        </div>
      )}

      {client === "gemini" && (
        <div className="space-y-3 text-sm">
          <p><b>Gemini CLI / Code Assist</b> — one command (they share settings):</p>
          <Snippet text={`gemini mcp add --transport http --header "Authorization: Bearer ${token}" purser ${mcpUrl}`} />
          <p className="text-xs text-muted-foreground">
            The consumer Gemini app does not support custom MCP servers.
          </p>
        </div>
      )}

      {client === "other" && (
        <div className="space-y-3 text-sm">
          <p>Any MCP client speaking streamable HTTP — typical config shape:</p>
          <Snippet text={genericConfig} />
          <p className="text-xs text-muted-foreground">
            Clients that only speak stdio can bridge with <code>mcp-remote</code>.
          </p>
        </div>
      )}

      <p className="text-xs text-muted-foreground">
        URLs above use this browser's address — adjust the host if your agent reaches
        Purser somewhere else.
      </p>
      <Button onClick={onFinish}>Finish</Button>
    </div>
  );
}
