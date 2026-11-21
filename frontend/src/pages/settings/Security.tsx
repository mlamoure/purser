import { useId, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, errorDetail } from "../../lib/api";
import { configQuery } from "../../lib/config";
import { useFormatTimestamp } from "../../lib/clock";
import { QueryState } from "../../components/QueryState";
import { Badge, Button, Card, CardTitle, ErrorText, Input, Label } from "../../components/ui";
import { RecordKeyPanel, type EncryptionKey } from "../wizard/EncryptionKeyStep";
import { Saved, selectClass, useSaveConfig, type ConfigOut, type ConfigUpdate } from "./saveConfig";

function SsoCard({ initial }: { initial: ConfigOut }) {
  const [issuer, setIssuer] = useState(initial.values.oidc_issuer ?? "");
  const [clientId, setClientId] = useState(initial.values.oidc_client_id ?? "");
  const [clientSecret, setClientSecret] = useState("");
  const [autoProvision, setAutoProvision] = useState(initial.values.oidc_auto_provision);
  const [error, setError] = useState<string | null>(null);
  const save = useSaveConfig(setError);
  const id = useId();

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
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        <div className="space-y-1">
          <Label htmlFor={`${id}-issuer`}>Issuer URL</Label>
          <Input
            id={`${id}-issuer`}
            type="url"
            value={issuer}
            onChange={(e) => setIssuer(e.target.value)}
            placeholder="https://auth.example.com/application/o/purser/"
          />
        </div>
        <div className="space-y-1">
          <Label htmlFor={`${id}-client-id`}>Client ID</Label>
          <Input id={`${id}-client-id`} value={clientId} onChange={(e) => setClientId(e.target.value)} />
        </div>
        <div className="space-y-1">
          <div className="flex items-center gap-2">
            <Label htmlFor={`${id}-client-secret`}>Client secret</Label>
            <Badge variant={initial.oidc_client_secret_set ? "green" : "default"}>
              {initial.oidc_client_secret_set ? "set" : "not set"}
            </Badge>
          </div>
          <Input
            id={`${id}-client-secret`}
            type="password"
            value={clientSecret}
            onChange={(e) => setClientSecret(e.target.value)}
            placeholder={
              initial.oidc_client_secret_set ? "enter a new secret to replace" : "client secret"
            }
          />
        </div>
        <div className="space-y-1">
          <Label htmlFor={`${id}-unknown-users`}>Unknown users</Label>
          <select
            id={`${id}-unknown-users`}
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


// Record the key after setup (#539): the wizard's key step is lost if the
// first admin reloads, and the server keeps revealing the key
// (GET /auth/encryption-key) until it is acknowledged. The key lives in this
// component's state only while the panel is open.
function RecordKeyAction() {
  const qc = useQueryClient();
  const [keyInfo, setKeyInfo] = useState<EncryptionKey | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const done = () => qc.invalidateQueries({ queryKey: ["config"] });

  if (keyInfo) {
    return (
      <div className="mb-4 rounded-md border border-border p-3">
        <p className="mb-3 text-sm text-muted-foreground">
          Store this key in your password manager. Once you confirm, Purser never shows it
          again: it lives only in your environment (<code>PURSER_ENCRYPTION_KEY</code>).
        </p>
        <RecordKeyPanel
          keyInfo={keyInfo}
          confirmLabel="Confirm key recorded"
          onAcknowledged={() => {
            setKeyInfo(null); // never keep the key past this step
            done();
          }}
        />
        <Button className="mt-2" size="sm" variant="ghost" onClick={() => setKeyInfo(null)}>
          Hide key
        </Button>
      </div>
    );
  }
  return (
    <div className="mb-4">
      <Button
        size="sm"
        disabled={busy}
        onClick={async () => {
          setBusy(true);
          setError(null);
          try {
            const { data, error: err, response } = await api.GET("/auth/encryption-key");
            if (response.status === 410) {
              done(); // acknowledged elsewhere since this page loaded
            } else if (err || !data) {
              setError(err ? errorDetail(err) : "Could not load the key.");
            } else {
              setKeyInfo(data);
            }
          } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
          } finally {
            setBusy(false);
          }
        }}
      >
        Record your key
      </Button>
      <ErrorText>{error}</ErrorText>
    </div>
  );
}

function EncryptionCard({ initial }: { initial: ConfigOut }) {
  const enc = initial.encryption;
  const formatTimestamp = useFormatTimestamp();
  // Plain words first (#541); the storage terms an operator may need —
  // fingerprint, the exact encrypted scopes, the self-check, the plaintext
  // reporting views — sit in "Technical detail".
  return (
    <Card>
      <CardTitle>
        Encryption{" "}
        <Badge variant={enc.field_encryption_active ? "green" : "red"}>
          {enc.field_encryption_active ? "on" : "check failed"}
        </Badge>
      </CardTitle>
      {enc.field_encryption_active ? (
        <p className="mb-2 text-sm text-muted-foreground">
          Payee names, descriptions, memos, schedule names and the audit trail are encrypted
          inside the database with this instance's key. Amounts, dates and categories stay
          readable, so reports and dashboards can add them up.
        </p>
      ) : (
        <p className="mb-2 text-sm text-destructive">
          Purser could not read back its test value with the current key. Check that{" "}
          <code>PURSER_ENCRYPTION_KEY</code> is the key this database was set up with.
        </p>
      )}
      <p className="mb-2 text-sm text-muted-foreground">
        Key saved by an administrator:{" "}
        {enc.key_acknowledged_at ? formatTimestamp(enc.key_acknowledged_at) : "not yet"}.
      </p>
      {!enc.key_acknowledged_at && <RecordKeyAction />}
      <details className="text-xs text-muted-foreground">
        <summary className="cursor-pointer">Technical detail</summary>
        <p className="mt-2">
          Key fingerprint <code className="rounded bg-muted px-1">{enc.key_fingerprint}</code> ·
          self-check (canary) {enc.canary_ok ? "passed" : "failed"}.
        </p>
        {enc.encrypted_scopes.length > 0 && (
          <p className="mt-2">Encrypted: {enc.encrypted_scopes.join(", ")}.</p>
        )}
        <p className="mt-2">
          Only the numeric skeleton — amounts, dates, statuses, category paths, ids — and the
          readmodel v2 views over it stay plaintext by design, because that is the
          direct-SQL/Grafana contract every invariant and aggregate computes on. Encrypting the
          database files themselves is a storage/deployment concern, not something Purser can
          attest from in here.
        </p>
      </details>
    </Card>
  );
}

function SessionsCard({ initial }: { initial: ConfigOut }) {
  const [ttlDays, setTtlDays] = useState(String(initial.values.session_ttl_days));
  const [idleHours, setIdleHours] = useState(String(initial.values.session_idle_hours));
  const [error, setError] = useState<string | null>(null);
  const save = useSaveConfig(setError);
  const id = useId();

  return (
    <Card>
      <CardTitle>Session lifetime</CardTitle>
      <p className="mb-4 text-sm text-muted-foreground">
        How long a browser sign-in lasts, for everyone. Each person sees and revokes their own
        sign-ins and agent tokens under <Link to="/settings/agents">Agents &amp; API</Link>.
      </p>
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        <div className="space-y-1">
          <Label htmlFor={`${id}-ttl`}>Session lifetime (days)</Label>
          <Input
            id={`${id}-ttl`}
            type="number"
            min={1}
            max={365}
            value={ttlDays}
            onChange={(e) => setTtlDays(e.target.value)}
          />
        </div>
        <div className="space-y-1">
          <Label htmlFor={`${id}-idle`}>Idle timeout (hours)</Label>
          <Input
            id={`${id}-idle`}
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

/** Settings -> Security (#542): the encryption key, how people sign in
 * (single sign-on) and how long a browser sign-in lasts. Admin-only. */
export default function Security() {
  const config = useQuery(configQuery);
  if (!config.data) return <QueryState query={config} what="settings" />;

  return (
    <div className="space-y-6">
      <EncryptionCard initial={config.data} />
      <SsoCard initial={config.data} />
      <SessionsCard initial={config.data} />
    </div>
  );
}
