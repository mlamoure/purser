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
 * and there is nothing to edit. */

import { useState } from "react";
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
              <Td>{o.date}</Td>
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
          {evidence.amount ? ` · amount variability ${(Number(evidence.amount.cv) * 100).toFixed(0)}%` : ""}
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
};

function CardHeader({ suggestion }: { suggestion: SuggestionOut }) {
  return (
    <div className="flex flex-wrap items-center gap-2">
      <span className="grow font-medium">{suggestion.headline}</span>
      <Badge>{sourceLabel(suggestion.source)}</Badge>
      {suggestion.confidence && (
        <Badge variant={confidenceVariant(suggestion.confidence)}>
          {Math.round(Number(suggestion.confidence) * 100)}% sure
        </Badge>
      )}
      {suggestion.seen_count > 1 && (
        <span className="text-xs text-muted-foreground">seen {suggestion.seen_count}×</span>
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
  return <ScheduleCard suggestion={suggestion} onDecided={onDecided} />;
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
  const variance = Number(draft.variance);

  return (
    <div className="rounded-lg border border-border p-3 text-sm">
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
        {variance === 0 ? "same amount" : <>variance {fmtAmount(draft.variance)}</>}
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
              Accept
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
    <div className="rounded-lg border border-border p-3 text-sm">
      <CardHeader suggestion={suggestion} />
      <EvidenceLine suggestion={suggestion} noun="occurrences" />

      {editing && (
        <div className="mt-3 grid grid-cols-2 gap-2 md:grid-cols-3">
          {(["name", "rrule", "dtstart", "amount", "category"] as const).map((key) => (
            <div key={key}>
              <Label className="text-xs">{key}</Label>
              <Input
                value={edits[key]}
                onChange={(e) => setEdits({ ...edits, [key]: e.target.value })}
                placeholder={key === "category" ? "expenses.…" : ""}
              />
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
              Accept
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
              Accept with edits
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
      <div className="rounded-lg border border-border p-3 text-sm">
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
    <div className="rounded-lg border border-border p-3 text-sm">
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
              Accept
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
              Accept with edits
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
