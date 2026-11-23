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
 * remembers they are not one, and there is nothing to edit. A `split`
 * suggestion (#590, decision 118) offers a new bank row its payee's last
 * split: accept applies the lines as shown, Edit opens the split editor
 * prefilled and accepts the edited lines; never applied on its own. A `trade_rule`
 * suggestion (#591, decision 119) — proposed when you mark a row as a
 * securities trade — accepts into a DRAFT categorize rule that marks rows
 * like it as a securities trade; activating it is the next step.
 *
 * Every card reads accounts and categories by name (#552): the server's
 * headline names them, and the card's own lines turn the draft's paths into
 * names with the #541 helpers. The draft keeps the paths accepting needs. */

import { useId, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, errorDetail } from "../lib/api";
import { formatMoney, moneySign, negateMoney } from "../lib/money";
import { formatDate } from "../lib/dates";
import type { components } from "../api/schema";
import { Badge, Button, ErrorText, Input, Label, Table, Td, Th } from "./ui";
import SplitEditor from "./SplitEditor";
import { EstimateText, EstimatedAmount } from "./EstimatedAmount";
import CategorySelect from "./CategorySelect";
import { useAccountNames, usePathNames } from "../lib/names";

export type SuggestionOut = components["schemas"]["SuggestionOut"];
type Decision = components["schemas"]["DecisionRequest"];
type DecisionOut = components["schemas"]["DecisionOut"];

/** The standard transfer window, when a transfer_link draft sets none. */
const STANDARD_WINDOW_DAYS = 3;

function sourceLabel(source: string): string {
  if (source === "account_added") return "new account";
  if (source === "confirm") return "your confirm";
  if (source === "mark") return "your mark";
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
                    {formatDate(o.date)}
                  </Link>
                ) : (
                  formatDate(o.date)
                )}
              </Td>
              {pairs && <Td>{o.incoming_date ? formatDate(o.incoming_date) : ""}</Td>}
              <Td className="text-right font-mono">{formatMoney(o.amount)}</Td>
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
      <span className="min-w-0 grow break-words font-medium">
        <EstimateText text={suggestion.headline} />
      </span>
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
            ? ` on ${formatDate(summary.first)}`
            : ` from ${formatDate(summary.first)} to ${formatDate(summary.last)}`
          : ""}
        {summary.amount_range
          ? summary.amount_range.low === summary.amount_range.high
            ? ` · ${formatMoney(summary.amount_range.low)}`
            : ` · ${formatMoney(summary.amount_range.low)} to ${formatMoney(summary.amount_range.high)}`
          : ""}
        {summary.cadence ? ` · ${summary.cadence}` : ""}
        {" · "}
        <button className="underline" onClick={() => setShowEvidence((v) => !v)}>
          {showEvidence ? "hide evidence" : "evidence"}
        </button>
      </p>
      {summary.warning && <p className="mt-2 text-sm text-warning">{summary.warning}</p>}
      {summary.price_changed && (
        <p className="mt-2 text-sm text-warning">
          Recent amount changed to {formatMoney(summary.latest_amount)}. Verify the current price
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
  if (suggestion.kind === "split")
    return <SplitCard suggestion={suggestion} onDecided={onDecided} />;
  if (suggestion.kind === "trade_rule")
    return <TradeRuleCard suggestion={suggestion} onDecided={onDecided} />;
  return <ScheduleCard suggestion={suggestion} onDecided={onDecided} />;
}

type SplitDraft = {
  transaction_id: string;
  template_id: string;
  account: string;
  date: string;
  amount: string;
  template_amount: string;
  payee: string | null;
  lines: { target: string; amount: string; memo: string | null; backing_ids: string[] }[];
};

/** A split suggestion (#590): "split like last time?" for a new bank row
 * from a payee whose last split you (or your agent) made. The lines are the
 * last split's, with the difference in amount on its largest category line;
 * an account line names the one bank row that backs it exactly, when there
 * is one. Accept splits the row as shown; Edit opens the split editor; reject
 * is remembered (or never for this payee); snooze only hides the card. */
function SplitCard({
  suggestion,
  onDecided,
}: {
  suggestion: SuggestionOut;
  onDecided?: () => void;
}) {
  const qc = useQueryClient();
  const pathName = usePathNames();
  const draft = suggestion.draft as unknown as SplitDraft;
  const [rejecting, setRejecting] = useState(false);
  const [editing, setEditing] = useState(false);
  const decided = () => {
    qc.invalidateQueries({ queryKey: ["suggestions"] });
    qc.invalidateQueries({ queryKey: ["register"] });
    qc.invalidateQueries({ queryKey: ["accounts"] });
    onDecided?.();
  };
  const { decide, error } = useDecide(suggestion.id, decided);
  const lineName = (target: string) =>
    /^\[(.*)\]$/.test(target) ? `To ${pathName(target.slice(1, -1))}` : pathName(target);

  return (
    <div className="min-w-0 break-words rounded-lg border border-border p-3 text-sm">
      <CardHeader suggestion={suggestion} />
      <p className="mt-1 text-xs text-muted-foreground">
        {formatMoney(draft.amount)} on {pathName(draft.account)} · {formatDate(draft.date)}
        {draft.payee ? ` · ${draft.payee}` : ""} · last time {formatMoney(draft.template_amount)}
      </p>
      <Table className="mt-2 text-xs">
        <thead>
          <tr>
            <Th>Line</Th>
            <Th className="text-right">Amount</Th>
            <Th>Backed by</Th>
          </tr>
        </thead>
        <tbody>
          {draft.lines.map((l, i) => (
            <tr key={i}>
              <Td>
                {lineName(l.target)}
                {l.memo ? <span className="text-muted-foreground"> · {l.memo}</span> : null}
              </Td>
              <Td className="text-right font-mono">{formatMoney(l.amount)}</Td>
              <Td className="text-muted-foreground">
                {/^\[.*\]$/.test(l.target)
                  ? l.backing_ids.length
                    ? `${l.backing_ids.length} bank row${l.backing_ids.length === 1 ? "" : "s"}`
                    : "none yet"
                  : ""}
              </Td>
            </tr>
          ))}
        </tbody>
      </Table>
      <Link className="mt-2 inline-block text-xs underline" to={`/selection?id=${draft.transaction_id}`}>
        Inspect the transaction
      </Link>
      {editing && (
        <div className="mt-3">
          <SplitEditor
            txId={draft.transaction_id}
            amount={draft.amount}
            accountPath={draft.account}
            isSplit={false}
            initialLines={draft.lines.map((l) => ({
              category: l.target,
              amount: l.amount,
              memo: l.memo ?? "",
              backing: l.backing_ids,
            }))}
            onSave={async (lines) => {
              await decide.mutateAsync({
                decision: "edit_then_accept",
                reject_scope: "exact",
                edits: {
                  lines: lines.map((l) => ({
                    target: l.category,
                    amount: l.amount,
                    memo: l.memo ?? null,
                    backing_ids: l.backing_ids ?? [],
                  })),
                },
              });
            }}
            onClose={() => setEditing(false)}
          />
        </div>
      )}
      <div className="mt-3 flex flex-wrap gap-2">
        {!rejecting && !editing && (
          <>
            <Button
              size="sm"
              disabled={decide.isPending}
              onClick={() => decide.mutate({ decision: "accept", reject_scope: "exact" })}
            >
              Split like this
            </Button>
            <Button size="sm" variant="outline" onClick={() => setEditing(true)}>
              Edit…
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
          <RejectButtons
            pending={decide.isPending}
            neverLabel="Never for this payee"
            neverTitle="Never suggest splitting this payee's rows on this account again"
            onReject={(scope) => decide.mutate({ decision: "reject", reject_scope: scope })}
            onCancel={() => setRejecting(false)}
          />
        )}
      </div>
      {error && <ErrorText>{error}</ErrorText>}
    </div>
  );
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
  const accountName = useAccountNames();
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
            <Td>{accountName(draft.from_account)}</Td>
            <Td>{formatDate(draft.outgoing_date)}</Td>
            <Td className="max-w-56 truncate">{draft.outgoing_description ?? ""}</Td>
            <Td className="text-right font-mono">{formatMoney(negateMoney(draft.amount))}</Td>
          </tr>
          <tr>
            <Td>Arrives</Td>
            <Td>{accountName(draft.to_account)}</Td>
            <Td>{formatDate(draft.incoming_date)}</Td>
            <Td className="max-w-56 truncate">{draft.incoming_description ?? ""}</Td>
            <Td className="text-right font-mono">{formatMoney(draft.amount)}</Td>
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
  const accountName = useAccountNames();
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
            <Td>{formatDate(draft.expected_date)}</Td>
            <Td className="text-right font-mono">{formatMoney(draft.expected_amount)}</Td>
          </tr>
          <tr>
            <Td>Bank row</Td>
            <Td>{formatDate(draft.posted_date)}</Td>
            <Td className="text-right font-mono">{formatMoney(draft.posted_amount)}</Td>
          </tr>
        </tbody>
      </Table>
      <p className="mt-1 text-xs text-muted-foreground">
        on {accountName(draft.account)} ·{" "}
        {sameAmount ? "same amount" : <>variance {formatMoney(draft.variance, { signDisplay: "always" })}</>}
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
  const accountName = useAccountNames();
  const draft = suggestion.draft as Record<string, string | null>;
  // The category picker's value (#552, as on the schedule form): a category
  // path, or "[account path]" for a transfer — the draft's
  // counterparty_account. A schedule is one or the other, never both.
  const draftTarget = draft.counterparty_account
    ? `[${draft.counterparty_account}]`
    : (draft.category ?? "");
  const [edits, setEdits] = useState<Record<string, string>>({
    payee: draft.payee ?? "",
    memo: draft.memo ?? "",
    rrule: draft.rrule ?? "",
    dtstart: draft.dtstart ?? "",
    amount: draft.amount ?? "",
    target: draftTarget,
  });
  const { decide, error } = useDecide(suggestion.id, () => {
    qc.invalidateQueries({ queryKey: ["suggestions"] });
    qc.invalidateQueries({ queryKey: ["schedules"] });
    onDecided?.();
  });

  const submitEdits = () => {
    const changed: Record<string, string | null> = {};
    const { target, ...fields } = edits;
    for (const [key, value] of Object.entries(fields)) {
      const before = draft[key] ?? "";
      if (value !== before) changed[key] = value === "" ? null : value;
    }
    if (target !== draftTarget) {
      const transfer = /^\[(.*)\]$/.exec(target);
      const category = transfer || target === "" ? null : target;
      const counterparty = transfer ? transfer[1] : null;
      if (category !== (draft.category ?? null)) changed.category = category;
      if (counterparty !== (draft.counterparty_account ?? null))
        changed.counterparty_account = counterparty;
    }
    // The draft's name is the derived label (#534): keep it in step with a
    // payee or memo edit, so accepting doesn't read the old name as a memo.
    if ("payee" in changed || "memo" in changed) {
      const label = (edits.memo || edits.payee || draft.name || "").trim();
      if (label) changed.name = label;
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
        Creates an active scheduled transaction on {accountName(draft.account)}; no payment is
        sent. First date:{" "}
        {formatDate(edits.dtstart)}. Amount:{" "}
        {draft.amount_type === "estimated" && edits.amount ? (
          <EstimatedAmount value={edits.amount} />
        ) : (
          formatMoney(edits.amount)
        )}
        . Pause or edit it from Scheduled transactions.
      </p>

      {editing && (
        <div className="mt-3 grid grid-cols-2 gap-2 md:grid-cols-3">
          {(["payee", "memo", "rrule", "dtstart", "amount", "target"] as const).map((key) => (
            <div key={key}>
              <Label className="text-xs" htmlFor={`${formId}-${key}`}>
                {
                  {
                    payee: "Payee",
                    memo: "Memo",
                    rrule: "Repeats",
                    dtstart: "First date",
                    amount: "Amount (negative for money out)",
                    target: "Category, or transfer to",
                  }[key]
                }
              </Label>
              {key === "rrule" ? (
                <RecurrenceInput
                  id={`${formId}-${key}`}
                  value={edits.rrule}
                  onChange={(value) => setEdits({ ...edits, rrule: value })}
                />
              ) : key === "target" ? (
                <CategorySelect
                  id={`${formId}-${key}`}
                  value={edits.target}
                  currentAccountPath={draft.account ?? ""}
                  direction={moneySign(edits.amount) > 0 ? "credit" : "debit"}
                  onChange={(value) => setEdits({ ...edits, target: value })}
                />
              ) : (
                <Input
                  id={`${formId}-${key}`}
                  type={key === "dtstart" ? "date" : "text"}
                  value={edits[key]}
                  onChange={(e) => setEdits({ ...edits, [key]: e.target.value })}
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
              Create scheduled transaction
            </Button>
            <Button size="sm" variant="outline" onClick={() => setEditing(true)}>
              Edit first
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
  const accountName = useAccountNames();
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
          <Link to="/settings/rules" className="self-center text-xs underline">
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
        {accountName(draft.from_account)} and {accountName(draft.to_account)} · payee{" "}
        <span className="font-medium">{draft.payee}</span> or an alias · legs within {windowDays}{" "}
        days{draft.window_days == null ? " (standard)" : ""}
        {draft.amount_exact ? ` · exactly ${formatMoney(draft.amount_exact)}` : " · any amount"}
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

type TradeRuleDraft = {
  account: string;
  account_id: string;
  match_field: "payee" | "description" | "payee_or_description";
  match_mode: "equals" | "prefix" | "contains";
  match_text: string;
  direction: "debit" | "credit" | null;
};

const MATCH_FIELD_LABEL: Record<TradeRuleDraft["match_field"], string> = {
  payee: "payee",
  description: "description",
  payee_or_description: "payee or description",
};
const MATCH_MODE_LABEL: Record<TradeRuleDraft["match_mode"], string> = {
  equals: "is",
  prefix: "starts with",
  contains: "contains",
};

/** A trade_rule suggestion (#591, decision 119): you marked a row on a
 * brokerage account as a securities trade, and Purser offers a rule that
 * marks rows like it the same way. Purser never decides which rows are
 * trades by itself (brokers word them differently), so the wording is
 * yours to edit. Accept creates the rule as a draft; activating it is the
 * next step, and history is marked only through the Rules page's
 * apply-history (a dry run first). */
function TradeRuleCard({
  suggestion,
  onDecided,
}: {
  suggestion: SuggestionOut;
  onDecided?: () => void;
}) {
  const qc = useQueryClient();
  const accountName = useAccountNames();
  const draft = suggestion.draft as unknown as TradeRuleDraft;
  const [editing, setEditing] = useState(false);
  const [rejecting, setRejecting] = useState(false);
  const [edits, setEdits] = useState({
    match_field: draft.match_field,
    match_mode: draft.match_mode,
    match_text: draft.match_text,
    direction: draft.direction ?? "",
  });
  const [created, setCreated] = useState<{ id: string; projected: number } | null>(null);
  const [activationError, setActivationError] = useState<string | null>(null);

  const finish = () => {
    qc.invalidateQueries({ queryKey: ["suggestions"] });
    qc.invalidateQueries({ queryKey: ["rules"] });
    onDecided?.();
  };
  const { decide, error } = useDecide(suggestion.id, (data) => {
    if (data.rule) {
      qc.invalidateQueries({ queryKey: ["rules"] });
      setCreated({ id: data.rule.id, projected: data.projected_matches ?? 0 });
      return;
    }
    finish();
  });
  const activate = useMutation({
    mutationFn: async (ruleId: string) => {
      const { error: err } = await api.POST("/rules/{rule_id}/state", {
        params: { path: { rule_id: ruleId } },
        body: { state: "active" },
      });
      if (err) throw new Error(errorDetail(err));
    },
    onSuccess: finish,
    onError: (e: Error) => setActivationError(e.message),
  });

  const submitEdits = () => {
    const changed: Record<string, string | null> = {};
    if (edits.match_field !== draft.match_field) changed.match_field = edits.match_field;
    if (edits.match_mode !== draft.match_mode) changed.match_mode = edits.match_mode;
    if (edits.match_text.trim() !== draft.match_text) changed.match_text = edits.match_text.trim();
    if (edits.direction !== (draft.direction ?? ""))
      changed.direction = edits.direction === "" ? null : edits.direction;
    if (Object.keys(changed).length === 0) {
      decide.mutate({ decision: "accept", reject_scope: "exact" });
      return;
    }
    decide.mutate({ decision: "edit_then_accept", edits: changed, reject_scope: "exact" });
  };

  const direction =
    draft.direction === "debit"
      ? "money out (buys)"
      : draft.direction === "credit"
        ? "money in (sells)"
        : "either direction";

  if (created) {
    return (
      <div className="min-w-0 break-words rounded-lg border border-border p-3 text-sm">
        <CardHeader suggestion={suggestion} />
        <p className="mt-2">
          Rule created as a <strong>draft</strong>. It matches <strong>{created.projected}</strong>{" "}
          row(s) in your history today. Active, it marks new rows like these as a securities trade
          as they arrive; mark past rows from the Rules page (apply to history, with a preview
          first).
        </p>
        <div className="mt-3 flex flex-wrap gap-2">
          <Button size="sm" disabled={activate.isPending} onClick={() => activate.mutate(created.id)}>
            Activate
          </Button>
          <Button size="sm" variant="ghost" onClick={finish}>
            Leave as draft
          </Button>
          <Link to="/settings/rules" className="self-center text-xs underline">
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
        On {accountName(draft.account)}: {MATCH_FIELD_LABEL[draft.match_field]}{" "}
        {MATCH_MODE_LABEL[draft.match_mode]} <span className="font-medium">"{draft.match_text}"</span>{" "}
        · {direction} → Securities trade
      </p>
      <EvidenceLine suggestion={suggestion} noun="marked row(s)" />

      {editing && (
        <div className="mt-3 grid grid-cols-2 gap-2 md:grid-cols-4">
          <div>
            <Label className="text-xs" htmlFor={`${suggestion.id}-field`}>
              Match on
            </Label>
            <select
              id={`${suggestion.id}-field`}
              className="h-9 w-full rounded-md border border-border bg-card px-2 text-sm"
              value={edits.match_field}
              onChange={(e) =>
                setEdits({ ...edits, match_field: e.target.value as TradeRuleDraft["match_field"] })
              }
            >
              <option value="payee_or_description">payee or description</option>
              <option value="payee">payee</option>
              <option value="description">description</option>
            </select>
          </div>
          <div>
            <Label className="text-xs" htmlFor={`${suggestion.id}-mode`}>
              How
            </Label>
            <select
              id={`${suggestion.id}-mode`}
              className="h-9 w-full rounded-md border border-border bg-card px-2 text-sm"
              value={edits.match_mode}
              onChange={(e) =>
                setEdits({ ...edits, match_mode: e.target.value as TradeRuleDraft["match_mode"] })
              }
            >
              <option value="prefix">starts with</option>
              <option value="contains">contains</option>
              <option value="equals">is</option>
            </select>
          </div>
          <div>
            <Label className="text-xs" htmlFor={`${suggestion.id}-text`}>
              Text
            </Label>
            <Input
              id={`${suggestion.id}-text`}
              value={edits.match_text}
              onChange={(e) => setEdits({ ...edits, match_text: e.target.value })}
            />
          </div>
          <div>
            <Label className="text-xs" htmlFor={`${suggestion.id}-direction`}>
              Direction
            </Label>
            <select
              id={`${suggestion.id}-direction`}
              className="h-9 w-full rounded-md border border-border bg-card px-2 text-sm"
              value={edits.direction}
              onChange={(e) => setEdits({ ...edits, direction: e.target.value })}
            >
              <option value="debit">money out (buys)</option>
              <option value="credit">money in (sells)</option>
              <option value="">either</option>
            </select>
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
            <Button
              size="sm"
              disabled={decide.isPending || !edits.match_text.trim()}
              onClick={submitEdits}
            >
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
            neverLabel="Never for this account"
            neverTitle="Never suggest a securities-trade rule for this account again"
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
