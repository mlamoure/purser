import { useState } from "react";
import type { components } from "../../api/schema";
import { Button, Card, CardTitle, ErrorText } from "../../components/ui";
import { api, errorDetail } from "../../lib/api";
import { CopyButton } from "./CopyButton";
import { useStepRunner } from "./useStepRunner";

export type EncryptionKey = components["schemas"]["EncryptionKeyOut"];

/** The key with a copy button and its fingerprint, a "stored it" checkbox,
 * and the button that acknowledges it (`POST /auth/encryption-key/ack`, after
 * which the server never reveals the key again). The wizard's key step and
 * Config -> Settings (for a key left unacknowledged) both render it; each
 * drops the key from memory in `onAcknowledged`. */
export function RecordKeyPanel({
  keyInfo,
  confirmLabel,
  onAcknowledged,
}: {
  keyInfo: EncryptionKey;
  confirmLabel: string;
  onAcknowledged: () => void;
}) {
  const { busy, error, run } = useStepRunner();
  const [keyStored, setKeyStored] = useState(false);

  return (
    <div>
      <div className="mb-2 flex items-center gap-2">
        <code className="flex-1 break-all rounded-md border border-border bg-card p-3 font-mono text-xs">
          {keyInfo.key}
        </code>
        <CopyButton text={keyInfo.key} />
      </div>
      <p className="mb-4 text-xs text-muted-foreground">
        Key fingerprint: <code>{keyInfo.fingerprint}</code>
      </p>
      <label className="mb-4 flex items-center gap-2 text-sm">
        <input
          type="checkbox"
          checked={keyStored}
          onChange={(e) => setKeyStored(e.target.checked)}
        />
        I have stored this key somewhere safe
      </label>
      <Button
        disabled={!keyStored || busy}
        onClick={() =>
          run(async () => {
            const { error: err } = await api.POST("/auth/encryption-key/ack");
            if (err) throw new Error(errorDetail(err));
            onAcknowledged();
          })
        }
      >
        {confirmLabel}
      </Button>
      <ErrorText>{error}</ErrorText>
    </div>
  );
}

/** Step 2: show the encryption key and acknowledge that it was stored.
 * After `onAcknowledged` the wizard drops the key — it is never kept in
 * memory past this step. */
export function EncryptionKeyStep({
  keyInfo,
  onAcknowledged,
}: {
  keyInfo: EncryptionKey;
  onAcknowledged: () => void;
}) {
  return (
    <Card>
      <CardTitle>Record your encryption key</CardTitle>
      <p className="mb-4 text-sm text-muted-foreground">
        Purser encrypts your archived bank data <b>and the narrative layer</b> — payees,
        descriptions, memos, schedule names, and your audit trail — with this key
        (<code>PURSER_ENCRYPTION_KEY</code>). Purser shows it only until you confirm you have
        stored it. Store it in your password manager now — without it, that data is
        unrecoverable.
      </p>
      <RecordKeyPanel keyInfo={keyInfo} confirmLabel="Continue" onAcknowledged={onAcknowledged} />
    </Card>
  );
}
