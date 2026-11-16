import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import type { components } from "../api/schema";
import { api, errorDetail } from "../lib/api";
import { fmtAmount } from "../lib/utils";
import { Badge, Button, ErrorText, Input, Label } from "./ui";

type Certainty = components["schemas"]["CertaintyOut"];
type Evidence = components["schemas"]["AmountEvidence"];
const labels: Record<Certainty["status"], string> = {
  estimated: "Estimate", confirmed: "Confirmed upcoming", pending_review: "Match awaiting review",
  matched: "Matched actual", matched_pending: "Matched pending bank entry",
};

export function OccurrenceCertainty({ value }: { value?: Certainty | null }) {
  if (!value) return null;
  return <details className="text-xs">
    <summary className="cursor-pointer">
      <Badge variant={value.status === "estimated" ? "amber" : value.status === "matched" ? "green" : undefined}>
        {labels[value.status]}
      </Badge>
      {value.amount_estimated && " · amount estimated"}
      {value.date_estimated && " · date estimated"}
    </summary>
    <div className="mt-1 space-y-1 text-muted-foreground">
      <p>Amount: {value.amount.state} · {value.amount.source} · as of {value.amount.as_of ?? "not recorded"}. {value.amount.note}{value.amount.evidence_value && ` Evidence was recorded against ${fmtAmount(value.amount.evidence_value)}; the current value is estimated.`}</p>
      <p>Date: {value.date.state} · {value.date.source} · as of {value.date.as_of ?? "not recorded"}. {value.date.note}</p>
      {value.original && <p>
        {value.original.backfilled ? "First retained snapshot" : "Original estimate"}: {fmtAmount(value.original.amount)} on {value.original.date}
        {` · ${value.original.amount_basis} · captured ${value.original.as_of}`}
      </p>}
      {value.original?.window && <p>Observation window: {String(value.original.window.from ?? "none")} to {String(value.original.window.to ?? "none")} ({String(value.original.window.count ?? 0)} observations).</p>}
      {value.actual && <p>Linked {value.actual.status} transaction: {fmtAmount(value.actual.amount)} on {value.actual.date}.
        {value.actual.variance && ` Difference from original: ${fmtAmount(value.actual.variance.amount)}; ${value.actual.variance.days} days (positive means later).`}
      </p>}
      {value.status === "pending_review" && <p>This is a proposed match, not confirmation. Its bank movement already contributes to the forecast where applicable.</p>}
    </div>
  </details>;
}

export function OccurrenceEvidence({ transactionId, slot, onSaved }: {
  transactionId?: string;
  slot?: components["schemas"]["SlotOut"];
  onSaved: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [dimension, setDimension] = useState<"amount" | "date">("amount");
  const [state, setState] = useState<Evidence["state"]>("estimated");
  const [source, setSource] = useState<Evidence["source"]>("user");
  const [asOf, setAsOf] = useState("");
  const [note, setNote] = useState("");
  const [value, setValue] = useState("");
  const save = useMutation({
    mutationFn: async () => {
      const evidence = { state, source, as_of: asOf, note, ...(value ? { value } : {}) };
      const body = { [dimension]: evidence };
      const { error } = slot
        ? await api.PATCH("/schedules/{schedule_id}/occurrences/{occurrence_date}/certainty", {
          params: { path: { schedule_id: slot.schedule_id, occurrence_date: slot.occurrence_date } }, body,
        })
        : await api.PATCH("/schedules/instances/{transaction_id}/certainty", {
          params: { path: { transaction_id: transactionId! } }, body,
        });
      if (error) throw new Error(errorDetail(error));
    },
    onSuccess: onSaved,
  });
  return <details className="mt-1 text-xs" onToggle={e => setOpen(e.currentTarget.open)}>
    <summary className="cursor-pointer underline">Update evidence</summary>
    {open && <div className="mt-2 flex flex-col gap-2">
      <Label>Dimension<select aria-label="Evidence dimension" value={dimension} onChange={e => { setDimension(e.target.value as "amount" | "date"); setValue(""); }}>
        <option value="amount">Amount</option><option value="date">Date</option>
      </select></Label>
      <Label>Certainty<select aria-label="Evidence certainty" value={state} onChange={e => setState(e.target.value as Evidence["state"])}>
        <option value="estimated">Estimated</option><option value="confirmed">Confirmed by evidence</option>
      </select></Label>
      <Label>Source<select aria-label="Evidence source" value={source} onChange={e => setSource(e.target.value as Evidence["source"])}>
        {["history", "statement", "payroll", "invoice", "approval", "user", "agent", "other"].map(s => <option key={s} value={s}>{s}</option>)}
      </select></Label>
      <Label>As of<Input type="date" aria-label="Evidence as of" value={asOf} onChange={e => setAsOf(e.target.value)} /></Label>
      <Label>Evidence<Input aria-label="Evidence note" maxLength={500} value={note} onChange={e => setNote(e.target.value)} /></Label>
      {!slot && <Label>Value (leave blank to keep current)<Input aria-label="Evidence value" type={dimension === "date" ? "date" : "text"} value={value} onChange={e => setValue(e.target.value)} /></Label>}
      <p>{slot && "The forecast amount and date stay unchanged. "}Only this dimension of this occurrence changes. Confirmation does not post a transaction.</p>
      <Button size="sm" disabled={!asOf || !note.trim() || save.isPending} onClick={() => save.mutate()}>Save evidence</Button>
      {save.error && <ErrorText>{save.error.message}</ErrorText>}
      {save.isSuccess && <p role="status">Evidence saved.</p>}
    </div>}
  </details>;
}
