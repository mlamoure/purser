import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, errorDetail } from "../../lib/api";
import { Button, Card, CardTitle, ErrorText, Input, Label } from "../../components/ui";
import type { components } from "../../api/schema";
import { formatDate } from "../../lib/dates";
import { useAccountNames } from "../../lib/names";

type RefetchOut = components["schemas"]["RefetchOut"];
type GapRepair = components["schemas"]["GapRepairOut"];

// One queryFn for the shared ["inventory"] key (the Connections page and Re-fetch read
// it): a failed read throws, so it shows as an error, not as "nothing synced".
export const inventoryQuery = {
  queryKey: ["inventory"],
  queryFn: async () => {
    const { data, error } = await api.GET("/connections/inventory");
    if (error) throw new Error(errorDetail(error));
    return data ?? [];
  },
};

function isoDaysAgo(days: number): string {
  const d = new Date();
  d.setDate(d.getDate() - days);
  return d.toISOString().slice(0, 10);
}

// Re-fetch a date range for some or all mapped accounts (#481, #489): for a gap
// a bank outage left that the automatic repair after a sync did not cover.
export function RefetchCard({ connectionId }: { connectionId: string }) {
  const qc = useQueryClient();
  const inventory = useQuery(inventoryQuery);
  const accountName = useAccountNames();
  const mapped = (inventory.data ?? [])
    .flatMap((c) => c.logins)
    .flatMap((l) => l.accounts)
    .filter((a) => a.connected && a.account)
    .map((a) => a.account as string);
  const [start, setStart] = useState(isoDaysAgo(30));
  const [end, setEnd] = useState(isoDaysAgo(0));
  const [chosen, setChosen] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<RefetchOut | null>(null);

  const run = useMutation({
    mutationFn: async () => {
      const { data, error: err } = await api.POST("/connections/{connection_id}/refetch", {
        params: { path: { connection_id: connectionId } },
        body: { start, end, ...(chosen.length ? { accounts: chosen } : {}) },
      });
      if (err) throw new Error(errorDetail(err));
      return data ?? null;
    },
    onSuccess: (data) => {
      setResult(data);
      qc.invalidateQueries({ queryKey: ["connections"] });
      qc.invalidateQueries({ queryKey: ["discovered"] });
      qc.invalidateQueries({ queryKey: ["inventory"] });
    },
    onError: (e) => setError(e.message),
  });

  return (
    <Card>
      <CardTitle>Re-fetch a date range</CardTitle>
      <p className="mb-4 text-sm text-muted-foreground">
        Fetches the range again and imports only what the ledger is missing. Use it for a gap a
        bank outage left. At most 90 days per run, and the bank serves about 90 days of history;
        each run uses some of the day&apos;s 24 fetches.
      </p>
      <div className="flex flex-wrap items-end gap-2">
        <div className="space-y-1">
          <Label htmlFor="refetch-start">From</Label>
          <Input id="refetch-start" type="date" value={start} onChange={(e) => setStart(e.target.value)} />
        </div>
        <div className="space-y-1">
          <Label htmlFor="refetch-end">To</Label>
          <Input id="refetch-end" type="date" value={end} onChange={(e) => setEnd(e.target.value)} />
        </div>
        <div className="space-y-1">
          <Label htmlFor="refetch-accounts">Accounts (none selected = all)</Label>
          <select
            id="refetch-accounts"
            multiple
            className="min-h-9 rounded-md border border-border bg-card px-2 text-sm"
            value={chosen}
            onChange={(e) => setChosen(Array.from(e.target.selectedOptions, (o) => o.value))}
          >
            {mapped.map((path) => (
              <option key={path} value={path}>
                {accountName(path)}
              </option>
            ))}
          </select>
        </div>
        <Button
          disabled={run.isPending || !start || !end}
          onClick={() => {
            setError(null);
            setResult(null);
            run.mutate();
          }}
        >
          Re-fetch
        </Button>
      </div>
      {result && (
        <p className="mt-2 text-sm text-muted-foreground">
          {formatDate(result.start)} to {formatDate(result.end)}: {result.imported} imported, {result.skipped} already
          in the ledger ({result.accounts.length} account{result.accounts.length === 1 ? "" : "s"}).
        </p>
      )}
      <ErrorText>{error}</ErrorText>
    </Card>
  );
}

// What a "Sync now" recovered from an outage, or why it could not yet (#481).
export function GapRepairNote({ repair }: { repair: GapRepair }) {
  const accounts = repair.accounts.join(", ");
  if (repair.deferred) {
    return (
      <p className="mt-2 text-sm text-warning">
        {accounts} came back from an outage; re-fetching {formatDate(repair.start)} to {formatDate(repair.end)} is
        postponed ({repair.deferred === "rate_budget" ? "today's fetch budget is spent" : "the fetch failed"})
        and runs on the next sync.
      </p>
    );
  }
  return (
    <p className="mt-2 text-sm text-muted-foreground">
      Recovered from an outage: re-fetched {formatDate(repair.start)} to {formatDate(repair.end)} for {accounts},{" "}
      {repair.imported} transaction{repair.imported === 1 ? "" : "s"} imported.
      {repair.unreachable_before
        ? ` Rows before ${formatDate(repair.unreachable_before)} are beyond the bank's reach; import them from a QIF or CSV file.`
        : ""}
    </p>
  );
}
