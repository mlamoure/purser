import { useState } from "react";
import { Button, Label } from "../../components/ui";
import { OPTIONAL_COLUMNS, REQUIRED_COLUMNS, selectClass, type ColumnField } from "./model";

/** Shown when the server does not recognise a CSV layout: the user says which
 * header column holds which field (date, amount and payee required). */
export function ColumnMapForm({
  columns,
  initial,
  busy,
  onApply,
}: {
  columns: string[];
  initial: Record<string, string> | null;
  busy: boolean;
  onApply: (map: Record<string, string>) => void;
}) {
  const [sel, setSel] = useState<Record<ColumnField, string>>(() => ({
    date: initial?.date ?? "",
    amount: initial?.amount ?? "",
    payee: initial?.payee ?? "",
    memo: initial?.memo ?? "",
    category: initial?.category ?? "",
    account: initial?.account ?? "",
    cleared: initial?.cleared ?? "",
    scheduled: initial?.scheduled ?? "",
  }));
  const ready = REQUIRED_COLUMNS.every((f) => sel[f]);
  const columnSelect = (field: ColumnField, required: boolean) => (
    <div key={field} className="space-y-1">
      <Label className="text-xs">
        {field}
        {required ? " *" : ""}
      </Label>
      <select
        className={selectClass}
        value={sel[field]}
        onChange={(e) => setSel((s) => ({ ...s, [field]: e.target.value }))}
      >
        <option value="">—</option>
        {columns.map((c) => (
          <option key={c} value={c}>
            {c}
          </option>
        ))}
      </select>
    </div>
  );
  return (
    <div className="space-y-3 rounded-md border border-amber-500/40 bg-amber-500/5 p-3">
      <div className="text-sm">
        <span className="font-medium">Unrecognized CSV layout.</span> Tell Purser which column is
        which; date, amount, and payee are required.
      </div>
      <div className="flex flex-wrap gap-3">
        {REQUIRED_COLUMNS.map((f) => columnSelect(f, true))}
        {OPTIONAL_COLUMNS.map((f) => columnSelect(f, false))}
      </div>
      <Button
        size="sm"
        disabled={busy || !ready}
        onClick={() => {
          const map: Record<string, string> = {};
          for (const f of [...REQUIRED_COLUMNS, ...OPTIONAL_COLUMNS]) {
            if (sel[f]) map[f] = sel[f];
          }
          onApply(map);
        }}
      >
        Use these columns
      </Button>
    </div>
  );
}
