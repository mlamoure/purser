import { useState } from "react";
import { Button, Card, CardTitle, ErrorText } from "../../components/ui";
import { Saved, useSaveConfig, type ConfigOut } from "./saveConfig";

/** What an agent receives by default (#437). Settings -> Agents & API shows
 * it to admins (#542), beside the tokens agents connect with. */
export function PrivacyCard({ initial }: { initial: ConfigOut }) {
  const [purchase, setPurchase] = useState(initial.values.privacy_purchase);
  const [amount, setAmount] = useState(initial.values.privacy_amount);
  const [error, setError] = useState<string | null>(null);
  const save = useSaveConfig(setError);

  return (
    <Card>
      <CardTitle>Privacy with agents</CardTitle>
      <p className="mb-4 text-sm text-muted-foreground">
        What your AI agent receives by default. Everything it reads goes to its model provider,
        so these keep details out unless a task needs them. They are defaults, not limits: an
        agent can ask for a field on any call, and the audit log shows every call that did.
        Totals and balances are always exact. Signed-in pages always show full detail.
      </p>
      <div className="space-y-3">
        <label className="flex items-start gap-2 text-sm">
          <input
            type="checkbox"
            className="mt-1"
            checked={purchase}
            onChange={(e) => setPurchase(e.target.checked)}
          />
          <span>
            <b>Privatize the purchase</b> — payees become stable references (like py_8k2mq4x7ra)
            and descriptions, memos and bill names are left out.
          </span>
        </label>
        <label className="flex items-start gap-2 text-sm">
          <input
            type="checkbox"
            className="mt-1"
            checked={amount}
            onChange={(e) => setAmount(e.target.checked)}
          />
          <span>
            <b>Privatize the amount</b> — single transaction and bill amounts are left out
            (never rounded or faked).
          </span>
        </label>
        <Button
          disabled={save.isPending}
          onClick={() => {
            setError(null);
            save.mutate({ privacy_purchase: purchase, privacy_amount: amount });
          }}
        >
          Save
        </Button>
      </div>
      <Saved visible={save.isSuccess} />
      <ErrorText>{error}</ErrorText>
    </Card>
  );
}

