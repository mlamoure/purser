import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import type { components } from "../api/schema";
import { api, errorDetail } from "../lib/api";
import { formatDate } from "../lib/dates";
import { compareMoney, formatMoney, moneySign } from "../lib/money";
import { useLedgerToday } from "../lib/clock";
import { Button, Card, ErrorText, Input } from "./ui";
import { useAccounts } from "./AccountsTable";
import { ASSET_KINDS, AccountIcon, assetKindLabel, type AssetKind } from "./AccountIcon";

type AccountOut = components["schemas"]["AccountOut"];

const selectClass = "h-9 rounded-md border border-border bg-card px-2 text-sm";
// A decimal string with at most 4 places (NUMERIC(20,4)); the server checks too.
const AMOUNT = /^-?\d{1,16}(\.\d{1,4})?$/;
const VALUE = /^\d{1,16}(\.\d{1,4})?$/;

/** A hand-valued asset's panel on its account page (#566, #567): what it
 * is, its equity against the loans it secures, and the two ways to change
 * its value — set it on a date (a reconcile, which posts the difference) or
 * record a change (+/−, or money put in). Every entry lands in the register
 * below, where a wrong one is undone. */
export function AssetPanel({ account }: { account: AccountOut }) {
  const qc = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [open, setOpen] = useState<"value" | "change" | "details" | "dispose" | null>(null);
  const closed = !!account.closed_at;

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["account", account.id] });
    qc.invalidateQueries({ queryKey: ["accounts"] });
    qc.invalidateQueries({ queryKey: ["transactions"] });
    qc.invalidateQueries({ queryKey: ["networth"] });
  };
  const done = (message: string) => {
    setOpen(null);
    setError(null);
    setNotice(message);
    refresh();
  };

  return (
    <Card className="space-y-3" role="region" aria-label="Asset">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="space-y-1 text-sm">
          <p className="flex items-center gap-2 font-medium">
            <AccountIcon type="property" kind={account.asset?.kind} />
            {assetKindLabel(account.asset?.kind)}
            {account.asset?.acquired_on && (
              <span className="font-normal text-muted-foreground">
                · acquired {formatDate(account.asset.acquired_on)}
              </span>
            )}
          </p>
          {account.asset?.description && (
            <p className="text-muted-foreground">{account.asset.description}</p>
          )}
          <p className="text-muted-foreground">
            {account.asset?.last_valued
              ? `Value last stated ${formatDate(account.asset.last_valued)}`
              : "No value recorded yet"}
            {account.asset?.revalue_after_days
              ? ` · reminder after ${account.asset.revalue_after_days} days`
              : ""}
          </p>
          {account.asset?.stale && (
            <p className="text-warning" role="note">
              This value is older than {account.asset.revalue_after_days} days. Record a new one.
            </p>
          )}
          {account.asset?.money_put_in != null && account.asset?.gain != null && (
            <p className="text-muted-foreground">
              Money put in{" "}
              <span className="font-mono">
                {formatMoney(account.asset.money_put_in, { currency: account.currency })}
              </span>{" "}
              · {moneySign(account.asset.gain) < 0 ? "Loss" : "Gain"}{" "}
              <span className="font-mono">
                {formatMoney(account.asset.gain, { currency: account.currency, signDisplay: "always" })}
              </span>
            </p>
          )}
          {account.secured_loan_ids.length > 0 && (
            <p>
              Equity{" "}
              <span className="font-mono font-semibold">
                {formatMoney(account.equity, { currency: account.currency })}
              </span>{" "}
              <span className="text-muted-foreground">
                (value less {account.secured_loan_ids.length === 1 ? "the loan" : "the loans"}{" "}
                it secures)
              </span>
            </p>
          )}
        </div>
        {!closed && (
          <div className="flex flex-wrap gap-2">
            <Button size="sm" onClick={() => setOpen(open === "value" ? null : "value")}>
              Record value
            </Button>
            <Button
              size="sm"
              variant="outline"
              onClick={() => setOpen(open === "change" ? null : "change")}
            >
              Change value
            </Button>
            <Button
              size="sm"
              variant="outline"
              onClick={() => setOpen(open === "details" ? null : "details")}
            >
              Edit details
            </Button>
            <Button
              size="sm"
              variant="outline"
              onClick={() => setOpen(open === "dispose" ? null : "dispose")}
            >
              Sell or dispose…
            </Button>
          </div>
        )}
      </div>

      {open === "value" && (
        <RecordValueForm account={account} onDone={done} onError={setError} />
      )}
      {open === "change" && (
        <ChangeValueForm account={account} onDone={done} onError={setError} />
      )}
      {open === "details" && (
        <DetailsForm account={account} onDone={done} onError={setError} />
      )}
      {open === "dispose" && (
        <DisposeForm account={account} onDone={done} onError={setError} />
      )}
      <SecuredLoans account={account} onChanged={refresh} onError={setError} />
      {notice && <p className="text-sm text-muted-foreground" role="status">{notice}</p>}
      <ErrorText>{error}</ErrorText>
    </Card>
  );
}

function RecordValueForm({
  account,
  onDone,
  onError,
}: {
  account: AccountOut;
  onDone: (message: string) => void;
  onError: (message: string) => void;
}) {
  const today = useLedgerToday();
  // null until edited: the ledger day may arrive after the form opens.
  const [asOf, setAsOf] = useState<string | null>(null);
  const [value, setValue] = useState("");
  const [note, setNote] = useState("");
  const save = useMutation({
    mutationFn: async () => {
      const { data, error } = await api.POST("/accounts/{account_id}/reconcile", {
        params: { path: { account_id: account.id } },
        body: {
          as_of: asOf || today,
          statement_balance: value.trim(),
          basis: "posted",
          component: "balance",
          dry_run: false,
          ...(note.trim() ? { note: note.trim() } : {}),
        },
      });
      if (error) throw new Error(errorDetail(error));
      return data;
    },
    onSuccess: (data) =>
      onDone(
        data && moneySign(data.adjustment) !== 0
          ? `Value set: a change of ${formatMoney(data.adjustment, { currency: account.currency, signDisplay: "always" })} on ${formatDate(data.as_of)}.`
          : "The value already matched; nothing was posted.",
      ),
    onError: (e) => onError(e.message),
  });
  const valid = VALUE.test(value.trim());
  return (
    <form
      className="flex flex-wrap items-end gap-2 text-sm"
      onSubmit={(e) => {
        e.preventDefault();
        if (valid) save.mutate();
      }}
    >
      <label className="space-y-1">
        <span className="block text-muted-foreground">Worth</span>
        <Input
          aria-label="Value"
          className="w-36 font-mono"
          inputMode="decimal"
          placeholder="612500.00"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          autoFocus
        />
      </label>
      <label className="space-y-1">
        <span className="block text-muted-foreground">On</span>
        <Input
          aria-label="Value date"
          type="date"
          max={today}
          value={asOf ?? today}
          onChange={(e) => setAsOf(e.target.value)}
        />
      </label>
      <label className="min-w-48 flex-1 space-y-1">
        <span className="block text-muted-foreground">Source (optional)</span>
        <Input
          aria-label="Value source"
          placeholder="Appraisal"
          value={note}
          onChange={(e) => setNote(e.target.value)}
        />
      </label>
      <Button size="sm" type="submit" disabled={!valid || save.isPending}>
        Save value
      </Button>
      <p className="w-full text-xs text-muted-foreground">
        Purser posts the difference from the current value on that date as a change in value.
        One value per date; for an earlier date than the latest value, record a change instead.
      </p>
    </form>
  );
}

function ChangeValueForm({
  account,
  onDone,
  onError,
}: {
  account: AccountOut;
  onDone: (message: string) => void;
  onError: (message: string) => void;
}) {
  const today = useLedgerToday();
  // null until edited: the ledger day may arrive after the form opens.
  const [asOf, setAsOf] = useState<string | null>(null);
  const [amount, setAmount] = useState("");
  const [kind, setKind] = useState<"revaluation" | "capital">("revaluation");
  const [note, setNote] = useState("");
  const save = useMutation({
    mutationFn: async () => {
      const { data, error } = await api.POST("/accounts/{account_id}/value-changes", {
        params: { path: { account_id: account.id } },
        body: {
          as_of: asOf || today,
          amount: amount.trim(),
          kind,
          ...(note.trim() ? { note: note.trim() } : {}),
        },
      });
      if (error) throw new Error(errorDetail(error));
      return data;
    },
    onSuccess: (data) =>
      onDone(
        `Recorded. Value now ${formatMoney(data?.balance, { currency: account.currency })}.`,
      ),
    onError: (e) => onError(e.message),
  });
  const trimmed = amount.trim();
  const valid = AMOUNT.test(trimmed) && moneySign(trimmed) !== 0;
  return (
    <form
      className="flex flex-wrap items-end gap-2 text-sm"
      onSubmit={(e) => {
        e.preventDefault();
        if (valid) save.mutate();
      }}
    >
      <label className="space-y-1">
        <span className="block text-muted-foreground">Change</span>
        <Input
          aria-label="Change amount"
          className="w-36 font-mono"
          inputMode="decimal"
          placeholder="25000.00 or -4000.00"
          value={amount}
          onChange={(e) => setAmount(e.target.value)}
          autoFocus
        />
      </label>
      <label className="space-y-1">
        <span className="block text-muted-foreground">On</span>
        <Input
          aria-label="Change date"
          type="date"
          max={today}
          value={asOf ?? today}
          onChange={(e) => setAsOf(e.target.value)}
        />
      </label>
      <label className="space-y-1">
        <span className="block text-muted-foreground">What it is</span>
        <select
          aria-label="Change kind"
          className={selectClass}
          value={kind}
          onChange={(e) => setKind(e.target.value as "revaluation" | "capital")}
        >
          <option value="revaluation">Change in value</option>
          <option value="capital">Money put in</option>
        </select>
      </label>
      <label className="min-w-48 flex-1 space-y-1">
        <span className="block text-muted-foreground">Note (optional)</span>
        <Input
          aria-label="Change note"
          value={note}
          onChange={(e) => setNote(e.target.value)}
        />
      </label>
      <Button size="sm" type="submit" disabled={!valid || save.isPending}>
        Save change
      </Button>
      <p className="w-full text-xs text-muted-foreground">
        Positive when it gained value, negative when it lost value. Money put in is a purchase or
        an improvement no tracked account paid; one paid from a tracked account is a transfer to
        this asset in that account's register.
      </p>
    </form>
  );
}

function DetailsForm({
  account,
  onDone,
  onError,
}: {
  account: AccountOut;
  onDone: (message: string) => void;
  onError: (message: string) => void;
}) {
  const today = useLedgerToday();
  const [kind, setKind] = useState<AssetKind>((account.asset?.kind ?? "other") as AssetKind);
  const [acquired, setAcquired] = useState(account.asset?.acquired_on ?? "");
  const [description, setDescription] = useState(account.asset?.description ?? "");
  const [reminder, setReminder] = useState(
    account.asset?.revalue_after_days ? String(account.asset.revalue_after_days) : "",
  );
  const reminderOk = reminder.trim() === "" || /^\d{1,4}$/.test(reminder.trim());
  const save = useMutation({
    mutationFn: async () => {
      const { error } = await api.PATCH("/accounts/{account_id}", {
        params: { path: { account_id: account.id } },
        body: {
          asset_kind: kind,
          acquired_on: acquired || null,
          asset_description: description.trim() || null,
          revalue_after_days: reminder.trim() ? Number.parseInt(reminder.trim(), 10) : null,
        },
      });
      if (error) throw new Error(errorDetail(error));
    },
    onSuccess: () => onDone("Details saved."),
    onError: (e) => onError(e.message),
  });
  return (
    <form
      className="flex flex-wrap items-end gap-2 text-sm"
      onSubmit={(e) => {
        e.preventDefault();
        save.mutate();
      }}
    >
      <label className="space-y-1">
        <span className="block text-muted-foreground">Kind</span>
        <select
          aria-label="Asset kind"
          className={selectClass}
          value={kind}
          onChange={(e) => setKind(e.target.value as AssetKind)}
        >
          {ASSET_KINDS.map((k) => (
            <option key={k} value={k}>
              {assetKindLabel(k)}
            </option>
          ))}
        </select>
      </label>
      <label className="space-y-1">
        <span className="block text-muted-foreground">Acquired</span>
        <Input
          aria-label="Acquired on"
          type="date"
          max={today}
          value={acquired}
          onChange={(e) => setAcquired(e.target.value)}
        />
      </label>
      <label className="min-w-56 flex-1 space-y-1">
        <span className="block text-muted-foreground">Description (stored encrypted)</span>
        <Input
          aria-label="Asset description"
          placeholder="Address, VIN, notes"
          maxLength={500}
          value={description}
          onChange={(e) => setDescription(e.target.value)}
        />
      </label>
      <label className="space-y-1">
        <span className="block text-muted-foreground">Remind after (days)</span>
        <Input
          aria-label="Remind after days"
          className="w-24 font-mono"
          inputMode="numeric"
          placeholder="35"
          value={reminder}
          onChange={(e) => setReminder(e.target.value)}
        />
      </label>
      <Button size="sm" type="submit" disabled={save.isPending || !reminderOk}>
        Save details
      </Button>
      <p className="w-full text-xs text-muted-foreground">
        With a reminder, Purser flags the asset (and tells your agent) when its value has not
        been updated for that many days. Leave it empty for no reminder.
      </p>
    </form>
  );
}

/** The loans this asset secures, with link and unlink: a loan's
 * `secured_by_id` points at the asset, which then reports its equity. */
function SecuredLoans({
  account,
  onChanged,
  onError,
}: {
  account: AccountOut;
  onChanged: () => void;
  onError: (message: string) => void;
}) {
  const accounts = useAccounts();
  const [choice, setChoice] = useState("");
  const loans = (accounts.data ?? []).filter((a) => a.type === "loan");
  const linked = loans.filter((l) => l.secured_by_id === account.id);
  const free = loans.filter((l) => !l.secured_by_id);
  const link = useMutation({
    mutationFn: async ({ loan, asset }: { loan: string; asset: string | null }) => {
      const { error } = await api.PATCH("/accounts/{account_id}", {
        params: { path: { account_id: loan } },
        body: { secured_by_id: asset },
      });
      if (error) throw new Error(errorDetail(error));
    },
    onSuccess: () => {
      setChoice("");
      onChanged();
    },
    onError: (e) => onError(e.message),
  });
  if (linked.length === 0 && free.length === 0) return null;
  return (
    <div className="space-y-2 border-t border-border pt-3 text-sm">
      <p className="font-medium">Loans secured by this asset</p>
      {linked.length === 0 && <p className="text-muted-foreground">None linked.</p>}
      <ul className="space-y-1">
        {linked.map((l) => (
          <li key={l.id} className="flex flex-wrap items-center gap-2">
            <AccountIcon type="loan" />
            <span>{l.name}</span>
            <span className="font-mono text-muted-foreground">
              {formatMoney(l.balance, { currency: l.currency })}
            </span>
            {!account.closed_at && (
              <Button
                size="sm"
                variant="ghost"
                aria-label={`Unlink ${l.name}`}
                disabled={link.isPending}
                onClick={() => link.mutate({ loan: l.id, asset: null })}
              >
                Unlink
              </Button>
            )}
          </li>
        ))}
      </ul>
      {!account.closed_at && free.length > 0 && (
        <div className="flex flex-wrap items-center gap-2">
          <select
            aria-label="Loan to link"
            className={selectClass}
            value={choice}
            onChange={(e) => setChoice(e.target.value)}
          >
            <option value="">Link a loan…</option>
            {free.map((l) => (
              <option key={l.id} value={l.id}>
                {l.name}
              </option>
            ))}
          </select>
          <Button
            size="sm"
            variant="outline"
            disabled={!choice || link.isPending}
            onClick={() => link.mutate({ loan: choice, asset: account.id })}
          >
            Link
          </Button>
        </div>
      )}
    </div>
  );
}

/** Sell, trade in or write off (#569): a dry run shows what is left on the
 * asset (with no proceeds recorded, its whole value) before the confirm,
 * which posts it as the closing change in value and closes the account. */
function DisposeForm({
  account,
  onDone,
  onError,
}: {
  account: AccountOut;
  onDone: (message: string) => void;
  onError: (message: string) => void;
}) {
  const today = useLedgerToday();
  const [asOf, setAsOf] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const [preview, setPreview] = useState<components["schemas"]["DisposalOut"] | null>(null);
  const send = useMutation({
    mutationFn: async (dry_run: boolean) => {
      const { data, error } = await api.POST("/accounts/{account_id}/dispose", {
        params: { path: { account_id: account.id } },
        body: { as_of: asOf ?? today, dry_run, ...(note.trim() ? { note: note.trim() } : {}) },
      });
      if (error || !data) throw new Error(errorDetail(error));
      return data;
    },
    onSuccess: (data) => {
      if (data.dry_run) setPreview(data);
      else
        onDone(
          `Closed on ${formatDate(data.as_of)}. Total ${moneySign(data.gain) < 0 ? "loss" : "gain"}: ${formatMoney(data.gain, { currency: account.currency, signDisplay: "always" })}.`,
        );
    },
    onError: (e) => onError(e.message),
  });
  const writeOff =
    preview && account.balance != null && compareMoney(preview.value_before, account.balance) === 0;
  return (
    <div className="space-y-2 text-sm">
      <p className="text-muted-foreground">
        First record the sale's money as transfers out of this asset (the deposit of the proceeds,
        a loan payoff), in those accounts' registers. Whatever is left then is the rest of its
        change in value, and the account closes at zero.
      </p>
      <div className="flex flex-wrap items-end gap-2">
        <label className="space-y-1">
          <span className="block text-muted-foreground">Sold or disposed of on</span>
          <Input
            aria-label="Disposal date"
            type="date"
            max={today}
            value={asOf ?? today}
            onChange={(e) => {
              setAsOf(e.target.value);
              setPreview(null);
            }}
          />
        </label>
        <label className="min-w-48 flex-1 space-y-1">
          <span className="block text-muted-foreground">Note (optional)</span>
          <Input
            aria-label="Disposal note"
            placeholder="Sold"
            value={note}
            onChange={(e) => setNote(e.target.value)}
          />
        </label>
        <Button size="sm" variant="outline" disabled={send.isPending} onClick={() => send.mutate(true)}>
          Preview
        </Button>
      </div>
      {preview && (
        <div className="space-y-1 rounded-md border border-border p-2" role="status">
          <p>
            Left on the asset on {formatDate(preview.as_of)}:{" "}
            <span className="font-mono">
              {formatMoney(preview.value_before, { currency: account.currency })}
            </span>
            , posted as a change in value of{" "}
            <span className="font-mono">
              {formatMoney(preview.closing_change, { currency: account.currency, signDisplay: "always" })}
            </span>
            .
          </p>
          <p>
            Total {moneySign(preview.gain) < 0 ? "loss" : "gain"}:{" "}
            <span className="font-mono">
              {formatMoney(preview.gain, { currency: account.currency, signDisplay: "always" })}
            </span>
          </p>
          {writeOff && (
            <p className="text-warning">
              No proceeds are recorded, so its whole value is written off.
            </p>
          )}
          <Button size="sm" disabled={send.isPending} onClick={() => send.mutate(false)}>
            Confirm and close
          </Button>
        </div>
      )}
    </div>
  );
}
