import { useState } from "react";
import { Badge } from "../components/ui";
import { ConnectAgentStep } from "./wizard/ConnectAgentStep";
import { ConnectFeedStep } from "./wizard/ConnectFeedStep";
import { CreateAdminStep } from "./wizard/CreateAdminStep";
import { EncryptionKeyStep, type EncryptionKey } from "./wizard/EncryptionKeyStep";
import { ImportHistoryStep } from "./wizard/ImportHistoryStep";
import { MapAccountsStep } from "./wizard/MapAccountsStep";
import { useFeedConnection } from "./wizard/useFeedConnection";

const STEPS = [
  "Create admin",
  "Record encryption key",
  "Connect SimpleFIN",
  "Map accounts",
  "Import history",
  "Connect agent",
];

// Leaving the wizard is a full page load, so the app re-reads setup status.
const leaveWizard = () => {
  window.location.href = "/";
};

// First-run wizard: create admin -> record encryption key -> connect SimpleFIN
// -> map accounts -> optional QIF import -> connect agent. Reaching first sync
// and a connected agent should need no docs.
//
// Each step is a component under ./wizard/ with its own form and busy/error
// state. This shell holds only what crosses steps: the step index (0-based;
// the badges count from 1), the encryption key between creating the admin
// and recording it, and the SimpleFIN connection between connecting the feed
// and mapping its accounts (useFeedConnection).
export default function Wizard() {
  const [step, setStep] = useState(0);
  const [keyInfo, setKeyInfo] = useState<EncryptionKey | null>(null);
  const feed = useFeedConnection();

  return (
    <div className="mx-auto max-w-2xl p-10">
      <h1 className="mb-2 text-2xl font-bold text-primary">Welcome to Purser</h1>
      <p className="mb-6 text-sm text-muted-foreground">
        The finance backend for your personal agent stack. Six quick steps.
      </p>
      <div className="mb-6 flex flex-wrap gap-2">
        {STEPS.map((s, i) => (
          <Badge key={s} variant={i === step ? "green" : "default"}>
            {i + 1}. {s}
          </Badge>
        ))}
      </div>

      {step === 0 && (
        <CreateAdminStep
          onCreated={(key) => {
            if (key) {
              setKeyInfo(key);
              setStep(1);
            } else {
              setStep(2);
            }
          }}
        />
      )}

      {step === 1 && keyInfo && (
        <EncryptionKeyStep
          keyInfo={keyInfo}
          onAcknowledged={() => {
            setKeyInfo(null); // never keep the key in memory past this step
            setStep(2);
          }}
        />
      )}

      {step === 2 && (
        <ConnectFeedStep
          connect={feed.connect}
          onConnected={() => setStep(3)}
          onSkip={() => setStep(4)}
        />
      )}

      {step === 3 && feed.connectionId && (
        <MapAccountsStep
          discovered={feed.discovered}
          mapAccount={feed.mapAccount}
          onContinue={() => setStep(4)}
        />
      )}

      {step === 4 && <ImportHistoryStep onDone={() => setStep(5)} />}

      {step === 5 && <ConnectAgentStep onExit={leaveWizard} />}
    </div>
  );
}
