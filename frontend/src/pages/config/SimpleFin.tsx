import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, errorDetail } from "../../lib/api";
import { fmtAmount } from "../../lib/utils";
import { Badge, Button, Card, CardTitle, ErrorText, Input, Label, Table, Td, Th } from "../../components/ui";
import type { components } from "../../api/schema";

type SyncSchedule = components["schemas"]["SyncSchedule"];
type ConfigOut = components["schemas"]["ConfigOut"];
type BackfillOut = components["schemas"]["BackfillOut"];
type DiscoveredOut = components["schemas"]["DiscoveredOut"];
type DriftBasis = NonNullable<DiscoveredOut["drift_basis"]>;

// The ledger basis an account's drift is measured on (issue #283): chosen per
// account from its sync history — some banks leave pending charges out of the
// balance they report — unless an admin pinned it (issue #307).
const DRIFT_BASIS: Record<DriftBasis, { label: string; hint: string }> = {
  pending_inclusive: {
    label: "incl. pending",
    hint: "Measured against the ledger balance including pending transactions.",
  },
  posted_only: {
    label: "posted only",
    hint: "This bank leaves pending charges out of its balance, so drift is measured against posted transactions only.",
  },
};

function providerBadge(provider: string): { label: string; variant: "green" | "default" } {
  if (provider === "simplefin") return { label: "SimpleFIN", variant: "default" };
  return { label: provider, variant: "default" };
}

const PRESETS: [SyncSchedule["preset"], string][] = [
  ["hourly", "Every hour"],
  ["every_6h", "Every 6 hours"],
  ["every_12h", "Every 12 hours"],
  ["daily", "Once daily at…"],
];

const selectClass = "h-9 rounded-md border border-border bg-card px-2 text-sm";

function pad(n: number): string {
  return String(n).padStart(2, "0");
}

function SyncScheduleCard({ config }: { config: ConfigOut }) {
  const qc = useQueryClient();
  const initial = config.values.sync_schedule;
  const [preset, setPreset] = useState<SyncSchedule["preset"]>(initial.preset);
  const [dailyTime, setDailyTime] = useState(
    `${pad(initial.hour ?? 6)}:${pad(initial.minute ?? 30)}`,
  );
  const [error, setError] = useState<string | null>(null);

  const save = useMutation({
    mutationFn: async () => {
      const schedule: SyncSchedule =
        preset === "daily"
          ? {
              preset,
              hour: Number(dailyTime.split(":")[0]),
              minute: Number(dailyTime.split(":")[1]),
            }
          : { preset };
      const { error: err } = await api.PUT("/config", { body: { sync_schedule: schedule } });
      if (err) throw new Error(errorDetail(err));
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ["config"] }),
    onError: (e) => setError(e.message),
  });

  return (
    <Card>
      <CardTitle>Sync schedule</CardTitle>
      <p className="mb-4 text-sm text-muted-foreground">
        How often Purser pulls new data from SimpleFIN. All times are UTC; the Bridge allows at
        most 24 fetches per day.
      </p>
      <div className="flex items-end gap-2">
        <div className="space-y-1">
          <Label>Frequency</Label>
          <select
            className={selectClass}
            value={preset}
            onChange={(e) => setPreset(e.target.value as SyncSchedule["preset"])}
          >
            {PRESETS.map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </div>
        {preset === "daily" && (
          <div className="space-y-1">
            <Label>Time (UTC)</Label>
            <Input type="time" value={dailyTime} onChange={(e) => setDailyTime(e.target.value)} />
          </div>
        )}
        <Button disabled={save.isPending} onClick={() => { setError(null); save.mutate(); }}>
          Save
        </Button>
      </div>
      {!config.sync_reschedule_live && (
        <p className="mt-2 text-xs text-muted-foreground">
          This instance runs its scheduler in a separate worker: changes apply when the worker
          restarts.
        </p>
      )}
      {save.isSuccess && <p className="mt-2 text-sm text-emerald-700">Saved.</p>}
      <ErrorText>{error}</ErrorText>
    </Card>
  );
}

const AUTO = "auto";

// The basis pin for one mapped account (issue #307): "Auto" lets the sync
// history decide (decision 64); a pin overrides it until cleared. Only rows the
// drift check runs on (ledger_balance set) get the control.
function BasisSelect({ row, connectionId }: { row: DiscoveredOut; connectionId: string }) {
  const qc = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const save = useMutation({
    mutationFn: async (next: DriftBasis | null) => {
      const { error: err } = await api.PATCH("/accounts/{account_id}", {
        params: { path: { account_id: row.mapped_account_id! } },
        body: { drift_basis_override: next },
      });
      if (err) throw new Error(errorDetail(err));
    },
    onSuccess: () => {
      setError(null);
      qc.invalidateQueries({ queryKey: ["discovered", connectionId] });
      qc.invalidateQueries({ queryKey: ["accounts"] });
    },
    onError: (e) => setError(e.message),
  });

  const learned = row.drift_basis_learned ?? row.drift_basis;
  const value = row.drift_basis_pinned && row.drift_basis ? row.drift_basis : AUTO;
  return (
    <div className="space-y-1">
      <select
        aria-label={`Drift basis for ${row.mapped_account_path ?? row.external_id}`}
        className={selectClass}
        value={value}
        disabled={save.isPending}
        onChange={(e) =>
          save.mutate(e.target.value === AUTO ? null : (e.target.value as DriftBasis))
        }
      >
        <option value={AUTO}>
          Auto{learned ? ` (learned: ${DRIFT_BASIS[learned].label})` : ""}
        </option>
        <option value="pending_inclusive">Pin incl. pending</option>
        <option value="posted_only">Pin posted only</option>
      </select>
      <ErrorText>{error}</ErrorText>
    </div>
  );
}

function FeedAccounts({ connectionId }: { connectionId: string }) {
  // Live reported-vs-ledger comparison (issue #149): recomputed on every read
  // from the latest archived payload — drift shows here immediately, while
  // the balance.drift event stream stays behind its emission gate.
  const discovered = useQuery({
    queryKey: ["discovered", connectionId],
    queryFn: async () =>
      (
        await api.GET("/connections/{connection_id}/discovered", {
          params: { path: { connection_id: connectionId } },
        })
      ).data ?? [],
  });

  const rows = discovered.data ?? [];
  if (rows.length === 0) return null; // no fetch archived yet (or endpoint error)
  return (
    <Card>
      <CardTitle>Feed accounts</CardTitle>
      <p className="mb-4 text-sm text-muted-foreground">
        What the bank reports vs the Purser ledger, from the latest sync. Drift is a warning,
        never a gate, measured on the basis each bank uses (some leave pending charges out of
        their balance). Purser learns that basis from each account&apos;s sync history; pin it
        when the history is short or misleading. Loans and property reconcile through
        valuations instead. Map new accounts from the setup wizard.
      </p>
      <Table>
        <thead>
          <tr>
            <Th>Bank account</Th>
            <Th>Feed balance</Th>
            <Th>Ledger balance</Th>
            <Th>Drift</Th>
            <Th>Basis</Th>
            <Th>Mapping</Th>
          </tr>
        </thead>
        <tbody>
          {rows.map((d) => (
            <tr key={d.external_id}>
              <Td>
                {d.name ?? d.external_id}
                {d.org ? <span className="text-muted-foreground"> · {d.org}</span> : null}
              </Td>
              <Td>{fmtAmount(d.balance)}</Td>
              <Td>{fmtAmount(d.ledger_balance)}</Td>
              <Td>
                {d.drift != null ? (
                  <span className="inline-flex items-center gap-2">
                    <Badge variant="amber">{fmtAmount(d.drift)} off</Badge>
                    {d.drift_basis ? (
                      <span
                        className="text-xs text-muted-foreground"
                        title={DRIFT_BASIS[d.drift_basis].hint}
                      >
                        {DRIFT_BASIS[d.drift_basis].label}
                      </span>
                    ) : null}
                    {d.drift_basis_pinned ? <Badge>pinned</Badge> : null}
                  </span>
                ) : d.ledger_balance != null ? (
                  <Badge variant="green">in sync</Badge>
                ) : (
                  <span className="text-muted-foreground">—</span>
                )}
              </Td>
              <Td>
                {d.ledger_balance != null && d.mapped_account_id ? (
                  <BasisSelect row={d} connectionId={connectionId} />
                ) : (
                  <span className="text-muted-foreground">—</span>
                )}
              </Td>
              <Td>
                {d.mapped_account_path ? (
                  <Badge variant="green">{d.mapped_account_path}</Badge>
                ) : (
                  <Badge variant="amber">unmapped</Badge>
                )}
              </Td>
            </tr>
          ))}
        </tbody>
      </Table>
    </Card>
  );
}

export default function SimpleFin() {
  const qc = useQueryClient();
  const [setupToken, setSetupToken] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [backfillResult, setBackfillResult] = useState<BackfillOut | null>(null);

  const connections = useQuery({
    queryKey: ["connections"],
    queryFn: async () => (await api.GET("/connections")).data ?? [],
  });
  const config = useQuery({
    queryKey: ["config"],
    queryFn: async () => (await api.GET("/config")).data ?? null,
  });

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["connections"] });
    qc.invalidateQueries({ queryKey: ["discovered"] });
  };

  return (
    <div className="space-y-6">
      <Card>
        <div className="mb-4 flex items-center justify-between">
          <CardTitle className="mb-0">SimpleFIN connections</CardTitle>
        </div>
        <Table>
          <thead>
            <tr>
              <Th>Provider</Th><Th>Status</Th><Th>Last fetch</Th>
              <Th>Fetches today</Th><Th>History</Th><Th>Bridge errors</Th><Th />
            </tr>
          </thead>
          <tbody>
            {(connections.data ?? []).map((c) => (
              <tr key={c.id}>
                <Td>
                  <Badge variant={providerBadge(c.provider).variant}>
                    {providerBadge(c.provider).label}
                  </Badge>
                </Td>
                <Td>
                  <Badge variant={c.status === "active" ? "green" : c.status === "warning" ? "amber" : "red"}>
                    {c.status}
                  </Badge>
                </Td>
                <Td>{c.last_fetch_at ? new Date(c.last_fetch_at).toLocaleString() : "never"}</Td>
                <Td>{c.fetches_today} / 24</Td>
                <Td>
                  {c.backfill_complete ? (
                    <Badge variant="green">All history fetched</Badge>
                  ) : (
                    <span className="text-xs text-muted-foreground">
                      back to {c.backfill_cursor ?? "~90 days"}
                    </span>
                  )}
                </Td>
                <Td className="max-w-64 truncate text-xs text-muted-foreground">
                  {c.last_errors.join("; ") || "—"}
                </Td>
                <Td>
                  <div className="flex flex-col items-start gap-1">
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={busy}
                      onClick={async () => {
                        setBusy(true);
                        setError(null);
                        const { error: err } = await api.POST("/connections/{connection_id}/sync", {
                          params: { path: { connection_id: c.id } },
                        });
                        setBusy(false);
                        if (err) setError(errorDetail(err));
                        refresh();
                      }}
                    >
                      Sync now
                    </Button>
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={busy || c.backfill_complete}
                      onClick={async () => {
                        setBusy(true);
                        setError(null);
                        setBackfillResult(null);
                        const { data, error: err } = await api.POST(
                          "/connections/{connection_id}/backfill",
                          { params: { path: { connection_id: c.id } }, body: { windows: 4 } },
                        );
                        setBusy(false);
                        if (err) setError(errorDetail(err));
                        else if (data) setBackfillResult(data);
                        refresh();
                      }}
                    >
                      Fetch older history
                    </Button>
                  </div>
                </Td>
              </tr>
            ))}
          </tbody>
        </Table>
        {connections.data?.length === 0 && (
          <p className="text-sm text-muted-foreground">No SimpleFIN connections yet.</p>
        )}
        <ErrorText>{error}</ErrorText>
        {backfillResult && (
          <div className="mt-3 space-y-2">
            <p className="text-sm text-muted-foreground">
              Fetched {backfillResult.windows_fetched}{" "}
              window{backfillResult.windows_fetched === 1 ? "" : "s"}
              {backfillResult.coverage_start ? ` back to ${backfillResult.coverage_start}` : ""} —{" "}
              {backfillResult.imported} imported.{" "}
              {backfillResult.backfill_complete
                ? "All available history fetched."
                : "Run again to continue."}
            </p>
            {Object.keys(backfillResult.qif_overlap).length > 0 && (
              <div className="rounded-md border border-amber-300 bg-amber-50 p-2 text-xs text-amber-800">
                Manual QIF rows overlap this backfilled range and may be duplicates:{" "}
                {Object.entries(backfillResult.qif_overlap)
                  .map(([path, n]) => `${path} (${n})`)
                  .join(", ")}
                .
              </div>
            )}
          </div>
        )}
        <p className="mt-2 text-xs text-muted-foreground">
          Fetching older history may take several runs — the bridge allows ~24 fetches/day and
          history depth varies by institution.
        </p>
      </Card>

      {(connections.data ?? []).map((c) => (
        <FeedAccounts key={c.id} connectionId={c.id} />
      ))}

      <Card>
        <CardTitle>Add a SimpleFIN connection</CardTitle>
        <div className="flex items-end gap-2">
          <div className="flex-1 space-y-1">
            <Label>Setup token (one-time; access URL stored encrypted)</Label>
            <Input value={setupToken} onChange={(e) => setSetupToken(e.target.value)} placeholder="aHR0cHM6Ly9…" />
          </div>
          <Button
            disabled={busy || !setupToken.trim()}
            onClick={async () => {
              setBusy(true);
              setError(null);
              const { error: err } = await api.POST("/connections", {
                body: { setup_token: setupToken.trim() },
              });
              setBusy(false);
              if (err) setError(errorDetail(err));
              else setSetupToken("");
              refresh();
            }}
          >
            Connect
          </Button>
        </div>
        <ErrorText>{error}</ErrorText>
      </Card>

      {config.data && <SyncScheduleCard config={config.data} />}
    </div>
  );
}
