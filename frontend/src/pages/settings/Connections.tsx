import { Fragment, useEffect, useId, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, errorDetail } from "../../lib/api";
import { useIsAdmin } from "../../lib/useIsAdmin";
import { configQuery } from "../../lib/config";
import { formatMoney } from "../../lib/money";
import { formatDate } from "../../lib/dates";
import { useFormatTimestamp } from "../../lib/clock";
import { formatTimeOfDay, parseTimeOfDay } from "../../lib/timeOfDay";
import { QueryState } from "../../components/QueryState";
import { Badge, Button, Card, CardTitle, ErrorText, Input, Label, Table, Td, Th } from "../../components/ui";
import type { components } from "../../api/schema";
import { GapRepairNote, RefetchCard, inventoryQuery } from "./FeedStatus";
import { MapFeedAccountForm, mapFeedAccount } from "../wizard/MapFeedAccountForm";
import { AccountIcon } from "../../components/AccountIcon";
import { useAccounts } from "../../components/AccountsTable";

type SyncSchedule = components["schemas"]["SyncSchedule"];
type ConfigOut = components["schemas"]["ConfigOut"];
type BackfillOut = components["schemas"]["BackfillOut"];
type DiscoveredOut = components["schemas"]["DiscoveredOut"];
type GapRepair = components["schemas"]["GapRepairOut"];
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

type HealthOut = components["schemas"]["HealthOut"];
type InventoryConnection = components["schemas"]["InventoryConnectionOut"];
type InventoryLogin = components["schemas"]["InventoryLoginOut"];
type InventoryAccount = components["schemas"]["InventoryAccountOut"];

function providerName(provider: string): string {
  return provider === "simplefin" ? "SimpleFIN" : provider;
}

const PRESETS: [SyncSchedule["preset"], string][] = [
  ["hourly", "Every hour"],
  ["every_6h", "Every 6 hours"],
  ["every_12h", "Every 12 hours"],
  ["daily", "Once daily at…"],
];

const selectClass = "h-9 rounded-md border border-border bg-card px-2 text-sm";

/** The daily sync time is a clock time in the household time zone, stored
 * and run as such (SyncSchedule, decision 111; #556): it stays at the same
 * time on the clock across daylight-saving changes. */
function SyncScheduleCard({ config }: { config: ConfigOut }) {
  const qc = useQueryClient();
  const id = useId();
  const timeZone = config.values.timezone;
  const initial = config.values.sync_schedule;
  const [preset, setPreset] = useState<SyncSchedule["preset"]>(initial.preset);
  const [dailyTime, setDailyTime] = useState(
    formatTimeOfDay({ hour: initial.hour ?? 6, minute: initial.minute ?? 30 }),
  );
  const [staleDays, setStaleDays] = useState(String(config.values.feed_stale_days));
  const [error, setError] = useState<string | null>(null);
  const isUtc = timeZone === "UTC" || timeZone === "Etc/UTC";
  const local = parseTimeOfDay(dailyTime);

  const save = useMutation({
    mutationFn: async () => {
      let schedule: SyncSchedule = { preset };
      if (preset === "daily") {
        if (!local) throw new Error("Enter a time of day.");
        schedule = { preset, hour: local.hour, minute: local.minute };
      }
      const days = Number(staleDays);
      if (!Number.isInteger(days) || days < 1 || days > 90) {
        throw new Error("Stale after must be a whole number of days from 1 to 90.");
      }
      const { error: err } = await api.PUT("/config", {
        body: { sync_schedule: schedule, feed_stale_days: days },
      });
      if (err) throw new Error(errorDetail(err));
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ["config"] }),
    onError: (e) => setError(e.message),
  });

  return (
    <Card>
      <CardTitle>Sync schedule</CardTitle>
      <p className="mb-4 text-sm text-muted-foreground">
        How often Purser pulls new data from SimpleFIN; the Bridge allows at most 24 fetches per
        day. The daily time is in the household time zone ({timeZone}). An account whose bank
        balance has not updated for more than &ldquo;Stale after&rdquo; days is flagged
        here and to your agent.
      </p>
      <div className="flex flex-wrap items-end gap-2">
        <div className="space-y-1">
          <Label htmlFor={`${id}-preset`}>Frequency</Label>
          <select
            id={`${id}-preset`}
            className={`block ${selectClass}`}
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
            <Label htmlFor={`${id}-time`}>Time ({timeZone})</Label>
            <Input
              id={`${id}-time`}
              type="time"
              value={dailyTime}
              onChange={(e) => setDailyTime(e.target.value)}
            />
          </div>
        )}
        <div className="space-y-1">
          <Label htmlFor="feed-stale-days">Stale after (days)</Label>
          <Input
            id="feed-stale-days"
            type="number"
            min={1}
            max={90}
            className="w-24"
            value={staleDays}
            onChange={(e) => setStaleDays(e.target.value)}
          />
        </div>
        <Button disabled={save.isPending} onClick={() => { setError(null); save.mutate(); }}>
          Save
        </Button>
      </div>
      {preset === "daily" && !isUtc && (
        <p className="mt-2 text-xs text-muted-foreground">
          The time stays the same on the clock when daylight saving starts or ends. A time the
          clocks skip that night runs an hour later; a time in the repeated hour runs twice.
        </p>
      )}
      {!config.sync_reschedule_live && (
        <p className="mt-2 text-xs text-muted-foreground">
          This instance runs its scheduler in a separate worker: changes apply when the worker
          restarts.
        </p>
      )}
      {save.isSuccess && <p className="mt-2 text-sm text-success">Saved.</p>}
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


/** DOM id of a bank account's row (the target of a `?map=` link). */
function feedRowId(externalId: string): string {
  return `feed-account-${externalId}`;
}

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

/** One bank account on the page: what the inventory says about it (every
 * session) joined with the admin-only discovery row (balances, drift,
 * mapping) when there is one. */
type AccountRow = { key: string; inv?: InventoryAccount; disc?: DiscoveredOut };
type BankGroup = {
  key: string;
  name: string;
  /** Set when several logins share the bank's name: "login 2". */
  label?: string;
  issue: InventoryLogin["issue"];
  rows: AccountRow[];
};

const isWaiting = (r: AccountRow) =>
  r.disc
    ? !r.disc.mapped_account_id && !r.disc.ignored
    : !!r.inv && !r.inv.account && !r.inv.ignored;
const isIgnored = (r: AccountRow) => r.disc?.ignored ?? r.inv?.ignored ?? false;
const needsLogin = (g: BankGroup) => !!g.issue && g.issue.code.startsWith("con.auth");

/** The page's one list: the inventory's bank logins, each with its accounts,
 * joined with the discovery rows by SimpleFIN account id. A discovery row
 * the inventory does not carry joins the login with its `conn_id`, else a
 * group named after its bank. Banks that need the user come first, then
 * banks with an account waiting to be mapped; inside a bank, waiting
 * accounts first and ignored ones last. */
export function bankGroups(
  inventory: InventoryConnection | undefined,
  discovered: DiscoveredOut[],
): BankGroup[] {
  const groups: BankGroup[] = [];
  const byExternalId = new Map<string, AccountRow>();
  (inventory?.logins ?? []).forEach((login, i) => {
    const group: BankGroup = {
      key: login.conn_id ?? `login-${i}`,
      name: login.institution ?? "Unknown bank",
      issue: login.issue ?? null,
      rows: [],
    };
    login.accounts.forEach((inv, j) => {
      const row: AccountRow = { key: inv.external_id ?? `${group.key}-${j}`, inv };
      // An id two logins share stays two rows; only the first takes the join.
      if (inv.external_id && !byExternalId.has(inv.external_id))
        byExternalId.set(inv.external_id, row);
      group.rows.push(row);
    });
    groups.push(group);
  });
  for (const disc of discovered) {
    const known = byExternalId.get(disc.external_id);
    if (known && !known.disc) {
      known.disc = disc;
      continue;
    }
    const key = disc.conn_id ?? `bank-${disc.org ?? ""}`;
    let group = groups.find((g) => g.key === key);
    if (!group) {
      group = { key, name: disc.org ?? "Unknown bank", issue: null, rows: [] };
      groups.push(group);
    }
    group.rows.push({ key: disc.external_id, disc });
  }
  // Two logins at one bank (two people's online banking) read as two banks
  // with one name; number them so each account's login is clear.
  const seen = new Map<string, number>();
  const total = new Map<string, number>();
  for (const g of groups) total.set(g.name, (total.get(g.name) ?? 0) + 1);
  for (const g of groups) {
    const n = (seen.get(g.name) ?? 0) + 1;
    seen.set(g.name, n);
    if ((total.get(g.name) ?? 0) > 1) g.label = `login ${n}`;
  }
  const rowRank = (r: AccountRow) => (isWaiting(r) ? 0 : isIgnored(r) ? 2 : 1);
  const groupRank = (g: BankGroup) => (g.issue ? 0 : g.rows.some(isWaiting) ? 1 : 2);
  for (const g of groups) g.rows = [...g.rows].sort((a, b) => rowRank(a) - rowRank(b));
  return [...groups].sort((a, b) => groupRank(a) - groupRank(b));
}

/** What the latest sync reported that no bank or account row shows: general
 * messages, a v1 fetch's plain strings, and an issue for a login or account
 * the inventory does not list. Shown in full, each with its code. */
function otherMessages(connection: HealthOut, groups: BankGroup[]): string[] {
  const shown = new Set<string>();
  for (const g of groups) {
    if (g.issue) shown.add(g.issue.message);
    for (const r of g.rows) if (r.inv?.issue) shown.add(r.inv.issue.message);
  }
  if (connection.last_issues.length === 0) return connection.last_errors;
  return connection.last_issues
    .filter((i) => !shown.has(i.msg))
    .map((i) => (i.code && i.code !== "gen." ? `${i.code}: ${i.msg}` : i.msg));
}

function useDiscovered(connectionId: string, enabled: boolean) {
  // Live reported-vs-ledger comparison (issue #149): recomputed on every read
  // from the latest archived payload. Admin-scoped, and only once the
  // connection has fetched: before that the server answers "no fetches yet".
  return useQuery({
    queryKey: ["discovered", connectionId],
    enabled,
    queryFn: async () => {
      const { data, error } = await api.GET("/connections/{connection_id}/discovered", {
        params: { path: { connection_id: connectionId } },
      });
      if (error) throw new Error(errorDetail(error));
      return data ?? [];
    },
  });
}

/** One SimpleFIN connection: a summary of the latest sync in words, then its
 * banks and their accounts in one list (#561). */
function ConnectionCard({
  connection,
  inventory,
}: {
  connection: HealthOut;
  inventory: InventoryConnection | undefined;
}) {
  const qc = useQueryClient();
  const isAdmin = useIsAdmin();
  const formatTimestamp = useFormatTimestamp();
  const [error, setError] = useState<string | null>(null);
  const [gapRepair, setGapRepair] = useState<GapRepair | null>(null);
  const discovered = useDiscovered(connection.id, isAdmin && !!connection.last_fetch_at);

  const sync = useMutation({
    mutationFn: async () => {
      const { data, error: err } = await api.POST("/connections/{connection_id}/sync", {
        params: { path: { connection_id: connection.id } },
      });
      if (err) throw new Error(errorDetail(err));
      return data;
    },
    onMutate: () => {
      setError(null);
      setGapRepair(null);
    },
    onSuccess: (data) => setGapRepair(data?.gap_repair ?? null),
    onError: (e) => setError(e.message),
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ["connections"] });
      qc.invalidateQueries({ queryKey: ["discovered"] });
      qc.invalidateQueries({ queryKey: ["inventory"] });
    },
  });

  const groups = bankGroups(inventory, discovered.data ?? []);
  const rows = groups.flatMap((g) => g.rows);
  const login = groups.filter(needsLogin).length;
  const failing = groups.filter((g) => g.issue && !needsLogin(g)).length;
  const waiting = rows.filter(isWaiting).length;
  const stale = rows.filter((r) => r.inv?.stale && r.inv.connected).length;
  const messages = otherMessages(connection, groups);
  const healthy = login + failing + waiting + stale + messages.length === 0;

  return (
    <Card role="region" aria-label={`${providerName(connection.provider)} connection`}>
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <CardTitle className="mb-0">{providerName(connection.provider)}</CardTitle>
        <Button size="sm" variant="outline" disabled={sync.isPending} onClick={() => sync.mutate()}>
          {sync.isPending ? "Syncing…" : "Sync now"}
        </Button>
      </div>
      <p className="text-sm text-muted-foreground">
        {connection.last_fetch_at
          ? `Last sync ${formatTimestamp(connection.last_fetch_at)}`
          : "Not synced yet"}
        {" · "}
        {connection.fetches_today} of 24 fetches used today
      </p>
      {connection.status === "disabled" ? (
        <p className="mt-2 text-sm">
          <Badge variant="red">disabled</Badge> This connection no longer syncs.
        </p>
      ) : (
        groups.length > 0 && (
          <ul className="mt-3 flex flex-wrap gap-2 text-sm" aria-label="Connection summary">
            {healthy && (
              <li>
                <Badge variant="green">
                  All {plural(groups.length, "bank")} connected
                </Badge>
              </li>
            )}
            {login > 0 && (
              <li>
                <Badge variant="red">
                  {login} of {plural(groups.length, "bank")} need{login === 1 ? "s" : ""} a new
                  login
                </Badge>
              </li>
            )}
            {failing > 0 && (
              <li>
                <Badge variant="amber">
                  {plural(failing, "bank")} report{failing === 1 ? "s" : ""} an error
                </Badge>
              </li>
            )}
            {waiting > 0 && (
              <li>
                <Badge variant="amber">{plural(waiting, "account")} not in Purser yet</Badge>
              </li>
            )}
            {stale > 0 && (
              <li>
                <Badge variant="amber">
                  {plural(stale, "account")} not updated in over {inventory?.stale_days} days
                </Badge>
              </li>
            )}
          </ul>
        )
      )}
      {messages.length > 0 && (
        <div className="mt-3 rounded-md border border-warning/40 bg-warning-soft p-3 text-sm">
          <p className="font-medium">SimpleFIN reported with the latest sync:</p>
          <ul className="mt-1 list-disc pl-5">
            {messages.map((m, i) => (
              <li key={i} className="break-words">
                {m}
              </li>
            ))}
          </ul>
        </div>
      )}
      <ErrorText>{error}</ErrorText>
      {gapRepair && <GapRepairNote repair={gapRepair} />}
      {discovered.error && !discovered.data && (
        <div className="mt-3">
          <QueryState query={discovered} what="bank balances and mappings" />
        </div>
      )}
      {groups.length > 0 && (
        <BankAccounts
          connectionId={connection.id}
          groups={groups}
          waiting={waiting}
          admin={isAdmin && !!discovered.data}
        />
      )}
    </Card>
  );
}

function BankAccounts({
  connectionId,
  groups,
  waiting,
  admin,
}: {
  connectionId: string;
  groups: BankGroup[];
  waiting: number;
  admin: boolean;
}) {
  const qc = useQueryClient();
  // The Purser accounts the rows map to, for their icons (#567).
  const known = useAccounts({ showHidden: true, showClosed: true });
  const byPath = new Map((known.data ?? []).map((a) => [a.path, a]));
  // `?map=<external id>` (the Dashboard's links) opens that account's form.
  const [params] = useSearchParams();
  const asked = params.get("map");
  const [mapping, setMapping] = useState<string | null>(asked);
  const [ignoreError, setIgnoreError] = useState<string | null>(null);
  const setIgnored = useMutation({
    mutationFn: async (v: { externalId: string; ignored: boolean }) => {
      const { error: err } = await api.POST("/connections/{connection_id}/ignore", {
        params: { path: { connection_id: connectionId } },
        body: { external_id: v.externalId, ignored: v.ignored },
      });
      if (err) throw new Error(errorDetail(err));
    },
    onSuccess: () => {
      setIgnoreError(null);
      setMapping(null);
      qc.invalidateQueries({ queryKey: ["discovered", connectionId] });
      qc.invalidateQueries({ queryKey: ["inventory"] });
    },
    onError: (e) => setIgnoreError(e.message),
  });

  const all = groups.flatMap((g) => g.rows);
  const askedLoaded = !!asked && all.some((r) => r.disc?.external_id === asked);
  useEffect(() => {
    if (askedLoaded)
      document.getElementById(feedRowId(asked!))?.scrollIntoView?.({ block: "center" });
  }, [askedLoaded, asked]);

  // An unmapped account with the name and currency of a mapped one is most
  // often the same account delivered through a second login (a joint account).
  const twinOf = (d: DiscoveredOut) =>
    d.mapped_account_id || d.ignored || !d.name
      ? undefined
      : all.find(
          (r) =>
            r.disc?.mapped_account_path &&
            r.disc.name === d.name &&
            r.disc.currency === d.currency,
        )?.disc?.mapped_account_path;
  const columns = admin ? 4 : 3;

  return (
    <div className="mt-4">
      {admin && waiting > 0 && (
        <p role="status" className="mb-3 rounded-md border border-warning/40 bg-warning-soft p-3 text-sm">
          {waiting === 1 ? "One bank account is" : `${waiting} bank accounts are`} not in Purser
          yet (listed first under {waiting === 1 ? "its" : "their"} bank). <b>Map</b> links one
          to a new or existing Purser account and imports its already-fetched transactions.{" "}
          <b>Ignore</b> is for a duplicate, or an account that does not belong in this ledger;
          it can be tracked again later.
        </p>
      )}
      <ErrorText>{ignoreError}</ErrorText>
      <Table>
        <thead>
          <tr>
            <Th>Bank account</Th>
            <Th>Purser account</Th>
            <Th>Bank balance</Th>
            {admin && <Th>Compared with Purser</Th>}
          </tr>
        </thead>
        {groups.map((g) => (
          <tbody key={g.key} aria-label={g.label ? `${g.name}, ${g.label}` : g.name}>
            <tr className="bg-muted/40">
              <Th scope="rowgroup" colSpan={columns} className="text-foreground">
                {/* Pinned to the visible edge and no wider than the screen, so
                    what to do stays readable when a phone scrolls the table. */}
                <span className="sticky left-3 flex max-w-[calc(100vw-5rem)] flex-wrap items-center gap-2">
                  <span className="font-semibold">
                    {g.name}
                    {g.label ? <span className="font-normal text-muted-foreground"> · {g.label}</span> : null}
                  </span>
                  {needsLogin(g) ? (
                    <Badge variant="red">needs login</Badge>
                  ) : g.issue ? (
                    <Badge variant="amber">error</Badge>
                  ) : (
                    <Badge variant="green">ok</Badge>
                  )}
                  <span className="text-xs font-normal text-muted-foreground">
                    {needsLogin(g)
                      ? "Log in to this bank again in SimpleFIN Bridge; its accounts stop updating until you do."
                      : g.issue
                        ? g.issue.message
                        : plural(g.rows.length, "account")}
                  </span>
                </span>
              </Th>
            </tr>
            {g.rows.map((r) => {
              const d = r.disc;
              const name = d?.name ?? r.inv?.name ?? d?.external_id ?? "Unnamed account";
              const path = d?.mapped_account_path ?? r.inv?.account ?? null;
              const twin = d ? twinOf(d) : undefined;
              const balanceDate = r.inv?.balance_date ?? d?.balance_date ?? null;
              return (
                <Fragment key={r.key}>
                  <tr id={d ? feedRowId(d.external_id) : undefined}>
                    <Td className="pl-6">
                      {name}
                      {twin && (
                        <span className="block text-xs text-muted-foreground">
                          Looks like a duplicate of {twin}, delivered through another bank login.
                          Ignore it unless it is a different account.
                        </span>
                      )}
                      {(r.inv?.ambiguous || d?.ambiguous) && (
                        <span className="block text-xs text-warning">
                          Two bank logins send this account id, so it is not imported.
                        </span>
                      )}
                    </Td>
                    <Td>
                      {path ? (
                        <Badge variant={r.inv?.closed ? "default" : "green"}>
                          <span className="inline-flex items-center gap-1">
                            {byPath.get(path) && (
                              <AccountIcon
                                type={byPath.get(path)!.type}
                                kind={byPath.get(path)!.asset?.kind}
                                className="h-3.5 w-3.5"
                              />
                            )}
                            {path}
                            {r.inv?.closed ? " (closed)" : ""}
                          </span>
                        </Badge>
                      ) : isIgnored(r) ? (
                        <span className="inline-flex flex-wrap items-center gap-2">
                          <Badge>ignored</Badge>
                          {admin && d && (
                            <Button
                              size="sm"
                              variant="ghost"
                              disabled={setIgnored.isPending}
                              aria-label={`Track ${name} again`}
                              onClick={() =>
                                setIgnored.mutate({ externalId: d.external_id, ignored: false })
                              }
                            >
                              Track again
                            </Button>
                          )}
                        </span>
                      ) : (
                        <span className="inline-flex flex-wrap items-center gap-2">
                          <Badge variant="amber">not in Purser yet</Badge>
                          {admin && d && mapping !== d.external_id && (
                            <Button
                              size="sm"
                              variant="outline"
                              aria-label={`Map ${name}`}
                              onClick={() => setMapping(d.external_id)}
                            >
                              Map
                            </Button>
                          )}
                          {admin && d && (
                            <Button
                              size="sm"
                              variant="ghost"
                              disabled={setIgnored.isPending}
                              aria-label={`Ignore ${name}`}
                              onClick={() =>
                                setIgnored.mutate({ externalId: d.external_id, ignored: true })
                              }
                            >
                              Ignore
                            </Button>
                          )}
                        </span>
                      )}
                    </Td>
                    <Td>
                      {admin && d && (
                        <span className="font-mono">
                          {formatMoney(d.balance, { currency: d.currency })}
                        </span>
                      )}
                      <span className="block whitespace-nowrap text-xs text-muted-foreground">
                        {balanceDate ? `as of ${formatDate(balanceDate)}` : "no date from the bank"}
                        {r.inv?.days_since_balance ? ` (${r.inv.days_since_balance}d ago)` : ""}
                      </span>
                      <span className="mt-1 flex flex-wrap gap-1">
                        {r.inv?.stale && r.inv.connected && <Badge variant="amber">stale</Badge>}
                        {r.inv?.issue && (
                          <Badge variant="amber" title={r.inv.issue.message}>
                            {r.inv.issue.code}
                          </Badge>
                        )}
                      </span>
                    </Td>
                    {admin && (
                      <Td>
                        {d?.drift != null ? (
                          <Badge variant="amber" title={d.drift_basis ? DRIFT_BASIS[d.drift_basis].hint : undefined}>
                            <span className="font-mono">
                              {formatMoney(d.drift, { currency: d.currency })}
                            </span>
                            &nbsp;off
                          </Badge>
                        ) : d?.ledger_balance != null ? (
                          <Badge variant="green">matches</Badge>
                        ) : (
                          <span className="text-muted-foreground">—</span>
                        )}
                      </Td>
                    )}
                  </tr>
                  {admin && d && isWaiting(r) && mapping === d.external_id && (
                    <tr>
                      <Td colSpan={columns}>
                        <MapFeedAccountForm
                          account={d}
                          onMap={async (target) => {
                            await mapFeedAccount(connectionId, d.external_id, target);
                            setMapping(null);
                            // The mapping created or moved a ledger account and
                            // imported its archived rows: everything reading the
                            // feed or the accounts is stale.
                            await Promise.all([
                              qc.invalidateQueries({ queryKey: ["discovered", connectionId] }),
                              qc.invalidateQueries({ queryKey: ["inventory"] }),
                              qc.invalidateQueries({ queryKey: ["accounts"] }),
                              qc.invalidateQueries({ queryKey: ["networth"] }),
                            ]);
                          }}
                          onCancel={() => setMapping(null)}
                        />
                      </Td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
          </tbody>
        ))}
      </Table>
      {admin && (
        <p className="mt-2 text-xs text-muted-foreground">
          &ldquo;Compared with Purser&rdquo; checks the bank&apos;s balance against the Purser
          account&apos;s; a difference is a warning, never a block. Loans, property and
          investment accounts are checked through their valuations and holdings instead.
        </p>
      )}
    </div>
  );
}

/** Maintenance: how each account's balance is compared (issue #283, #307).
 * Purser learns from an account's sync history whether its bank counts
 * pending charges in the balance; an admin can pin the answer. */
function ComparisonCard({ connectionId, fetched }: { connectionId: string; fetched: boolean }) {
  const discovered = useDiscovered(connectionId, fetched);
  const rows = (discovered.data ?? []).filter((d) => d.ledger_balance != null && d.mapped_account_id);
  if (rows.length === 0) return null;
  return (
    <Card>
      <CardTitle>How balances are compared</CardTitle>
      <p className="mb-4 text-sm text-muted-foreground">
        Some banks leave pending charges out of the balance they report. Purser learns which
        from each account&apos;s sync history and compares on that basis; pin it when the
        history is short or misleading.
      </p>
      <Table>
        <thead>
          <tr>
            <Th>Purser account</Th>
            <Th>Bank balance</Th>
            <Th>Purser balance</Th>
            <Th>Difference</Th>
            <Th>Basis</Th>
          </tr>
        </thead>
        <tbody>
          {rows.map((d) => (
            <tr key={d.external_id}>
              <Td>{d.mapped_account_path}</Td>
              <Td className="font-mono">{formatMoney(d.balance, { currency: d.currency })}</Td>
              <Td className="font-mono">{formatMoney(d.ledger_balance, { currency: d.currency })}</Td>
              <Td>
                {d.drift != null ? (
                  <span className="inline-flex flex-wrap items-center gap-2">
                    <span className="font-mono">{formatMoney(d.drift, { currency: d.currency })}</span>
                    {d.drift_basis ? (
                      <span className="text-xs text-muted-foreground" title={DRIFT_BASIS[d.drift_basis].hint}>
                        {DRIFT_BASIS[d.drift_basis].label}
                      </span>
                    ) : null}
                    {d.drift_basis_pinned ? <Badge>pinned</Badge> : null}
                  </span>
                ) : (
                  <span className="text-muted-foreground">none</span>
                )}
              </Td>
              <Td>
                <BasisSelect row={d} connectionId={connectionId} />
              </Td>
            </tr>
          ))}
        </tbody>
      </Table>
    </Card>
  );
}

/** Maintenance: fetch history older than the first sync's window. */
function OlderHistoryCard({ connection }: { connection: HealthOut }) {
  const qc = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const run = useMutation({
    mutationFn: async () => {
      const { data, error: err } = await api.POST("/connections/{connection_id}/backfill", {
        params: { path: { connection_id: connection.id } },
        body: { windows: 8 },
      });
      if (err) throw new Error(errorDetail(err));
      return data ?? null;
    },
    onMutate: () => setError(null),
    onError: (e) => setError(e.message),
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ["connections"] });
      qc.invalidateQueries({ queryKey: ["discovered"] });
      qc.invalidateQueries({ queryKey: ["inventory"] });
    },
  });
  const result: BackfillOut | null = run.data ?? null;
  return (
    <Card>
      <CardTitle>Older history</CardTitle>
      <p className="mb-4 text-sm text-muted-foreground">
        A new connection starts with about 90 days. Fetching further back may take several runs:
        the Bridge allows about 24 fetches a day, and how far back a bank goes varies.
      </p>
      <div className="flex flex-wrap items-center gap-3">
        <Button
          size="sm"
          variant="outline"
          disabled={run.isPending || connection.backfill_complete}
          onClick={() => run.mutate()}
        >
          Fetch older history
        </Button>
        {connection.backfill_complete ? (
          <Badge variant="green">All history fetched</Badge>
        ) : (
          <span className="text-sm text-muted-foreground">
            Fetched back to{" "}
            {connection.backfill_cursor ? formatDate(connection.backfill_cursor) : "about 90 days ago"}
          </span>
        )}
      </div>
      {result && (
        <div className="mt-3 space-y-2">
          <p className="text-sm text-muted-foreground">
            Fetched {plural(result.windows_fetched, "window")}
            {result.coverage_start ? ` back to ${formatDate(result.coverage_start)}` : ""} —{" "}
            {result.imported} imported.{" "}
            {result.backfill_complete ? "All available history fetched." : "Run again to continue."}
          </p>
          {Object.keys(result.qif_overlap).length > 0 && (
            <div className="rounded-md border border-warning/40 bg-warning-soft p-2 text-xs text-warning">
              Manual QIF rows overlap this backfilled range and may be duplicates:{" "}
              {Object.entries(result.qif_overlap)
                .map(([path, n]) => `${path} (${n})`)
                .join(", ")}
              .
            </div>
          )}
        </div>
      )}
      <ErrorText>{error}</ErrorText>
    </Card>
  );
}

function AddConnectionCard() {
  const qc = useQueryClient();
  const [setupToken, setSetupToken] = useState("");
  const [error, setError] = useState<string | null>(null);
  const id = useId();
  const add = useMutation({
    mutationFn: async () => {
      const { error: err } = await api.POST("/connections", {
        body: { setup_token: setupToken.trim() },
      });
      if (err) throw new Error(errorDetail(err));
    },
    onMutate: () => setError(null),
    onSuccess: () => {
      setSetupToken("");
      qc.invalidateQueries({ queryKey: ["connections"] });
    },
    onError: (e) => setError(e.message),
  });
  return (
    <Card>
      <CardTitle>Add a SimpleFIN connection</CardTitle>
      <p className="mb-4 text-sm text-muted-foreground">
        Create a setup token in SimpleFIN Bridge and paste it here. It works once; Purser trades
        it for an access link that is stored encrypted.
      </p>
      <div className="flex items-end gap-2">
        <div className="flex-1 space-y-1">
          <Label htmlFor={`${id}-token`}>Setup token</Label>
          <Input
            id={`${id}-token`}
            value={setupToken}
            onChange={(e) => setSetupToken(e.target.value)}
            placeholder="aHR0cHM6Ly9…"
          />
        </div>
        <Button disabled={add.isPending || !setupToken.trim()} onClick={() => add.mutate()}>
          Connect
        </Button>
      </div>
      <ErrorText>{error}</ErrorText>
    </Card>
  );
}

/** Settings -> Connections (#561): each SimpleFIN connection as a summary
 * and one list of its banks and accounts, then the rarely used controls
 * under Maintenance. A member session (read + write) syncs, fetches older
 * history, re-fetches and sees every bank's state; balances, mapping, the
 * comparison basis, the sync schedule (GET/PUT /config) and adding a
 * connection are admin-scoped, so a member's view never calls them (#542). */
export default function Connections() {
  const isAdmin = useIsAdmin();
  const connections = useQuery({
    queryKey: ["connections"],
    queryFn: async () => {
      const { data, error: err } = await api.GET("/connections");
      if (err) throw new Error(errorDetail(err));
      return data ?? [];
    },
  });
  const inventory = useQuery(inventoryQuery);
  const config = useQuery({ ...configQuery, enabled: isAdmin });
  const list = connections.data ?? [];
  // The inventory lists the connections that are not disabled, oldest first
  // like GET /connections; a browser session also gets their ids.
  const enabled = list.filter((c) => c.status !== "disabled");
  const inventoryOf = (c: HealthOut) =>
    (inventory.data ?? []).find((i) => i.connection_id === c.id) ??
    (inventory.data ?? []).filter((i) => !i.connection_id)[enabled.indexOf(c)];

  return (
    <div className="space-y-6">
      {list.map((c) => (
        <ConnectionCard key={c.id} connection={c} inventory={inventoryOf(c)} />
      ))}
      {inventory.error && !inventory.data && (
        <Card>
          <QueryState query={inventory} what="the banks and accounts" />
        </Card>
      )}
      {list.length === 0 && (
        <Card>
          <CardTitle>Bank connections</CardTitle>
          <QueryState
            query={connections}
            what="SimpleFIN connections"
            empty="No bank feed is connected yet. Purser imports from your banks through SimpleFIN Bridge."
          />
        </Card>
      )}
      {isAdmin && connections.isSuccess && list.length === 0 && <AddConnectionCard />}

      {(list.length > 0 || isAdmin) && connections.isSuccess && (
        <details className="group rounded-xl border border-border">
          <summary className="cursor-pointer select-none px-4 py-3 text-sm font-medium sm:px-6">
            Maintenance and schedule
            <span className="ml-2 font-normal text-muted-foreground">
              sync times, older history, re-fetching a date range
              {isAdmin ? ", balance comparison, adding a connection" : ""}
            </span>
          </summary>
          <div className="space-y-6 border-t border-border p-4 sm:p-6">
            {isAdmin && (
              <QueryState query={config} what="the sync settings">
                {(data) => <SyncScheduleCard config={data} />}
              </QueryState>
            )}
            {list.map((c) => (
              <Fragment key={c.id}>
                <OlderHistoryCard connection={c} />
                <RefetchCard connectionId={c.id} />
                {isAdmin && <ComparisonCard connectionId={c.id} fetched={!!c.last_fetch_at} />}
              </Fragment>
            ))}
            {isAdmin && list.length > 0 && <AddConnectionCard />}
          </div>
        </details>
      )}
    </div>
  );
}
