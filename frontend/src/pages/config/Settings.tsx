import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, errorDetail } from "../../lib/api";
import { Badge, Button, Card, CardTitle, ErrorText, Input, Label } from "../../components/ui";
import type { components } from "../../api/schema";

type ConfigOut = components["schemas"]["ConfigOut"];
type ConfigUpdate = components["schemas"]["ConfigUpdate"];
type AiProvider = NonNullable<ConfigUpdate["ai_provider"]>;

const selectClass = "h-9 w-full rounded-md border border-border bg-card px-2 text-sm";

function useSaveConfig(onError: (message: string) => void) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (body: ConfigUpdate) => {
      const { error: err } = await api.PUT("/config", { body });
      if (err) throw new Error(errorDetail(err));
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ["config"] }),
    onError: (e) => onError(e.message),
  });
}

function Saved({ visible }: { visible: boolean }) {
  return visible ? <p className="mt-2 text-sm text-emerald-700">Saved.</p> : null;
}

function AiCard({ initial }: { initial: ConfigOut }) {
  const [provider, setProvider] = useState<AiProvider>(
    (initial.values.ai_provider as AiProvider) ?? "none",
  );
  const [model, setModel] = useState(initial.values.ai_model ?? "");
  const [baseUrl, setBaseUrl] = useState(initial.values.ai_base_url);
  const [apiKey, setApiKey] = useState("");
  const [error, setError] = useState<string | null>(null);
  const save = useSaveConfig(setError);

  return (
    <Card>
      <CardTitle>AI categorization</CardTitle>
      <p className="mb-4 text-sm text-muted-foreground">
        Optional bring-your-own provider for categorizing new transactions. Purser is fully
        functional with this off.
      </p>
      <div className="grid grid-cols-2 gap-4">
        <div className="space-y-1">
          <Label>Provider</Label>
          <select
            className={selectClass}
            value={provider}
            onChange={(e) => setProvider(e.target.value as AiProvider)}
          >
            <option value="none">none (off)</option>
            <option value="openai">OpenAI-compatible</option>
            <option value="anthropic">Anthropic</option>
          </select>
        </div>
        {provider !== "none" && (
          <>
            <div className="space-y-1">
              <Label>Model</Label>
              <Input
                value={model}
                onChange={(e) => setModel(e.target.value)}
                placeholder="e.g. gpt-4.1-mini"
              />
            </div>
            <div className="space-y-1">
              <Label>Base URL</Label>
              <Input
                type="url"
                value={baseUrl}
                onChange={(e) => setBaseUrl(e.target.value)}
                placeholder="https://api.openai.com/v1"
              />
            </div>
            <div className="space-y-1">
              <Label>
                API key{" "}
                <Badge variant={initial.ai_api_key_set ? "green" : "default"}>
                  {initial.ai_api_key_set ? "set" : "not set"}
                </Badge>
              </Label>
              <Input
                type="password"
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                placeholder={initial.ai_api_key_set ? "enter a new key to replace" : "sk-…"}
              />
            </div>
          </>
        )}
      </div>
      <Button
        className="mt-4"
        disabled={save.isPending}
        onClick={() => {
          setError(null);
          const body: ConfigUpdate = { ai_provider: provider };
          if (provider !== "none") {
            body.ai_model = model.trim() || null;
            body.ai_base_url = baseUrl.trim();
            if (apiKey.trim()) body.ai_api_key = apiKey.trim();
          }
          save.mutate(body, { onSuccess: () => setApiKey("") });
        }}
      >
        Save
      </Button>
      <Saved visible={save.isSuccess} />
      <ErrorText>{error}</ErrorText>
    </Card>
  );
}

function TransfersCard({ initial }: { initial: ConfigOut }) {
  const [autoLink, setAutoLink] = useState(initial.values.transfer_auto_link);
  const [error, setError] = useState<string | null>(null);
  const save = useSaveConfig(setError);

  return (
    <Card>
      <CardTitle>Transfers</CardTitle>
      <p className="mb-4 text-sm text-muted-foreground">
        Deterministic auto-linking: exact opposite amounts within the date window whose text
        names the transfer (keyword or counterparty account name) merge automatically — evented,
        badged in the register, one click to unlink. Amount-only pairs always stay confirm-first.
      </p>
      <div className="flex items-end gap-2">
        <div className="space-y-1">
          <Label>Auto-link strong pairs</Label>
          <select
            className={selectClass}
            value={autoLink ? "on" : "off"}
            onChange={(e) => setAutoLink(e.target.value === "on")}
          >
            <option value="on">On (recommended)</option>
            <option value="off">Off — everything stays a candidate</option>
          </select>
        </div>
        <Button
          disabled={save.isPending}
          onClick={() => {
            setError(null);
            save.mutate({ transfer_auto_link: autoLink });
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

function PrivacyCard({ initial }: { initial: ConfigOut }) {
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

function SessionsCard({ initial }: { initial: ConfigOut }) {
  const [ttlDays, setTtlDays] = useState(String(initial.values.session_ttl_days));
  const [idleHours, setIdleHours] = useState(String(initial.values.session_idle_hours));
  const [error, setError] = useState<string | null>(null);
  const save = useSaveConfig(setError);

  return (
    <Card>
      <CardTitle>Sessions</CardTitle>
      <p className="mb-4 text-sm text-muted-foreground">
        Browser login lifetimes. API tokens are managed on the Tokens page.
      </p>
      <div className="grid grid-cols-2 gap-4">
        <div className="space-y-1">
          <Label>Session lifetime (days)</Label>
          <Input
            type="number"
            min={1}
            max={365}
            value={ttlDays}
            onChange={(e) => setTtlDays(e.target.value)}
          />
        </div>
        <div className="space-y-1">
          <Label>Idle timeout (hours)</Label>
          <Input
            type="number"
            min={1}
            max={720}
            value={idleHours}
            onChange={(e) => setIdleHours(e.target.value)}
          />
        </div>
      </div>
      <Button
        className="mt-4"
        disabled={save.isPending || !Number(ttlDays) || !Number(idleHours)}
        onClick={() => {
          setError(null);
          save.mutate({
            session_ttl_days: Number(ttlDays),
            session_idle_hours: Number(idleHours),
          });
        }}
      >
        Save
      </Button>
      <Saved visible={save.isSuccess} />
      <ErrorText>{error}</ErrorText>
    </Card>
  );
}

function LinksCard({ initial }: { initial: ConfigOut }) {
  const [grafanaUrl, setGrafanaUrl] = useState(initial.values.grafana_url ?? "");
  const [error, setError] = useState<string | null>(null);
  const save = useSaveConfig(setError);

  return (
    <Card>
      <CardTitle>Links</CardTitle>
      <p className="mb-4 text-sm text-muted-foreground">
        Grafana base URL for dashboard deep links on tool responses. Leave blank to omit links.
      </p>
      <div className="flex items-end gap-2">
        <div className="flex-1 space-y-1">
          <Label>Grafana URL</Label>
          <Input
            type="url"
            value={grafanaUrl}
            onChange={(e) => setGrafanaUrl(e.target.value)}
            placeholder="https://grafana.example.com"
          />
        </div>
        <Button
          disabled={save.isPending}
          onClick={() => {
            setError(null);
            save.mutate({ grafana_url: grafanaUrl.trim() || null });
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

function SsoCard({ initial }: { initial: ConfigOut }) {
  const [issuer, setIssuer] = useState(initial.values.oidc_issuer ?? "");
  const [clientId, setClientId] = useState(initial.values.oidc_client_id ?? "");
  const [clientSecret, setClientSecret] = useState("");
  const [autoProvision, setAutoProvision] = useState(initial.values.oidc_auto_provision);
  const [error, setError] = useState<string | null>(null);
  const save = useSaveConfig(setError);

  return (
    <Card>
      <CardTitle>
        Single sign-on (OIDC){" "}
        <Badge variant={initial.values.oidc_enabled ? "green" : "default"}>
          {initial.values.oidc_enabled ? "enabled" : "disabled"}
        </Badge>
      </CardTitle>
      <p className="mb-4 text-sm text-muted-foreground">
        Log in via an OpenID Provider (e.g. Authentik). Enabled once issuer, client ID, and
        client secret are all set. Password login always keeps working, so a bad value here
        can't lock you out. The redirect URI is {"{public URL}"}/auth/oidc/callback.
      </p>
      <div className="grid grid-cols-2 gap-4">
        <div className="space-y-1">
          <Label>Issuer URL</Label>
          <Input
            type="url"
            value={issuer}
            onChange={(e) => setIssuer(e.target.value)}
            placeholder="https://auth.example.com/application/o/purser/"
          />
        </div>
        <div className="space-y-1">
          <Label>Client ID</Label>
          <Input value={clientId} onChange={(e) => setClientId(e.target.value)} />
        </div>
        <div className="space-y-1">
          <Label>
            Client secret{" "}
            <Badge variant={initial.oidc_client_secret_set ? "green" : "default"}>
              {initial.oidc_client_secret_set ? "set" : "not set"}
            </Badge>
          </Label>
          <Input
            type="password"
            value={clientSecret}
            onChange={(e) => setClientSecret(e.target.value)}
            placeholder={
              initial.oidc_client_secret_set ? "enter a new secret to replace" : "client secret"
            }
          />
        </div>
        <div className="space-y-1">
          <Label>Unknown users</Label>
          <select
            className={selectClass}
            value={autoProvision ? "auto" : "deny"}
            onChange={(e) => setAutoProvision(e.target.value === "auto")}
          >
            <option value="deny">Refused — pre-create users here (recommended)</option>
            <option value="auto">Auto-provision as member (verified email only)</option>
          </select>
        </div>
      </div>
      <Button
        className="mt-4"
        disabled={save.isPending}
        onClick={() => {
          setError(null);
          const body: ConfigUpdate = {
            oidc_issuer: issuer.trim() || null,
            oidc_client_id: clientId.trim() || null,
            oidc_auto_provision: autoProvision,
          };
          if (clientSecret.trim()) body.oidc_client_secret = clientSecret.trim();
          save.mutate(body, { onSuccess: () => setClientSecret("") });
        }}
      >
        Save
      </Button>
      <Saved visible={save.isSuccess} />
      <ErrorText>{error}</ErrorText>
    </Card>
  );
}


function EncryptionCard({ initial }: { initial: ConfigOut }) {
  const enc = initial.encryption;
  return (
    <Card>
      <CardTitle>
        Encryption{" "}
        <Badge variant={enc.field_encryption_active ? "green" : "red"}>
          {enc.field_encryption_active ? "field encryption active" : "canary check failed"}
        </Badge>
      </CardTitle>
      <p className="mb-2 text-sm text-muted-foreground">
        Sensitive data is encrypted inside the database with the instance key
        (fingerprint <code className="rounded bg-muted px-1">{enc.key_fingerprint}</code>):{" "}
        {enc.encrypted_scopes.join(", ")}.
      </p>
      <p className="mb-2 text-sm text-muted-foreground">
        Key recorded by operator:{" "}
        {enc.key_acknowledged_at
          ? new Date(enc.key_acknowledged_at).toLocaleString()
          : "not yet acknowledged"}
        .
      </p>
      <p className="text-xs text-muted-foreground">
        The narrative layer (payees, descriptions, memos, schedule names, audit and event
        payloads) is encrypted; only the numeric skeleton — amounts, dates, statuses,
        category paths, ids — and the readmodel v2 views over it stay plaintext by design,
        because that is the direct-SQL/Grafana contract every invariant and aggregate
        computes on. Encrypting the database files themselves is a storage/deployment
        concern, not something Purser can attest from in here.
      </p>
    </Card>
  );
}

export default function Settings() {
  const config = useQuery({
    queryKey: ["config"],
    queryFn: async () => (await api.GET("/config")).data ?? null,
  });

  if (config.isLoading) return <p className="text-sm text-muted-foreground">Loading…</p>;
  if (!config.data) return <ErrorText>Failed to load config.</ErrorText>;

  return (
    <div className="space-y-6">
      <TransfersCard initial={config.data} />
      <PrivacyCard initial={config.data} />
      <AiCard initial={config.data} />
      <SsoCard initial={config.data} />
      <EncryptionCard initial={config.data} />
      <SessionsCard initial={config.data} />
      <LinksCard initial={config.data} />
    </div>
  );
}
