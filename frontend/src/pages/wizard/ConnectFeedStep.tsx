import { useState } from "react";
import { Button, Card, CardTitle, ErrorText, Input, Label } from "../../components/ui";
import { useStepRunner } from "./useStepRunner";

/** Step 3: exchange a SimpleFIN setup token (`connect`, from
 * useFeedConnection), or skip the feed for now. */
export function ConnectFeedStep({
  connect,
  onConnected,
  onSkip,
}: {
  connect: (setupToken: string) => Promise<void>;
  onConnected: () => void;
  onSkip: () => void;
}) {
  const { busy, error, run } = useStepRunner();
  const [setupToken, setSetupToken] = useState("");

  return (
    <Card>
      <CardTitle>Connect your bank feed (SimpleFIN)</CardTitle>
      <p className="mb-4 text-sm text-muted-foreground">
        Get a <b>setup token</b> from{" "}
        <a className="underline" href="https://bridge.simplefin.org" target="_blank" rel="noreferrer">
          SimpleFIN Bridge
        </a>{" "}
        (Bridge → Connections → “Get setup token”) and paste it below. The token is
        exchanged once; the resulting access URL is stored encrypted.
      </p>
      <form
        className="space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          run(async () => {
            await connect(setupToken.trim());
            onConnected();
          });
        }}
      >
        <div className="space-y-1">
          <Label htmlFor="setup-feed-token">Setup token</Label>
          <Input id="setup-feed-token" value={setupToken} onChange={(e) => setSetupToken(e.target.value)} placeholder="aHR0cHM6Ly9…" required />
        </div>
        <div className="flex gap-2">
          <Button type="submit" disabled={busy}>{busy ? "Connecting…" : "Connect & fetch"}</Button>
          <Button type="button" variant="ghost" onClick={() => onSkip()}>Skip for now</Button>
        </div>
        <ErrorText>{error}</ErrorText>
      </form>
    </Card>
  );
}
