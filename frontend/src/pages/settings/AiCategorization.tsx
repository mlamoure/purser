import { useId, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { api, errorDetail } from "../../lib/api";
import { configQuery } from "../../lib/config";
import type { components } from "../../api/schema";
import { QueryState } from "../../components/QueryState";
import { Badge, Button, Card, CardTitle, ErrorText, Input, Label } from "../../components/ui";
import { Saved, selectClass, useSaveConfig, type ConfigOut, type ConfigUpdate } from "./saveConfig";

type AiProvider = NonNullable<ConfigUpdate["ai_provider"]>;
type AiCheckIn = components["schemas"]["AiCheckIn"];
type AiModelsOut = components["schemas"]["AiModelsOut"];
type AiTestOut = components["schemas"]["AiTestOut"];

/** The env default of ai_base_url (purser/config.py): right for a hosted
 * OpenAI-compatible service, never a local model's address (#640). */
export const CLOUD_DEFAULT_BASE_URL = "https://api.openai.com/v1";
const LOCAL_PLACEHOLDER = "http://localhost:11434/v1";
const TIMEOUT_MIN = 5;
const TIMEOUT_MAX = 600;

const PROVIDER_LABELS: Record<AiProvider, string> = {
  none: "none (off)",
  local: "Local model (OpenAI-compatible)",
  openai: "Hosted OpenAI-compatible service",
  anthropic: "Anthropic",
};

const trimUrl = (url: string) => url.trim().replace(/\/+$/, "");
const isCloudDefault = (url: string) => trimUrl(url) === CLOUD_DEFAULT_BASE_URL;

/** Where the data goes and what that means, per provider: the plain answer
 * to "does this leave my house?" (#640). */
function destination(provider: AiProvider): { to: string; after: string } {
  if (provider === "local")
    return {
      to: "the server at this address",
      after: "With a model on your own hardware, this stays on your network.",
    };
  if (provider === "anthropic")
    return { to: "Anthropic", after: "This leaves your network, and Anthropic's terms apply." };
  return {
    to: "the service at this URL",
    after: "With a hosted service, this leaves your network and that provider's terms apply.",
  };
}

function Disclosure({ provider, initial }: { provider: AiProvider; initial: ConfigOut }) {
  const { to, after } = destination(provider);
  return (
    <section aria-label="What Purser sends" className="mt-4 rounded-md border border-border p-3">
      <h3 className="mb-1 text-sm font-medium">What Purser sends</h3>
      {initial.ai_disclosure.map((d) => (
        <div key={d.capability} className="mb-2 text-sm text-muted-foreground">
          <p>
            {d.when}, Purser sends {to}:
          </p>
          <ul className="list-disc pl-5">
            {d.sends.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </div>
      ))}
      <p className="text-sm text-muted-foreground">
        Each request carries one transaction and nothing else: no balances, no other
        transactions, and never your bank login. {after}
      </p>
    </section>
  );
}

function parseTimeout(typed: string): number | null {
  const t = typed.trim();
  const n = /^\d+$/.test(t) ? Number(t) : Number.NaN;
  return Number.isInteger(n) && n >= TIMEOUT_MIN && n <= TIMEOUT_MAX ? n : null;
}

/** Settings -> Categories -> AI categorization (#694), an admin-only tab:
 * everything in it is GET/PUT /config. */
export default function AiCategorization() {
  const config = useQuery(configQuery);
  if (!config.data) return <QueryState query={config} what="the AI categorization settings" />;
  return (
    <div className="space-y-6">
      <AiCard initial={config.data} />
      <MinConfidenceCard initial={config.data} />
    </div>
  );
}

/** A stored confidence (0 to 1) as the percentage people read, to a tenth. */
export function confidencePercent(value: number): string {
  return String(Math.round(value * 1000) / 10);
}

/** A typed percentage as the stored 0..1 value, or null when it is not a
 * number from 0 to 100 with at most one decimal place. */
export function parseConfidencePercent(typed: string): number | null {
  const t = typed.trim().replace(/%$/, "").trim();
  if (!/^\d{1,3}(\.\d)?$/.test(t)) return null;
  const percent = Number(t);
  return percent <= 100 ? Math.round(percent * 10) / 1000 : null;
}

/** `ai_min_confidence` (decision 141): how sure the model must be before its
 * category is applied. Shown and typed as a percentage, stored 0 to 1. */
function MinConfidenceCard({ initial }: { initial: ConfigOut }) {
  const [typed, setTyped] = useState(confidencePercent(initial.values.ai_min_confidence));
  const [error, setError] = useState<string | null>(null);
  const save = useSaveConfig(setError);
  const id = useId();

  return (
    <Card>
      <CardTitle>Minimum confidence</CardTitle>
      <p className="mb-4 text-sm text-muted-foreground">
        A guess below this is not applied: the transaction stays uncategorized, so it is flagged
        for review.
      </p>
      <div className="flex flex-wrap items-end gap-2">
        <div className="space-y-1">
          <Label htmlFor={`${id}-floor`}>Minimum confidence</Label>
          <div className="flex items-center gap-1">
            <Input
              id={`${id}-floor`}
              className="w-24"
              inputMode="decimal"
              aria-describedby={`${id}-floor-help`}
              value={typed}
              onChange={(e) => setTyped(e.target.value)}
            />
            <span className="text-sm text-muted-foreground">%</span>
          </div>
        </div>
        <Button
          disabled={save.isPending}
          onClick={() => {
            setError(null);
            const value = parseConfidencePercent(typed);
            if (value === null) {
              setError("Minimum confidence must be a percentage from 0 to 100.");
              return;
            }
            save.mutate({ ai_min_confidence: value });
          }}
        >
          Save
        </Button>
      </div>
      <p id={`${id}-floor-help`} className="mt-1 text-xs text-muted-foreground">
        0 to 100 (70 by default). A guess that a transaction is a transfer to one of your
        accounts needs at least 85 whatever this says. Raise it when a small model guesses too
        freely.
        {initial.values.ai_provider === "none" &&
          " AI categorization is off, so this applies once you choose a provider."}
      </p>
      <Saved visible={save.isSuccess} />
      <ErrorText>{error}</ErrorText>
    </Card>
  );
}

/** The optional provider that categorizes new transactions, on Settings ->
 * Categories' AI categorization tab (#542, #694): it decides how new rows
 * get the categories the section manages. A local model is its own choice (#640,
 * decision 133): no cloud default, an optional key, a model list from the
 * server, a connection test and wording that says the data stays home. */
export function AiCard({ initial }: { initial: ConfigOut }) {
  const [provider, setProvider] = useState<AiProvider>(
    (initial.values.ai_provider as AiProvider) ?? "none",
  );
  const [model, setModel] = useState(initial.values.ai_model ?? "");
  // The hosted default is shown only to a hosted choice, so a fresh install
  // that picks a local model starts from an empty, required field.
  const [baseUrl, setBaseUrl] = useState(
    isCloudDefault(initial.values.ai_base_url) && initial.values.ai_provider !== "openai"
      ? ""
      : initial.values.ai_base_url,
  );
  const [apiKey, setApiKey] = useState("");
  const [timeout, setTimeoutText] = useState(String(initial.values.ai_timeout_seconds));
  const [models, setModels] = useState<AiModelsOut | null>(null);
  const [result, setResult] = useState<AiTestOut | null>(null);
  const [error, setError] = useState<string | null>(null);
  const save = useSaveConfig(setError);
  const clearKey = useSaveConfig(setError);
  const id = useId();

  const usesBaseUrl = provider === "local" || provider === "openai";
  const keyOptional = provider === "local";
  // The stored key goes only to the saved provider and address (#667): a
  // check of anything else in the form sends the typed key or none.
  const savedDestination =
    provider === initial.values.ai_provider &&
    (!usesBaseUrl || trimUrl(baseUrl) === trimUrl(initial.values.ai_base_url));
  const storedKeyHeld = initial.ai_api_key_set && !savedDestination && !apiKey.trim();

  const choose = (next: AiProvider) => {
    setProvider(next);
    setModels(null);
    setResult(null);
    // A local model never starts from the hosted default, and a hosted
    // service gets it back when the field is empty.
    if (next === "local" && isCloudDefault(baseUrl)) setBaseUrl("");
    if (next === "openai" && !baseUrl.trim()) setBaseUrl(CLOUD_DEFAULT_BASE_URL);
  };

  /** The form as a check request: nothing is saved by it. */
  const checkBody = (): AiCheckIn => {
    const body: AiCheckIn = {
      provider: provider as Exclude<AiProvider, "none">,
      use_stored_key: savedDestination,
    };
    if (usesBaseUrl) body.base_url = baseUrl.trim();
    if (model.trim()) body.model = model.trim();
    if (apiKey.trim()) body.api_key = apiKey.trim();
    const seconds = parseTimeout(timeout);
    if (seconds !== null) body.timeout_seconds = seconds;
    return body;
  };

  const formError = (): string | null => {
    if (provider === "local" && !baseUrl.trim())
      return "A local model needs its server's base URL, for example " + LOCAL_PLACEHOLDER + ".";
    if (provider !== "none" && parseTimeout(timeout) === null)
      return `Timeout must be a whole number of seconds, ${TIMEOUT_MIN} to ${TIMEOUT_MAX}.`;
    return null;
  };

  const findModels = useMutation({
    mutationFn: async () => {
      const { data, error: err } = await api.POST("/config/ai/models", { body: checkBody() });
      if (err) throw new Error(errorDetail(err));
      return data as AiModelsOut;
    },
    onSuccess: setModels,
    onError: (e) => setError(e.message),
  });
  const test = useMutation({
    mutationFn: async () => {
      const { data, error: err } = await api.POST("/config/ai/test", { body: checkBody() });
      if (err) throw new Error(errorDetail(err));
      return data as AiTestOut;
    },
    onSuccess: setResult,
    onError: (e) => setError(e.message),
  });

  const runCheck = (check: { mutate: () => void }) => {
    setError(null);
    const problem = formError();
    if (problem) {
      setError(problem);
      return;
    }
    check.mutate();
  };

  return (
    <Card>
      <CardTitle>AI categorization</CardTitle>
      <p className="mb-4 text-sm text-muted-foreground">
        Optional: a model categorizes new transactions that no rule or earlier choice covers.
        Purser is fully functional with this off. A local model keeps your transactions on your
        own hardware.
      </p>
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        <div className="space-y-1">
          <Label htmlFor={`${id}-provider`}>Provider</Label>
          <select
            id={`${id}-provider`}
            className={selectClass}
            value={provider}
            onChange={(e) => choose(e.target.value as AiProvider)}
          >
            {(["none", "local", "openai", "anthropic"] as const).map((p) => (
              <option key={p} value={p}>
                {PROVIDER_LABELS[p]}
              </option>
            ))}
          </select>
        </div>
        {usesBaseUrl && (
          <div className="space-y-1">
            <Label htmlFor={`${id}-base-url`}>Base URL</Label>
            <Input
              id={`${id}-base-url`}
              type="url"
              required={provider === "local"}
              aria-describedby={`${id}-base-url-help`}
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
              placeholder={provider === "local" ? LOCAL_PLACEHOLDER : CLOUD_DEFAULT_BASE_URL}
            />
            <p id={`${id}-base-url-help`} className="text-xs text-muted-foreground">
              {provider === "local"
                ? "Your server's OpenAI-compatible address, usually ending in /v1. If Purser runs in Docker, localhost is the Purser container: use host.docker.internal or the host's address."
                : "The service's OpenAI-compatible address."}
            </p>
          </div>
        )}
        {provider !== "none" && (
          <>
            <div className="space-y-1">
              <div className="flex items-center gap-2">
                <Label htmlFor={`${id}-api-key`}>{keyOptional ? "API key (optional)" : "API key"}</Label>
                <Badge variant={initial.ai_api_key_set ? "green" : "default"}>
                  {initial.ai_api_key_set ? "set" : "not set"}
                </Badge>
                {initial.ai_api_key_set && (
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={clearKey.isPending}
                    onClick={() => {
                      setError(null);
                      clearKey.mutate({ ai_api_key: null });
                    }}
                  >
                    Clear key
                  </Button>
                )}
              </div>
              <Input
                id={`${id}-api-key`}
                type="password"
                autoComplete="off"
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                placeholder={
                  initial.ai_api_key_set
                    ? "enter a new key to replace"
                    : keyOptional
                      ? "leave empty if your server needs none"
                      : "your provider's key"
                }
              />
              {keyOptional && initial.ai_api_key_set && (
                <p className="text-xs text-muted-foreground">
                  {savedDestination
                    ? "A key is stored, and Purser sends it to this server."
                    : "A key is stored, and saving sends it to this server."}{" "}
                  Clear it if your server needs none.
                </p>
              )}
              {storedKeyHeld && (
                <p className="text-xs text-muted-foreground">
                  Find models and Test connection send the stored key only to the saved provider
                  and address, so they check this one without a key. Enter its key to check with
                  one.
                </p>
              )}
              {clearKey.isSuccess && !initial.ai_api_key_set && (
                <p className="text-xs text-success">Key cleared.</p>
              )}
            </div>
            <div className="space-y-1">
              <Label htmlFor={`${id}-model`}>Model</Label>
              <div className="flex gap-2">
                <Input
                  id={`${id}-model`}
                  list={`${id}-models`}
                  value={model}
                  onChange={(e) => setModel(e.target.value)}
                  placeholder={provider === "local" ? "e.g. llama3.1:8b" : "e.g. gpt-4.1-mini"}
                />
                <Button
                  variant="outline"
                  disabled={findModels.isPending}
                  onClick={() => runCheck(findModels)}
                >
                  Find models
                </Button>
              </div>
              <datalist id={`${id}-models`}>
                {(models?.models ?? []).map((m) => (
                  <option key={m} value={m} />
                ))}
              </datalist>
              {models && (
                <p role="status" className="text-xs text-muted-foreground">
                  {models.listed && models.models.length > 0
                    ? `${models.message} Pick one from the Model field's list or type a name.`
                    : models.message}
                </p>
              )}
            </div>
            <div className="space-y-1">
              <Label htmlFor={`${id}-timeout`}>Timeout (seconds)</Label>
              <Input
                id={`${id}-timeout`}
                inputMode="numeric"
                aria-describedby={`${id}-timeout-help`}
                value={timeout}
                onChange={(e) => setTimeoutText(e.target.value)}
              />
              <p id={`${id}-timeout-help`} className="text-xs text-muted-foreground">
                How long to wait for one answer. A model on modest hardware can need 120 or more.
              </p>
            </div>
          </>
        )}
      </div>
      {provider !== "none" && <Disclosure provider={provider} initial={initial} />}
      <div className="mt-4 flex flex-wrap gap-2">
        <Button
          disabled={save.isPending}
          onClick={() => {
            setError(null);
            const problem = formError();
            if (problem) {
              setError(problem);
              return;
            }
            const body: ConfigUpdate = { ai_provider: provider };
            if (provider !== "none") {
              body.ai_model = model.trim() || null;
              body.ai_timeout_seconds = parseTimeout(timeout) ?? undefined;
              if (usesBaseUrl) body.ai_base_url = baseUrl.trim();
              if (apiKey.trim()) body.ai_api_key = apiKey.trim();
            }
            save.mutate(body, { onSuccess: () => setApiKey("") });
          }}
        >
          Save
        </Button>
        {provider !== "none" && (
          <Button variant="outline" disabled={test.isPending} onClick={() => runCheck(test)}>
            {test.isPending ? "Testing…" : "Test connection"}
          </Button>
        )}
      </div>
      <Saved visible={save.isSuccess} />
      {result && provider !== "none" && (
        <div role="status" className="mt-2 space-y-1 text-sm">
          <p className={result.ok ? "text-success" : "text-destructive"}>{result.message}</p>
          {result.warning && <p className="text-warning">{result.warning}</p>}
        </div>
      )}
      <ErrorText>{error}</ErrorText>
    </Card>
  );
}
