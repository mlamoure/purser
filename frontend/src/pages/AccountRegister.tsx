import { useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, errorDetail } from "../lib/api";
import { useDebouncedValue } from "../lib/useDebouncedValue";
import { useIsAdmin } from "../lib/useIsAdmin";
import { cn, fmtAmount, overrideBody } from "../lib/utils";
import { Badge, Button, Card, ErrorText, Input, Table, Td, Th } from "../components/ui";
import CategorySelect from "../components/CategorySelect";
import PositionsPanel from "./Positions";
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
 * seen — SimpleFIN imports, manual entries, agent proposals — with the
 * review actions that used to live on /review inline on each row. */
export default function AccountRegister() {
  const { accountId } = useParams<{ accountId: string }>();
  const [q, setQ] = useState("");
  const searchQ = useDebouncedValue(q, SEARCH_DEBOUNCE_MS);
  const [statusFilter, setStatusFilter] = useState<"live" | "posted" | "pending" | "expected">(
    "live",
  );
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [offset, setOffset] = useState(0);
  // A brokerage account tracked by hand (#390) adds a Positions tab.
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

  const statuses =
    statusFilter === "live" ? ["pending", "posted"] : [statusFilter];
  const page = useQuery({
    queryKey: ["register", accountId, searchQ, statusFilter, start, end, offset],
    queryFn: async () => {
      const { data, error } = await api.GET("/transactions", {
        params: {
          query: {
            account: accountId!,
            status: statuses,
            with_category_provenance: true,
            limit: PAGE_SIZE,
            offset,
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

  if (!accountId) return null;
  const total = page.data?.total ?? 0;
  const manualHoldings =
    account.data?.type === "brokerage" && account.data?.holdings_source === "manual";

  return (
    <div className="space-y-6">
      <AccountHeader account={account.data} />

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
        <PositionsPanel accountId={accountId} />
      ) : (
        <>
      <div className="flex flex-wrap items-end gap-3">
        <div className="grow">
          <Input
            placeholder="Search description…"
            value={q}
            onChange={(e) => {
              setQ(e.target.value);
              setOffset(0);
            }}
          />
        </div>
        <select
          className={selectClass}
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
          value={start}
          onChange={(e) => {
            setStart(e.target.value);
            setOffset(0);
          }}
        />
        <Input
          type="date"
          className="w-40"
          value={end}
          onChange={(e) => {
            setEnd(e.target.value);
            setOffset(0);
          }}
        />
      </div>

      <Card>
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
            {(page.data?.items ?? []).map((t) => (
              <RegisterRow
                key={t.id}
                t={t}
                accountPath={account.data?.path ?? ""}
                candidates={candidates.data ?? []}
                proposed={proposed.data ?? []}
              />
            ))}
          </tbody>
        </Table>
        {page.data && page.data.items.length === 0 && (
          <p className="text-sm text-muted-foreground">No transactions match.</p>
        )}
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

/** A split transaction's category legs (issue #274, decision 87): read-only
 * in v1, so the row shows "Split (n)" instead of a category picker; the legs
 * load from the detail's postings when opened, in posting sign (a category
 * leg of +80.00 is 80.00 of spending). */
export function SplitLines({
  txId,
  count,
  realPath,
}: {
  txId: string;
  count: number;
  realPath: string | null;
}) {
  const [open, setOpen] = useState(false);
  const detail = useQuery({
    queryKey: ["transaction", txId],
    queryFn: async () => {
      const { data, error } = await api.GET("/transactions/{tx_id}", {
        params: { path: { tx_id: txId } },
      });
      if (error) throw new Error(errorDetail(error));
      return data;
    },
    enabled: open,
  });
  // Every leg but the one real leg is a category line.
  const legs = (detail.data?.postings ?? []).filter((p) => p.account_path !== realPath);
  return (
    <span className="flex flex-col gap-1">
      <Button
        size="sm"
        variant="outline"
        title="A split row: one bank amount across several categories. Split rows are read-only."
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        Split ({count})
      </Button>
      {open && (
        <ul className="space-y-0.5 text-xs text-muted-foreground">
          {legs.map((p) => (
            <li key={p.id} className="flex gap-2">
              <span className="font-mono">{fmtAmount(p.amount)}</span>
              <span className="max-w-40 truncate">{p.account_path}</span>
              {p.memo ? <span className="max-w-32 truncate">· {p.memo}</span> : null}
            </li>
          ))}
        </ul>
      )}
    </span>
  );
}

export function RegisterRow({
  t,
  accountPath,
  candidates,
  proposed,
}: {
  t: TransactionOut;
  accountPath: string;
  candidates: TransferCandidateOut[];
  proposed: MatchOut[];
}) {
  const qc = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const refresh = () => qc.invalidateQueries();

  const candidate = candidates.find((c) => c.outgoing_id === t.id || c.incoming_id === t.id);
  const match = proposed.find((m) => m.posted_id === t.id);

  const recategorize = useMutation({
    mutationFn: async (category: string) => {
      const { error: err } = await api.PATCH("/transactions/{tx_id}", {
        params: { path: { tx_id: t.id } },
        body: { category, account: accountPath },
      });
      if (err) throw new Error(errorDetail(err));
    },
    onSuccess: refresh,
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

  const onCategoryChange = (next: string) => {
    if (
      t.status === "posted" &&
      !window.confirm(
        "This row is posted (immutable). Purser will record a reversal and a corrected replacement — the register shows the corrected row. Continue?",
      )
    )
      return;
    recategorize.mutate(next);
  };

  return (
    <>
      <tr className={t.status === "void" ? "opacity-50" : undefined}>
        <Td className="whitespace-nowrap">{t.date}</Td>
        <Td className="max-w-40 truncate">{t.payee}</Td>
        <Td className="max-w-56 truncate text-muted-foreground">{t.description}</Td>
        <Td
          className={`text-right font-mono ${
            t.amount != null && Number(t.amount) > 0 ? "text-emerald-700" : ""
          }`}
        >
          {fmtAmount(t.amount)}
        </Td>
        <Td className="text-right font-mono text-muted-foreground">
          {t.balance != null ? fmtAmount(t.balance) : "\u2014"}
        </Td>
        <Td>
          <span className="flex items-center gap-1">
            {t.split_lines > 0 ? (
              <SplitLines txId={t.id} count={t.split_lines} realPath={t.account_path} />
            ) : (
              <CategorySelect
                value={categoryValue}
                currentAccountPath={accountPath}
                onChange={onCategoryChange}
                disabled={recategorize.isPending}
              />
            )}
            {t.category_source === "ai" && <Badge variant="amber">AI</Badge>}
            {t.category_source === "deterministic" && <Badge>auto</Badge>}
            {t.category_source === "rule" && <Badge>rule</Badge>}
          </span>
          {splitRemainder && (
            <span
              className="mt-1 flex items-center gap-1 text-xs text-muted-foreground"
              title="Split loan payment: both bank legs stay exact; this interest/fee remainder books to the category"
            >
              <Badge>split</Badge>
              <span className="font-mono">{fmtAmount(splitRemainder.amount)}</span>
              <span className="max-w-40 truncate">{splitRemainder.category}</span>
            </span>
          )}
        </Td>
        <Td>
          <span className="flex gap-1">
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
              <Link to="/schedules" title="Materialized by a schedule">
                <Badge>scheduled</Badge>
              </Link>
            )}
            {t.schedule_pinned && (
              <Badge variant="amber" title="You edited this occurrence; the rule leaves it alone">
                pinned
              </Badge>
            )}
            {/* The unreviewed set is a filter over this register, not a queue
                (#175): a row is unreviewed until something token-bearing
                touches it. Membership is the server's call (#248) — no mark
                is not "needs review" (expected instances, manual and QIF/CSV
                rows never need one). `flagged` is a reviewer handing it back. */}
            {t.escalated_at && <Badge variant="red">flagged</Badge>}
            {t.needs_review && <Badge variant="amber">needs review</Badge>}
          </span>
        </Td>
        <Td>
          <span className="flex flex-wrap gap-1">
            {candidate && (
              <Button
                size="sm"
                variant="outline"
                title={`Looks like a transfer (${fmtAmount(candidate.amount)} pair)`}
                onClick={() =>
                  confirmTransfer.mutate({
                    outgoing_id: candidate.outgoing_id,
                    incoming_id: candidate.incoming_id,
                  })
                }
              >
                Link ⇄
              </Button>
            )}
            {match && (
              <>
                <Button
                  size="sm"
                  variant="outline"
                  title={`Matches expected: ${match.expected_payee ?? ""} ${fmtAmount(
                    match.expected_amount,
                  )} on ${match.expected_date ?? ""}`}
                  onClick={() => confirmMatch.mutate()}
                >
                  Match ✓
                </Button>
                <Button size="sm" variant="ghost" onClick={() => rejectMatch.mutate()}>
                  ✗
                </Button>
              </>
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
                    if (window.confirm("Skip this occurrence? It will not come back.")) voidTx.mutate();
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
      {isInstance && picking && (
        <tr>
          <Td colSpan={8}>
            <PaidPicker
              accountPath={accountPath}
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

/** Posted rows on the account within a week of the instance's date — the
 * candidates "Mark paid" can settle the expectation against (#236). */
function PaidPicker({
  accountPath,
  around,
  onPick,
  onCancel,
}: {
  accountPath: string;
  around: string;
  onPick: (postedId: string) => void;
  onCancel: () => void;
}) {
  const shift = (days: number) => {
    const d = new Date(around);
    d.setDate(d.getDate() + days);
    return d.toISOString().slice(0, 10);
  };
  const candidates = useQuery({
    queryKey: ["paid-candidates", accountPath, around],
    queryFn: async () =>
      (
        await api.GET("/transactions", {
          params: {
            query: {
              account: accountPath,
              status: ["posted", "pending"],
              start: shift(-10),
              end: shift(10),
              limit: 25,
            },
          },
        })
      ).data?.items ?? [],
  });
  const rows = (candidates.data ?? []).filter((c) => !c.schedule_id);
  return (
    <div className="rounded-md border border-border p-2 text-sm">
      <p className="mb-1 text-muted-foreground">Which bank row paid this? (±10 days)</p>
      {candidates.isLoading && <p className="text-muted-foreground">Loading…</p>}
      {rows.length === 0 && !candidates.isLoading && (
        <p className="text-muted-foreground">No posted rows near that date.</p>
      )}
      <div className="flex flex-col gap-1">
        {rows.map((c) => (
          <button
            key={c.id}
            className="flex items-center gap-3 rounded px-2 py-1 text-left hover:bg-muted"
            onClick={() => onPick(c.id)}
          >
            <span className="w-24">{c.date}</span>
            <span className="grow truncate">{c.payee ?? c.description ?? "—"}</span>
            <span className="font-mono">{fmtAmount(c.amount)}</span>
          </button>
        ))}
      </div>
      <Button size="sm" variant="ghost" className="mt-2" onClick={onCancel}>
        Cancel
      </Button>
    </div>
  );
}

/** Header actions for the account lifecycle (issue #124): inline rename,
 * hide/unhide, an expanding close panel, and reopen — no modal (the repo has
 * no dialog primitive), so the close flow is a Card-styled inline panel. */
function AccountHeader({ account }: { account: AccountOut | undefined }) {
  const qc = useQueryClient();
  const isAdmin = useIsAdmin();
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
          <Link to="/" className="text-sm text-muted-foreground hover:underline">
            ← Dashboard
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
              {account.name}
              <span className="font-mono text-sm text-muted-foreground">{account.path}</span>
              {hidden && <Badge>hidden</Badge>}
              {closed && <Badge variant="amber">closed</Badge>}
              {!closed && (
                <button
                  className="text-sm text-muted-foreground hover:text-foreground"
                  title="Rename account"
                  onClick={() => {
                    setName(account.name);
                    setRenaming(true);
                  }}
                >
                  ✎
                </button>
              )}
            </h1>
          )}
        </div>
        <div className="shrink-0 text-right">
          <p className="text-sm text-muted-foreground">Balance</p>
          <p className="font-mono text-2xl font-bold">{fmtAmount(account.balance)}</p>
          {account.balance_method === "valuation" && (
            <p className="text-xs text-muted-foreground">
              valuation{account.balance_as_of ? ` as of ${account.balance_as_of}` : ""}
            </p>
          )}
          {account.balance_method === "postings+holdings" && (
            <p className="text-xs text-muted-foreground">
              cash {fmtAmount(account.postings_balance)} + holdings
              {account.balance_as_of ? ` as of ${account.balance_as_of}` : ""}
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
                        ? "Track this account's holdings by hand? Its feed snapshots stay as history, and the feed no longer records holdings for it; its cash still syncs."
                        : "Take this account's holdings from the feed again? The hand-tracked holdings stay in effect until the first sync after this switch, which replaces them from its date: a position the feed does not report ends there. The positions stay, read-only.";
                    if (window.confirm(message)) setHoldingsSource.mutate(next);
                  }}
                >
                  <option value="feed">from the feed</option>
                  <option value="manual">tracked by hand</option>
                </select>
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
  // The balancing adjustment zeroes POSTINGS — a loan/property headline
  // balance is a valuation, so use the postings figure when present.
  const postingsBalance = account.postings_balance ?? account.balance;
  const balance = Number(postingsBalance ?? 0);
  const needsAdjustment = balance !== 0;

  const categories = useQuery({
    queryKey: ["categories"],
    queryFn: async () => (await api.GET("/categories")).data ?? [],
    enabled: needsAdjustment,
  });
  // Only leaf categories can carry postings (a path that is nobody's prefix).
  const leafPaths = useMemo(() => {
    const paths = (categories.data ?? []).map((c) => c.path);
    return paths.filter((p) => !paths.some((o) => o !== p && o.startsWith(`${p}.`)));
  }, [categories.data]);

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
        Live balance: <span className="font-mono">{fmtAmount(postingsBalance)}</span>
      </p>
      {needsAdjustment && (
        <div className="space-y-2">
          <label className="text-sm font-medium">Balancing category</label>
          <select
            className={selectClass}
            value={category}
            onChange={(e) => setCategory(e.target.value)}
          >
            {!leafPaths.includes("expenses.adjustments") && (
              <option value="expenses.adjustments">expenses.adjustments (will be created)</option>
            )}
            {leafPaths.map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </select>
          <p className="text-sm text-muted-foreground">
            Purser will post a final balancing transaction of{" "}
            <span className="font-mono">{fmtAmount(String(-balance))}</span> to this category.
          </p>
        </div>
      )}
      <Button size="sm" disabled={close.isPending} onClick={() => close.mutate()}>
        Confirm close
      </Button>
    </Card>
  );
}
