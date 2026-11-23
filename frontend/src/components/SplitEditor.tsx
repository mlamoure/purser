/** The split editor (issue #590, decision 118): one bank row spread over
 * category lines and Quicken-style `[Account]` lines that add up to the
 * row's amount. The row keeps the bank's amount; the editor shows how much
 * is left to assign and saves only when nothing is. An account line can be
 * backed by that account's own bank rows (checkboxes), which the server
 * merges into the split so the money is counted once.
 *
 * Money stays a decimal string throughout: sums are exact integer
 * arithmetic on ten-thousandths (NUMERIC(20,4)), never a float. */

import { useEffect, useId, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { components } from "../api/schema";
import { api, errorDetail } from "../lib/api";
import { formatDate } from "../lib/dates";
import { formatMoney, moneyEditText, negateMoney } from "../lib/money";
import CategorySelect from "./CategorySelect";
import { Button, ErrorText, Input } from "./ui";

type SplitLineIn = components["schemas"]["SplitLineIn"];

export type EditorLine = {
  /** Category path, or `[account path]` for an account line. */
  category: string;
  amount: string;
  memo: string;
  backing: string[];
};

type Line = EditorLine & { key: number };

const UNITS = /^([+-])?(\d*)(?:\.(\d{0,4}))?$/;

/** A decimal string in ten-thousandths, or null when it is not a number
 * with at most four places. */
export function toUnits(value: string): bigint | null {
  const m = UNITS.exec(value.trim());
  if (!m || (!m[2] && !m[3])) return null;
  const units = BigInt(m[2] || "0") * 10000n + BigInt((m[3] ?? "").padEnd(4, "0") || "0");
  return m[1] === "-" ? -units : units;
}

/** Ten-thousandths back to a decimal string, cents unless finer. */
export function fromUnits(units: bigint): string {
  const negative = units < 0n;
  const abs = negative ? -units : units;
  const int = abs / 10000n;
  const frac = (abs % 10000n).toString().padStart(4, "0");
  const shown = frac.endsWith("00") ? frac.slice(0, 2) : frac;
  return `${negative ? "-" : ""}${int}.${shown}`;
}

const isAccountLine = (category: string) => /^\[.+\]$/.test(category);

let nextKey = 1;
const line = (over: Partial<EditorLine> = {}): Line => ({
  key: nextKey++,
  category: "",
  amount: "",
  memo: "",
  backing: [],
  ...over,
});

function BackingCandidates({
  txId,
  account,
  amount,
  selected,
  currency,
  onChange,
}: {
  txId: string;
  account: string;
  amount: string;
  selected: string[];
  currency?: string | null;
  onChange: (ids: string[]) => void;
}) {
  // The leg the line books on that account: its amount, negated.
  const leg = negateMoney(amount);
  const candidates = useQuery({
    queryKey: ["split-candidates", txId, account, leg],
    queryFn: async () => {
      const { data, error } = await api.GET("/transactions/{tx_id}/split/candidates", {
        params: { path: { tx_id: txId }, query: { account, amount: leg } },
      });
      if (error) throw new Error(errorDetail(error));
      return data;
    },
  });
  const rows = candidates.data?.candidates ?? [];
  return (
    <fieldset className="mt-1 rounded-md border border-border p-2 text-xs">
      <legend className="px-1 text-muted-foreground">
        Bank rows on this account that back the line (counted once)
      </legend>
      {candidates.isPending && <p role="status">Loading bank rows…</p>}
      {candidates.error && <ErrorText>{candidates.error.message}</ErrorText>}
      {candidates.isSuccess && !rows.length && (
        <p className="text-muted-foreground">
          No bank row on this account near the date. Left unbacked, the line is Purser's own
          entry there.
        </p>
      )}
      <ul className="space-y-1">
        {rows.map((c) => (
          <li key={c.id}>
            <label className="flex flex-wrap items-center gap-2">
              <input
                type="checkbox"
                checked={selected.includes(c.id)}
                onChange={(e) =>
                  onChange(
                    e.target.checked ? [...selected, c.id] : selected.filter((id) => id !== c.id),
                  )
                }
              />
              <span>{formatDate(c.date)}</span>
              <span className="font-mono">{formatMoney(c.amount, { currency })}</span>
              <span className="max-w-48 truncate">{c.payee ?? c.description ?? ""}</span>
              <span className="text-muted-foreground">{c.backing ? "backing" : c.status}</span>
            </label>
          </li>
        ))}
      </ul>
    </fieldset>
  );
}

/** The editor. For a row that is already a split it loads the lines (and
 * the split's own account and amount) from the transaction detail and offers
 * "Remove split", asking first (#606); for a flat row it starts from the
 * row's category. `onSave`
 * replaces the default save (PUT /transactions/{id}/split), e.g. for a
 * "split like last time" card that accepts with edited lines. */
export default function SplitEditor({
  txId,
  amount,
  accountPath,
  category,
  currency,
  isSplit,
  initialLines,
  onSave,
  onClose,
  onDone,
}: {
  txId: string;
  /** The row's amount on its own account (a flat row's; a split loads its own). */
  amount?: string | null;
  accountPath?: string | null;
  /** A flat row's category: the first line starts with it. */
  category?: string | null;
  currency?: string | null;
  isSplit: boolean;
  initialLines?: EditorLine[];
  onSave?: (lines: SplitLineIn[]) => Promise<void>;
  onClose: () => void;
  onDone?: () => void;
}) {
  const qc = useQueryClient();
  const headingId = useId();
  const confirmId = useId();
  const [error, setError] = useState<string | null>(null);
  // "Remove split" asks first. Focus moves to the safe choice when the
  // question opens and back to "Remove split" when it is dismissed.
  const [confirmingRemove, setConfirmingRemove] = useState(false);
  const [refocusRemove, setRefocusRemove] = useState(false);
  const keepSplit = () => {
    setConfirmingRemove(false);
    setRefocusRemove(true);
  };
  const [lines, setLines] = useState<Line[]>(() =>
    initialLines?.length
      ? initialLines.map((l) => line(l))
      : isSplit
        ? []
        : [
            line({ category: category ?? "", amount: amount ? moneyEditText(amount) : "" }),
            line(),
          ],
  );
  const detail = useQuery({
    queryKey: ["transaction", txId],
    queryFn: async () => {
      const { data, error: err } = await api.GET("/transactions/{tx_id}", {
        params: { path: { tx_id: txId } },
      });
      if (err) throw new Error(errorDetail(err));
      return data;
    },
    enabled: isSplit,
  });
  const loaded = detail.data?.split;
  useEffect(() => {
    if (isSplit && loaded && !initialLines?.length) {
      setLines(
        loaded.lines.map((l) =>
          line({
            category: l.target,
            amount: moneyEditText(l.amount),
            memo: l.memo ?? "",
            backing: l.backing_ids ?? [],
          }),
        ),
      );
    }
  }, [isSplit, loaded, initialLines]);

  const rowAmount = isSplit ? (detail.data?.amount ?? null) : (amount ?? null);
  const ownAccount = isSplit ? (loaded?.account_path ?? accountPath ?? "") : (accountPath ?? "");
  const total = toUnits(rowAmount ?? "");
  const parsed = lines.map((l) => toUnits(l.amount));
  const assigned = parsed.reduce<bigint>((sum, u) => sum + (u ?? 0n), 0n);
  const left = total === null ? null : total - assigned;
  const linesValid =
    lines.length >= 2 &&
    lines.every((l, i) => l.category !== "" && parsed[i] !== null && parsed[i] !== 0n);
  const canSave = left === 0n && linesValid;

  const update = (key: number, patch: Partial<EditorLine>) =>
    setLines((ls) => ls.map((l) => (l.key === key ? { ...l, ...patch } : l)));

  const putRest = () => {
    if (left === null || left === 0n) return;
    const category = left < 0n ? "expenses.uncategorized" : "income.uncategorized";
    setLines((ls) => {
      const existing = ls.find((l) => l.category === category);
      if (!existing) return [...ls, line({ category, amount: fromUnits(left) })];
      const next = (toUnits(existing.amount) ?? 0n) + left;
      if (next === 0n) return ls.filter((l) => l !== existing);
      return ls.map((l) => (l === existing ? { ...l, amount: fromUnits(next) } : l));
    });
  };

  const body = (): SplitLineIn[] =>
    lines.map((l) => ({
      category: l.category,
      amount: fromUnits(toUnits(l.amount) ?? 0n),
      ...(l.memo.trim() ? { memo: l.memo.trim() } : {}),
      backing_ids: isAccountLine(l.category) ? l.backing : [],
    }));

  const finish = () => {
    setError(null);
    qc.invalidateQueries({ queryKey: ["register"] });
    qc.invalidateQueries({ queryKey: ["transaction"] });
    qc.invalidateQueries({ queryKey: ["accounts"] });
    qc.invalidateQueries({ queryKey: ["account"] });
    qc.invalidateQueries({ queryKey: ["suggestions"] });
    onDone?.();
    onClose();
  };
  const save = useMutation({
    mutationFn: async () => {
      if (onSave) return onSave(body());
      const { error: err } = await api.PUT("/transactions/{tx_id}/split", {
        params: { path: { tx_id: txId } },
        body: { lines: body(), dry_run: false },
      });
      if (err) throw new Error(errorDetail(err));
    },
    onSuccess: finish,
    onError: (e: Error) => setError(e.message),
  });
  const remove = useMutation({
    mutationFn: async () => {
      const { error: err } = await api.DELETE("/transactions/{tx_id}/split", {
        params: { path: { tx_id: txId } },
      });
      if (err) throw new Error(errorDetail(err));
    },
    onSuccess: finish,
    onError: (e: Error) => setError(e.message),
  });

  if (isSplit && detail.isPending) return <p role="status">Loading the split…</p>;
  if (isSplit && detail.error) return <ErrorText>{detail.error.message}</ErrorText>;

  return (
    <section
      aria-labelledby={headingId}
      className="min-w-0 rounded-lg border border-border bg-card p-3 text-sm"
    >
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 id={headingId} className="font-medium">
          Split {formatMoney(rowAmount, { currency })}
        </h3>
        <span className="text-xs text-muted-foreground">
          Lines are this row's share: negative is money out. An account in brackets moves money
          to that account.
        </span>
      </div>
      <ul className="mt-2 space-y-2">
        {lines.map((l, i) => (
          <li key={l.key} className="flex flex-col gap-1">
            <div className="flex flex-wrap items-center gap-2">
              <div className="min-w-0 grow sm:max-w-64">
                <CategorySelect
                  value={l.category}
                  currentAccountPath={ownAccount}
                  onChange={(category) => update(l.key, { category, backing: [] })}
                  aria-label={`Line ${i + 1} category or account`}
                />
              </div>
              <Input
                className="h-8 w-28 font-mono text-xs"
                inputMode="decimal"
                aria-label={`Line ${i + 1} amount`}
                aria-invalid={l.amount !== "" && parsed[i] === null}
                value={l.amount}
                onChange={(e) => update(l.key, { amount: e.target.value })}
              />
              <Input
                className="h-8 min-w-0 grow text-xs sm:max-w-48"
                aria-label={`Line ${i + 1} memo`}
                placeholder="Memo"
                maxLength={500}
                value={l.memo}
                onChange={(e) => update(l.key, { memo: e.target.value })}
              />
              <Button
                size="sm"
                variant="ghost"
                aria-label={`Remove line ${i + 1}`}
                onClick={() => setLines((ls) => ls.filter((x) => x.key !== l.key))}
              >
                ✕
              </Button>
            </div>
            {isAccountLine(l.category) && parsed[i] !== null && parsed[i] !== 0n && (
              <BackingCandidates
                txId={txId}
                account={l.category.slice(1, -1)}
                amount={fromUnits(parsed[i] as bigint)}
                selected={l.backing}
                currency={currency}
                onChange={(backing) => update(l.key, { backing })}
              />
            )}
          </li>
        ))}
      </ul>
      <div className="mt-2 flex flex-wrap items-center gap-2">
        <Button size="sm" variant="outline" onClick={() => setLines((ls) => [...ls, line()])}>
          Add line
        </Button>
        <span
          role="status"
          className={`text-xs ${left === 0n ? "text-success" : "text-warning"}`}
        >
          Left to assign: {left === null ? "—" : formatMoney(fromUnits(left), { currency })}
        </span>
        {left !== null && left !== 0n && (
          <Button size="sm" variant="ghost" onClick={putRest}>
            Put the rest on Uncategorized
          </Button>
        )}
      </div>
      <div className="mt-3 flex flex-wrap gap-2">
        <Button
          size="sm"
          disabled={!canSave || save.isPending}
          title={canSave ? undefined : "The lines must add up to the row's amount (two or more)"}
          onClick={() => save.mutate()}
        >
          Save split
        </Button>
        <Button size="sm" variant="ghost" onClick={onClose}>
          Cancel
        </Button>
        {isSplit && !onSave && !confirmingRemove && (
          <Button
            size="sm"
            variant="destructive"
            autoFocus={refocusRemove}
            title="Back to one category; the bank rows that backed account lines come back"
            onClick={() => setConfirmingRemove(true)}
          >
            Remove split
          </Button>
        )}
      </div>
      {isSplit && !onSave && confirmingRemove && (
        <div
          role="group"
          aria-labelledby={confirmId}
          className="mt-3 rounded-md border border-destructive p-2"
          onKeyDown={(e) => {
            if (e.key === "Escape") {
              e.stopPropagation();
              keepSplit();
            }
          }}
        >
          <p id={confirmId} className="text-sm">
            Remove this split? The row goes back to one category, Uncategorized, and the bank rows
            that backed its account lines come back as they were.
          </p>
          <div className="mt-2 flex flex-wrap gap-2">
            <Button
              size="sm"
              variant="destructive"
              disabled={remove.isPending}
              onClick={() => remove.mutate()}
            >
              Remove split
            </Button>
            <Button size="sm" variant="outline" autoFocus onClick={keepSplit}>
              Keep split
            </Button>
          </div>
        </div>
      )}
      {error && <ErrorText>{error}</ErrorText>}
    </section>
  );
}
