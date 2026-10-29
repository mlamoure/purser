import { useState } from "react";
import { Button, Card, CardTitle, ErrorText, Input, Label } from "../../components/ui";
import { api, errorDetail } from "../../lib/api";
import { AgentConnectPanel } from "./AgentConnectPanel";
import { useStepRunner } from "./useStepRunner";

/** Step 6: mint a read + write API token for the user's agent, then show
 * per-client setup. `onExit` leaves the wizard (skip, or Finish). */
export function ConnectAgentStep({ onExit }: { onExit: () => void }) {
  const { busy, error, run } = useStepRunner();
  const [tokenName, setTokenName] = useState("agent");
  const [agentToken, setAgentToken] = useState<string | null>(null);

  return (
    <Card>
      <CardTitle>Connect your AI agent</CardTitle>
      {!agentToken ? (
        <form
          className="space-y-4"
          onSubmit={(e) => {
            e.preventDefault();
            run(async () => {
              const { data, error: err } = await api.POST("/tokens", {
                body: { name: tokenName.trim(), scopes: ["read", "write"] },
              });
              if (err) throw new Error(errorDetail(err));
              setAgentToken(data!.token);
            });
          }}
        >
          <p className="text-sm text-muted-foreground">
            Purser speaks MCP. This mints an API token with <b>read + write</b> scopes:
            your agent acts for you. Every write it makes is recorded in the audit log
            and can be reversed; when it acts on its own idea it proposes instead, so you
            decide. It can never grant itself more access.
          </p>
          <div className="space-y-1">
            <Label htmlFor="setup-token-name">Token name</Label>
            <Input id="setup-token-name" value={tokenName} onChange={(e) => setTokenName(e.target.value)} required />
          </div>
          <div className="flex gap-2">
            <Button type="submit" disabled={busy}>Create agent token</Button>
            <Button type="button" variant="ghost" onClick={() => onExit()}>
              Skip for now
            </Button>
          </div>
          <p className="text-xs text-muted-foreground">
            You can mint or revoke tokens any time from the Tokens page.
          </p>
          <ErrorText>{error}</ErrorText>
        </form>
      ) : (
        <AgentConnectPanel
          token={agentToken}
          onFinish={() => {
            setAgentToken(null); // convention: never keep secrets in state past their step
            onExit();
          }}
        />
      )}
    </Card>
  );
}
