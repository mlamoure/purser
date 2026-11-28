import { useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, errorDetail } from "../../lib/api";
import { configQuery } from "../../lib/config";
import { useIsAdmin } from "../../lib/useIsAdmin";
import { useFormatTimestamp } from "../../lib/clock";
import { QueryState } from "../../components/QueryState";
import { Badge, Button, Card, CardTitle, ErrorText, Input, Label, Table, Td, Th } from "../../components/ui";
import type { components } from "../../api/schema";

type ConfigOut = components["schemas"]["ConfigOut"];
type ConfigUpdate = components["schemas"]["ConfigUpdate"];
type FetchOut = components["schemas"]["FetchOut"];
type PriceTestOut = components["schemas"]["PriceTestOut"];
type Provider = NonNullable<ConfigUpdate["price_provider"]>;

const selectClass = "h-9 w-full rounded-md border border-border bg-card px-2 text-sm";

/** POST /prices/test (#682): a key being entered, or the stored one when
 * the body is empty. The answer carries a status and a fixed reason only. */
async function testProvider(body: components["schemas"]["PriceTestIn"]): Promise<PriceTestOut> {
  const { data, error: err } = await api.POST("/prices/test", { body });
  if (err || !data) throw new Error(errorDetail(err));
  return data;
}

/** "the provider refused it (HTTP 401)" for a failed test. */
function testFailure(t: PriceTestOut) {
  return `${t.reason ?? "failed"}${t.status != null ? ` (HTTP ${t.status})` : ""}`;
}

/** What each provider covers, shown under the choice (#686). */
const PROVIDERS: { value: Provider; label: string; help: string }[] = [
  { value: "none", label: "None", help: "" },
  {
    value: "tiingo",
    label: "Tiingo (end-of-day)",
    help: "US stocks, ETFs and mutual funds. A free account and API key at tiingo.com.",
  },
  {
    value: "eodhd",
    label: "EODHD (end-of-day)",
    help:
      "Exchanges worldwide (e.g. VOD.LSE, SHOP.TO) and US mutual funds. A free key allows 20 requests a day and one year of history; a paid plan lifts both. London prices are in pence.",
  },
];

/** The provider's key field: write-only, so only whether one is set shows. */
function KeyField({
  id,
  label,
  isSet,
  value,
  onChange,
}: {
  id: string;
  label: string;
  isSet: boolean;
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <div className="space-y-1">
      <Label htmlFor={id}>
        {label}{" "}
        <Badge variant={isSet ? "green" : "default"}>{isSet ? "set" : "not set"}</Badge>
      </Label>
      <Input
        id={id}
        type="password"
        autoComplete="off"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={isSet ? "enter a new key to replace" : "key"}
      />
    </div>
  );
}

/** The daily-close provider, a fallback for what it cannot answer (#686),
 * and the staleness threshold (issue #390, decision 89). Keys are
 * write-only: only their presence is shown, and a new one is tested before
 * it is saved (#682). */
function ProviderCard({ initial }: { initial: ConfigOut }) {
  const qc = useQueryClient();
  const [provider, setProvider] = useState<Provider>(
    (initial.values.price_provider as Provider) ?? "none",
  );
  const [fallback, setFallback] = useState<Provider>(
    (initial.values.price_fallback_provider as Provider) ?? "none",
  );
  const [apiKey, setApiKey] = useState("");
  const [fallbackKey, setFallbackKey] = useState("");
  const [staleDays, setStaleDays] = useState(String(initial.values.price_stale_days));
  const [error, setError] = useState<string | null>(null);
  const [tested, setTested] = useState<string | null>(null);
  const save = useMutation({
    mutationFn: async (body: ConfigUpdate) => {
      // A new key is tested first: a refused key is never saved (#682).
      const checks: [Provider | null | undefined, string | null | undefined, string][] = [
        [body.price_provider, body.price_api_key, "The key"],
        [body.price_fallback_provider, body.price_fallback_api_key, "The fallback key"],
      ];
      for (const [name, key, what] of checks) {
        if (!key || !name || name === "none") continue;
        const t = await testProvider({ provider: name, api_key: key });
        if (!t.ok) throw new Error(`${what} was not saved: the test failed, ${testFailure(t)}.`);
      }
      const { error: err } = await api.PUT("/config", { body });
      if (err) throw new Error(errorDetail(err));
    },
    onSuccess: () => {
      setApiKey("");
      setFallbackKey("");
      qc.invalidateQueries({ queryKey: ["config"] });
      qc.invalidateQueries({ queryKey: ["price-status"] });
    },
    onError: (e) => setError(e.message),
  });
  const test = useMutation({
    mutationFn: (name: Exclude<Provider, "none">) => testProvider({ provider: name }),
    onSuccess: (t) => {
      setError(null);
      setTested(t.ok ? `Connection OK: ${t.provider} answered with the saved key.` : null);
      if (!t.ok) setError(`Connection test failed: ${testFailure(t)}.`);
    },
    onError: (e) => {
      setTested(null);
      setError(e.message);
    },
  });
  const help = (name: Provider) => PROVIDERS.find((p) => p.value === name)?.help;
  const savedProvider = initial.values.price_provider as Provider;
  const savedFallback = (initial.values.price_fallback_provider ?? "none") as Provider;

  return (
    <Card>
      <CardTitle>Price provider</CardTitle>
      <p className="mb-4 text-sm text-muted-foreground">
        The provider sees ticker symbols only, never share counts, accounts or values. If it
        doesn&apos;t carry a symbol, the fallback provider is asked.
      </p>
      <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
        <div className="space-y-1">
          <Label htmlFor="price-provider">Provider</Label>
          <select
            id="price-provider"
            className={selectClass}
            value={provider}
            onChange={(e) => {
              const next = e.target.value as Provider;
              setProvider(next);
              if (next === fallback) setFallback("none");
            }}
          >
            {PROVIDERS.map((p) => (
              <option key={p.value} value={p.value}>
                {p.label}
              </option>
            ))}
          </select>
          {help(provider) && <p className="text-xs text-muted-foreground">{help(provider)}</p>}
        </div>
        {provider !== "none" && (
          <KeyField
            id="price-key"
            label="API key"
            isSet={initial.price_api_key_set && provider === savedProvider}
            value={apiKey}
            onChange={setApiKey}
          />
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
        {provider !== "none" && (
          <div className="space-y-1">
            <Label htmlFor="price-fallback">Fallback provider</Label>
            <select
              id="price-fallback"
              className={selectClass}
              value={fallback}
              onChange={(e) => setFallback(e.target.value as Provider)}
            >
              {PROVIDERS.filter((p) => p.value !== provider).map((p) => (
                <option key={p.value} value={p.value}>
                  {p.value === "none" ? "none" : p.label}
                </option>
              ))}
            </select>
            {fallback !== "none" && help(fallback) && (
              <p className="text-xs text-muted-foreground">{help(fallback)}</p>
            )}
          </div>
        )}
        {provider !== "none" && fallback !== "none" && (
          <KeyField
            id="price-fallback-key"
            label="Fallback API key"
            isSet={!!initial.price_fallback_api_key_set && fallback === savedFallback}
            value={fallbackKey}
            onChange={setFallbackKey}
          />
        )}
      </div>
      <div className="mt-4 flex flex-wrap gap-2">
        <Button
          disabled={save.isPending}
          onClick={() => {
            setError(null);
            setTested(null);
            // Digits only, then Number: "7.5", "10abc", "1e2", "0x10" and "+7"
            // are refused, never read as 7, 10, 100, 16 and 7.
            const typed = staleDays.trim();
            const days = /^\d+$/.test(typed) ? Number(typed) : Number.NaN;
            if (!Number.isInteger(days) || days < 1 || days > 365) {
              setError("Stale after must be a whole number of days, 1 to 365.");
              return;
            }
            const chosenFallback = provider === "none" ? "none" : fallback;
            const body: ConfigUpdate = { price_provider: provider, price_stale_days: days };
            if (chosenFallback !== savedFallback) body.price_fallback_provider = chosenFallback;
            if (provider !== "none" && apiKey.trim()) body.price_api_key = apiKey.trim();
            if (chosenFallback !== "none" && fallbackKey.trim())
              body.price_fallback_api_key = fallbackKey.trim();
            save.mutate(body);
          }}
        >
          {save.isPending ? "Saving…" : "Save"}
        </Button>
        {savedProvider !== "none" && initial.price_api_key_set && (
          <Button variant="outline" disabled={test.isPending} onClick={() => test.mutate(savedProvider)}>
            {test.isPending ? "Testing…" : "Test connection"}
          </Button>
        )}
        {savedFallback !== "none" && initial.price_fallback_api_key_set && (
          <Button variant="outline" disabled={test.isPending} onClick={() => test.mutate(savedFallback)}>
            Test fallback
          </Button>
        )}
      </div>
      {save.isSuccess && <p className="mt-2 text-sm text-success">Saved.</p>}
      {tested && (
        <p className="mt-2 text-sm text-success" role="status">
          {tested}
        </p>
      )}
      <ErrorText>{error}</ErrorText>
    </Card>
  );
}

/** GET /prices/status (read scope), shared by the summary and the status card. */
const priceStatusQuery = {
  queryKey: ["price-status"],
  queryFn: async () => {
    const { data, error: err } = await api.GET("/prices/status");
    if (err) throw new Error(errorDetail(err));
    return data;
  },
};

function StatusCard({ canFetch }: { canFetch: boolean }) {
  const qc = useQueryClient();
  const [result, setResult] = useState<FetchOut | null>(null);
  const [error, setError] = useState<string | null>(null);
  const status = useQuery(priceStatusQuery);
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
  const formatTimestamp = useFormatTimestamp();

  return (
    <Card>
      <div className="mb-4 flex items-center justify-between gap-4">
        <CardTitle className="mb-0">Last run</CardTitle>
        {canFetch && (
          <Button size="sm" variant="outline" disabled={fetchNow.isPending} onClick={() => fetchNow.mutate()}>
            {fetchNow.isPending ? "Fetching…" : "Fetch now"}
          </Button>
        )}
      </div>
      {s && (
        <div className="space-y-1 text-sm text-muted-foreground">
          <p>
            Last run: {s.last_run_at ? formatTimestamp(s.last_run_at) : "never"}.
            {s.next_run_at && <> Next run: {formatTimestamp(s.next_run_at)}.</>}
          </p>
          <p>
            Latest price fetched:{" "}
            {s.last_fetched_at ? formatTimestamp(s.last_fetched_at) : "none yet"}.
          </p>
          {s.last_failure && (
            <p className="text-destructive">
              Last failure: {s.last_failure.symbol ?? "?"}
              {failureDetail(s.last_failure.status, s.last_failure.reason)} on{" "}
              {formatTimestamp(s.last_failure.at)}.
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
      {s && <SymbolSummary symbols={s.symbols} />}
      <QueryState query={status} what="the price status" />
      <ErrorText>{error}</ErrorText>
    </Card>
  );
}


type SymbolPrice = components["schemas"]["SymbolPriceOut"];

/** The symbols Purser prices, in one line (#684): how many, and which are
 * stale or have no price. Each holding, with its price and where it came
 * from, is on the Holdings page. */
function SymbolSummary({ symbols }: { symbols: SymbolPrice[] }) {
  if (symbols.length === 0) {
    return <p className="mt-2 text-sm text-muted-foreground">Purser prices no symbol today.</p>;
  }
  const noPrice = symbols.filter((r) => r.close == null).map((r) => r.symbol);
  const stale = symbols.filter((r) => r.stale && r.close != null).map((r) => r.symbol);
  return (
    <p className="mt-2 text-sm text-muted-foreground" role="note">
      Purser prices {symbols.length} symbol{symbols.length === 1 ? "" : "s"}
      {stale.length === 0 && noPrice.length === 0 && ", all current"}.
      {stale.length > 0 && <> Stale: <span className="font-mono">{stale.join(", ")}</span>.</>}
      {noPrice.length > 0 && <> No price: <span className="font-mono">{noPrice.join(", ")}</span>.</>}{" "}
      Each holding and where its price comes from is on the{" "}
      <Link to="/holdings" className="underline">
        Holdings
      </Link>{" "}
      page.
    </p>
  );
}

/** " (HTTP 404, not found)": the status and the reason a fetch failed. */
function failureDetail(status: number | null | undefined, reason: string | null | undefined) {
  const parts = [status != null ? `HTTP ${status}` : null, reason ?? null].filter(Boolean);
  return parts.length ? ` (${parts.join(", ")})` : "";
}
/** What this page prices (#678, #689), in one sentence. Shown to every
 * session, above the admin-only provider card. */
function Scope() {
  return (
    <p className="text-sm text-muted-foreground" role="note">
      Market data prices the holdings of every account that isn&apos;t connected through SimpleFIN.
      SimpleFIN accounts are valued by your bank.
    </p>
  );
}

type SymbolSetting = components["schemas"]["SymbolSettingOut"];

const symbolSettingsQuery = {
  queryKey: ["price-symbols"],
  queryFn: async (): Promise<SymbolSetting[]> => {
    const { data, error: err } = await api.GET("/prices/symbols");
    if (err || !data) throw new Error(errorDetail(err));
    return data;
  },
};

/** Per-symbol price settings (#686): the symbol's ticker at each provider
 * (a London listing at EODHD is VOD.LSE, say), and symbols never sent to a
 * provider, whose prices are entered by hand (a plan-only fund). Write
 * scope. */
function SymbolSettingsCard() {
  const qc = useQueryClient();
  const settings = useQuery(symbolSettingsQuery);
  const [symbol, setSymbol] = useState("");
  const [tiingo, setTiingo] = useState("");
  const [eodhd, setEodhd] = useState("");
  const [skip, setSkip] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const done = () => {
    setError(null);
    qc.invalidateQueries({ queryKey: ["price-symbols"] });
    qc.invalidateQueries({ queryKey: ["price-status"] });
  };
  const save = useMutation({
    mutationFn: async () => {
      const { data, error: err } = await api.PUT("/prices/symbols/{symbol}", {
        params: { path: { symbol: symbol.trim() } },
        body: { skip_providers: skip, aliases: { tiingo: tiingo.trim(), eodhd: eodhd.trim() } },
      });
      if (err) throw new Error(errorDetail(err));
      return data;
    },
    onSuccess: () => {
      setSymbol("");
      setTiingo("");
      setEodhd("");
      setSkip(false);
      done();
    },
    onError: (e) => setError(e.message),
  });
  const remove = useMutation({
    mutationFn: async (target: string) => {
      const { error: err } = await api.DELETE("/prices/symbols/{symbol}", {
        params: { path: { symbol: target } },
      });
      if (err) throw new Error(errorDetail(err));
    },
    onSuccess: done,
    onError: (e) => setError(e.message),
  });
  const edit = (row: SymbolSetting) => {
    setSymbol(row.symbol);
    setTiingo(row.aliases.tiingo ?? "");
    setEodhd(row.aliases.eodhd ?? "");
    setSkip(row.skip_providers);
  };

  return (
    <Card>
      <CardTitle>Symbol settings</CardTitle>
      <p className="mb-4 text-sm text-muted-foreground">
        Use this when a provider spells a ticker differently (EODHD lists Vodafone in London as{" "}
        <span className="font-mono">VOD.LSE</span>), or to stop Purser asking providers for a
        symbol none of them carry. Changing a ticker asks the provider again.
      </p>
      {settings.data && settings.data.length > 0 && (
        <Table className="mb-4">
          <thead>
            <tr>
              <Th>Symbol</Th>
              <Th>Tiingo ticker</Th>
              <Th>EODHD ticker</Th>
              <Th>Fetched</Th>
              <Th />
            </tr>
          </thead>
          <tbody>
            {settings.data.map((row) => (
              <tr key={row.symbol}>
                <Td className="font-mono">{row.symbol}</Td>
                <Td className="font-mono">{row.aliases.tiingo ?? "—"}</Td>
                <Td className="font-mono">{row.aliases.eodhd ?? "—"}</Td>
                <Td>{row.skip_providers ? "no" : "yes"}</Td>
                <Td className="text-right">
                  <Button size="sm" variant="outline" onClick={() => edit(row)}>
                    Edit
                  </Button>{" "}
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={remove.isPending}
                    onClick={() => remove.mutate(row.symbol)}
                    aria-label={`Remove ${row.symbol}`}
                  >
                    Remove
                  </Button>
                </Td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
      <div className="grid grid-cols-1 gap-4 md:grid-cols-4">
        <div className="space-y-1">
          <Label htmlFor="sym-symbol">Symbol</Label>
          <Input id="sym-symbol" value={symbol} onChange={(e) => setSymbol(e.target.value)} placeholder="VOD" />
        </div>
        <div className="space-y-1">
          <Label htmlFor="sym-tiingo">Tiingo ticker</Label>
          <Input id="sym-tiingo" value={tiingo} onChange={(e) => setTiingo(e.target.value)} placeholder="its own spelling" />
        </div>
        <div className="space-y-1">
          <Label htmlFor="sym-eodhd">EODHD ticker</Label>
          <Input id="sym-eodhd" value={eodhd} onChange={(e) => setEodhd(e.target.value)} placeholder="e.g. VOD.LSE" />
        </div>
        <label className="flex items-end gap-2 pb-2 text-sm">
          <input type="checkbox" checked={skip} onChange={(e) => setSkip(e.target.checked)} />
          <span>
            Don&apos;t fetch prices for this symbol
            <span className="block text-xs text-muted-foreground">You&apos;ll enter its prices yourself.</span>
          </span>
        </label>
      </div>
      <Button className="mt-4" disabled={save.isPending || !symbol.trim()} onClick={() => save.mutate()}>
        Save symbol
      </Button>
      <QueryState query={settings} what="the symbol settings" />
      <ErrorText>{error}</ErrorText>
    </Card>
  );
}

/** Settings -> Market data (#678; Prices before). The provider (GET/PUT /config) and Fetch now
 * (POST /prices/fetch) are admin-scoped; a member session sees the price
 * status (read scope) only, and never reads the config (#542). */
export default function MarketData() {
  const isAdmin = useIsAdmin();
  return (
    <div className="space-y-6">
      <Scope />
      {isAdmin && <Provider />}
      <StatusCard canFetch={isAdmin} />
      <SymbolSettingsCard />
    </div>
  );
}

function Provider() {
  const config = useQuery(configQuery);
  if (!config.data) return <QueryState query={config} what="settings" />;
  return <ProviderCard initial={config.data} />;
}
