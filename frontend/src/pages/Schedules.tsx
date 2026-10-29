/** Schedules page (#236): the rules behind the forecast. List with status
 * filter, a create/edit form whose cadence picker writes the RRULE, pause /
 * resume / end, "Detect now" into the suggestions inbox, a per-schedule
 * instance panel (skip / override / reset, a card payment's statement
 * balance), and a per-account forecast chart
 * where projected occurrences draw hollow and the coverage line says what the
 * projection is made of. The page dogfoods the same REST surface agents use. */

import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, errorDetail } from "../lib/api";
import { fmtAmount, overrideBody } from "../lib/utils";
import type { components } from "../api/schema";
import CategorySelect from "../components/CategorySelect";
import { Badge, Button, Card, CardTitle, ErrorText, Input, Label, Table, Td, Th } from "../components/ui";

type ScheduleOut = components["schemas"]["ScheduleOut"];
type ScheduleDetail = components["schemas"]["ScheduleDetail"];
type ScheduleCreate = components["schemas"]["ScheduleCreate"];
type AccountOut = components["schemas"]["AccountOut"];
type ForecastOut = components["schemas"]["ForecastOut"];

const selectClass =
  "h-9 rounded-md border border-input bg-background px-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring";

const WEEKDAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"] as const;
const WEEKDAY_LABELS: Record<string, string> = {
  MO: "Monday",
  TU: "Tuesday",
  WE: "Wednesday",
  TH: "Thursday",
  FR: "Friday",
  SA: "Saturday",
  SU: "Sunday",
};

type Cadence = "weekly" | "biweekly" | "semimonthly" | "monthly" | "quarterly" | "yearly" | "custom";

/** Cadence picker state → RRULE. The picker covers the shapes detection
 * proposes; anything else is a raw RRULE. */
function buildRrule(c: {
  cadence: Cadence;
  weekday: string;
  day: number;
  day2: number;
  month: number;
  custom: string;
}): string {
  const day = Math.min(Math.max(c.day, 1), 28);
  const day2 = Math.min(Math.max(c.day2, 1), 28);
  switch (c.cadence) {
    case "weekly":
      return `FREQ=WEEKLY;BYDAY=${c.weekday}`;
    case "biweekly":
      return `FREQ=WEEKLY;INTERVAL=2;BYDAY=${c.weekday}`;
    case "semimonthly":
      return `FREQ=MONTHLY;BYMONTHDAY=${Math.min(day, day2)},${Math.max(day, day2)}`;
    case "monthly":
      return `FREQ=MONTHLY;BYMONTHDAY=${day}`;
    case "quarterly":
      return `FREQ=MONTHLY;INTERVAL=3;BYMONTHDAY=${day}`;
    case "yearly":
      return `FREQ=YEARLY;BYMONTH=${c.month};BYMONTHDAY=${day}`;
    default:
      return c.custom.trim();
  }
}

function rruleParams(rrule: string): Record<string, string> {
  /* Only the RRULE line: a rule may carry EXDATE/RDATE lines after it
   * (agent-authored schedules do), which are not key=value parameters. */
  const line = rrule.split(/\r?\n/).find((l) => /^(RRULE:)?FREQ=/i.test(l.trim())) ?? rrule;
  const out: Record<string, string> = {};
  for (const part of line.trim().replace(/^RRULE:/i, "").split(";")) {
    const [k, v] = part.split("=");
    if (k && v) out[k.toUpperCase()] = v.trim();
  }
  return out;
}

/** Human cadence for the table, mirroring core_forecasting.cadence_label
 * plus the day-of-month detail the RRULE carries. */
export function cadenceLabel(rrule: string): string {
  const p = rruleParams(rrule);
  const interval = Number(p.INTERVAL ?? "1");
  const days = p.BYMONTHDAY ? ` on the ${p.BYMONTHDAY.split(",").join(" & ")}` : "";
  const weekday = p.BYDAY ? ` on ${WEEKDAY_LABELS[p.BYDAY] ?? p.BYDAY}` : "";
  switch (p.FREQ) {
    case "WEEKLY":
      return (interval === 1 ? "weekly" : interval === 2 ? "every 2 weeks" : `every ${interval} weeks`) + weekday;
    case "MONTHLY":
      if (interval === 1) return (p.BYMONTHDAY?.includes(",") ? "semi-monthly" : "monthly") + days;
      return `every ${interval} months${days}`;
    case "YEARLY":
      return `yearly${p.BYMONTH ? ` in month ${p.BYMONTH}` : ""}${days}${/EXDATE/i.test(rrule) ? " (with exclusions)" : ""}`;
    case "DAILY":
      return interval === 1 ? "daily" : `every ${interval} days`;
    default:
      return rrule;
  }
}

function today(): string {
  return new Date().toISOString().slice(0, 10);
}

/* --------------------------------------------------------------------------
 * Create / edit form
 * ------------------------------------------------------------------------ */

type FormState = {
  name: string;
  account: string;
  target: string; // category path, or "[account.path]" for a transfer counterparty
  cadence: Cadence;
  weekday: string;
  day: number;
  day2: number;
  month: number;
  custom: string;
  dtstart: string;
  amount_type: "fixed" | "estimated";
  estimate_basis: "last" | "rolling_avg_3" | "rolling_avg_12" | "counterparty_balance";
  amount: string;
  payee: string;
  autopost_policy: "auto_post" | "wait_for_match" | "reminder_only";
};

function initialForm(accounts: AccountOut[], existing?: ScheduleOut, accountPath?: string): FormState {
  const p = existing ? rruleParams(existing.rrule) : {};
  const bymonthday = p.BYMONTHDAY?.split(",").map(Number) ?? [];
  let cadence: Cadence = "monthly";
  if (existing) {
    if (p.FREQ === "WEEKLY") cadence = Number(p.INTERVAL ?? "1") === 2 ? "biweekly" : "weekly";
    else if (p.FREQ === "MONTHLY" && Number(p.INTERVAL ?? "1") === 3) cadence = "quarterly";
    else if (p.FREQ === "MONTHLY" && bymonthday.length === 2) cadence = "semimonthly";
    else if (p.FREQ === "MONTHLY" && Number(p.INTERVAL ?? "1") === 1) cadence = "monthly";
    else if (p.FREQ === "YEARLY") cadence = "yearly";
    else cadence = "custom";
  }
  const counterparty = existing?.counterparty_account_id
    ? accounts.find((a) => a.id === existing.counterparty_account_id)?.path
    : undefined;
  return {
    name: existing?.name ?? "",
    account: accountPath ?? accounts.find((a) => !a.is_virtual)?.path ?? "",
    target: counterparty ? `[${counterparty}]` : (existing?.category_path ?? ""),
    cadence,
    weekday: p.BYDAY ?? "FR",
    day: bymonthday[0] ?? Number(today().slice(8, 10)),
    day2: bymonthday[1] ?? 20,
    month: Number(p.BYMONTH ?? today().slice(5, 7)),
    custom: existing?.rrule ?? "",
    dtstart: existing?.dtstart ?? today(),
    amount_type: (existing?.amount_type as "fixed" | "estimated") ?? "fixed",
    estimate_basis: (existing?.estimate_basis as FormState["estimate_basis"]) ?? "rolling_avg_3",
    amount: existing?.amount ?? "",
    payee: "",
    autopost_policy: (existing?.autopost_policy as FormState["autopost_policy"]) ?? "wait_for_match",
  };
}

function ScheduleForm({
  accounts,
  existing,
  onDone,
  onCancel,
}: {
  accounts: AccountOut[];
  existing?: ScheduleOut;
  onDone: () => void;
  onCancel: () => void;
}) {
  const [form, setForm] = useState<FormState>(() => {
    const accountPath = existing ? accounts.find((a) => a.id === existing.account_id)?.path : undefined;
    return initialForm(accounts, existing, accountPath);
  });
  const [error, setError] = useState<string | null>(null);
  const set = <K extends keyof FormState>(key: K, value: FormState[K]) =>
    setForm((f) => ({ ...f, [key]: value }));
  const rrule = buildRrule(form);
  const isTransfer = form.target.startsWith("[");

  const save = useMutation({
    mutationFn: async () => {
      const body: ScheduleCreate = {
        name: form.name,
        account: form.account,
        rrule,
        dtstart: form.dtstart || null,
        amount_type: form.amount_type,
        amount: form.amount === "" ? null : form.amount,
        payee: form.payee || null,
        category: isTransfer || form.target === "" ? null : form.target,
        counterparty_account: isTransfer ? form.target.slice(1, -1) : null,
        estimate_basis: form.amount_type === "estimated" ? form.estimate_basis : null,
        autopost_policy: form.autopost_policy,
        propose: false,
      };
      if (existing) {
        const { error: err } = await api.PATCH("/schedules/{schedule_id}", {
          params: { path: { schedule_id: existing.id } },
          body: {
            name: body.name,
            rrule: body.rrule,
            dtstart: body.dtstart,
            amount_type: body.amount_type,
            amount: body.amount,
            payee: body.payee,
            category: body.category,
            counterparty_account: body.counterparty_account,
            estimate_basis: body.estimate_basis,
            autopost_policy: body.autopost_policy,
          },
        });
        if (err) throw new Error(errorDetail(err));
      } else {
        const { error: err } = await api.POST("/schedules", { body });
        if (err) throw new Error(errorDetail(err));
      }
    },
    onSuccess: onDone,
    onError: (e) => setError(e.message),
  });

  const realAccounts = accounts.filter((a) => !a.is_virtual && !a.closed_at);

  return (
    <Card className="mt-4">
      <CardTitle>{existing ? `Edit ${existing.name}` : "New schedule"}</CardTitle>
      <div className="mt-3 grid grid-cols-1 gap-3 md:grid-cols-3">
        <div>
          <Label>Name</Label>
          <Input value={form.name} onChange={(e) => set("name", e.target.value)} placeholder="Citi card payment" />
        </div>
        <div>
          <Label>Account</Label>
          <select
            className={`${selectClass} w-full`}
            value={form.account}
            disabled={!!existing}
            onChange={(e) => set("account", e.target.value)}
          >
            {realAccounts.map((a) => (
              <option key={a.id} value={a.path}>
                {a.name} ({a.path})
              </option>
            ))}
          </select>
        </div>
        <div>
          <Label>Category, or transfer to</Label>
          <CategorySelect value={form.target} currentAccountPath={form.account} onChange={(v) => set("target", v)} />
          <p className="mt-1 text-xs text-muted-foreground">
            A transfer target makes this a two-account schedule (card payment, contribution).
          </p>
        </div>

        <div>
          <Label>Cadence</Label>
          <select
            className={`${selectClass} w-full`}
            value={form.cadence}
            onChange={(e) => set("cadence", e.target.value as Cadence)}
          >
            <option value="weekly">Weekly</option>
            <option value="biweekly">Every 2 weeks</option>
            <option value="semimonthly">Semi-monthly (two days)</option>
            <option value="monthly">Monthly</option>
            <option value="quarterly">Every 3 months</option>
            <option value="yearly">Yearly</option>
            <option value="custom">Custom RRULE</option>
          </select>
        </div>
        {(form.cadence === "weekly" || form.cadence === "biweekly") && (
          <div>
            <Label>Weekday</Label>
            <select
              className={`${selectClass} w-full`}
              value={form.weekday}
              onChange={(e) => set("weekday", e.target.value)}
            >
              {WEEKDAYS.map((d) => (
                <option key={d} value={d}>
                  {WEEKDAY_LABELS[d]}
                </option>
              ))}
            </select>
          </div>
        )}
        {(form.cadence === "monthly" || form.cadence === "quarterly" || form.cadence === "yearly" || form.cadence === "semimonthly") && (
          <div>
            <Label>{form.cadence === "semimonthly" ? "First day of month" : "Day of month (1–28)"}</Label>
            <Input type="number" min={1} max={28} value={form.day} onChange={(e) => set("day", Number(e.target.value))} />
          </div>
        )}
        {form.cadence === "semimonthly" && (
          <div>
            <Label>Second day of month</Label>
            <Input type="number" min={1} max={28} value={form.day2} onChange={(e) => set("day2", Number(e.target.value))} />
          </div>
        )}
        {form.cadence === "yearly" && (
          <div>
            <Label>Month (1–12)</Label>
            <Input type="number" min={1} max={12} value={form.month} onChange={(e) => set("month", Number(e.target.value))} />
          </div>
        )}
        {form.cadence === "custom" && (
          <div className="md:col-span-2">
            <Label>RRULE</Label>
            <Input value={form.custom} onChange={(e) => set("custom", e.target.value)} placeholder="FREQ=MONTHLY;BYMONTHDAY=6,20" />
          </div>
        )}
        <div>
          <Label>Anchor date (dtstart)</Label>
          <Input type="date" value={form.dtstart} onChange={(e) => set("dtstart", e.target.value)} />
          <p className="mt-1 text-xs text-muted-foreground">Every-2-weeks counts from here; pass the last real occurrence.</p>
        </div>

        <div>
          <Label>Amount</Label>
          <select
            className={`${selectClass} w-full`}
            value={form.amount_type}
            onChange={(e) => set("amount_type", e.target.value as FormState["amount_type"])}
          >
            <option value="fixed">Fixed</option>
            <option value="estimated">Estimated from history</option>
          </select>
        </div>
        {form.amount_type === "estimated" && (
          <div>
            <Label>Estimate basis</Label>
            <select
              className={`${selectClass} w-full`}
              value={form.estimate_basis}
              onChange={(e) => set("estimate_basis", e.target.value as FormState["estimate_basis"])}
            >
              <option value="last">Last matched amount</option>
              <option value="rolling_avg_3">Average of last 3</option>
              <option value="rolling_avg_12">Average of last 12</option>
              {isTransfer && <option value="counterparty_balance">Pay the other account's balance</option>}
            </select>
          </div>
        )}
        <div>
          <Label>{form.amount_type === "fixed" ? "Amount (negative = money out)" : "Fallback amount"}</Label>
          <Input value={form.amount} onChange={(e) => set("amount", e.target.value)} placeholder="-1500.00" />
        </div>
        {!isTransfer && (
          <div>
            <Label>Payee</Label>
            <Input value={form.payee} onChange={(e) => set("payee", e.target.value)} placeholder="optional" />
          </div>
        )}
        <div>
          <Label>When the bill posts</Label>
          <select
            className={`${selectClass} w-full`}
            value={form.autopost_policy}
            onChange={(e) => set("autopost_policy", e.target.value as FormState["autopost_policy"])}
          >
            <option value="wait_for_match">Wait for the bank row</option>
            <option value="auto_post">Post automatically</option>
            <option value="reminder_only">Reminder only</option>
          </select>
        </div>
      </div>
      <p className="mt-3 text-xs text-muted-foreground">
        Rule: <span className="font-mono">{rrule || "—"}</span> · {rrule ? cadenceLabel(rrule) : ""}
      </p>
      {error && <ErrorText>{error}</ErrorText>}
      <div className="mt-3 flex gap-2">
        <Button disabled={save.isPending || !form.name || !rrule} onClick={() => save.mutate()}>
          {existing ? "Save" : "Create"}
        </Button>
        <Button variant="ghost" onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </Card>
  );
}

/* --------------------------------------------------------------------------
 * Instances panel (skip / override / reset / statement)
 * ------------------------------------------------------------------------ */

type InstanceOut = ScheduleDetail["instances"][number];

/** Where an instance's amount comes from (decision 80), when it is worth a
 * badge: a recorded card statement, a user's override, or the card's current
 * balance a card payment estimates from. */
function BasisBadge({ instance }: { instance: InstanceOut }) {
  if (instance.basis === "statement") {
    return (
      <Badge variant="green" title={`statement balance ${fmtAmount(instance.statement_balance)}`}>
        {instance.statement_date ? `statement · closed ${instance.statement_date}` : "statement"}
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

  if (detail.isLoading) return <p className="text-sm text-muted-foreground">Loading instances…</p>;
  const instances = detail.data?.instances ?? [];
  if (instances.length === 0) return <p className="text-sm text-muted-foreground">No live instances.</p>;
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
              <Td>{i.date}</Td>
              <Td className="text-muted-foreground">{i.occurrence_date ?? "—"}</Td>
              <Td className="text-right font-mono">{fmtAmount(i.amount)}</Td>
              <Td>
                <span className="flex gap-1">
                  {i.overdue && <Badge variant="red">overdue</Badge>}
                  <BasisBadge instance={i} />
                </span>
              </Td>
              <Td>
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
      {error && <ErrorText>{error}</ErrorText>}
    </div>
  );
}

/* --------------------------------------------------------------------------
 * Forecast chart
 * ------------------------------------------------------------------------ */

/** How a forecast event's amount came about (decisions 80, 85). */
const BASIS_LABEL: Record<string, string> = {
  fixed: "fixed",
  last: "last amount",
  rolling_avg_3: "3-payment average",
  rolling_avg_12: "12-payment average",
  counterparty_balance: "current balance",
  override: "your amount",
  statement: "statement balance",
};

const CHART_W = 720;
const CHART_H = 220;
const PAD = { l: 56, r: 12, t: 12, b: 28 };

export function ForecastChart({ accountPath }: { accountPath: string }) {
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
    const balances = data.days.map((d) => Number(d.balance));
    // The estimate range band (#247): every day's pessimistic/optimistic
    // balance, present only when an estimated event carries a range.
    const banded = data.days.filter((d) => d.low != null && d.high != null);
    const bandValues = banded.flatMap((d) => [Number(d.low), Number(d.high)]);
    const floor = data.min_balance != null ? Number(data.min_balance) : 0;
    const lo = Math.min(...balances, ...bandValues, floor, Number(data.start_balance));
    const hi = Math.max(...balances, ...bandValues, floor, Number(data.start_balance));
    const span = hi - lo || 1;
    const x = (i: number) => PAD.l + (i / Math.max(data.days.length - 1, 1)) * (CHART_W - PAD.l - PAD.r);
    const y = (v: number) => PAD.t + (1 - (v - lo) / span) * (CHART_H - PAD.t - PAD.b);
    // A step line: the balance holds until the day an event lands.
    const path = data.days
      .map((d, i) =>
        i === 0
          ? `M${x(0).toFixed(1)},${y(Number(d.balance)).toFixed(1)}`
          : `H${x(i).toFixed(1)} V${y(Number(d.balance)).toFixed(1)}`,
      )
      .join(" ");
    const events = data.days.flatMap((d, i) =>
      d.events.map((e) => ({ ...e, x: x(i), y: y(Number(d.balance)), date: d.date })),
    );
    const lowIndex = balances.indexOf(Math.min(...balances));
    // Stepped like the line: highs left to right, back along the lows.
    const edge = (pick: "low" | "high") =>
      data.days.flatMap((d, i) => {
        const v = Number(d[pick]);
        const prev = i > 0 ? Number(data.days[i - 1][pick]) : v;
        return i === 0 ? [[x(0), y(v)]] : [[x(i), y(prev)], [x(i), y(v)]];
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
      {forecast.isLoading && <p className="text-sm text-muted-foreground">Loading…</p>}
      {forecast.error && <ErrorText>{(forecast.error as Error).message}</ErrorText>}
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
              {fmtAmount(String(geometry.hi))}
            </text>
            <text x={PAD.l - 6} y={geometry.y(geometry.lo) + 4} textAnchor="end" fontSize={10} fill="currentColor">
              {fmtAmount(String(geometry.lo))}
            </text>
            {geometry.band && <path data-testid="forecast-band" d={geometry.band} fill="var(--chart-band)" stroke="none" />}
            <path d={geometry.path} fill="none" stroke="currentColor" strokeWidth={1.5} />
            {geometry.events.map((e, i) => (
              <circle
                key={i}
                cx={e.x}
                cy={e.y}
                r={3.5}
                fill={e.projected ? "var(--background, white)" : "currentColor"}
                stroke="currentColor"
                strokeWidth={1.5}
              >
                <title>
                  {`${e.label} ${fmtAmount(e.amount)} · ${e.date}${e.projected ? " · projected" : ""}${e.overdue ? " · overdue" : ""}` +
                    (e.basis ? ` · ${BASIS_LABEL[e.basis] ?? e.basis}` : "") +
                    (e.range ? ` · ${fmtAmount(e.range.low)} to ${fmtAmount(e.range.high)}` : "")}
                </title>
              </circle>
            ))}
            <text x={PAD.l} y={CHART_H - 8} fontSize={10} fill="currentColor">
              {data.days[0]?.date}
            </text>
            <text x={CHART_W - PAD.r} y={CHART_H - 8} fontSize={10} textAnchor="end" fill="currentColor">
              {data.days[data.days.length - 1]?.date}
            </text>
          </svg>
          <p className="mt-1 text-xs text-muted-foreground">
            {data.coverage.schedules_active === 0
              ? "No schedules are modeled on this account — the line is flat by construction."
              : `Modeled from ${data.coverage.schedules_active} schedule${data.coverage.schedules_active === 1 ? "" : "s"}: ${data.coverage.instances} instance${data.coverage.instances === 1 ? "" : "s"}, ${data.coverage.projected} projected (hollow).`}
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
            {data.first_shortfall && ` Projected below the floor on ${data.first_shortfall}.`}
            {` Low ${fmtAmount(data.days[geometry.lowIndex]?.balance)} on ${data.days[geometry.lowIndex]?.date}.`}
            {data.pessimistic_low &&
              Number(data.pessimistic_low.balance) < geometry.floor &&
              !data.first_shortfall &&
              ` Could dip to ${fmtAmount(data.pessimistic_low.balance)} on ${data.pessimistic_low.date} if estimates come in at the edge of their range (shaded).`}
          </p>
        </>
      )}
    </div>
  );
}

/* --------------------------------------------------------------------------
 * Page
 * ------------------------------------------------------------------------ */

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
    queryFn: async () =>
      (
        await api.GET("/accounts", {
          params: { query: { include_balances: false, include_hidden: true, include_closed: true } },
        })
      ).data ?? [],
  });
  const schedules = useQuery({
    queryKey: ["schedules", statusFilter],
    queryFn: async () =>
      (
        await api.GET("/schedules", {
          params: { query: statusFilter === "all" ? {} : { status: [statusFilter] } },
        })
      ).data ?? [],
  });
  const suggestionsOpen = useQuery({
    queryKey: ["suggestions"],
    queryFn: async () => (await api.GET("/suggestions", { params: { query: {} } })).data ?? [],
  });

  const accountById = new Map((accounts.data ?? []).map((a) => [a.id, a]));
  const realAccounts = (accounts.data ?? []).filter((a) => !a.is_virtual);
  const chartAccount = forecastAccount || realAccounts.find((a) => !a.closed_at)?.path || "";

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
        <h1 className="text-2xl font-bold">Schedules</h1>
        <select
          className={selectClass}
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
        <Button onClick={() => setCreating(true)}>New schedule</Button>
      </div>
      {(suggestionsOpen.data?.length ?? 0) > 0 && (
        <p className="text-sm text-muted-foreground">
          {suggestionsOpen.data!.length} suggestion{suggestionsOpen.data!.length === 1 ? " awaits" : "s await"} a decision on the{" "}
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
          onDone={() => {
            setCreating(false);
            refresh();
          }}
          onCancel={() => setCreating(false)}
        />
      )}
      {editing && accounts.data && (
        <ScheduleForm
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
        <Table>
          <thead>
            <tr>
              <Th>Name</Th>
              <Th>Account</Th>
              <Th>With</Th>
              <Th>Cadence</Th>
              <Th className="text-right">Amount</Th>
              <Th>Next due</Th>
              <Th>Status</Th>
              <Th></Th>
            </tr>
          </thead>
          <tbody>
            {(schedules.data ?? []).map((s) => {
              const account = accountById.get(s.account_id);
              const counterparty = s.counterparty_account_id
                ? accountById.get(s.counterparty_account_id)
                : undefined;
              return (
                <ScheduleRow
                  key={s.id}
                  schedule={s}
                  accountLabel={account?.path ?? s.account_id}
                  withLabel={counterparty ? `→ ${counterparty.path}` : ""}
                  cardPayment={counterparty?.type === "credit_card"}
                  expanded={expanded === s.id}
                  onToggle={() => setExpanded(expanded === s.id ? null : s.id)}
                  onEdit={() => {
                    setCreating(false);
                    setEditing(s);
                  }}
                  onAction={(verb) => action.mutate({ id: s.id, verb })}
                />
              );
            })}
            {schedules.data?.length === 0 && (
              <tr>
                <Td colSpan={8} className="text-muted-foreground">
                  No {statusFilter === "all" ? "" : statusFilter + " "}schedules yet — create one, or run
                  detection and accept what Purser finds.
                </Td>
              </tr>
            )}
          </tbody>
        </Table>
      </Card>

      <Card>
        <div className="mb-2 flex items-center gap-3">
          <CardTitle>Account forecast</CardTitle>
          <select
            className={selectClass}
            value={chartAccount}
            onChange={(e) => setForecastAccount(e.target.value)}
          >
            {realAccounts.map((a) => (
              <option key={a.id} value={a.path}>
                {a.name} ({a.path})
              </option>
            ))}
          </select>
        </div>
        {chartAccount && <ForecastChart accountPath={chartAccount} />}
      </Card>
    </div>
  );
}

function ScheduleRow({
  schedule: s,
  accountLabel,
  withLabel,
  expanded,
  onToggle,
  onEdit,
  onAction,
  cardPayment = false,
}: {
  schedule: ScheduleOut;
  accountLabel: string;
  withLabel: string;
  cardPayment?: boolean;
  expanded: boolean;
  onToggle: () => void;
  onEdit: () => void;
  onAction: (verb: "pause" | "resume" | "end") => void;
}) {
  return (
    <>
      <tr>
        <Td>
          <button className="text-left font-medium underline-offset-2 hover:underline" onClick={onToggle}>
            {s.name}
          </button>
        </Td>
        <Td className="text-muted-foreground">{accountLabel}</Td>
        <Td className="text-muted-foreground">{withLabel}</Td>
        <Td>{cadenceLabel(s.rrule)}</Td>
        <Td className="text-right font-mono">
          {s.amount_type === "estimated" ? (
            <span title={`estimated: ${s.estimate_basis ?? ""}`} className="text-muted-foreground">
              {s.estimate_basis === "counterparty_balance"
                ? "pays balance"
                : s.amount != null
                  ? `~${fmtAmount(s.amount)}`
                  : "from history"}
            </span>
          ) : (
            fmtAmount(s.amount)
          )}
        </Td>
        <Td>{s.next_due ?? "—"}</Td>
        <Td>
          <Badge variant={s.status === "active" ? "green" : s.status === "paused" ? "amber" : "default"}>{s.status}</Badge>
        </Td>
        <Td>
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
