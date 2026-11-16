/** Suggestion cards (#236, decisions 57 and 67): one rendering of the
 * suggestions inbox — the MCP tools are the other. A card shows the headline,
 * where the suggestion came from and how sure Purser is, an evidence summary
 * that expands into the rows behind it, and the four decisions: accept as
 * shown, edit then accept, snooze, reject (this one / never for this pair).
 *
 * The card is kind-aware. A `schedule` suggestion accepts into an active
 * schedule. A `transfer_link` suggestion — proposed by a manual transfer
 * confirm — accepts into a DRAFT rule; activating it is a second, explicit
 * step that confirms how many past pairs it will link (decision 46), the
 * same confirm the Rules page asks for. A `match` suggestion mirrors an
 * open match proposal (decision 86): the expected row against the bank row
 * and the variance; accept confirms it, reject rejects that pair for good,
 * and there is nothing to edit. A `transfer_pair` suggestion is a transfer
 * candidate (#386, decision 102): two bank rows of exactly opposite amounts
 * that nothing links on its own; accept links them as one transfer, reject
 * remembers they are not one, and there is nothing to edit. */

import { useId, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, errorDetail } from "../lib/api";
import { fmtAmount } from "../lib/utils";
import type { components } from "../api/schema";
import { Badge, Button, ErrorText, Input, Label, Table, Td, Th } from "./ui";

export type SuggestionOut = components["schemas"]["SuggestionOut"];
type Decision = components["schemas"]["DecisionRequest"];
type DecisionOut = components["schemas"]["DecisionOut"];

/** The standard transfer window, when a transfer_link draft sets none. */
const STANDARD_WINDOW_DAYS = 3;

function sourceLabel(source: string): string {
  if (source === "account_added") return "new account";
  if (source === "confirm") return "your confirm";
  return source;
}

function confidenceVariant(confidence: string | null): "green" | "amber" | "default" {
  if (confidence == null) return "default";
  const n = Number(confidence);
  return n >= 0.75 ? "green" : n >= 0.5 ? "amber" : "default";
}

type Occurrence = {
  date: string;
  amount: string;
  transaction_id: string | null;
  /** transfer_link evidence: the confirmed pair's incoming side. */
  incoming_date?: string;
  description?: string;
};

function EvidenceTable({ id }: { id: string }) {
  const detail = useQuery({
    queryKey: ["suggestion", id],
    queryFn: async () => {
      const { data, error } = await api.GET("/suggestions/{suggestion_id}", {
        params: { path: { suggestion_id: id } },
      });
      if (error) throw new Error(errorDetail(error));
      return data;
    },
  });
  const evidence = detail.data?.evidence as
    | {
        occurrences?: Occurrence[];
        gaps?: { median_days: number; misses: number; out_of_bucket: number };
        amount?: { cv: string };
      }
    | null
    | undefined;
  if (detail.error) return <ErrorText>{detail.error.message}</ErrorText>;
  if (detail.isLoading) return <p className="text-xs text-muted-foreground">Loading evidence…</p>;
  if (!evidence?.occurrences?.length)
    return <p className="text-xs text-muted-foreground">No occurrence history attached.</p>;
  const pairs = evidence.occurrences.some((o) => o.incoming_date);
  return (
    <div className="mt-2">
      <Table className="text-xs">
        <thead>
          <tr>
            <Th>{pairs ? "Left" : "Date"}</Th>
            {pairs && <Th>Arrived</Th>}
            <Th className="text-right">Amount</Th>
          </tr>
        </thead>
        <tbody>
          {evidence.occurrences.map((o) => (
            <tr key={`${o.date}-${o.transaction_id ?? o.amount}`}>
              <Td>
                {o.transaction_id ? (
                  <Link className="underline" to={`/selection?id=${o.transaction_id}`}>
                    {o.date}
                  </Link>
                ) : (
                  o.date
                )}
              </Td>
              {pairs && <Td>{o.incoming_date ?? ""}</Td>}
              <Td className="text-right font-mono">{fmtAmount(o.amount)}</Td>
            </tr>
          ))}
        </tbody>
      </Table>
      {evidence.gaps && (
        <p className="mt-1 text-xs text-muted-foreground">
          Typical gap {evidence.gaps.median_days} days
          {evidence.gaps.misses ? ` · ${evidence.gaps.misses} skipped` : ""}
          {evidence.gaps.out_of_bucket ? ` · ${evidence.gaps.out_of_bucket} irregular` : ""}
          {evidence.amount
            ? ` · amount variability ${(Number(evidence.amount.cv) * 100).toFixed(0)}%`
            : ""}
        </p>
      )}
    </div>
  );
}

type Summary = {
  count?: number;
  first?: string | null;
  last?: string | null;
  amount_range?: { low: string; high: string } | null;
  cadence?: string | null;
  /** A proposing agent's note (#250): what it read, e.g. a statement line. */
  note?: string | null;
  latest_amount?: string | null;
  price_changed?: boolean;
  warning?: string;
};

function CardHeader({ suggestion }: { suggestion: SuggestionOut }) {
  return (
    <div className="flex flex-wrap items-center gap-2">
      <span className="min-w-0 grow break-words font-medium">{suggestion.headline}</span>
      <Badge>{sourceLabel(suggestion.source)}</Badge>
      {suggestion.confidence && (
        <Badge
          variant={confidenceVariant(suggestion.confidence)}
          title="Heuristic evidence score, not a probability that the suggestion is correct"
        >
          Pattern strength {Math.round(Number(suggestion.confidence) * 100)}/100
        </Badge>
      )}
      {suggestion.seen_count > 1 && (
        <span
          className="text-xs text-muted-foreground"
          title="Detector checks, not independent transactions"
        >
          checked {suggestion.seen_count} times
        </span>
      )}
    </div>
  );
}

/** "3 occurrences from A to B · 15.99 · monthly · evidence" — the noun is
 * the kind's: detected occurrences, or the transfer pairs you confirmed. */
function EvidenceLine({ suggestion, noun }: { suggestion: SuggestionOut; noun: string }) {
  const [showEvidence, setShowEvidence] = useState(false);
  const summary = suggestion.evidence_summary as Summary;
  return (
    <>
      <p className="mt-1 text-xs text-muted-foreground">
        {summary.count ? `${summary.count} ${noun}` : "no history"}
        {summary.first && summary.last
          ? summary.first === summary.last
            ? ` on ${summary.first}`
            : ` from ${summary.first} to ${summary.last}`
          : ""}
        {summary.amount_range
          ? summary.amount_range.low === summary.amount_range.high
            ? ` · ${fmtAmount(summary.amount_range.low)}`
            : ` · ${fmtAmount(summary.amount_range.low)} to ${fmtAmount(summary.amount_range.high)}`
          : ""}
        {summary.cadence ? ` · ${summary.cadence}` : ""}
        {" · "}
        <button className="underline" onClick={() => setShowEvidence((v) => !v)}>
          {showEvidence ? "hide evidence" : "evidence"}
        </button>
      </p>
      {summary.warning && <p className="mt-2 text-sm text-amber-700">{summary.warning}</p>}
      {summary.price_changed && (
        <p className="mt-2 text-sm text-amber-700">
          Recent amount changed to {fmtAmount(summary.latest_amount)}. Verify the current price
          before creating a schedule.
        </p>
      )}
      {summary.note && (
        <p className="mt-1 text-xs">
          <span className="text-muted-foreground">Agent's note: </span>
          {summary.note}
        </p>
      )}
      {showEvidence && <EvidenceTable id={suggestion.id} />}
    </>
  );
}

function useDecide(suggestionId: string, onSuccess: (data: DecisionOut) => void) {
  const qc = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const mutation = useMutation({
    mutationFn: async (body: Decision) => {
      const { data, error: err } = await api.POST("/suggestions/{suggestion_id}/decide", {
        params: { path: { suggestion_id: suggestionId } },
        body,
      });
      if (err) throw new Error(errorDetail(err));
      return data as DecisionOut;
    },
    onSuccess: (data) => {
      setError(null);
      qc.invalidateQueries({ queryKey: ["unreviewed"] });
      qc.invalidateQueries({ queryKey: ["register"] });
      onSuccess(data);
    },
    onError: (e: Error) => setError(e.message),
  });
  return { decide: mutation, error, setError };
}

/** Reject asks which memory to keep: just this suggestion, or never again
 * for the pair (`reject_scope = counterparty`). */
function RejectButtons({
  pending,
  neverLabel,
  neverTitle,
  onReject,
  onCancel,
}: {
  pending: boolean;
  neverLabel: string;
  neverTitle: string;
  onReject: (scope: "exact" | "counterparty") => void;
  onCancel: () => void;
}) {
  return (
    <>
      <Button size="sm" variant="destructive" disabled={pending} onClick={() => onReject("exact")}>
        Reject this one
      </Button>
      <Button
        size="sm"
        variant="destructive"
        disabled={pending}
        title={neverTitle}
        onClick={() => onReject("counterparty")}
      >
        {neverLabel}
      </Button>
      <p className="w-full text-xs text-muted-foreground">
        Reject this one remembers this pattern. Never suggest again also suppresses future patterns
        for this account and payee. Neither changes bank transactions.
      </p>
      <Button size="sm" variant="ghost" onClick={onCancel}>
        Cancel
      </Button>
    </>
  );
}

export function SuggestionCard({
  suggestion,
  onDecided,
}: {
  suggestion: SuggestionOut;
  onDecided?: () => void;
}) {
  if (suggestion.kind === "transfer_link")
    return <TransferLinkCard suggestion={suggestion} onDecided={onDecided} />;
  if (suggestion.kind === "match")
    return <MatchCard suggestion={suggestion} onDecided={onDecided} />;
  if (suggestion.kind === "transfer_pair")
    return <TransferPairCard suggestion={suggestion} onDecided={onDecided} />;
  return <ScheduleCard suggestion={suggestion} onDecided={onDecided} />;
}

type TransferPairDraft = {
  outgoing_id: string;
  incoming_id: string;
  from_account: string;
  to_account: string;
  amount: string;
  outgoing_date: string;
  incoming_date: string;
  date_gap_days: number;
  outgoing_description: string | null;
  incoming_description: string | null;
};

/** A transfer_pair suggestion (#386): money leaving one of your accounts
 * and the same amount arriving in another within a few days, with nothing
 * in the bank text, a rule or a schedule to link them on its own. Accept
 * links them as one transfer — the register's Link ⇄, which may also propose
 * a rule for the pair; "Not a transfer" is remembered, so no rule or keyword
 * ever links these two rows automatically; snooze only hides the card. */
function TransferPairCard({
  suggestion,
  onDecided,
}: {
  suggestion: SuggestionOut;
  onDecided?: () => void;
}) {
  const qc = useQueryClient();
  const draft = suggestion.draft as unknown as TransferPairDraft;
  const [rejecting, setRejecting] = useState(false);
  const { decide, error } = useDecide(suggestion.id, () => {
    qc.invalidateQueries({ queryKey: ["suggestions"] });
    qc.invalidateQueries({ queryKey: ["transfer-candidates"] });
    qc.invalidateQueries({ queryKey: ["register"] });
    onDecided?.();
  });
  const gap =
    draft.date_gap_days === 0
      ? "same day"
      : `${draft.date_gap_days} day${draft.date_gap_days === 1 ? "" : "s"} apart`;

  return (
    <div className="min-w-0 break-words rounded-lg border border-border p-3 text-sm">
      <CardHeader suggestion={suggestion} />
      <Table className="mt-2 text-xs">
        <thead>
          <tr>
            <Th />
            <Th>Account</Th>
            <Th>Date</Th>
            <Th>Bank text</Th>
            <Th className="text-right">Amount</Th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <Td>Leaves</Td>
            <Td className="font-mono">{draft.from_account}</Td>
            <Td>{draft.outgoing_date}</Td>
            <Td className="max-w-56 truncate">{draft.outgoing_description ?? ""}</Td>
            <Td className="text-right font-mono">{fmtAmount(`-${draft.amount}`)}</Td>
          </tr>
          <tr>
            <Td>Arrives</Td>
            <Td className="font-mono">{draft.to_account}</Td>
            <Td>{draft.incoming_date}</Td>
            <Td className="max-w-56 truncate">{draft.incoming_description ?? ""}</Td>
            <Td className="text-right font-mono">{fmtAmount(draft.amount)}</Td>
          </tr>
        </tbody>
      </Table>
      <p className="mt-1 text-xs text-muted-foreground">
        {gap} · exactly opposite amounts, but no transfer signal links them on its own
      </p>
      <Link
        className="mt-2 inline-block text-xs underline"
        to={`/selection?id=${draft.outgoing_id}&id=${draft.incoming_id}`}
      >
        Inspect both transactions
      </Link>
      <p className="mt-2 text-xs text-muted-foreground">
        Equal amounts and nearby dates do not prove a transfer. Check both bank descriptions;
        purchases, refunds and investment allocations can coincide.
      </p>
      <p className="mt-1 text-xs text-muted-foreground">
        Linking combines these rows as one transfer. You can unlink it in the account register.
      </p>

      <div className="mt-3 flex flex-wrap gap-2">
        {!rejecting && (
          <>
            <Button
              size="sm"
              disabled={decide.isPending}
              title="Links the two rows as one transfer between these accounts"
              onClick={() => decide.mutate({ decision: "accept", reject_scope: "exact" })}
            >
              Link as transfer
            </Button>
            <Button
              size="sm"
              variant="ghost"
              disabled={decide.isPending}
              onClick={() => decide.mutate({ decision: "snooze", reject_scope: "exact" })}
            >
              Snooze 30d
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setRejecting(true)}>
              Reject
            </Button>
          </>
        )}
        {rejecting && (
          <>
            <Button
              size="sm"
              variant="destructive"
              disabled={decide.isPending}
              title="Never link these two rows automatically — no rule or keyword will"
              onClick={() => decide.mutate({ decision: "reject", reject_scope: "exact" })}
            >
              Not a transfer
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setRejecting(false)}>
              Cancel
            </Button>
          </>
        )}
      </div>
      {error && <ErrorText>{error}</ErrorText>}
    </div>
  );
}

type MatchDraft = {
  expected_id: string;
  posted_id: string;
  account: string;
  label: string;
  expected_date: string;
  expected_amount: string;
  posted_date: string;
  posted_amount: string;
  variance: string;
};

/** A match suggestion: the bill Purser expected against the bank row the
 * matcher thinks paid it. Accept confirms the match (the bill is paid and
 * the bank row counts as reviewed); reject is remembered, so the pair is not
 * proposed again; snooze only hides the card — the proposal keeps holding
 * the bank row. To pair the bill with a different row, use Mark paid in the
 * register: there is nothing to edit here. */
function MatchCard({
  suggestion,
  onDecided,
}: {
  suggestion: SuggestionOut;
  onDecided?: () => void;
}) {
  const qc = useQueryClient();
  const draft = suggestion.draft as unknown as MatchDraft;
  const [rejecting, setRejecting] = useState(false);
  const { decide, error } = useDecide(suggestion.id, () => {
    qc.invalidateQueries({ queryKey: ["suggestions"] });
    qc.invalidateQueries({ queryKey: ["late-bills"] });
    qc.invalidateQueries({ queryKey: ["schedules"] });
    onDecided?.();
  });
  const sameAmount = /^-?0(?:\.0+)?$/.test(draft.variance);

  return (
    <div className="min-w-0 break-words rounded-lg border border-border p-3 text-sm">
      <CardHeader suggestion={suggestion} />
      <Table className="mt-2 text-xs">
        <thead>
          <tr>
            <Th />
            <Th>Date</Th>
            <Th className="text-right">Amount</Th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <Td>Expected · {draft.label}</Td>
            <Td>{draft.expected_date}</Td>
            <Td className="text-right font-mono">{fmtAmount(draft.expected_amount)}</Td>
          </tr>
          <tr>
            <Td>Bank row</Td>
            <Td>{draft.posted_date}</Td>
            <Td className="text-right font-mono">{fmtAmount(draft.posted_amount)}</Td>
          </tr>
        </tbody>
      </Table>
      <p className="mt-1 text-xs text-muted-foreground">
        on <span className="font-mono">{draft.account}</span> ·{" "}
        {sameAmount ? "same amount" : <>variance {fmtAmount(draft.variance)}</>}
      </p>

      <Link
        className="mt-2 inline-block text-xs underline"
        to={`/selection?id=${draft.expected_id}&id=${draft.posted_id}`}
      >
        Inspect expectation and bank transaction
      </Link>
      <p className="mt-1 text-xs text-muted-foreground">
        Confirming settles this expectation with the bank row and records its review. Use Unmatch in
        the register to correct a mistake.
      </p>
      <div className="mt-3 flex flex-wrap gap-2">
        {!rejecting && (
          <>
            <Button
              size="sm"
              disabled={decide.isPending}
              title="Confirms the match: the bill is paid by this bank row"
              onClick={() => decide.mutate({ decision: "accept", reject_scope: "exact" })}
            >
              Confirm payment match
            </Button>
            <Button
              size="sm"
              variant="ghost"
              disabled={decide.isPending}
              onClick={() => decide.mutate({ decision: "snooze", reject_scope: "exact" })}
            >
              Snooze 30d
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setRejecting(true)}>
              Reject
            </Button>
          </>
        )}
        {rejecting && (
          <>
            <Button
              size="sm"
              variant="destructive"
              disabled={decide.isPending}
              title="Not this bank row — it will not be proposed for this bill again"
              onClick={() => decide.mutate({ decision: "reject", reject_scope: "exact" })}
            >
              Not this row
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setRejecting(false)}>
              Cancel
            </Button>
          </>
        )}
      </div>
      {error && <ErrorText>{error}</ErrorText>}
    </div>
  );
}

function ScheduleCard({
  suggestion,
  onDecided,
}: {
  suggestion: SuggestionOut;
  onDecided?: () => void;
}) {
  const qc = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [rejecting, setRejecting] = useState(false);
  const formId = useId();
  const draft = suggestion.draft as Record<string, string | null>;
  const [edits, setEdits] = useState<Record<string, string>>({
    name: draft.name ?? "",
    rrule: draft.rrule ?? "",
    dtstart: draft.dtstart ?? "",
    amount: draft.amount ?? "",
    category: draft.category ?? "",
  });
  const { decide, error } = useDecide(suggestion.id, () => {
    qc.invalidateQueries({ queryKey: ["suggestions"] });
    qc.invalidateQueries({ queryKey: ["schedules"] });
    onDecided?.();
  });

  const submitEdits = () => {
    const changed: Record<string, string | null> = {};
    for (const [key, value] of Object.entries(edits)) {
      const before = draft[key] ?? "";
      if (value !== before) changed[key] = value === "" ? null : value;
    }
    if (Object.keys(changed).length === 0) {
      decide.mutate({ decision: "accept", reject_scope: "exact" });
      return;
    }
    decide.mutate({ decision: "edit_then_accept", edits: changed, reject_scope: "exact" });
  };

  return (
    <div className="min-w-0 break-words rounded-lg border border-border p-3 text-sm">
      <CardHeader suggestion={suggestion} />
      <EvidenceLine suggestion={suggestion} noun="occurrences" />
      <p className="mt-2 text-xs text-muted-foreground">
        Creates an active forecast schedule on {draft.account}; no payment is sent. First date:{" "}
        {edits.dtstart}. Amount: {fmtAmount(edits.amount)}. Pause or edit it from Schedules.
      </p>

      {editing && (
        <div className="mt-3 grid grid-cols-2 gap-2 md:grid-cols-3">
          {(["name", "rrule", "dtstart", "amount", "category"] as const).map((key) => (
            <div key={key}>
              <Label className="text-xs" htmlFor={`${formId}-${key}`}>
                {
                  {
                    name: "Schedule name",
                    rrule: "Repeats",
                    dtstart: "First date",
                    amount: "Amount (negative for money out)",
                    category: "Category",
                  }[key]
                }
              </Label>
              {key === "rrule" ? (
                <RecurrenceInput
                  id={`${formId}-${key}`}
                  value={edits.rrule}
                  onChange={(value) => setEdits({ ...edits, rrule: value })}
                />
              ) : (
                <Input
                  id={`${formId}-${key}`}
                  type={key === "dtstart" ? "date" : "text"}
                  value={edits[key]}
                  onChange={(e) => setEdits({ ...edits, [key]: e.target.value })}
                  placeholder={key === "category" ? "expenses.…" : ""}
                />
              )}
            </div>
          ))}
        </div>
      )}

      <div className="mt-3 flex flex-wrap gap-2">
        {!editing && !rejecting && (
          <>
            <Button
              size="sm"
              disabled={decide.isPending}
              onClick={() => decide.mutate({ decision: "accept", reject_scope: "exact" })}
            >
              Create schedule
            </Button>
            <Button size="sm" variant="outline" onClick={() => setEditing(true)}>
              Edit schedule
            </Button>
            <Button
              size="sm"
              variant="ghost"
              disabled={decide.isPending}
              onClick={() => decide.mutate({ decision: "snooze", reject_scope: "exact" })}
            >
              Snooze 30d
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setRejecting(true)}>
              Reject
            </Button>
          </>
        )}
        {editing && (
          <>
            <Button size="sm" disabled={decide.isPending} onClick={submitEdits}>
              Create with edits
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setEditing(false)}>
              Cancel
            </Button>
          </>
        )}
        {rejecting && (
          <RejectButtons
            pending={decide.isPending}
            neverLabel="Never for this payee"
            neverTitle="Never suggest this payee/counterparty on this account again"
            onReject={(scope) => decide.mutate({ decision: "reject", reject_scope: scope })}
            onCancel={() => setRejecting(false)}
          />
        )}
      </div>
      {error && <ErrorText>{error}</ErrorText>}
    </div>
  );
}

type TransferLinkDraft = {
  from_account: string;
  to_account: string;
  payee: string;
  amount_exact: string | null;
  window_days: number | null;
  effective_from: string | null;
};

type CreatedRule = { id: string; windowDays: number; projected: number };

/** A transfer_link suggestion: a rule declaring this account pair a transfer
 * whenever one leg carries the payee (or an alias of it). Accept lands the
 * rule as a draft and then asks — with the projected link count — whether to
 * activate it; leaving it as a draft is always fine (Config → Rules). */
function TransferLinkCard({
  suggestion,
  onDecided,
}: {
  suggestion: SuggestionOut;
  onDecided?: () => void;
}) {
  const qc = useQueryClient();
  const draft = suggestion.draft as unknown as TransferLinkDraft;
  const [editing, setEditing] = useState(false);
  const [rejecting, setRejecting] = useState(false);
  const [edits, setEdits] = useState({
    window_days: draft.window_days == null ? "" : String(draft.window_days),
    amount_exact: draft.amount_exact ?? "",
    effective_from: draft.effective_from ?? "",
  });
  // Set once the accept created the draft rule: the card then stays to ask
  // about activation, so the inbox is refreshed only when that is answered.
  const [created, setCreated] = useState<CreatedRule | null>(null);
  const [activationError, setActivationError] = useState<string | null>(null);

  const finish = () => {
    qc.invalidateQueries({ queryKey: ["suggestions"] });
    qc.invalidateQueries({ queryKey: ["rules"] });
    onDecided?.();
  };

  const { decide, error } = useDecide(suggestion.id, (data) => {
    if (data.rule && data.projected_links != null) {
      qc.invalidateQueries({ queryKey: ["rules"] });
      setCreated({
        id: data.rule.id,
        windowDays: data.rule.window_days ?? STANDARD_WINDOW_DAYS,
        projected: data.projected_links,
      });
      return;
    }
    finish();
  });

  // The existing activation path (POST /rules/{id}/state) with the count the
  // user saw. If history moved since, the server refuses and names the new
  // count; show it and let the user confirm that one instead.
  const activate = useMutation({
    mutationFn: async (rule: CreatedRule) => {
      const { error: err } = await api.POST("/rules/{rule_id}/state", {
        params: { path: { rule_id: rule.id } },
        body: { state: "active", confirm_links: rule.projected },
      });
      if (err) throw new Error(errorDetail(err));
    },
    onSuccess: finish,
    onError: (e: Error) => {
      const now = Number(e.message.match(/would link (\d+)/)?.[1]);
      if (created && !Number.isNaN(now)) setCreated({ ...created, projected: now });
      setActivationError(e.message);
    },
  });

  const submitEdits = () => {
    const changed: Record<string, string | number | null> = {};
    const window = edits.window_days.trim();
    if (window !== (draft.window_days == null ? "" : String(draft.window_days)))
      changed.window_days = window === "" ? null : Number(window);
    if (edits.amount_exact.trim() !== (draft.amount_exact ?? ""))
      changed.amount_exact = edits.amount_exact.trim() === "" ? null : edits.amount_exact.trim();
    if (edits.effective_from !== (draft.effective_from ?? ""))
      changed.effective_from = edits.effective_from === "" ? null : edits.effective_from;
    if (Object.keys(changed).length === 0) {
      decide.mutate({ decision: "accept", reject_scope: "exact" });
      return;
    }
    decide.mutate({ decision: "edit_then_accept", edits: changed, reject_scope: "exact" });
  };

  const windowDays = draft.window_days ?? STANDARD_WINDOW_DAYS;

  if (created) {
    return (
      <div className="min-w-0 break-words rounded-lg border border-border p-3 text-sm">
        <CardHeader suggestion={suggestion} />
        <p className="mt-2">
          Rule created as a <strong>draft</strong>. Activating it links{" "}
          <strong>{created.projected}</strong> past pair(s) with legs up to {created.windowDays}{" "}
          days apart, then future pairs as they post.
        </p>
        <p className="mt-1 text-xs text-muted-foreground">
          Merging two rows leaves every balance unchanged, so the post-sync balance check cannot
          catch a wrong merge — this count is your check. Undo any link with unlink.
        </p>
        <div className="mt-3 flex flex-wrap gap-2">
          <Button size="sm" disabled={activate.isPending} onClick={() => activate.mutate(created)}>
            Activate (links {created.projected})
          </Button>
          <Button size="sm" variant="ghost" onClick={finish}>
            Leave as draft
          </Button>
          <Link to="/config/rules" className="self-center text-xs underline">
            review on the Rules page
          </Link>
        </div>
        {activationError && <ErrorText>{activationError}</ErrorText>}
      </div>
    );
  }

  return (
    <div className="min-w-0 break-words rounded-lg border border-border p-3 text-sm">
      <CardHeader suggestion={suggestion} />
      <p className="mt-1 text-xs">
        <span className="font-mono">{draft.from_account}</span> and{" "}
        <span className="font-mono">{draft.to_account}</span> · payee{" "}
        <span className="font-medium">{draft.payee}</span> or an alias · legs within {windowDays}{" "}
        days{draft.window_days == null ? " (standard)" : ""}
        {draft.amount_exact ? ` · exactly ${fmtAmount(draft.amount_exact)}` : " · any amount"}
        {draft.effective_from ? ` · from ${draft.effective_from}` : ""}
      </p>
      <EvidenceLine suggestion={suggestion} noun="confirmed pair(s)" />

      {editing && (
        <div className="mt-3 grid grid-cols-2 gap-2 md:grid-cols-3">
          <div>
            <Label className="text-xs">window_days (0–14)</Label>
            <Input
              type="number"
              min={0}
              max={14}
              value={edits.window_days}
              onChange={(e) => setEdits({ ...edits, window_days: e.target.value })}
              placeholder={String(STANDARD_WINDOW_DAYS)}
            />
          </div>
          <div>
            <Label className="text-xs">amount_exact</Label>
            <Input
              value={edits.amount_exact}
              onChange={(e) => setEdits({ ...edits, amount_exact: e.target.value })}
              placeholder="any amount"
            />
          </div>
          <div>
            <Label className="text-xs">effective_from</Label>
            <Input
              type="date"
              value={edits.effective_from}
              onChange={(e) => setEdits({ ...edits, effective_from: e.target.value })}
            />
          </div>
        </div>
      )}

      <div className="mt-3 flex flex-wrap gap-2">
        {!editing && !rejecting && (
          <>
            <Button
              size="sm"
              disabled={decide.isPending}
              title="Creates the rule as a draft; activating it is the next step"
              onClick={() => decide.mutate({ decision: "accept", reject_scope: "exact" })}
            >
              Create draft rule
            </Button>
            <Button size="sm" variant="outline" onClick={() => setEditing(true)}>
              Edit & accept
            </Button>
            <Button
              size="sm"
              variant="ghost"
              disabled={decide.isPending}
              onClick={() => decide.mutate({ decision: "snooze", reject_scope: "exact" })}
            >
              Snooze 30d
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setRejecting(true)}>
              Reject
            </Button>
          </>
        )}
        {editing && (
          <>
            <Button size="sm" disabled={decide.isPending} onClick={submitEdits}>
              Create with edits
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setEditing(false)}>
              Cancel
            </Button>
          </>
        )}
        {rejecting && (
          <RejectButtons
            pending={decide.isPending}
            neverLabel="Never for this pair"
            neverTitle="Never suggest a transfer link between these two accounts again"
            onReject={(scope) => decide.mutate({ decision: "reject", reject_scope: scope })}
            onCancel={() => setRejecting(false)}
          />
        )}
      </div>
      {error && <ErrorText>{error}</ErrorText>}
    </div>
  );
}

function RecurrenceInput({
  id,
  value,
  onChange,
}: {
  id: string;
  value: string;
  onChange: (value: string) => void;
}) {
  const monthly = /^FREQ=MONTHLY;BYMONTHDAY=(\d{1,2})$/.exec(value);
  const weekly = /^FREQ=WEEKLY;(?:INTERVAL=(2|4);)?BYDAY=(MO|TU|WE|TH|FR|SA|SU)$/.exec(value);
  const mode = monthly ? "monthly" : weekly ? `weeks-${weekly[1] || "1"}` : "custom";
  const [custom, setCustom] = useState(mode === "custom");
  return (
    <div className="space-y-2">
      <select
        id={id}
        className="h-9 w-full rounded-md border border-border bg-card px-2 text-sm"
        value={custom ? "custom" : mode}
        onChange={(e) => {
          const next = e.target.value;
          setCustom(next === "custom");
          if (next === "monthly") onChange(`FREQ=MONTHLY;BYMONTHDAY=${monthly?.[1] || "1"}`);
          else if (next !== "custom")
            onChange(
              `FREQ=WEEKLY;${next === "weeks-1" ? "" : `INTERVAL=${next.slice(-1)};`}BYDAY=${weekly?.[2] || "MO"}`,
            );
        }}
      >
        <option value="monthly">Every month</option>
        <option value="weeks-1">Every week</option>
        <option value="weeks-2">Every 2 weeks</option>
        <option value="weeks-4">Every 4 weeks</option>
        <option value="custom">Custom recurrence</option>
      </select>
      {custom ? (
        <Input
          aria-label="Custom recurrence rule"
          value={value}
          onChange={(e) => onChange(e.target.value)}
        />
      ) : monthly ? (
        <Input
          type="number"
          aria-label="Day of month"
          min={1}
          max={31}
          value={monthly[1]}
          onChange={(e) => onChange(`FREQ=MONTHLY;BYMONTHDAY=${e.target.value}`)}
        />
      ) : (
        <select
          className="h-9 w-full rounded-md border border-border bg-card px-2 text-sm"
          aria-label="Day of week"
          value={weekly?.[2] || "MO"}
          onChange={(e) => onChange(value.replace(/BYDAY=\w+/, `BYDAY=${e.target.value}`))}
        >
          {["MO", "TU", "WE", "TH", "FR", "SA", "SU"].map((day, i) => (
            <option key={day} value={day}>
              {["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][i]}
            </option>
          ))}
        </select>
      )}
    </div>
  );
}
