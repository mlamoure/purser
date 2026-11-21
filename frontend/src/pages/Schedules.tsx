/** Scheduled transactions page (#236, #535): the rules behind the forecast.
 * The account forecast comes first; below it every scheduled transaction is
 * listed under its account (a transfer under both of its accounts), with
 * pause / resume / end, "Detect now" into the suggestions inbox, a
 * per-schedule instance panel (skip / override / reset, a card payment's
 * statement balance) and the create/edit form (payee + memo, #534). The page
 * dogfoods the same REST surface agents use. */

import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, errorDetail } from "../lib/api";
import { overrideBody } from "../lib/utils";
import { compareMoney, formatMoney, negateMoney } from "../lib/money";
import { formatDate } from "../lib/dates";
import { amountBasisLabel } from "../lib/labels";
import { useCategoryNames, type PathName } from "../lib/names";
import { plotCoordinate } from "../lib/plot";
import type { components } from "../api/schema";
import { OccurrenceCertainty, OccurrenceEvidence } from "../components/OccurrenceCertainty";
import { Badge, Button, Card, CardTitle, ErrorText, Input, Table, Td, Th } from "../components/ui";
import { QueryState } from "../components/QueryState";
import { cadenceLabel, selectClass } from "./schedules/cadence";
import ScheduleForm from "./schedules/ScheduleForm";

export { cadenceLabel };

type ScheduleOut = components["schemas"]["ScheduleOut"];
type ScheduleDetail = components["schemas"]["ScheduleDetail"];
type AccountOut = components["schemas"]["AccountOut"];
type ForecastOut = components["schemas"]["ForecastOut"];

/* --------------------------------------------------------------------------
 * Instances panel (skip / override / reset / statement)
 * ------------------------------------------------------------------------ */

type InstanceOut = ScheduleDetail["instances"][number];

/** Where an instance's amount comes from (decision 80), when it is worth a
 * badge: a recorded card statement, a user's override, or the card's current
 * balance a card payment estimates from. */
function BasisBadge({ instance, currency }: { instance: InstanceOut; currency?: string | null }) {
  if (instance.basis === "statement") {
    return (
      <Badge
        variant="green"
        title={`statement balance ${formatMoney(instance.statement_balance, { currency })}`}
      >
        {instance.statement_date ? `statement · closed ${formatDate(instance.statement_date)}` : "statement"}
      </Badge>
    );
  }
  if (instance.pinned) return <Badge variant="amber">pinned</Badge>;
  if (instance.basis === "counterparty_balance") return <Badge>current balance</Badge>;
  return null;
}

export function InstancesPanel({
  scheduleId,
  cardPayment = false,
}: {
  scheduleId: string;
  /** a payment to a credit card: offers recording its statement balance */
  cardPayment?: boolean;
}) {
  const qc = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState<{ amount: string; date: string }>({ amount: "", date: "" });
  // The card statement form (#351): the balance as printed and its closing date.
  const [stating, setStating] = useState<string | null>(null);
  const [statement, setStatement] = useState<{ balance: string; closed: string }>({ balance: "", closed: "" });
  const detail = useQuery({
    queryKey: ["schedule", scheduleId],
    queryFn: async () => {
      const { data, error: err } = await api.GET("/schedules/{schedule_id}", {
        params: { path: { schedule_id: scheduleId } },
      });
      if (err) throw new Error(errorDetail(err));
      return data as ScheduleDetail;
    },
  });
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["schedule", scheduleId] });
    qc.invalidateQueries({ queryKey: ["schedules"] });
    qc.invalidateQueries({ queryKey: ["forecast"] });
  };
  const skip = useMutation({
    mutationFn: async (txId: string) => {
      const { error: err } = await api.POST("/transactions/{tx_id}/void", { params: { path: { tx_id: txId } } });
      if (err) throw new Error(errorDetail(err));
    },
    onSuccess: refresh,
    onError: (e) => setError(e.message),
  });
  const reset = useMutation({
    mutationFn: async (txId: string) => {
      const { error: err } = await api.POST("/transactions/{tx_id}/reset-instance", {
        params: { path: { tx_id: txId } },
      });
      if (err) throw new Error(errorDetail(err));
    },
    onSuccess: refresh,
    onError: (e) => setError(e.message),
  });
  const override = useMutation({
    mutationFn: async (instance: InstanceOut) => {
      const body = overrideBody(instance, draft);
      if (Object.keys(body).length === 0) return; // nothing changed: no edit, no pin
      const { error: err } = await api.PATCH("/transactions/{tx_id}", {
        params: { path: { tx_id: instance.transaction_id } },
        body,
      });
      if (err) throw new Error(errorDetail(err));
    },
    onSuccess: () => {
      setEditing(null);
      refresh();
    },
    onError: (e) => setError(e.message),
  });

  const record = useMutation({
    mutationFn: async (txId: string) => {
      const { error: err } = await api.POST("/schedules/statement-balance", {
        body: {
          transaction_id: txId,
          statement_balance: statement.balance.trim(),
          ...(statement.closed ? { statement_date: statement.closed } : {}),
        },
      });
      if (err) throw new Error(errorDetail(err));
    },
    onSuccess: () => {
      setStating(null);
      refresh();
    },
    onError: (e) => setError(e.message),
  });

  if (detail.data === undefined) return <QueryState query={detail} what="instances" />;
  const currency = detail.data.currency;
  const instances = detail.data.instances ?? [];
  const matched = detail.data.matched_instances ?? [];
  if (instances.length === 0 && matched.length === 0) return <p className="text-sm text-muted-foreground">No live instances.</p>;
  return (
    <div className="mt-2">
      <Table>
        <thead>
          <tr>
            <Th>Due</Th>
            <Th>Slot</Th>
            <Th className="text-right">Amount</Th>
            <Th></Th>
            <Th></Th>
          </tr>
        </thead>
        <tbody>
          {instances.map((i) => (
            <tr key={i.transaction_id}>
              <Td className="whitespace-nowrap">{formatDate(i.date)}</Td>
              <Td className="whitespace-nowrap text-muted-foreground">{formatDate(i.occurrence_date)}</Td>
              <Td className="text-right font-mono">{formatMoney(i.amount, { currency })}</Td>
              <Td>
                <span className="flex gap-1">
                  {i.overdue && <Badge variant="red">overdue</Badge>}
                  <BasisBadge instance={i} currency={currency} />
                  <OccurrenceCertainty value={i.certainty} />
                </span>
              </Td>
              <Td>
                <OccurrenceEvidence transactionId={i.transaction_id} onSaved={refresh} />
                {stating === i.transaction_id ? (
                  <span className="flex flex-wrap items-center gap-1">
                    <Input
                      className="w-32"
                      value={statement.balance}
                      onChange={(e) => setStatement({ ...statement, balance: e.target.value })}
                      placeholder="statement balance"
                      aria-label="Statement balance (as printed)"
                    />
                    <Input
                      className="w-36"
                      type="date"
                      value={statement.closed}
                      onChange={(e) => setStatement({ ...statement, closed: e.target.value })}
                      aria-label="Statement closing date"
                    />
                    <Button size="sm" onClick={() => record.mutate(i.transaction_id)}>
                      Save
                    </Button>
                    <Button size="sm" variant="ghost" onClick={() => setStating(null)}>
                      Cancel
                    </Button>
                  </span>
                ) : editing === i.transaction_id ? (
                  <span className="flex flex-wrap items-center gap-1">
                    <Input
                      className="w-28"
                      value={draft.amount}
                      onChange={(e) => setDraft({ ...draft, amount: e.target.value })}
                      placeholder="amount"
                    />
                    <Input
                      className="w-36"
                      type="date"
                      value={draft.date}
                      onChange={(e) => setDraft({ ...draft, date: e.target.value })}
                    />
                    <Button size="sm" onClick={() => override.mutate(i)}>
                      Save
                    </Button>
                    <Button size="sm" variant="ghost" onClick={() => setEditing(null)}>
                      Cancel
                    </Button>
                  </span>
                ) : (
                  <span className="flex gap-1">
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() => {
                        setDraft({ amount: i.amount ?? "", date: i.date });
                        setStating(null);
                        setEditing(i.transaction_id);
                      }}
                    >
                      Override
                    </Button>
                    {cardPayment && (
                      <Button
                        size="sm"
                        variant="outline"
                        title="Record the card statement balance this payment pays"
                        onClick={() => {
                          setStatement({ balance: i.statement_balance ?? "", closed: i.statement_date ?? "" });
                          setEditing(null);
                          setStating(i.transaction_id);
                        }}
                      >
                        Statement
                      </Button>
                    )}
                    {i.pinned && (
                      <Button
                        size="sm"
                        variant="ghost"
                        title="Undo your override — the rule's date, name and estimate come back"
                        onClick={() => reset.mutate(i.transaction_id)}
                      >
                        Reset
                      </Button>
                    )}
                    <Button
                      size="sm"
                      variant="ghost"
                      title="Skip this occurrence — it will not come back"
                      onClick={() => skip.mutate(i.transaction_id)}
                    >
                      Skip
                    </Button>
                  </span>
                )}
              </Td>
            </tr>
          ))}
        </tbody>
      </Table>
      {matched.length > 0 && <details className="mt-3">
        <summary>Recently matched occurrences ({matched.length}, up to 50)</summary>
        {matched.map(i => <div key={i.transaction_id} className="mt-2">
          <span>{formatDate(i.date)} · {formatMoney(i.amount, { currency })}</span>
          <OccurrenceCertainty value={i.certainty} />
        </div>)}
      </details>}
      {error && <ErrorText>{error}</ErrorText>}
    </div>
  );
}

/* --------------------------------------------------------------------------
 * Forecast chart
 * ------------------------------------------------------------------------ */

/** How a forecast event's amount came about (decisions 80, 85). */
const CHART_W = 720;
const CHART_H = 220;
const PAD = { l: 56, r: 12, t: 12, b: 28 };

export function ForecastChart({
  accountPath,
  currency,
}: {
  accountPath: string;
  /** The account's currency (USD when unknown). */
  currency?: string | null;
}) {
  const money = (value: string | null | undefined) => formatMoney(value, { currency });
  const [days, setDays] = useState<30 | 90 | 365>(30);
  const forecast = useQuery({
    queryKey: ["forecast", accountPath, days],
    enabled: accountPath !== "",
    queryFn: async () => {
      const { data, error } = await api.GET("/forecast", {
        params: { query: { account: accountPath, days } },
      });
      if (error) throw new Error(errorDetail(error));
      return data as ForecastOut;
    },
  });
  const data = forecast.data;
  const geometry = useMemo(() => {
    if (!data) return null;
    // Money stays decimal strings: the extremes, the floor and the low day
    // are picked by exact string comparison, and the axis labels show those
    // original strings. Only the pixel positions go through plotCoordinate.
    const balances = data.days.map((d) => d.balance);
    // The estimate range band (#247): every day's pessimistic/optimistic
    // balance, present only when an estimated event carries a range.
    const banded = data.days.filter((d) => d.low != null && d.high != null);
    const bandValues = banded.flatMap((d) => [d.low!, d.high!]);
    const floor = data.min_balance ?? "0";
    const all = [...balances, ...bandValues, floor, data.start_balance];
    const lo = all.reduce((a, b) => (compareMoney(b, a) < 0 ? b : a));
    const hi = all.reduce((a, b) => (compareMoney(b, a) > 0 ? b : a));
    const loY = plotCoordinate(lo);
    const span = plotCoordinate(hi) - loY || 1;
    const x = (i: number) => PAD.l + (i / Math.max(data.days.length - 1, 1)) * (CHART_W - PAD.l - PAD.r);
    const y = (value: string | null | undefined) =>
      PAD.t + (1 - (plotCoordinate(value) - loY) / span) * (CHART_H - PAD.t - PAD.b);
    // A step line: the balance holds until the day an event lands.
    const path = data.days
      .map((d, i) =>
        i === 0
          ? `M${x(0).toFixed(1)},${y(d.balance).toFixed(1)}`
          : `H${x(i).toFixed(1)} V${y(d.balance).toFixed(1)}`,
      )
      .join(" ");
    const events = data.days.flatMap((d, i) =>
      d.events.map((e) => ({ ...e, x: x(i), y: y(d.balance), date: d.date })),
    );
    // The first day at the lowest balance.
    const lowIndex = balances.reduce(
      (best, b, i) => (compareMoney(b, balances[best]) < 0 ? i : best),
      0,
    );
    // Stepped like the line: highs left to right, back along the lows.
    const edge = (pick: "low" | "high") =>
      data.days.flatMap((d, i) => {
        const v = y(d[pick]);
        const prev = i > 0 ? y(data.days[i - 1][pick]) : v;
        return i === 0 ? [[x(0), v]] : [[x(i), prev], [x(i), v]];
      });
    const band =
      banded.length === data.days.length && banded.length > 1
        ? `${[...edge("high"), ...edge("low").reverse()]
            .map(([px, py], i) => `${i === 0 ? "M" : "L"}${px.toFixed(1)},${py.toFixed(1)}`)
            .join(" ")} Z`
        : null;
    return { x, y, path, events, floor, lo, hi, lowIndex, band };
  }, [data]);

  return (
    <div>
      <div className="flex items-center justify-between">
        <p className="text-sm font-medium">Forecast</p>
        <div className="flex gap-1">
          {([30, 90, 365] as const).map((d) => (
            <Button key={d} size="sm" variant={d === days ? "default" : "outline"} onClick={() => setDays(d)}>
              {d}d
            </Button>
          ))}
        </div>
      </div>
      <QueryState query={forecast} what="the forecast" />
      {data && geometry && (
        <>
          <svg viewBox={`0 0 ${CHART_W} ${CHART_H}`} className="mt-2 w-full" role="img" aria-label={`Forecast for ${accountPath}`}>
            <line
              x1={PAD.l}
              x2={CHART_W - PAD.r}
              y1={geometry.y(geometry.floor)}
              y2={geometry.y(geometry.floor)}
              stroke="currentColor"
              strokeDasharray="4 4"
              opacity={0.4}
            />
            <text x={PAD.l - 6} y={geometry.y(geometry.hi) + 4} textAnchor="end" fontSize={10} fill="currentColor">
              {money(geometry.hi)}
            </text>
            <text x={PAD.l - 6} y={geometry.y(geometry.lo) + 4} textAnchor="end" fontSize={10} fill="currentColor">
              {money(geometry.lo)}
            </text>
            {geometry.band && <path data-testid="forecast-band" d={geometry.band} fill="var(--chart-band)" stroke="none" />}
            <path d={geometry.path} fill="none" stroke="currentColor" strokeWidth={1.5} />
            {geometry.events.map((e, i) => (
              <circle
                key={i}
                cx={e.x}
                cy={e.y}
                r={3.5}
                fill={e.projected ? "var(--color-card)" : "currentColor"}
                stroke="currentColor"
                strokeWidth={1.5}
              >
                <title>
                  {`${e.label} ${money(e.amount)} · ${formatDate(e.date)}${e.projected ? " · projected" : ""}${e.overdue ? " · overdue" : ""}` +
                    (e.basis ? ` · ${amountBasisLabel(e.basis)}` : "") +
                    (e.range ? ` · ${money(e.range.low)} to ${money(e.range.high)}` : "")}
                </title>
              </circle>
            ))}
            <text x={PAD.l} y={CHART_H - 8} fontSize={10} fill="currentColor">
              {formatDate(data.days[0]?.date)}
            </text>
            <text x={CHART_W - PAD.r} y={CHART_H - 8} fontSize={10} textAnchor="end" fill="currentColor">
              {formatDate(data.days[data.days.length - 1]?.date)}
            </text>
          </svg>
          <details className="mt-2 text-sm">
            <summary>Upcoming occurrences and estimate details</summary>
            <div className="max-h-80 space-y-2 overflow-auto">
              {data.days.flatMap(day => day.events.map((event, index) => <div key={`${day.date}-${index}`}>
                <span>{formatDate(day.date)} · {event.label} · {money(event.amount)}</span>
                <span className="text-xs text-muted-foreground"> · Amount calculation: {event.basis ? amountBasisLabel(event.basis) : "not recorded"}</span>
                <OccurrenceCertainty value={event.certainty} />
                {event.certainty?.slot && <OccurrenceEvidence slot={event.certainty.slot} onSaved={() => { void forecast.refetch(); }} />}
              </div>))}
            </div>
          </details>
          <p className="mt-1 text-xs text-muted-foreground">
            {data.coverage.schedules_active === 0
              ? "No scheduled transactions are modeled on this account — the line is flat by construction."
              : `Modeled from ${data.coverage.schedules_active} scheduled transaction${data.coverage.schedules_active === 1 ? "" : "s"}: ${data.coverage.instances} instance${data.coverage.instances === 1 ? "" : "s"}, ${data.coverage.projected} projected (hollow).`}
            {data.coverage.suggestions_open > 0 && (
              <>
                {" "}
                <Link to="/" className="underline">
                  {data.coverage.suggestions_open} suggestion{data.coverage.suggestions_open === 1 ? "" : "s"} open
                </Link>
                .
              </>
            )}
            {(data.coverage.matches_pending ?? 0) > 0 &&
              ` ${data.coverage.matches_pending} awaiting match confirmation (left out as paid).`}
            {data.first_shortfall && ` Projected below the floor on ${formatDate(data.first_shortfall)}.`}
            {` Low ${money(data.days[geometry.lowIndex]?.balance)} on ${formatDate(data.days[geometry.lowIndex]?.date)}.`}
            {data.pessimistic_low &&
              compareMoney(data.pessimistic_low.balance, geometry.floor) < 0 &&
              !data.first_shortfall &&
              ` Could dip to ${money(data.pessimistic_low.balance)} on ${formatDate(data.pessimistic_low.date)} if estimates come in at the edge of their range (shaded).`}
          </p>
        </>
      )}
    </div>
  );
}

/* --------------------------------------------------------------------------
 * Page: forecast first, then scheduled transactions grouped by account
 * ------------------------------------------------------------------------ */

/** A schedule as one account sees it: a transfer is listed under both of
 * its accounts (#535), the counterparty's copy marked `mirror`, with the
 * amount in that account's sign. */
type Row = { schedule: ScheduleOut; mirror: boolean; other?: AccountOut };

/** The other side's leg: flip the sign of a decimal string (no floats). */
export function negateAmount(value: string | null | undefined): string | null {
  return value == null ? null : negateMoney(value);
}

export function groupByAccount(
  schedules: ScheduleOut[],
  accounts: AccountOut[],
): { account: AccountOut; rows: Row[] }[] {
  const byId = new Map(accounts.map((a) => [a.id, a]));
  const groups = new Map<string, Row[]>();
  const push = (accountId: string, row: Row) => {
    const rows = groups.get(accountId) ?? [];
    rows.push(row);
    groups.set(accountId, rows);
  };
  for (const s of schedules) {
    const other = s.counterparty_account_id ? byId.get(s.counterparty_account_id) : undefined;
    push(s.account_id, { schedule: s, mirror: false, other });
    if (s.counterparty_account_id) {
      push(s.counterparty_account_id, { schedule: s, mirror: true, other: byId.get(s.account_id) });
    }
  }
  const due = (r: Row) => r.schedule.next_due ?? "9999-12-31";
  return [...groups.entries()]
    .filter(([id]) => byId.has(id))
    .map(([id, rows]) => ({
      account: byId.get(id)!,
      rows: rows.sort((a, b) => due(a).localeCompare(due(b)) || a.schedule.name.localeCompare(b.schedule.name)),
    }))
    .sort((a, b) => a.account.name.localeCompare(b.account.name));
}

export default function Schedules() {
  const qc = useQueryClient();
  const [statusFilter, setStatusFilter] = useState<"active" | "paused" | "ended" | "all">("active");
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<ScheduleOut | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [forecastAccount, setForecastAccount] = useState<string>("");

  const accounts = useQuery({
    queryKey: ["accounts", "schedules-page"],
    queryFn: async () => {
      const { data, error: err } = await api.GET("/accounts", {
        params: { query: { include_balances: false, include_hidden: true, include_closed: true } },
      });
      if (err) throw new Error(errorDetail(err));
      return data ?? [];
    },
  });
  const schedules = useQuery({
    queryKey: ["schedules", statusFilter],
    queryFn: async () => {
      const { data, error: err } = await api.GET("/schedules", {
        params: { query: statusFilter === "all" ? {} : { status: [statusFilter] } },
      });
      if (err) throw new Error(errorDetail(err));
      return data ?? [];
    },
  });
  // The open total, not the length of one capped inbox page (#386 review).
  const suggestionCounts = useQuery({
    queryKey: ["suggestions", "counts"],
    queryFn: async () => (await api.GET("/suggestions/counts")).data,
  });
  const suggestionsOpen = suggestionCounts.data?.total ?? 0;

  const categoryName = useCategoryNames();
  const allAccounts = accounts.data ?? [];
  const realAccounts = allAccounts.filter((a) => !a.is_virtual);
  const accountById = new Map(allAccounts.map((a) => [a.id, a]));
  const groups = groupByAccount(schedules.data ?? [], realAccounts);
  const chartAccount =
    forecastAccount ||
    groups.find((g) => !g.account.closed_at)?.account.path ||
    realAccounts.find((a) => !a.closed_at)?.path ||
    "";

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["schedules"] });
    qc.invalidateQueries({ queryKey: ["forecast"] });
    qc.invalidateQueries({ queryKey: ["suggestions"] });
  };
  const action = useMutation({
    mutationFn: async ({ id, verb }: { id: string; verb: "pause" | "resume" | "end" }) => {
      const params = { path: { schedule_id: id } };
      const { error: err } =
        verb === "pause"
          ? await api.POST("/schedules/{schedule_id}/pause", { params })
          : verb === "resume"
            ? await api.POST("/schedules/{schedule_id}/resume", { params })
            : await api.POST("/schedules/{schedule_id}/end", { params });
      if (err) throw new Error(errorDetail(err));
    },
    onSuccess: refresh,
    onError: (e) => setError(e.message),
  });
  const detect = useMutation({
    mutationFn: async () => {
      const { data, error: err } = await api.POST("/schedules/detect");
      if (err) throw new Error(errorDetail(err));
      return data;
    },
    onSuccess: (data) => {
      refresh();
      setNotice(
        data
          ? `Detection found ${data.created} new suggestion${data.created === 1 ? "" : "s"} and refreshed ${data.refreshed}.`
          : "Detection ran.",
      );
    },
    onError: (e) => setError(e.message),
  });

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-2xl font-bold">Scheduled transactions</h1>
        <select
          className={selectClass}
          aria-label="Show status"
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value as typeof statusFilter)}
        >
          <option value="active">Active</option>
          <option value="paused">Paused</option>
          <option value="ended">Ended</option>
          <option value="all">All</option>
        </select>
        <span className="grow" />
        <Button variant="outline" disabled={detect.isPending} onClick={() => detect.mutate()}>
          Detect now
        </Button>
        <Button
          onClick={() => {
            setEditing(null);
            setCreating(true);
          }}
        >
          New scheduled transaction
        </Button>
      </div>
      {suggestionsOpen > 0 && (
        <p className="text-sm text-muted-foreground">
          {suggestionsOpen} suggestion{suggestionsOpen === 1 ? " awaits" : "s await"} a decision on the{" "}
          <Link to="/" className="underline">
            Dashboard
          </Link>
          .
        </p>
      )}
      {notice && <p className="text-sm text-muted-foreground">{notice}</p>}
      {error && <ErrorText>{error}</ErrorText>}

      {creating && accounts.data && (
        <ScheduleForm
          accounts={accounts.data}
          defaultAccount={chartAccount}
          onDone={() => {
            setCreating(false);
            refresh();
          }}
          onCancel={() => setCreating(false)}
        />
      )}
      {editing && accounts.data && (
        <ScheduleForm
          key={editing.id}
          accounts={accounts.data}
          existing={editing}
          onDone={() => {
            setEditing(null);
            refresh();
          }}
          onCancel={() => setEditing(null)}
        />
      )}

      <Card>
        <div className="mb-2 flex flex-wrap items-center gap-3">
          <CardTitle>Account forecast</CardTitle>
          <select
            className={selectClass}
            aria-label="Forecast account"
            value={chartAccount}
            onChange={(e) => setForecastAccount(e.target.value)}
          >
            {realAccounts
              .filter((a) => !a.closed_at || a.path === chartAccount)
              .map((a) => (
                <option key={a.id} value={a.path}>
                  {a.name}
                </option>
              ))}
          </select>
        </div>
        <QueryState query={accounts} what="accounts" />
        {chartAccount && (
          <ForecastChart
            accountPath={chartAccount}
            currency={realAccounts.find((a) => a.path === chartAccount)?.currency}
          />
        )}
      </Card>

      <QueryState
        query={schedules}
        what="scheduled transactions"
        empty={
          <Card>
            <p className="text-sm text-muted-foreground">
              No {statusFilter === "all" ? "" : statusFilter + " "}scheduled transactions yet — create one, or
              run detection and accept what Purser finds.
            </p>
          </Card>
        }
      />
      {groups.map(({ account, rows }) => {
        const shown = account.path === chartAccount;
        const next = rows.map((r) => r.schedule.next_due).filter(Boolean).sort()[0];
        return (
          <section
            key={account.id}
            aria-label={account.name}
            className={shown ? "rounded-lg ring-2 ring-primary" : undefined}
          >
            <Card>
              <div className="mb-2 flex flex-wrap items-baseline gap-x-3 gap-y-1">
                <h2 className="text-lg font-semibold">{account.name}</h2>
                <span className="text-sm text-muted-foreground">
                  {rows.length} scheduled{next ? ` · next due ${formatDate(next)}` : ""}
                </span>
                <span className="grow" />
                {shown ? (
                  <span className="text-xs text-muted-foreground">Shown in the forecast</span>
                ) : (
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => {
                      setForecastAccount(account.path);
                      window.scrollTo?.({ top: 0, behavior: "smooth" });
                    }}
                  >
                    Show forecast
                  </Button>
                )}
              </div>
              <ul className="divide-y divide-border lg:hidden">
                {rows.map((row) => {
                  const key = `${account.id}:${row.schedule.id}`;
                  return (
                    <ScheduleItem
                      categoryName={categoryName}
                      key={key}
                      row={row}
                      cardPayment={
                        !!row.schedule.counterparty_account_id &&
                        accountById.get(row.schedule.counterparty_account_id)?.type === "credit_card"
                      }
                      expanded={expanded === key}
                      onToggle={() => setExpanded(expanded === key ? null : key)}
                      onEdit={() => {
                        setCreating(false);
                        setEditing(row.schedule);
                        window.scrollTo?.({ top: 0, behavior: "smooth" });
                      }}
                      onAction={(verb) => action.mutate({ id: row.schedule.id, verb })}
                    />
                  );
                })}
              </ul>
              <div className="hidden lg:block">
                <Table className="table-fixed">
                  <colgroup>
                    <col className="w-[16%]" />
                    <col className="w-[11%]" />
                    <col className="w-[13%]" />
                    <col className="w-[13%]" />
                    <col className="w-[11%]" />
                    <col className="w-[10%]" />
                    <col className="w-[7%]" />
                    <col className="w-[19%]" />
                  </colgroup>
                  <thead>
                    <tr>
                      <Th>Payee</Th>
                      <Th>Memo</Th>
                      <Th>Category</Th>
                      <Th>Repeats</Th>
                      <Th className="text-right">Amount</Th>
                      <Th>Next due</Th>
                      <Th>Status</Th>
                      <Th>
                        <span className="sr-only">Actions</span>
                      </Th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((row) => {
                      const key = `${account.id}:${row.schedule.id}`;
                      return (
                        <ScheduleRow
                          categoryName={categoryName}
                          key={key}
                          row={row}
                          cardPayment={
                            !!row.schedule.counterparty_account_id &&
                            accountById.get(row.schedule.counterparty_account_id)?.type === "credit_card"
                          }
                          expanded={expanded === key}
                          onToggle={() => setExpanded(expanded === key ? null : key)}
                          onEdit={() => {
                            setCreating(false);
                            setEditing(row.schedule);
                            window.scrollTo?.({ top: 0, behavior: "smooth" });
                          }}
                          onAction={(verb) => action.mutate({ id: row.schedule.id, verb })}
                        />
                      );
                    })}
                  </tbody>
                </Table>
              </div>
            </Card>
          </section>
        );
      })}
    </div>
  );
}

type RowProps = {
  row: Row;
  /** Category path -> name (#541): the table shows names, never paths. */
  categoryName: PathName;
  cardPayment: boolean;
  expanded: boolean;
  onToggle: () => void;
  onEdit: () => void;
  onAction: (verb: "pause" | "resume" | "end") => void;
};

function rowLabels({ schedule: s, mirror, other }: Row) {
  const isTransfer = !!s.counterparty_account_id;
  const otherName = other?.name ?? "another account";
  const who = isTransfer
    ? mirror
      ? `Transfer from ${otherName}`
      : `Transfer to ${otherName}`
    : (s.payee ?? s.name);
  const amount = mirror ? negateAmount(s.amount) : s.amount;
  const amountText =
    s.amount_type === "estimated"
      ? s.estimate_basis === "counterparty_balance"
        ? "pays balance"
        : amount != null
          ? `~${formatMoney(amount, { currency: s.currency })}`
          : "from history"
      : formatMoney(amount, { currency: s.currency });
  return { isTransfer, who, amountText };
}

function RowActions({ s, onEdit, onAction }: { s: ScheduleOut } & Pick<RowProps, "onEdit" | "onAction">) {
  return (
    <span className="flex flex-wrap gap-1">
      {s.status !== "ended" && (
        <Button size="sm" variant="outline" onClick={onEdit}>
          Edit
        </Button>
      )}
      {s.status === "active" && (
        <Button size="sm" variant="ghost" onClick={() => onAction("pause")}>
          Pause
        </Button>
      )}
      {s.status === "paused" && (
        <Button size="sm" variant="ghost" onClick={() => onAction("resume")}>
          Resume
        </Button>
      )}
      {s.status !== "ended" && (
        <Button
          size="sm"
          variant="ghost"
          onClick={() => {
            if (window.confirm(`End ${s.name}? This is permanent; history stays.`)) onAction("end");
          }}
        >
          End
        </Button>
      )}
    </span>
  );
}

function StatusBadge({ status }: { status: string }) {
  return (
    <Badge variant={status === "active" ? "green" : status === "paused" ? "amber" : "default"}>{status}</Badge>
  );
}

/** Phone layout: one stacked item per scheduled transaction. */
function ScheduleItem({ row, cardPayment, expanded, onToggle, onEdit, onAction }: RowProps) {
  const s = row.schedule;
  const { who, amountText } = rowLabels(row);
  return (
    <li className="py-3 text-sm">
      <div className="flex items-baseline justify-between gap-3">
        <button
          className="text-left font-medium underline-offset-2 hover:underline"
          aria-expanded={expanded}
          onClick={onToggle}
        >
          {who}
        </button>
        <span className="whitespace-nowrap font-mono">{amountText}</span>
      </div>
      <p className="mt-1 text-muted-foreground">
        {[s.memo, cadenceLabel(s.rrule), s.next_due ? `next ${formatDate(s.next_due)}` : null].filter(Boolean).join(" · ")}
      </p>
      <div className="mt-2 flex flex-wrap items-center gap-2">
        <StatusBadge status={s.status} />
        <RowActions s={s} onEdit={onEdit} onAction={onAction} />
      </div>
      {expanded && (
        <div className="mt-2 overflow-x-auto">
          <InstancesPanel scheduleId={s.id} cardPayment={cardPayment} />
        </div>
      )}
    </li>
  );
}

function ScheduleRow({ row, categoryName, cardPayment, expanded, onToggle, onEdit, onAction }: RowProps) {
  const s = row.schedule;
  const { isTransfer, who, amountText } = rowLabels(row);
  return (
    <>
      <tr>
        <Td>
          <button
            className="text-left font-medium underline-offset-2 hover:underline"
            aria-expanded={expanded}
            onClick={onToggle}
          >
            {who}
          </button>
        </Td>
        <Td className="text-muted-foreground">{s.memo ?? ""}</Td>
        <Td
          className="truncate text-muted-foreground"
          title={isTransfer || !s.category_path ? undefined : categoryName(s.category_path)}
        >
          {isTransfer ? "Transfer" : categoryName(s.category_path)}
        </Td>
        <Td>{cadenceLabel(s.rrule)}</Td>
        <Td
          className={`whitespace-nowrap text-right font-mono${s.amount_type === "estimated" ? " text-muted-foreground" : ""}`}
          title={s.amount_type === "estimated" ? s.estimate_basis ? `Estimated from the ${amountBasisLabel(s.estimate_basis)}` : "Estimated from history" : undefined}
        >
          {amountText}
        </Td>
        <Td className="whitespace-nowrap">{formatDate(s.next_due)}</Td>
        <Td>
          <StatusBadge status={s.status} />
        </Td>
        <Td>
          <RowActions s={s} onEdit={onEdit} onAction={onAction} />
        </Td>
      </tr>
      {expanded && (
        <tr>
          <Td colSpan={8}>
            <InstancesPanel scheduleId={s.id} cardPayment={cardPayment} />
          </Td>
        </tr>
      )}
    </>
  );
}
