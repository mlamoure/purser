import { useEffect, useMemo, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, errorDetail } from "../lib/api";
import { useDebouncedValue } from "../lib/useDebouncedValue";
import { useDocumentTitle } from "../lib/useDocumentTitle";
import { useIsAdmin } from "../lib/useIsAdmin";
import { cn, overrideBody } from "../lib/utils";
import { formatMoney, moneyEditText, moneySign, negateMoney } from "../lib/money";
import { addDays, formatDate } from "../lib/dates";
import { Badge, Button, Card, ErrorText, Input, Table, Td, Th } from "../components/ui";
import CategorySelect, { SECURITIES_TRADE_VALUE } from "../components/CategorySelect";
import SplitEditor from "../components/SplitEditor";
import { categoriesQuery, categoryValueLabel, namesFrom, usePathNames } from "../lib/names";
import { QueryState } from "../components/QueryState";
import { SearchHelp } from "../components/SearchHelp";
import PositionsPanel from "./Positions";
import { AccountIcon } from "../components/AccountIcon";
import { AssetPanel } from "../components/AssetPanel";
import { BalanceHistoryChart } from "../components/BalanceHistoryChart";
import { HoldingsCard } from "../components/HoldingsCard";
import type { components } from "../api/schema";

type TransactionOut = components["schemas"]["TransactionOut"];
type MatchOut = components["schemas"]["MatchOut"];
type TransferCandidateOut = components["schemas"]["TransferCandidateOut"];
type AccountOut = components["schemas"]["AccountOut"];

const PAGE_SIZE = 50;
// The search box queries once typing pauses this long, not per keystroke (#141).
const SEARCH_DEBOUNCE_MS = 250;
const selectClass = "h-9 rounded-md border border-border bg-card px-2 text-sm";

/** The per-account register (issue #112): every transaction the account has
 * seen — SimpleFIN imports, manual entries, agent proposals — with what fixes
 * a row inline on it. A row that needs review says why (decision 141), and
 * `?needs_review=true` (the Dashboard's links) opens only those rows. */
export default function AccountRegister() {
  const { accountId } = useParams<{ accountId: string }>();
  const [params] = useSearchParams();
  const [reviewOnly, setReviewOnly] = useState(params.get("needs_review") === "true");
  // Posted rows recategorized here: replacement id -> the category cell
  // value it replaced, for the row's "Corrected · Undo" (#541).
  const [corrections, setCorrections] = useState<Record<string, string>>({});
  const [q, setQ] = useState("");
  const searchQ = useDebouncedValue(q, SEARCH_DEBOUNCE_MS);
  const [statusFilter, setStatusFilter] = useState<"live" | "posted" | "pending" | "expected">(
    ["posted", "pending", "expected"].includes(params.get("status") ?? "")
      ? (params.get("status") as "posted" | "pending" | "expected")
      : "live",
  );
  const [start, setStart] = useState(params.get("start") ?? "");
  const [end, setEnd] = useState(params.get("end") ?? "");
  const [offset, setOffset] = useState(0);
  // A brokerage account whose holdings are entered in Purser (#390) adds a Positions tab.
  const [tab, setTab] = useState<"register" | "positions">("register");

  const account = useQuery({
    queryKey: ["account", accountId],
    queryFn: async () => {
      const { data, error } = await api.GET("/accounts/{account_id}", {
        params: { path: { account_id: accountId! } },
      });
      if (error) throw new Error(errorDetail(error));
      return data;
    },
    enabled: !!accountId,
  });

  const statuses = statusFilter === "live" ? ["pending", "posted"] : [statusFilter];
  const page = useQuery({
    queryKey: ["register", accountId, searchQ, statusFilter, start, end, offset, reviewOnly],
    queryFn: async () => {
      const { data, error } = await api.GET("/transactions", {
        params: {
          query: {
            account: accountId!,
            status: statuses,
            with_category_provenance: true,
            limit: PAGE_SIZE,
            offset,
            ...(reviewOnly ? { needs_review: true } : {}),
            ...(searchQ ? { q: searchQ } : {}),
            ...(start ? { start } : {}),
            ...(end ? { end } : {}),
          },
        },
      });
      if (error) throw new Error(errorDetail(error));
      return data;
    },
    enabled: !!accountId,
  });

  const candidates = useQuery({
    queryKey: ["transfer-candidates"],
    queryFn: async () => (await api.GET("/matching/transfer-candidates")).data ?? [],
  });
  const proposed = useQuery({
    queryKey: ["proposed-matches"],
    queryFn: async () => (await api.GET("/matching/proposed")).data ?? [],
  });

  useEffect(() => {
    const id = params.get("transaction");
    if (id && page.data)
      document.getElementById(`transaction-${id}`)?.scrollIntoView?.({ block: "center" });
  }, [params, page.data]);
  if (!accountId) return null;
  const currency = account.data?.currency;
  const total = page.data?.total ?? 0;
  const manualHoldings =
    account.data?.type === "brokerage" && account.data?.holdings_source === "manual";

  return (
    <div className="space-y-6">
      <AccountHeader account={account.data} />
      {account.data && !account.data.is_virtual && (
        <BalanceHistoryChart
          account={account.data}
          onPickDate={(day) => {
            // A day on the chart opens that day in the register (#594).
            setTab("register");
            setStart(day);
            setEnd(day);
            setOffset(0);
          }}
        />
      )}
      {account.data?.type === "property" && <AssetPanel account={account.data} />}
      {account.data?.type === "brokerage" && (
        <HoldingsCard
          accountId={account.data.id}
          currency={account.data.currency}
          onEditPositions={manualHoldings ? () => setTab("positions") : undefined}
        />
      )}

      {manualHoldings && (
        <div className="flex gap-1 border-b border-border" role="tablist">
          {(
            [
              ["register", "Register"],
              ["positions", "Positions"],
            ] as const
          ).map(([key, label]) => (
            <button
              key={key}
              role="tab"
              aria-selected={tab === key}
              className={cn(
                "-mb-px border-b-2 border-transparent px-3 py-2 text-sm font-medium text-muted-foreground hover:text-foreground",
                tab === key && "border-primary text-foreground",
              )}
              onClick={() => setTab(key)}
            >
              {label}
            </button>
          ))}
        </div>
      )}

      {manualHoldings && tab === "positions" ? (
        <PositionsPanel accountId={accountId} currency={account.data?.currency} />
      ) : (
        <>
          <div className="flex flex-wrap items-end gap-3">
            <div className="flex grow gap-1">
              <Input
                aria-label="Search transactions"
                placeholder="Search payee, description, memo…"
                value={q}
                onChange={(e) => {
                  setQ(e.target.value);
                  setOffset(0);
                }}
              />
              <SearchHelp />
            </div>
            <select
              className={selectClass}
              aria-label="Transaction status"
              value={statusFilter}
              onChange={(e) => {
                setStatusFilter(e.target.value as typeof statusFilter);
                setOffset(0);
              }}
            >
              <option value="live">Pending + posted</option>
              <option value="posted">Posted only</option>
              <option value="pending">Pending only</option>
              <option value="expected">Expected (upcoming)</option>
            </select>
            <Input
              type="date"
              className="w-40"
              aria-label="Start date"
              value={start}
              onChange={(e) => {
                setStart(e.target.value);
                setOffset(0);
              }}
            />
            <Input
              type="date"
              className="w-40"
              aria-label="End date"
              value={end}
              onChange={(e) => {
                setEnd(e.target.value);
                setOffset(0);
              }}
            />
          </div>

          <Card>
            <div className="mb-3 flex flex-wrap items-center gap-3 text-sm">
              <label
                className="flex items-center gap-2"
                title="A bank-sync row with no category, or a reconciliation adjustment nobody has explained or accepted"
              >
                <input
                  type="checkbox"
                  checked={reviewOnly}
                  onChange={(e) => {
                    setReviewOnly(e.target.checked);
                    setOffset(0);
                  }}
                />
                Needs review only
              </label>
            </div>
            {account.error && <ErrorText>{account.error.message}</ErrorText>}
            <QueryState
              query={page}
              what="transactions"
              empty={
                <p className="text-sm text-muted-foreground">
                  No transactions match.
                  {page.data?.did_you_mean && (
                    <>
                      {" "}
                      Did you mean{" "}
                      <button
                        type="button"
                        className="font-medium text-primary underline"
                        onClick={() => {
                          setQ(page.data?.did_you_mean ?? "");
                          setOffset(0);
                        }}
                      >
                        {page.data.did_you_mean}
                      </button>
                      ?
                    </>
                  )}
                </p>
              }
              isEmpty={(data) => data.items.length === 0}
            >
              {(data) => (
                <Table>
                  <thead>
                    <tr>
                      <Th>Date</Th>
                      <Th>Payee</Th>
                      <Th>Description</Th>
                      <Th className="text-right">Amount</Th>
                      <Th className="text-right">Balance</Th>
                      <Th>Category</Th>
                      <Th>Status</Th>
                      <Th>Actions</Th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.items.map((t) => (
                      <RegisterRow
                        key={t.id}
                        t={t}
                        accountPath={account.data?.path ?? ""}
                        brokerageFeed={
                          account.data?.type === "brokerage" &&
                          account.data?.holdings_source === "feed"
                        }
                        currency={currency}
                        candidates={candidates.data ?? []}
                        proposed={proposed.data ?? []}
                        correctedFrom={corrections[t.id]}
                        onCorrected={(id, previous) =>
                          setCorrections((c) => ({ ...c, [id]: previous }))
                        }
                        onUndone={(id) =>
                          setCorrections(({ [id]: _undone, ...rest }) => rest)
                        }
                      />
                    ))}
                  </tbody>
                </Table>
              )}
            </QueryState>
            <div className="mt-3 flex items-center justify-between text-sm text-muted-foreground">
              <span>
                {total} transaction{total === 1 ? "" : "s"}
              </span>
              <span className="flex gap-2">
                <Button
                  size="sm"
                  variant="outline"
                  disabled={offset === 0}
                  onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
                >
                  Prev
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  disabled={offset + PAGE_SIZE >= total}
                  onClick={() => setOffset(offset + PAGE_SIZE)}
                >
                  Next
                </Button>
              </span>
            </div>
          </Card>
        </>
      )}
    </div>
  );
}

/** A split transaction's cell (issues #274 and #590, decisions 87 and 118):
 * "Split (n)" — n category and account lines — opens the split editor. Seen
 * from the register of an account one of its lines books to, the row reads
 * "Split from <account>": the split belongs to that other account's row. */
export function SplitCell({
  t,
  accountPath,
  open,
  onToggle,
}: {
  t: TransactionOut;
  accountPath: string;
  open: boolean;
  onToggle: () => void;
}) {
  const pathName = usePathNames();
  const elsewhere = t.split_account_path && t.split_account_path !== accountPath;
  return (
    <Button
      size="sm"
      variant="outline"
      title="A split row: one bank amount across categories and accounts. Open to edit its lines."
      aria-expanded={open}
      onClick={onToggle}
    >
      {elsewhere
        ? `Split from ${pathName(t.split_account_path)}`
        : `Split (${t.split_lines})`}
    </Button>
  );
}

/** The category cell of an asset value entry: what it is, and Undo for a
 * live one (the reversal pair; the original stays in the history). */
function ValueChangeCell({ t }: { t: TransactionOut }) {
  const qc = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const undo = useMutation({
    mutationFn: async () => {
      const { error: err } = await api.POST("/value-changes/{transaction_id}/reverse", {
        params: { path: { transaction_id: t.id } },
      });
      if (err) throw new Error(errorDetail(err));
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["transactions"] });
      qc.invalidateQueries({ queryKey: ["accounts"] });
      qc.invalidateQueries({ queryKey: ["account"] });
      qc.invalidateQueries({ queryKey: ["networth"] });
    },
    onError: (e) => setError(e.message),
  });
  const live = t.status === "posted" && moneySign(t.amount) !== 0 && !t.has_linked_originals;
  return (
    <span className="flex flex-col">
      <span className="flex items-center gap-2 text-sm text-muted-foreground">
        <span
          title={
            t.money_put_in
              ? "Money put into this asset that no tracked account paid. To correct it, undo it and record the right figure."
              : "A change in this asset's value. To correct it, undo it (an agent's reverse_value_change, or the REST API) and record the right figure."
          }
        >
          {t.money_put_in ? "Money put in" : "Value change"}
        </span>
        {live && (
          <button
            type="button"
            className="text-xs font-medium text-primary hover:underline disabled:opacity-50"
            disabled={undo.isPending}
            aria-label={`Undo the ${formatDate(t.date)} ${t.money_put_in ? "money put in" : "value change"}`}
            onClick={() => {
              if (window.confirm("Undo this entry? Purser records a reversal; the original stays in the history."))
                undo.mutate();
            }}
          >
            Undo
          </button>
        )}
      </span>
      {error && <span className="text-xs text-destructive">{error}</span>}
    </span>
  );
}

/** The PATCH body for a category cell value: "Securities trade" is sent as
 * `securities_trade: true` (#591, decision 119), anything else as a category
 * (with the register account, the side a transfer keeps). */
export function categoryBody(
  value: string,
  accountPath: string,
): { securities_trade: true } | { category: string; account: string } {
  return value === SECURITIES_TRADE_VALUE
    ? { securities_trade: true }
    : { category: value, account: accountPath };
}

export function RegisterRow({
  t,
  accountPath,
  brokerageFeed = false,
  currency,
  candidates,
  proposed,
  correctedFrom,
  onCorrected,
  onUndone,
}: {
  /** The register is a brokerage account whose holdings come from the feed:
   * its category picker offers "Securities trade" (#591, decision 119). */
  brokerageFeed?: boolean;
  /** This row is the replacement of a posted row recategorized in this
   * session; the value is the category cell it had before (#541). */
  correctedFrom?: string;
  /** A posted row was corrected: `id` is the replacement row the register
   * now shows, `previous` the category cell value it replaced. */
  onCorrected?: (id: string, previous: string) => void;
  /** The correction on this row was undone. */
  onUndone?: (id: string) => void;
  t: TransactionOut;
  accountPath: string;
  /** The register account's currency (USD when unknown). */
  currency?: string | null;
  candidates: TransferCandidateOut[];
  proposed: MatchOut[];
}) {
  const qc = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const refresh = () => qc.invalidateQueries();
  const pathName = usePathNames();

  const candidate = candidates.find((c) => c.outgoing_id === t.id || c.incoming_id === t.id);
  const match = proposed.find((m) => m.posted_id === t.id);

  // A posted row is immutable (DESIGN §4): recategorizing it records a
  // reversal and a corrected replacement. That is the normal way to fix a
  // category, so it asks nothing first (#541); the replacement row then
  // offers "Corrected · Undo".
  const [ruleProposed, setRuleProposed] = useState(false);
  const recategorize = useMutation({
    mutationFn: async (category: string) => {
      const { data, error: err } = await api.PATCH("/transactions/{tx_id}", {
        params: { path: { tx_id: t.id } },
        body: categoryBody(category, accountPath),
      });
      if (err) throw new Error(errorDetail(err));
      return data;
    },
    onSuccess: (data) => {
      setError(null);
      setRuleProposed(!!data?.suggestion_id);
      if (t.status === "posted" && data?.id) onCorrected?.(data.id, categoryValue);
      refresh();
    },
    onError: (e) => setError(e.message),
  });
  // Undo puts the previous category back the only way a posted row allows:
  // one more correction (reversal + replacement), through the same PATCH.
  // The ledger keeps both corrections; the register shows the row as it was.
  const undoCorrection = useMutation({
    mutationFn: async (previous: string) => {
      const { error: err } = await api.PATCH("/transactions/{tx_id}", {
        params: { path: { tx_id: t.id } },
        body: categoryBody(previous, accountPath),
      });
      if (err) throw new Error(errorDetail(err));
    },
    onSuccess: () => {
      setError(null);
      onUndone?.(t.id);
      refresh();
    },
    onError: (e) => setError(e.message),
  });

  const useAction = (fn: () => Promise<{ error?: unknown }>) =>
    useMutation({
      mutationFn: async () => {
        const { error: err } = await fn();
        if (err) throw new Error(errorDetail(err));
      },
      onSuccess: refresh,
      onError: (e: Error) => setError(e.message),
    });

  const confirmMatch = useAction(() =>
    api.POST("/matching/{match_id}/confirm", {
      params: { path: { match_id: match?.id ?? "" } },
    }),
  );
  const rejectMatch = useAction(() =>
    api.POST("/matching/{match_id}/reject", {
      params: { path: { match_id: match?.id ?? "" } },
    }),
  );
  const unlink = useAction(() =>
    api.POST("/transactions/{tx_id}/unlink-transfer", {
      params: { path: { tx_id: t.id } },
    }),
  );
  const postTx = useAction(() =>
    api.POST("/transactions/{tx_id}/post", { params: { path: { tx_id: t.id } } }),
  );
  const voidTx = useAction(() =>
    api.POST("/transactions/{tx_id}/void", { params: { path: { tx_id: t.id } } }),
  );

  const confirmTransfer = useMutation({
    mutationFn: async (pair: { outgoing_id: string; incoming_id: string }) => {
      const { error: err } = await api.POST("/matching/confirm-transfer", { body: pair });
      if (err) throw new Error(errorDetail(err));
    },
    onSuccess: refresh,
    onError: (e) => setError(e.message),
  });
  // "Not a transfer" (#386): a pair with an inbox card is rejected through
  // the card, so the inbox and the register agree; an older pair, which has
  // no card, through the candidate endpoint. Either way it is remembered and
  // nothing links the pair automatically again.
  const notATransfer = useMutation({
    mutationFn: async (pair: TransferCandidateOut) => {
      if (pair.suggestion_id) {
        const { error: err } = await api.POST("/suggestions/{suggestion_id}/decide", {
          params: { path: { suggestion_id: pair.suggestion_id } },
          body: { decision: "reject", reject_scope: "exact" },
        });
        if (err) throw new Error(errorDetail(err));
        return;
      }
      const { error: err } = await api.POST("/matching/transfer-candidates/reject", {
        body: { outgoing_id: pair.outgoing_id, incoming_id: pair.incoming_id },
      });
      if (err) throw new Error(errorDetail(err));
    },
    onSuccess: refresh,
    onError: (e) => setError(e.message),
  });

  // A reconciliation adjustment needs review until it is explained (its
  // category changed or split away from expenses.reconciliation_adjustments)
  // or accepted as it is (decision 141). Accepting is recorded on the
  // reconciliation and audited, with an optional note.
  const [accepting, setAccepting] = useState(false);
  const [acceptNote, setAcceptNote] = useState("");
  const accept = useMutation({
    mutationFn: async () => {
      const note = acceptNote.trim();
      const { error: err } = await api.POST("/transactions/{tx_id}/accept-adjustment", {
        params: { path: { tx_id: t.id } },
        ...(note ? { body: { note } } : {}),
      });
      if (err) throw new Error(errorDetail(err));
    },
    onSuccess: () => {
      setError(null);
      setAccepting(false);
      setAcceptNote("");
      // The row's flag and every review count read the same predicate.
      qc.invalidateQueries({ queryKey: ["register"] });
      qc.invalidateQueries({ queryKey: ["needs-review"] });
      qc.invalidateQueries({ queryKey: ["search"] });
    },
    onError: (e) => setError(e.message),
  });

  // Schedule-instance actions (#236): an `expected` row a schedule
  // materialized can be overridden (pins it), skipped (the slot stays
  // filled, it never comes back) or marked paid against a posted row.
  const isInstance = t.status === "expected" && !!t.schedule_id;
  const [overriding, setOverriding] = useState(false);
  const [override, setOverride] = useState<{ amount: string; date: string }>({
    amount: t.amount ?? "",
    date: t.date,
  });
  const [picking, setPicking] = useState(false);
  const overrideTx = useMutation({
    mutationFn: async () => {
      // Only what changed (#350); an amount is this register's leg, so it
      // names the account — on a transfer instance that picks the side.
      const changed = overrideBody(t, override);
      if (Object.keys(changed).length === 0) return; // nothing changed: no edit, no pin
      const body = changed.amount === undefined ? changed : { ...changed, account: accountPath };
      const { error: err } = await api.PATCH("/transactions/{tx_id}", {
        params: { path: { tx_id: t.id } },
        body,
      });
      if (err) throw new Error(errorDetail(err));
    },
    onSuccess: () => {
      setOverriding(false);
      refresh();
    },
    onError: (e) => setError(e.message),
  });
  const markPaid = useMutation({
    mutationFn: async (postedId: string) => {
      const { error: err } = await api.POST("/matching/match", {
        body: { expected_id: t.id, posted_id: postedId },
      });
      if (err) throw new Error(errorDetail(err));
    },
    onSuccess: () => {
      setPicking(false);
      refresh();
    },
    onError: (e) => setError(e.message),
  });

  const categoryValue = t.is_transfer
    ? `[${t.transfer_account_path ?? ""}]`
    : t.securities_trade
      ? SECURITIES_TRADE_VALUE
      : (t.category_path ?? "");
  const autoLinked = t.is_transfer && t.origin === "system";
  // A split loan payment (DESIGN §3.3, decision 62) is a transfer that ALSO
  // books an interest/fee remainder to a category. The select shows the
  // counterparty account; the remainder posting is shown under it, never
  // hidden behind the [account] value.
  const splitRemainder =
    t.is_transfer && t.remainder_amount != null && t.category_path
      ? { amount: t.remainder_amount, category: t.category_path }
      : null;

  const onCategoryChange = (next: string) => recategorize.mutate(next);
  // Splits (#590): a bank pending is split once it posts (the posted row
  // replaces it); transfers, trade cash and asset value entries never split.
  const [splitting, setSplitting] = useState(false);
  const canSplit =
    t.split_lines === 0 &&
    !t.is_transfer &&
    !t.securities_trade &&
    !t.value_change &&
    !t.starting_balance &&
    moneySign(t.amount) !== 0 &&
    (t.status === "posted" || (t.status === "pending" && t.origin !== "import"));

  return (
    <>
      <tr id={`transaction-${t.id}`} className={t.status === "void" ? "opacity-50" : undefined}>
        <Td className="whitespace-nowrap">{formatDate(t.date)}</Td>
        <Td className="max-w-40 truncate">{t.payee}</Td>
        <Td className="max-w-56 truncate text-muted-foreground">{t.description}</Td>
        <Td
          className={`text-right font-mono ${
            moneySign(t.amount) > 0 ? "text-success" : ""
          }`}
        >
          {formatMoney(t.amount, { currency })}
        </Td>
        <Td className="text-right font-mono text-muted-foreground">
          {formatMoney(t.balance, { currency })}
        </Td>
        <Td>
          <span className="flex items-center gap-1">
            {t.value_change ? (
              // An entry in a hand-valued asset's value (#566, #567): a change
              // in value (equity.revaluation) or money put in (equity.capital),
              // corrected by reversing it, so it has no category to pick.
              <ValueChangeCell t={t} />
            ) : t.starting_balance ? (
              // The account's starting balance from the bank (#585, decision
              // 123): booked to the system account equity.opening_balances
              // and kept in step by Purser, so it has no category to pick.
              <span
                className="text-sm text-muted-foreground"
                title="What the account held before its first transaction, taken from the balance the bank reported. It is neither spending nor income. Purser lowers it by itself when older history is imported; to correct the account's balance, reconcile it to a statement."
              >
                Starting balance
              </span>
            ) : t.balance_adjustment ? (
              // Purser's post-sync balance true-up (#591, decision 119): cash
              // put in step with the broker's balance less its holdings.
              <span
                className="text-sm text-muted-foreground"
                title="Purser posted this after a sync so the account's cash plus its holdings equal the balance the broker reported (for example a buy the broker never sent as a row). It is neither spending nor income. Turn the balance true-up off in the account's settings to stop future ones."
              >
                Balance adjustment (invested cash)
              </span>
            ) : t.securities_trade && t.securities_trade_locked ? (
              // A recorded trade's cash (#390, decision 103): booked to the
              // system account equity.securities, it moves only with its
              // trade, so it has no category to pick.
              <span
                className="text-sm text-muted-foreground"
                title="The cash of a securities trade. It moves with the trade: to correct it, reverse the trade's position event (an agent's reverse_position_event, or the REST API) instead of editing this row."
              >
                Securities trade
              </span>
            ) : t.split_lines > 0 ? (
              <SplitCell
                t={t}
                accountPath={accountPath}
                open={splitting}
                onToggle={() => setSplitting((v) => !v)}
              />
            ) : (
              <CategorySelect
                value={categoryValue}
                currentAccountPath={accountPath}
                direction={moneySign(t.amount) > 0 ? "credit" : "debit"}
                securitiesTrade={(brokerageFeed && !t.is_transfer) || t.securities_trade}
                onChange={onCategoryChange}
                disabled={recategorize.isPending || undoCorrection.isPending}
                aria-label={`Category for ${t.payee || t.description || `the ${formatDate(t.date)} transaction`}`}
              />
            )}
            {t.category_source === "ai" && <Badge variant="amber">AI</Badge>}
            {t.category_source === "deterministic" && <Badge>auto</Badge>}
            {t.category_source === "rule" && <Badge>rule</Badge>}
          </span>
          {correctedFrom && (
            <span className="mt-1 flex items-center gap-1 text-xs text-muted-foreground">
              <span title="A posted row is never edited: Purser recorded a reversal and this corrected copy.">
                Corrected
              </span>
              ·
              <button
                type="button"
                className="font-medium text-primary hover:underline disabled:opacity-50"
                disabled={undoCorrection.isPending}
                aria-label={`Undo: back to ${categoryValueLabel(correctedFrom, pathName, moneySign(t.amount) > 0 ? "credit" : "debit")}`}
                onClick={() => undoCorrection.mutate(correctedFrom)}
              >
                Undo
              </button>
            </span>
          )}
          {ruleProposed && (
            <span className="mt-1 block text-xs text-muted-foreground">
              Marked as a securities trade. A rule for rows like this one is waiting in{" "}
              <Link to="/" className="underline">
                Needs attention
              </Link>
              .
            </span>
          )}
          {splitRemainder && (
            <span
              className="mt-1 flex items-center gap-1 text-xs text-muted-foreground"
              title="Split loan payment: both bank legs stay exact; this interest/fee remainder books to the category"
            >
              <Badge>split</Badge>
              <span className="font-mono">{formatMoney(splitRemainder.amount, { currency })}</span>
              <span className="max-w-40 truncate">{pathName(splitRemainder.category)}</span>
            </span>
          )}
        </Td>
        <Td>
          {/* Wrap, as the Actions cell does: in one line a row's badges
              (posted · auto-linked · scheduled …) widened the column past the
              table's edge and clipped the last one (#556). */}
          <span className="flex flex-wrap gap-1">
            <Badge
              variant={
                t.status === "posted" ? "green" : t.status === "pending" ? "amber" : "default"
              }
            >
              {t.status}
            </Badge>
            {t.origin === "agent" && <Badge variant="amber">agent</Badge>}
            {autoLinked && <Badge>auto-linked</Badge>}
            {t.schedule_id && (
              <Link to="/schedules" title="Expected from a scheduled transaction">
                <Badge>scheduled</Badge>
              </Link>
            )}
            {t.schedule_pinned && (
              <Badge variant="amber" title="You edited this occurrence; the rule leaves it alone">
                pinned
              </Badge>
            )}
            {/* Whether a row needs review, and why, is the server's call
                (decision 141): the same predicate as ?needs_review=true and
                the Dashboard's counts. It clears when the reason is gone. */}
            {t.needs_review && <ReviewBadge reason={t.review_reason} />}
          </span>
        </Td>
        <Td>
          <span className="flex flex-wrap gap-1">
            {candidate && (
              <>
                <Button
                  size="sm"
                  variant="outline"
                  title={`Looks like a transfer (${formatMoney(candidate.amount, { currency })} pair)`}
                  onClick={() =>
                    confirmTransfer.mutate({
                      outgoing_id: candidate.outgoing_id,
                      incoming_id: candidate.incoming_id,
                    })
                  }
                >
                  Link ⇄
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  aria-label="Not a transfer"
                  title="Not a transfer — these two rows are never linked automatically"
                  disabled={notATransfer.isPending}
                  onClick={() => notATransfer.mutate(candidate)}
                >
                  ✗
                </Button>
              </>
            )}
            {match && (
              <>
                <Button
                  size="sm"
                  variant="outline"
                  title={`Matches expected: ${match.expected_payee ?? ""} ${formatMoney(
                    match.expected_amount,
                    { currency },
                  )} on ${formatDate(match.expected_date)}`}
                  onClick={() => confirmMatch.mutate()}
                >
                  Match ✓
                </Button>
                <Button size="sm" variant="ghost" onClick={() => rejectMatch.mutate()}>
                  ✗
                </Button>
              </>
            )}
            {canSplit && !splitting && (
              <Button
                size="sm"
                variant="outline"
                title="Spread this amount over several categories or accounts"
                onClick={() => setSplitting(true)}
              >
                Split…
              </Button>
            )}
            {t.has_linked_originals && (
              <Button
                size="sm"
                variant="ghost"
                title="Undo this transfer link — the bank rows come back as separate transactions"
                onClick={() => {
                  if (window.confirm("Unlink this transfer? The original bank rows come back."))
                    unlink.mutate();
                }}
              >
                Unlink
              </Button>
            )}
            {t.review_reason === "reconciliation_adjustment" && !accepting && (
              <Button
                size="sm"
                variant="outline"
                title="The difference is right as it is: accept it, and it no longer needs review. To explain it instead, change its category or split it."
                onClick={() => setAccepting(true)}
              >
                Accept…
              </Button>
            )}
            {t.origin === "agent" && t.status === "pending" && (
              <>
                <Button size="sm" onClick={() => postTx.mutate()}>
                  Post
                </Button>
                <Button size="sm" variant="destructive" onClick={() => voidTx.mutate()}>
                  Void
                </Button>
              </>
            )}
            {isInstance && !overriding && !picking && (
              <>
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => {
                    setOverride({ amount: t.amount ?? "", date: t.date });
                    setOverriding(true);
                  }}
                >
                  Override
                </Button>
                <Button size="sm" variant="outline" onClick={() => setPicking(true)}>
                  Mark paid
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  title="Skip this occurrence — it will not come back"
                  onClick={() => {
                    if (window.confirm("Skip this occurrence? It will not come back."))
                      voidTx.mutate();
                  }}
                >
                  Skip
                </Button>
              </>
            )}
            {isInstance && overriding && (
              <>
                <Input
                  className="w-28"
                  value={override.amount}
                  onChange={(e) => setOverride({ ...override, amount: e.target.value })}
                />
                <Input
                  className="w-36"
                  type="date"
                  value={override.date}
                  onChange={(e) => setOverride({ ...override, date: e.target.value })}
                />
                <Button size="sm" onClick={() => overrideTx.mutate()}>
                  Save
                </Button>
                <Button size="sm" variant="ghost" onClick={() => setOverriding(false)}>
                  Cancel
                </Button>
              </>
            )}
          </span>
        </Td>
      </tr>
      {accepting && (
        <tr>
          <Td colSpan={8}>
            <form
              aria-label="Accept the reconciliation adjustment"
              className="flex flex-wrap items-center gap-2 text-sm"
              onSubmit={(e) => {
                e.preventDefault();
                accept.mutate();
              }}
            >
              <span className="text-muted-foreground">
                Accept this adjustment of {formatMoney(t.amount, { currency })} as it is? Purser
                records who accepted it and when.
              </span>
              <Input
                className="h-8 w-full sm:w-72"
                aria-label="Why the adjustment is right (optional)"
                placeholder="Why it is right (optional)"
                maxLength={200}
                value={acceptNote}
                onChange={(e) => setAcceptNote(e.target.value)}
              />
              <Button size="sm" type="submit" disabled={accept.isPending}>
                Accept adjustment
              </Button>
              <Button
                size="sm"
                type="button"
                variant="ghost"
                onClick={() => {
                  setAccepting(false);
                  setError(null);
                }}
              >
                Cancel
              </Button>
            </form>
          </Td>
        </tr>
      )}
      {splitting && (
        <tr>
          <Td colSpan={8}>
            <SplitEditor
              txId={t.id}
              isSplit={t.split_lines > 0}
              amount={t.amount}
              accountPath={accountPath}
              category={t.category_path}
              currency={currency}
              onClose={() => setSplitting(false)}
              onDone={refresh}
            />
          </Td>
        </tr>
      )}
      {isInstance && picking && (
        <tr>
          <Td colSpan={8}>
            <PaidPicker
              accountPath={accountPath}
              currency={currency}
              around={t.date}
              onPick={(id) => markPaid.mutate(id)}
              onCancel={() => setPicking(false)}
            />
          </Td>
        </tr>
      )}
      {error && (
        <tr>
          <Td colSpan={8}>
            <ErrorText>{error}</ErrorText>
          </Td>
        </tr>
      )}
    </>
  );
}

const REVIEW_REASONS: Record<NonNullable<TransactionOut["review_reason"]>, { label: string; help: string }> = {
  uncategorized: {
    label: "no category",
    help: "A bank-sync row with no category. Give it one and it no longer needs review.",
  },
  reconciliation_adjustment: {
    label: "reconciliation adjustment",
    help: "The difference a reconcile posted. Change its category or split it to explain it, or accept it as it is.",
  },
};

/** "needs review" with its reason (decision 141). */
function ReviewBadge({ reason }: { reason?: TransactionOut["review_reason"] }) {
  const why = reason ? REVIEW_REASONS[reason] : null;
  return (
    // The pill says "needs review"; the reason is small text under it that
    // wraps (#700). As one pill, "needs review · reconciliation adjustment"
    // widened the Status column past the table's edge (#556), and wrapped
    // inside the pill it read as a blob.
    <span className="flex flex-col items-start gap-0.5" title={why?.help}>
      <Badge variant="amber">needs review</Badge>
      {why && <span className="text-xs text-warning">{why.label}</span>}
    </span>
  );
}

/** Posted rows on the account within a week of the instance's date — the
 * candidates "Mark paid" can settle the expectation against (#236). */
function PaidPicker({
  accountPath,
  currency,
  around,
  onPick,
  onCancel,
}: {
  accountPath: string;
  currency?: string | null;
  around: string;
  onPick: (postedId: string) => void;
  onCancel: () => void;
}) {
  const candidates = useQuery({
    queryKey: ["paid-candidates", accountPath, around],
    queryFn: async () => {
      const { data, error } = await api.GET("/transactions", {
        params: {
          query: {
            account: accountPath,
            status: ["posted", "pending"],
            start: addDays(around, -10),
            end: addDays(around, 10),
            limit: 25,
          },
        },
      });
      if (error) throw new Error(errorDetail(error));
      // A trade's cash moves only with its trade (#390, decision 103): it
      // never settles a bill, so it is never offered here.
      return (data?.items ?? []).filter(
        (c) => !c.schedule_id && !c.securities_trade && !c.value_change && !c.starting_balance,
      );
    },
  });
  return (
    <div className="rounded-md border border-border p-2 text-sm">
      <p className="mb-1 text-muted-foreground">Which bank row paid this? (±10 days)</p>
      <QueryState
        query={candidates}
        what="nearby transactions"
        empty="No posted rows near that date."
      >
        {(rows) => (
          <div className="flex flex-col gap-1">
            {rows.map((c) => (
              <button
                key={c.id}
                className="flex items-center gap-3 rounded px-2 py-1 text-left hover:bg-muted"
                onClick={() => onPick(c.id)}
              >
                <span className="w-28 whitespace-nowrap">{formatDate(c.date)}</span>
                <span className="grow truncate">{c.payee ?? c.description ?? "—"}</span>
                <span className="font-mono">{formatMoney(c.amount, { currency })}</span>
              </button>
            ))}
          </div>
        )}
      </QueryState>
      <Button size="sm" variant="ghost" className="mt-2" onClick={onCancel}>
        Cancel
      </Button>
    </div>
  );
}

// The account types a floor applies to (mirrors the server's FLOOR_TYPES,
// #466): cash-flow accounts whose forecast projects their postings.
const FLOOR_TYPES = new Set(["checking", "savings", "cash", "credit_card"]);
// A decimal string with at most 4 places (NUMERIC(20,4)); the server checks too.
const FLOOR_PATTERN = /^-?\d{1,16}(\.\d{1,4})?$/;

/** The account's floor (#466): the minimum balance the nightly forecast scan
 * checks, emitting `forecast.shortfall` when the next 30 days project below
 * it. Inline editor; Clear sends an explicit null, which ends the warnings. */
function FloorEditor({
  account,
  onSaved,
  onError,
}: {
  account: AccountOut;
  onSaved: () => void;
  onError: (message: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState("");
  const current = account.min_balance;

  const save = useMutation({
    mutationFn: async (min_balance: string | null) => {
      const { error: err } = await api.PATCH("/accounts/{account_id}", {
        params: { path: { account_id: account.id } },
        body: { min_balance },
      });
      if (err) throw new Error(errorDetail(err));
    },
    onSuccess: () => {
      setEditing(false);
      onSaved();
    },
    onError: (e) => onError(e.message),
  });

  if (!editing)
    return (
      <span className="flex items-center gap-2 text-sm text-muted-foreground">
        {current != null ? (
          <>
            Floor{" "}
            <span className="font-mono">{formatMoney(current, { currency: account.currency })}</span>
          </>
        ) : (
          "No floor"
        )}
        <Button
          size="sm"
          variant="outline"
          onClick={() => {
            setValue(current != null ? moneyEditText(current) : "");
            setEditing(true);
          }}
        >
          {current != null ? "Edit floor" : "Set floor…"}
        </Button>
      </span>
    );

  const trimmed = value.trim();
  const valid = FLOOR_PATTERN.test(trimmed);
  return (
    <span className="flex flex-wrap items-center gap-2 text-sm">
      <label className="flex items-center gap-2 text-muted-foreground">
        Minimum balance
        <Input
          aria-label="Minimum balance"
          className="w-32 font-mono"
          inputMode="decimal"
          autoFocus
          placeholder="500.00"
          value={value}
          onChange={(e) => setValue(e.target.value)}
        />
      </label>
      <Button size="sm" disabled={!valid || save.isPending} onClick={() => save.mutate(trimmed)}>
        Save
      </Button>
      {current != null && (
        <Button
          size="sm"
          variant="outline"
          disabled={save.isPending}
          onClick={() => save.mutate(null)}
        >
          Clear
        </Button>
      )}
      <Button size="sm" variant="ghost" onClick={() => setEditing(false)}>
        Cancel
      </Button>
      <span className="w-full text-xs text-muted-foreground">
        Purser warns (forecast.shortfall) when the next 30 days project below this.
        {account.type === "credit_card" &&
          " A card is negative when owed: -4500.00 warns past 4,500 owed."}
        {trimmed !== "" && !valid && " Enter an amount such as 500.00."}
      </span>
    </span>
  );
}

/** A credit card's statement closing day (#674, decision 136): payments to
 * the card whose schedule sizes them from the statement cycle are resized
 * from it. Shows the day the card's history suggests; never applies it. */
function CloseDayEditor({
  account,
  onSaved,
  onError,
}: {
  account: AccountOut;
  onSaved: () => void;
  onError: (message: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState("");
  const current = account.statement_close_day ?? null;
  const cycle = useQuery({
    queryKey: ["statement-cycle", account.id],
    enabled: editing,
    queryFn: async () => {
      const { data, error: err } = await api.GET("/accounts/{account_id}/statement-cycle", {
        params: { path: { account_id: account.id } },
      });
      if (err) throw new Error(errorDetail(err));
      return data;
    },
  });

  const save = useMutation({
    mutationFn: async (statement_close_day: number | null) => {
      const { error: err } = await api.PATCH("/accounts/{account_id}", {
        params: { path: { account_id: account.id } },
        body: { statement_close_day },
      });
      if (err) throw new Error(errorDetail(err));
    },
    onSuccess: () => {
      setEditing(false);
      onSaved();
    },
    onError: (e) => onError(e.message),
  });

  if (!editing)
    return (
      <span className="flex items-center gap-2 text-sm text-muted-foreground">
        {current != null ? `Statement closes on day ${current}` : "No closing day"}
        <Button
          size="sm"
          variant="outline"
          onClick={() => {
            setValue(current != null ? String(current) : "");
            setEditing(true);
          }}
        >
          {current != null ? "Edit closing day" : "Set closing day…"}
        </Button>
      </span>
    );

  const day = Number(value.trim());
  const valid = /^\d{1,2}$/.test(value.trim()) && day >= 1 && day <= 31;
  const suggested = cycle.data?.suggested_close_day ?? null;
  return (
    <span className="flex flex-wrap items-center gap-2 text-sm">
      <label className="flex items-center gap-2 text-muted-foreground">
        Statement closing day
        <Input
          aria-label="Statement closing day"
          className="w-16 font-mono"
          inputMode="numeric"
          autoFocus
          placeholder="15"
          value={value}
          onChange={(e) => setValue(e.target.value)}
        />
      </label>
      <Button size="sm" disabled={!valid || save.isPending} onClick={() => save.mutate(day)}>
        Save
      </Button>
      {suggested != null && suggested !== current && (
        <Button
          size="sm"
          variant="outline"
          disabled={save.isPending}
          onClick={() => setValue(String(suggested))}
        >
          Use suggested day {suggested}
        </Button>
      )}
      {current != null && (
        <Button
          size="sm"
          variant="outline"
          disabled={save.isPending}
          onClick={() => save.mutate(null)}
        >
          Clear
        </Button>
      )}
      <Button size="sm" variant="ghost" onClick={() => setEditing(false)}>
        Cancel
      </Button>
      <span className="w-full text-xs text-muted-foreground">
        The day of the month the statement closes (a later day than the month has means its
        last day). Card payments set to the statement cycle pay what the statement showed.
        {cycle.data && ` Suggestion: ${cycle.data.suggestion_detail}.`}
        {value.trim() !== "" && !valid && " Enter a day from 1 to 31."}
      </span>
    </span>
  );
}

/** Header actions for the account lifecycle (issue #124): inline rename,
 * hide/unhide, an expanding close panel, and reopen — no modal (the repo has
 * no dialog primitive), so the close flow is a Card-styled inline panel. */
function AccountHeader({ account }: { account: AccountOut | undefined }) {
  const qc = useQueryClient();
  const isAdmin = useIsAdmin();
  useDocumentTitle(account?.name ?? "Account");
  const [renaming, setRenaming] = useState(false);
  const [name, setName] = useState("");
  const [closing, setClosing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const invalidate = () => {
    if (account) qc.invalidateQueries({ queryKey: ["account", account.id] });
    qc.invalidateQueries({ queryKey: ["accounts"] });
  };

  const rename = useMutation({
    mutationFn: async (next: string) => {
      const { error: err } = await api.PATCH("/accounts/{account_id}", {
        params: { path: { account_id: account!.id } },
        body: { name: next },
      });
      if (err) throw new Error(errorDetail(err));
    },
    onSuccess: () => {
      setRenaming(false);
      invalidate();
    },
    onError: (e) => setError(e.message),
  });

  const toggleHide = useMutation({
    mutationFn: async (hidden: boolean) => {
      const { error: err } = await api.PATCH("/accounts/{account_id}", {
        params: { path: { account_id: account!.id } },
        body: { hidden },
      });
      if (err) throw new Error(errorDetail(err));
    },
    onSuccess: invalidate,
    onError: (e) => setError(e.message),
  });

  // Where a brokerage account's holdings come from (#390, decision 88):
  // feed configuration, so admin only — the server enforces it too.
  const setHoldingsSource = useMutation({
    mutationFn: async (holdings_source: "feed" | "manual") => {
      const { error: err } = await api.PATCH("/accounts/{account_id}", {
        params: { path: { account_id: account!.id } },
        body: { holdings_source },
      });
      if (err) throw new Error(errorDetail(err));
    },
    onSuccess: () => {
      invalidate();
      if (account) qc.invalidateQueries({ queryKey: ["positions", account.id] });
    },
    onError: (e) => setError(e.message),
  });

  // The feed balance of a hand-tracked brokerage account is its cash (#390,
  // decision 103): admin opt-in that puts the account in the drift check.
  const setFeedBalanceIsCash = useMutation({
    mutationFn: async (feed_balance_is_cash: boolean) => {
      const { error: err } = await api.PATCH("/accounts/{account_id}", {
        params: { path: { account_id: account!.id } },
        body: { feed_balance_is_cash },
      });
      if (err) throw new Error(errorDetail(err));
    },
    onSuccess: invalidate,
    onError: (e) => setError(e.message),
  });

  const setBalanceTrueUp = useMutation({
    mutationFn: async (balance_true_up: boolean) => {
      const { error: err } = await api.PATCH("/accounts/{account_id}", {
        params: { path: { account_id: account!.id } },
        body: { balance_true_up },
      });
      if (err) throw new Error(errorDetail(err));
    },
    onSuccess: invalidate,
    onError: (e) => setError(e.message),
  });

  const reopen = useMutation({
    mutationFn: async () => {
      const { error: err } = await api.POST("/accounts/{account_id}/reopen", {
        params: { path: { account_id: account!.id } },
      });
      if (err) throw new Error(errorDetail(err));
    },
    onSuccess: invalidate,
    onError: (e) => setError(e.message),
  });

  if (!account) return <h1 className="text-xl font-bold">…</h1>;
  const closed = !!account.closed_at;
  const hidden = !!account.hidden_at;

  return (
    <div className="space-y-2">
      <div className="flex items-baseline justify-between gap-4">
        <div className="min-w-0">
          <Link to="/accounts" className="text-sm text-muted-foreground hover:underline">
            ← Accounts
          </Link>
          {renaming ? (
            <div className="flex items-center gap-2">
              <Input
                className="max-w-xs"
                autoFocus
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
              <Button
                size="sm"
                disabled={!name.trim() || rename.isPending}
                onClick={() => rename.mutate(name.trim())}
              >
                Save
              </Button>
              <Button size="sm" variant="ghost" onClick={() => setRenaming(false)}>
                Cancel
              </Button>
            </div>
          ) : (
            <h1 className="flex items-center gap-2 text-xl font-bold">
              <span className="text-muted-foreground">
                <AccountIcon type={account.type} kind={account.asset?.kind} className="h-5 w-5" />
              </span>
              {account.name}
              <span className="font-mono text-sm text-muted-foreground">{account.path}</span>
              {hidden && <Badge>hidden</Badge>}
              {closed && <Badge variant="amber">closed</Badge>}
              {!closed && (
                <button
                  className="text-sm text-muted-foreground hover:text-foreground"
                  title="Rename account"
                  aria-label="Rename account"
                  onClick={() => {
                    setName(account.name);
                    setRenaming(true);
                  }}
                >
                  <span aria-hidden="true">✎</span>
                </button>
              )}
            </h1>
          )}
        </div>
        <div className="shrink-0 text-right">
          <p className="text-sm text-muted-foreground">Balance</p>
          <p className="font-mono text-2xl font-bold">
            {formatMoney(account.balance, { currency: account.currency })}
          </p>
          {account.balance_method === "valuation" && (
            <p className="text-xs text-muted-foreground">
              valuation{account.balance_as_of ? ` as of ${formatDate(account.balance_as_of)}` : ""}
            </p>
          )}
          {account.balance_method === "postings+holdings" && (
            <p className="text-xs text-muted-foreground">
              cash {formatMoney(account.postings_balance, { currency: account.currency })} + holdings
              {account.balance_as_of ? ` as of ${formatDate(account.balance_as_of)}` : ""}
            </p>
          )}
        </div>
      </div>

      <div className="flex flex-wrap gap-2">
        {closed ? (
          <Button
            size="sm"
            variant="outline"
            onClick={() => {
              if (window.confirm("Reopen this account? It will accept writes again."))
                reopen.mutate();
            }}
          >
            Reopen
          </Button>
        ) : (
          <>
            <Button size="sm" variant="outline" onClick={() => toggleHide.mutate(!hidden)}>
              {hidden ? "Unhide" : "Hide"}
            </Button>
            <Button size="sm" variant="outline" onClick={() => setClosing((v) => !v)}>
              Close…
            </Button>
            {FLOOR_TYPES.has(account.type) && (
              <FloorEditor account={account} onSaved={invalidate} onError={setError} />
            )}
            {account.type === "credit_card" && (
              <CloseDayEditor account={account} onSaved={invalidate} onError={setError} />
            )}
            {account.type === "brokerage" && isAdmin && (
              <label className="flex items-center gap-2 text-sm text-muted-foreground">
                Holdings
                <select
                  aria-label="Holdings source"
                  className={selectClass}
                  value={account.holdings_source}
                  disabled={setHoldingsSource.isPending}
                  onChange={(e) => {
                    const next = e.target.value as "feed" | "manual";
                    const message =
                      next === "manual"
                        ? "Enter this account's holdings in Purser? Its SimpleFIN snapshots stay as history, and SimpleFIN no longer records holdings for it; its cash still syncs. Market data prices the holdings you enter."
                        : "Take this account's holdings from SimpleFIN again? The holdings entered in Purser stay in effect until the first sync after this switch, which replaces them from its date: a position SimpleFIN does not report ends there. The positions stay, read-only.";
                    if (window.confirm(message)) setHoldingsSource.mutate(next);
                  }}
                >
                  <option value="feed">from SimpleFIN</option>
                  <option value="manual">entered in Purser</option>
                </select>
              </label>
            )}
            {account.type === "brokerage" && account.holdings_source === "feed" && (
              <label
                className="flex items-center gap-2 text-sm text-muted-foreground"
                title="After each sync, Purser compares the balance the broker reported with this account's cash plus the holdings from the same sync. When they differ by a cent or more (a buy or sell the broker never sent as a row, say), it posts one 'Balance adjustment (invested cash)' entry for the difference, which is neither spending nor income. Turning this off stops future adjustments; past ones stay."
              >
                <input
                  type="checkbox"
                  checked={account.balance_true_up}
                  disabled={setBalanceTrueUp.isPending}
                  onChange={(e) => setBalanceTrueUp.mutate(e.target.checked)}
                />
                Keep cash in step with the broker's balance
              </label>
            )}
            {account.type === "brokerage" && isAdmin && account.holdings_source === "manual" && (
              <label
                className="flex items-center gap-2 text-sm text-muted-foreground"
                title="The feed's balance for this account is its cash (a cash sweep), not the account's total value. When on, each sync compares that balance with the account's transactions and reports drift, like a checking account's."
              >
                <input
                  type="checkbox"
                  checked={account.feed_balance_is_cash}
                  disabled={setFeedBalanceIsCash.isPending}
                  onChange={(e) => setFeedBalanceIsCash.mutate(e.target.checked)}
                />
                Feed balance is cash
              </label>
            )}
          </>
        )}
      </div>

      {closing && !closed && (
        <CloseAccountPanel
          account={account}
          onDone={() => {
            setClosing(false);
            invalidate();
          }}
          onError={setError}
        />
      )}
      <ErrorText>{error}</ErrorText>
    </div>
  );
}

/** The inline close panel: shows the live balance and, when nonzero, a leaf
 * category picker (defaulting to expenses.adjustments, which the server
 * auto-creates) for the final balancing transaction. */
function CloseAccountPanel({
  account,
  onDone,
  onError,
}: {
  account: AccountOut;
  onDone: () => void;
  onError: (message: string) => void;
}) {
  const [category, setCategory] = useState("expenses.adjustments");
  // The balancing adjustment zeroes POSTINGS — a loan headline balance is a
  // valuation, so use the postings figure when present.
  const postingsBalance = account.postings_balance ?? account.balance ?? "0";
  // A hand-valued asset closes with its leftover as a change in value, never
  // a category (#569); "Sell or dispose…" sets the date.
  const isAsset = account.type === "property";
  const needsAdjustment = moneySign(postingsBalance) !== 0 && !isAsset;

  const categories = useQuery({ ...categoriesQuery, enabled: needsAdjustment });
  // Only leaf categories can carry postings (a path that is nobody's prefix).
  const leafPaths = useMemo(() => {
    const paths = (categories.data ?? []).map((c) => c.path);
    return paths.filter((p) => !paths.some((o) => o !== p && o.startsWith(`${p}.`)));
  }, [categories.data]);
  const categoryName = useMemo(() => namesFrom([categories.data]), [categories.data]);

  const close = useMutation({
    mutationFn: async () => {
      const { error: err } = await api.POST("/accounts/{account_id}/close", {
        params: { path: { account_id: account.id } },
        body: needsAdjustment ? { adjustment_category: category } : {},
      });
      if (err) throw new Error(errorDetail(err));
    },
    onSuccess: onDone,
    onError: (e) => onError(e.message),
  });

  return (
    <Card className="space-y-3">
      <p className="text-sm font-medium">Close {account.name}</p>
      <p className="text-sm text-muted-foreground">
        Live balance: <span className="font-mono">{formatMoney(postingsBalance, { currency: account.currency })}</span>
      </p>
      {isAsset && moneySign(postingsBalance) !== 0 && (
        <p className="text-sm text-muted-foreground">
          Closing today posts this as a change in value, so the asset closes at zero. To close on
          a sale date, use Sell or dispose… instead.
        </p>
      )}
      {needsAdjustment && (
        <div className="space-y-2">
          <label className="text-sm font-medium">Balancing category</label>
          <select
            className={selectClass}
            value={category}
            onChange={(e) => setCategory(e.target.value)}
          >
            {!leafPaths.includes("expenses.adjustments") && (
              <option value="expenses.adjustments">Adjustments (will be created)</option>
            )}
            {leafPaths.map((p) => (
              <option key={p} value={p}>
                {categoryName(p)}
              </option>
            ))}
          </select>
          <QueryState query={categories} what="categories" />
          <p className="text-sm text-muted-foreground">
            Purser will post a final balancing transaction of{" "}
            <span className="font-mono">
              {formatMoney(negateMoney(postingsBalance), { currency: account.currency })}
            </span>{" "}
            to this category.
          </p>
        </div>
      )}
      <Button size="sm" disabled={close.isPending} onClick={() => close.mutate()}>
        Confirm close
      </Button>
    </Card>
  );
}
