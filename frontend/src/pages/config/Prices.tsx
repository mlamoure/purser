import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, errorDetail } from "../../lib/api";
import { Badge, Button, Card, CardTitle, ErrorText, Input, Label, Table, Td, Th } from "../../components/ui";
import { fmtQuantity } from "../Positions";
import type { components } from "../../api/schema";

type ConfigOut = components["schemas"]["ConfigOut"];
type ConfigUpdate = components["schemas"]["ConfigUpdate"];
type FetchOut = components["schemas"]["FetchOut"];
type Provider = NonNullable<ConfigUpdate["price_provider"]>;

const selectClass = "h-9 w-full rounded-md border border-border bg-card px-2 text-sm";

/** The daily-close provider and its staleness threshold (issue #390,
 * decision 89). The API key is write-only: only its presence is shown. */
function ProviderCard({ initial }: { initial: ConfigOut }) {
  const qc = useQueryClient();
  const [provider, setProvider] = useState<Provider>(
    (initial.values.price_provider as Provider) ?? "none",
  );
  const [apiKey, setApiKey] = useState("");
  const [staleDays, setStaleDays] = useState(String(initial.values.price_stale_days));
  const [error, setError] = useState<string | null>(null);
  const save = useMutation({
    mutationFn: async (body: ConfigUpdate) => {
      const { error: err } = await api.PUT("/config", { body });
      if (err) throw new Error(errorDetail(err));
    },
    onSuccess: () => {
      setApiKey("");
      qc.invalidateQueries({ queryKey: ["config"] });
      qc.invalidateQueries({ queryKey: ["price-status"] });
    },
    onError: (e) => setError(e.message),
  });

  return (
    <Card>
      <CardTitle>Price provider</CardTitle>
      <p className="mb-4 text-sm text-muted-foreground">
        Daily closes for positions tracked by hand. The provider sees ticker symbols only, never
        quantities, accounts or values. With no provider, record closes by hand (or an agent
        does, with record_price); a feed snapshot of the same symbol also implies a price.
      </p>
      <div className="grid grid-cols-3 gap-4">
        <div className="space-y-1">
          <Label htmlFor="price-provider">Provider</Label>
          <select
            id="price-provider"
            className={selectClass}
            value={provider}
            onChange={(e) => setProvider(e.target.value as Provider)}
          >
            <option value="none">none (record closes by hand)</option>
            <option value="tiingo">Tiingo (end-of-day)</option>
          </select>
        </div>
        {provider !== "none" && (
          <div className="space-y-1">
            <Label htmlFor="price-key">
              API key{" "}
              <Badge variant={initial.price_api_key_set ? "green" : "default"}>
                {initial.price_api_key_set ? "set" : "not set"}
              </Badge>
            </Label>
            <Input
              id="price-key"
              type="password"
              autoComplete="off"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder={initial.price_api_key_set ? "enter a new key to replace" : "key"}
            />
          </div>
        )}
        <div className="space-y-1">
          <Label htmlFor="price-stale">Stale after (days)</Label>
          <Input
            id="price-stale"
            inputMode="numeric"
            value={staleDays}
            onChange={(e) => setStaleDays(e.target.value)}
          />
        </div>
      </div>
      <Button
        className="mt-4"
        disabled={save.isPending}
        onClick={() => {
          setError(null);
          // Digits only, then Number: "7.5", "10abc", "1e2", "0x10" and "+7"
          // are refused, never read as 7, 10, 100, 16 and 7.
          const typed = staleDays.trim();
          const days = /^\d+$/.test(typed) ? Number(typed) : Number.NaN;
          if (!Number.isInteger(days) || days < 1 || days > 365) {
            setError("Stale after must be a whole number of days, 1 to 365.");
            return;
          }
          const body: ConfigUpdate = { price_provider: provider, price_stale_days: days };
          if (provider !== "none" && apiKey.trim()) body.price_api_key = apiKey.trim();
          save.mutate(body);
        }}
      >
        Save
      </Button>
      {save.isSuccess && <p className="mt-2 text-sm text-emerald-700">Saved.</p>}
      <ErrorText>{error}</ErrorText>
    </Card>
  );
}

function StatusCard() {
  const qc = useQueryClient();
  const [result, setResult] = useState<FetchOut | null>(null);
  const [error, setError] = useState<string | null>(null);
  const status = useQuery({
    queryKey: ["price-status"],
    queryFn: async () => {
      const { data, error: err } = await api.GET("/prices/status");
      if (err) throw new Error(errorDetail(err));
      return data;
    },
  });
  const fetchNow = useMutation({
    mutationFn: async () => {
      const { data, error: err } = await api.POST("/prices/fetch");
      if (err) throw new Error(errorDetail(err));
      return data;
    },
    onSuccess: (data) => {
      setResult(data ?? null);
      setError(null);
      qc.invalidateQueries({ queryKey: ["price-status"] });
      qc.invalidateQueries({ queryKey: ["positions"] });
    },
    onError: (e) => setError(e.message),
  });
  const s = status.data;

  return (
    <Card>
      <div className="mb-4 flex items-center justify-between gap-4">
        <CardTitle className="mb-0">Last run</CardTitle>
        <Button size="sm" variant="outline" disabled={fetchNow.isPending} onClick={() => fetchNow.mutate()}>
          {fetchNow.isPending ? "Fetching…" : "Fetch now"}
        </Button>
      </div>
      {s && (
        <div className="space-y-1 text-sm text-muted-foreground">
          <p>
            Newest provider close stored:{" "}
            {s.last_fetched_at ? new Date(s.last_fetched_at).toLocaleString() : "never"}. The job
            runs daily at 06:40 UTC.
          </p>
          {s.last_failure && (
            <p className="text-destructive">
              Last failure: {s.last_failure.symbol ?? "?"}
              {failureDetail(s.last_failure.status, s.last_failure.reason)} on{" "}
              {new Date(s.last_failure.at).toLocaleString()}.
            </p>
          )}
        </div>
      )}
      {result && (
        <p className="mt-2 text-sm" role="status">
          {result.skipped
            ? `Nothing fetched: ${result.skipped}.`
            : `Fetched ${result.fetched} close(s) for ${result.symbols} symbol(s).`}
          {result.failed.length > 0 &&
            ` ${result.failed.length} symbol(s) failed: ${result.failed
              .map((f) => `${f.symbol}${failureDetail(f.status, f.reason)}`)
              .join(", ")}.`}{" "}
          Re-valued {result.accounts_projected} account(s).
        </p>
      )}
      {s && s.symbols.length > 0 && (
        <Table className="mt-4">
          <thead>
            <tr>
              <Th>Symbol</Th>
              <Th className="text-right">Close</Th>
              <Th>As of</Th>
              <Th>Source</Th>
            </tr>
          </thead>
          <tbody>
            {s.symbols.map((row) => (
              <tr key={row.symbol}>
                <Td className="font-mono">{row.symbol}</Td>
                <Td className="text-right font-mono">{fmtQuantity(row.close)}</Td>
                <Td>
                  {row.price_as_of ?? "—"}
                  {row.stale && (
                    <Badge variant="amber" className="ml-2">
                      {row.close == null ? "no price" : "stale"}
                    </Badge>
                  )}
                </Td>
                <Td className="text-muted-foreground">{row.source ?? "—"}</Td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
      {s && s.symbols.length === 0 && (
        <p className="mt-2 text-sm text-muted-foreground">No open hand-tracked positions.</p>
      )}
      <ErrorText>{error ?? (status.error ? status.error.message : null)}</ErrorText>
    </Card>
  );
}


/** " (HTTP 404, not found)": the status and the reason a fetch failed. */
function failureDetail(status: number | null | undefined, reason: string | null | undefined) {
  const parts = [status != null ? `HTTP ${status}` : null, reason ?? null].filter(Boolean);
  return parts.length ? ` (${parts.join(", ")})` : "";
}
export default function Prices() {
  const config = useQuery({
    queryKey: ["config"],
    queryFn: async () => (await api.GET("/config")).data ?? null,
  });
  if (config.isLoading) return <p className="text-sm text-muted-foreground">Loading…</p>;
  if (!config.data) return <ErrorText>Failed to load config.</ErrorText>;
  return (
    <div className="space-y-6">
      <ProviderCard initial={config.data} />
      <StatusCard />
    </div>
  );
}
