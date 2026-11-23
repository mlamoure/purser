import { Button } from "../../components/ui";
import { ClientInstructions } from "./ClientInstructions";
import { CopyButton } from "./CopyButton";

/** The freshly minted agent token (shown once) plus copy-paste MCP setup for
 * the chosen client, pointed at this browser's origin. */
export function AgentConnectPanel({ token, onFinish }: { token: string; onFinish: () => void }) {
  return (
    <div className="space-y-4">
      <p className="text-sm text-muted-foreground">
        The token is shown <b>once</b>. Connect your client now, in whichever way you prefer:
        choose it below and follow its steps on the machine where your agent runs, or paste the
        token to your agent and let it connect itself. A pasted token is also kept in that
        conversation and sent to the agent&apos;s model provider. If you lose it, create a new
        one under Settings, Agents &amp; API.
      </p>
      <div className="flex items-center gap-2">
        <code className="flex-1 break-all rounded-md border border-border bg-card p-3 font-mono text-xs">
          {token}
        </code>
        <CopyButton text={token} />
      </div>
      <ClientInstructions token={token} />
      <Button onClick={onFinish}>Finish</Button>
    </div>
  );
}
