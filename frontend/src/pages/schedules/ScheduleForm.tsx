/** Create / edit form for a scheduled transaction (#236). It is entered as a
 * payee and an optional memo (#534): the schedule's name is derived from them
 * on the server (memo, else payee, else a transfer's other account). The
 * cadence picker writes the RRULE. */

import { useId, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { api, errorDetail } from "../../lib/api";
import type { components } from "../../api/schema";
import CategorySelect from "../../components/CategorySelect";
import { Button, Card, CardTitle, ErrorText, Input, Label } from "../../components/ui";
import {
  buildRrule,
  type Cadence,
  cadenceLabel,
  rruleParams,
  selectClass,
  today,
  WEEKDAY_LABELS,
  WEEKDAYS,
} from "./cadence";
import { AUTOPOST_LABELS } from "../../lib/labels";
import { moneySign } from "../../lib/money";

type ScheduleOut = components["schemas"]["ScheduleOut"];
type ScheduleCreate = components["schemas"]["ScheduleCreate"];
type AccountOut = components["schemas"]["AccountOut"];

type FormState = {
  payee: string;
  memo: string;
  account: string;
  target: string; // category path, or "[account.path]" for a transfer counterparty
  cadence: Cadence;
  weekday: string;
  day: number;
  day2: number;
  month: number;
  custom: string;
  dtstart: string;
  date_type: "fixed" | "estimated";
  amount_type: "fixed" | "estimated";
  estimate_basis: "last" | "rolling_avg_3" | "rolling_avg_12" | "counterparty_balance";
  amount: string;
  autopost_policy: "auto_post" | "wait_for_match" | "reminder_only";
  // A payment to a credit card sized from its statement cycle (#674); "" = by the
  // estimate basis, as before.
  payment_amount: "" | "statement_balance" | "minimum_due" | "fixed" | "current_balance";
  later_estimate: "statement_average" | "same_cycle_last_year" | "fixed";
  later_window: number;
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
    payee: existing?.payee ?? "",
    memo: existing?.memo ?? "",
    account: accountPath ?? accounts.find((a) => !a.is_virtual && !a.closed_at)?.path ?? "",
    target: counterparty ? `[${counterparty}]` : (existing?.category_path ?? ""),
    cadence,
    weekday: p.BYDAY ?? "FR",
    day: bymonthday[0] ?? Number(today().slice(8, 10)),
    day2: bymonthday[1] ?? 20,
    month: Number(p.BYMONTH ?? today().slice(5, 7)),
    custom: existing?.rrule ?? "",
    dtstart: existing?.dtstart ?? today(),
    date_type: existing?.date_type ?? "fixed",
    amount_type: (existing?.amount_type as "fixed" | "estimated") ?? "fixed",
    estimate_basis: (existing?.estimate_basis as FormState["estimate_basis"]) ?? "rolling_avg_3",
    amount: existing?.amount ?? "",
    autopost_policy: (existing?.autopost_policy as FormState["autopost_policy"]) ?? "wait_for_match",
    payment_amount: existing?.payment_amount ?? "",
    later_estimate: existing?.later_estimate ?? "statement_average",
    later_window: existing?.later_window ?? 6,
  };
}

export default function ScheduleForm({
  accounts,
  existing,
  defaultAccount,
  onDone,
  onCancel,
}: {
  accounts: AccountOut[];
  existing?: ScheduleOut;
  /** New only: the account the form starts on (the forecast's account). */
  defaultAccount?: string;
  onDone: () => void;
  onCancel: () => void;
}) {
  const id = useId();
  const [form, setForm] = useState<FormState>(() => {
    const accountPath = existing
      ? accounts.find((a) => a.id === existing.account_id)?.path
      : defaultAccount || undefined;
    return initialForm(accounts, existing, accountPath);
  });
  const [error, setError] = useState<string | null>(null);
  const set = <K extends keyof FormState>(key: K, value: FormState[K]) =>
    setForm((f) => ({ ...f, [key]: value }));
  const rrule = buildRrule(form);
  const isTransfer = form.target.startsWith("[");
  const card = isTransfer
    ? accounts.find((a) => a.path === form.target.slice(1, -1) && a.type === "credit_card")
    : undefined;
  const needsPayee = !isTransfer && form.payee.trim() === "" && form.memo.trim() === "";

  const save = useMutation({
    mutationFn: async () => {
      const payee = isTransfer ? null : form.payee.trim();
      const memo = form.memo.trim();
      const shared = {
        rrule,
        dtstart: form.dtstart || null,
        date_type: form.date_type,
        amount_type: form.amount_type,
        amount: form.amount === "" ? null : form.amount,
        category: isTransfer || form.target === "" ? null : form.target,
        counterparty_account: isTransfer ? form.target.slice(1, -1) : null,
        estimate_basis: form.amount_type === "estimated" ? form.estimate_basis : null,
        autopost_policy: form.autopost_policy,
        // Statement-cycle sizing (#674) only on a payment to a card; all three null
        // turns it off on an edit.
        ...(card
          ? form.payment_amount
            ? {
                payment_amount: form.payment_amount,
                later_estimate: form.later_estimate,
                later_window: form.later_window,
              }
            : existing?.payment_amount
              ? { payment_amount: null, later_estimate: null, later_window: null }
              : {}
          : {}),
      };
      if (existing) {
        // "" clears a payee or memo on the server; a transfer's payee is not ours to touch.
        const { error: err } = await api.PATCH("/schedules/{schedule_id}", {
          params: { path: { schedule_id: existing.id } },
          body: { ...shared, payee, memo },
        });
        if (err) throw new Error(errorDetail(err));
      } else {
        const body: ScheduleCreate = {
          ...shared,
          account: form.account,
          payee: payee || null,
          memo: memo || null,
          propose: false,
        };
        const { error: err } = await api.POST("/schedules", { body });
        if (err) throw new Error(errorDetail(err));
      }
    },
    onSuccess: onDone,
    onError: (e) => setError(e.message),
  });

  const realAccounts = accounts.filter((a) => !a.is_virtual && !a.closed_at);
  const field = (name: string) => `${id}-${name}`;

  return (
    <Card className="mt-4">
      <CardTitle>{existing ? `Edit ${existing.name}` : "New scheduled transaction"}</CardTitle>
      <div className="mt-3 grid grid-cols-1 gap-3 md:grid-cols-3">
        <div>
          <Label htmlFor={field("account")}>Account</Label>
          <select
            id={field("account")}
            className={`${selectClass} w-full`}
            value={form.account}
            disabled={!!existing}
            onChange={(e) => set("account", e.target.value)}
          >
            {realAccounts.map((a) => (
              <option key={a.id} value={a.path}>
                {a.name}
              </option>
            ))}
          </select>
        </div>
        <div>
          <Label htmlFor={field("target")}>Category, or transfer to</Label>
          <CategorySelect
            id={field("target")}
            value={form.target}
            currentAccountPath={form.account}
            direction={moneySign(form.amount) > 0 ? "credit" : "debit"}
            onChange={(v) => set("target", v)}
          />
          <p className="mt-1 text-xs text-muted-foreground">
            A transfer target makes this a two-account schedule (card payment, contribution).
          </p>
        </div>
        {!isTransfer ? (
          <div>
            <Label htmlFor={field("payee")}>Payee</Label>
            <Input
              id={field("payee")}
              value={form.payee}
              onChange={(e) => set("payee", e.target.value)}
              placeholder="Acme Gym"
              aria-invalid={needsPayee || undefined}
            />
          </div>
        ) : (
          <div />
        )}
        <div className="md:col-span-2">
          <Label htmlFor={field("memo")}>Memo</Label>
          <Input
            id={field("memo")}
            value={form.memo}
            maxLength={200}
            onChange={(e) => set("memo", e.target.value)}
            placeholder="optional note, e.g. family plan"
          />
        </div>
        <div />

        <div>
          <Label htmlFor={field("cadence")}>Repeats</Label>
          <select
            id={field("cadence")}
            className={`${selectClass} w-full`}
            value={form.cadence}
            onChange={(e) => set("cadence", e.target.value as Cadence)}
          >
            <option value="weekly">Weekly</option>
            <option value="biweekly">Every 2 weeks</option>
            <option value="semimonthly">Twice a month</option>
            <option value="monthly">Monthly</option>
            <option value="quarterly">Every 3 months</option>
            <option value="yearly">Yearly</option>
            <option value="custom">Custom rule (advanced)</option>
          </select>
        </div>
        {(form.cadence === "weekly" || form.cadence === "biweekly") && (
          <div>
            <Label htmlFor={field("weekday")}>Weekday</Label>
            <select
              id={field("weekday")}
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
        {(form.cadence === "monthly" ||
          form.cadence === "quarterly" ||
          form.cadence === "yearly" ||
          form.cadence === "semimonthly") && (
          <div>
            <Label htmlFor={field("day")}>
              {form.cadence === "semimonthly" ? "First day of month" : "Day of month (1–28)"}
            </Label>
            <Input
              id={field("day")}
              type="number"
              min={1}
              max={28}
              value={form.day}
              onChange={(e) => set("day", Number(e.target.value))}
            />
          </div>
        )}
        {form.cadence === "semimonthly" && (
          <div>
            <Label htmlFor={field("day2")}>Second day of month</Label>
            <Input
              id={field("day2")}
              type="number"
              min={1}
              max={28}
              value={form.day2}
              onChange={(e) => set("day2", Number(e.target.value))}
            />
          </div>
        )}
        {form.cadence === "yearly" && (
          <div>
            <Label htmlFor={field("month")}>Month (1–12)</Label>
            <Input
              id={field("month")}
              type="number"
              min={1}
              max={12}
              value={form.month}
              onChange={(e) => set("month", Number(e.target.value))}
            />
          </div>
        )}
        {form.cadence === "custom" && (
          <div className="md:col-span-2">
            <Label htmlFor={field("custom")}>Rule (RFC 5545 RRULE)</Label>
            <Input
              id={field("custom")}
              value={form.custom}
              onChange={(e) => set("custom", e.target.value)}
              placeholder="FREQ=MONTHLY;BYMONTHDAY=6,20"
            />
          </div>
        )}
        <div>
          <Label htmlFor={field("date_type")}>Date certainty</Label>
          <select
            id={field("date_type")}
            className={`${selectClass} w-full`}
            value={form.date_type}
            onChange={(e) => set("date_type", e.target.value as FormState["date_type"])}
          >
            <option value="fixed">Known recurring date</option>
            <option value="estimated">Estimated arrival date</option>
          </select>
          <Label htmlFor={field("dtstart")}>Starts from</Label>
          <Input
            id={field("dtstart")}
            type="date"
            value={form.dtstart}
            onChange={(e) => set("dtstart", e.target.value)}
          />
          <p className="mt-1 text-xs text-muted-foreground">
            Every-2-weeks counts from here; use the last real occurrence.
          </p>
        </div>

        <div>
          <Label htmlFor={field("amount_type")}>Amount</Label>
          <select
            id={field("amount_type")}
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
            <Label htmlFor={field("estimate_basis")}>Estimate basis</Label>
            <select
              id={field("estimate_basis")}
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
        {card && (
          <div>
            <Label htmlFor={field("payment_amount")}>Card payment</Label>
            <select
              id={field("payment_amount")}
              className={`${selectClass} w-full`}
              value={form.payment_amount}
              onChange={(e) => set("payment_amount", e.target.value as FormState["payment_amount"])}
            >
              <option value="">By the amount settings</option>
              <option value="statement_balance">Statement balance</option>
              <option value="minimum_due">Minimum due</option>
              <option value="fixed">Fixed amount</option>
              <option value="current_balance">Current balance</option>
            </select>
            <p className="mt-1 text-xs text-muted-foreground">
              {card.statement_close_day != null
                ? `Sized from the statement that closes on day ${card.statement_close_day}.`
                : "Set the card's statement closing day for this to take effect."}
            </p>
          </div>
        )}
        {card && form.payment_amount !== "" && (
          <div>
            <Label htmlFor={field("later_estimate")}>Later payments</Label>
            <select
              id={field("later_estimate")}
              className={`${selectClass} w-full`}
              value={form.later_estimate}
              onChange={(e) => set("later_estimate", e.target.value as FormState["later_estimate"])}
            >
              <option value="statement_average">Average of past statements</option>
              <option value="same_cycle_last_year">Same statement last year</option>
              <option value="fixed">Fixed amount</option>
            </select>
            {form.later_estimate !== "fixed" && (
              <>
                <Label htmlFor={field("later_window")}>Statements to average</Label>
                <Input
                  id={field("later_window")}
                  type="number"
                  min={1}
                  max={24}
                  value={form.later_window}
                  onChange={(e) => set("later_window", Number(e.target.value))}
                />
              </>
            )}
          </div>
        )}
        <div>
          <Label htmlFor={field("amount")}>
            {form.amount_type === "fixed" ? "Amount (negative = money out)" : "Fallback amount"}
          </Label>
          <Input
            id={field("amount")}
            value={form.amount}
            onChange={(e) => set("amount", e.target.value)}
            placeholder="-1500.00"
          />
        </div>
        <div>
          <Label htmlFor={field("autopost")}>When it posts</Label>
          <select
            id={field("autopost")}
            className={`${selectClass} w-full`}
            value={form.autopost_policy}
            onChange={(e) => set("autopost_policy", e.target.value as FormState["autopost_policy"])}
          >
            {Object.entries(AUTOPOST_LABELS).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </div>
      </div>
      <p className="mt-3 text-xs text-muted-foreground">
        {/* Words only (#541): the raw rule is in the custom-rule input above. */}
        Repeats {rrule ? cadenceLabel(rrule) : "—"}
      </p>
      {needsPayee && <p className="mt-1 text-xs text-muted-foreground">Enter a payee (or a memo) to save.</p>}
      {error && <ErrorText>{error}</ErrorText>}
      <div className="mt-3 flex gap-2">
        <Button disabled={save.isPending || needsPayee || !rrule} onClick={() => save.mutate()}>
          {existing ? "Save" : "Create"}
        </Button>
        <Button variant="ghost" onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </Card>
  );
}
