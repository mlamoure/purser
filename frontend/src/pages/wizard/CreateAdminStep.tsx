import { useState } from "react";
import { Button, Card, CardTitle, ErrorText, Input, Label } from "../../components/ui";
import { api, errorDetail } from "../../lib/api";
import type { EncryptionKey } from "./EncryptionKeyStep";
import { useStepRunner } from "./useStepRunner";

/** Step 1: create the admin account, then fetch the encryption key for step 2.
 * `onCreated(null)` means the key was already acknowledged (410 on a
 * re-entered wizard), so the key step is skipped. */
export function CreateAdminStep({ onCreated }: { onCreated: (key: EncryptionKey | null) => void }) {
  const { busy, error, run } = useStepRunner();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");

  return (
    <Card>
      <CardTitle>Create the admin account</CardTitle>
      <form
        className="space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          run(async () => {
            const { error: err } = await api.POST("/auth/setup", {
              body: { email, password },
            });
            if (err) throw new Error(errorDetail(err));
            // Reveal the key for recording; already-acknowledged (410,
            // re-entered wizard) skips straight to the feed step.
            const { data: keyData } = await api.GET("/auth/encryption-key");
            onCreated(keyData ?? null);
          });
        }}
      >
        <div className="space-y-1">
          <Label htmlFor="setup-email">Email</Label>
          <Input id="setup-email" type="email" value={email} onChange={(e) => setEmail(e.target.value)} required />
        </div>
        <div className="space-y-1">
          <Label htmlFor="setup-password">Password (8+ characters)</Label>
          <Input id="setup-password" type="password" minLength={8} value={password} onChange={(e) => setPassword(e.target.value)} required />
        </div>
        <Button type="submit" disabled={busy}>Create admin</Button>
        <ErrorText>{error}</ErrorText>
      </form>
    </Card>
  );
}
