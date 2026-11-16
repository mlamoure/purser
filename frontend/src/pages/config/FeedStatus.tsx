import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, errorDetail } from "../../lib/api";
import { Badge, Button, Card, CardTitle, ErrorText, Input, Label, Table, Td, Th } from "../../components/ui";
import type { components } from "../../api/schema";

type InventoryConnection = components["schemas"]["InventoryConnectionOut"];
type InventoryLogin = components["schemas"]["InventoryLoginOut"];
type RefetchOut = components["schemas"]["RefetchOut"];
type GapRepair = components["schemas"]["GapRepairOut"];

// Per bank login and per account, from the latest sync (#482, #489): which
// logins need the user, which accounts are not connected, stale or failing.
function loginStatus(login: InventoryLogin): { label: string; variant: "green" | "amber" | "red" } {
  if (!login.issue) return { label: "ok", variant: "green" };
  if (login.issue.code.startsWith("con.auth")) return { label: "needs login", variant: "red" };
  return { label: "error", variant: "amber" };
}

export function FeedHealth() {
  const inventory = useQuery({
    queryKey: ["inventory"],
    queryFn: async () => (await api.GET("/connections/inventory")).data ?? [],
  });
  const connections: InventoryConnection[] = inventory.data ?? [];
  const logins = connections.flatMap((c) => c.logins);
  if (logins.length === 0) return null; // nothing synced yet
  const staleDays = connections[0]?.stale_days;
  return (
    <Card>
      <CardTitle>Bank health</CardTitle>
      <p className="mb-4 text-sm text-muted-foreground">
        Every account SimpleFIN delivers, by bank login, from the latest sync. An account is
        stale when its bank balance is more than {staleDays} days old; a login that needs
        attention is fixed by logging in again in SimpleFIN Bridge.
      </p>
      <Table>
        <thead>
          <tr>
            <Th>Bank / account</Th>
            <Th>Status</Th>
            <Th>Ledger account</Th>
            <Th>Bank balance date</Th>
            <Th>Delivers</Th>
          </tr>
        </thead>
        <tbody>
          {logins.map((login, i) => {
            const status = loginStatus(login);
            return [
              <tr key={`login-${i}`} className="bg-muted/40">
                <Td className="font-medium">{login.institution ?? "Unknown bank"}</Td>
                <Td>
                  <Badge variant={status.variant}>{status.label}</Badge>
                </Td>
                <Td colSpan={3} className="text-xs text-muted-foreground">
                  {login.issue ? login.issue.message : `${login.accounts.length} account(s)`}
                </Td>
              </tr>,
              ...login.accounts.map((a, j) => (
                <tr key={`login-${i}-acct-${j}`}>
                  <Td className="pl-6">{a.name ?? "Unnamed account"}</Td>
                  <Td>
                    {a.issue ? (
                      <Badge variant="amber" title={a.issue.message}>
                        {a.issue.code}
                      </Badge>
                    ) : a.stale ? (
                      <Badge variant="amber">stale</Badge>
                    ) : a.ambiguous ? (
                      <Badge variant="amber">duplicate id</Badge>
                    ) : (
                      <span className="text-muted-foreground">—</span>
                    )}
                  </Td>
                  <Td>
                    {a.account ? (
                      <Badge variant={a.connected ? "green" : "default"}>
                        {a.account}
                        {a.closed ? " (closed)" : ""}
                      </Badge>
                    ) : (
                      <Badge variant="amber">not connected</Badge>
                    )}
                  </Td>
                  <Td>
                    {a.balance_date ?? "—"}
                    {a.days_since_balance != null ? (
                      <span className="text-xs text-muted-foreground">
                        {" "}
                        ({a.days_since_balance}d ago)
                      </span>
                    ) : null}
                  </Td>
                  <Td className="text-xs text-muted-foreground">
                    {a.transactions} txn · {a.holdings} holding{a.holdings === 1 ? "" : "s"}
                  </Td>
                </tr>
              )),
            ];
          })}
        </tbody>
      </Table>
    </Card>
  );
}

function isoDaysAgo(days: number): string {
  const d = new Date();
  d.setDate(d.getDate() - days);
  return d.toISOString().slice(0, 10);
}

// Re-fetch a date range for some or all mapped accounts (#481, #489): for a gap
// a bank outage left that the automatic repair after a sync did not cover.
export function RefetchCard({ connectionId }: { connectionId: string }) {
  const qc = useQueryClient();
  const inventory = useQuery({
    queryKey: ["inventory"],
    queryFn: async () => (await api.GET("/connections/inventory")).data ?? [],
  });
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
                {path}
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
          {result.start} to {result.end}: {result.imported} imported, {result.skipped} already
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
      <p className="mt-2 text-sm text-amber-800">
        {accounts} came back from an outage; re-fetching {repair.start} to {repair.end} is
        postponed ({repair.deferred === "rate_budget" ? "today's fetch budget is spent" : "the fetch failed"})
        and runs on the next sync.
      </p>
    );
  }
  return (
    <p className="mt-2 text-sm text-muted-foreground">
      Recovered from an outage: re-fetched {repair.start} to {repair.end} for {accounts},{" "}
      {repair.imported} transaction{repair.imported === 1 ? "" : "s"} imported.
      {repair.unreachable_before
        ? ` Rows before ${repair.unreachable_before} are beyond the bank's reach; import them from a QIF or CSV file.`
        : ""}
    </p>
  );
}
