import { useState } from "react";
import { Link } from "react-router-dom";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { api, errorDetail } from "../lib/api";
import { fmtAmount } from "../lib/utils";
import { SuggestionCard } from "./SuggestionCard";
import { Badge, Button, Card, CardTitle, ErrorText } from "./ui";

type Kind = "match" | "schedule" | "transfer_link" | "transfer_pair";
const groups: { kind: Kind; title: string; help: string }[] = [
  {
    kind: "match",
    title: "Payment matches",
    help: "Compare an expected payment with a bank transaction.",
  },
  {
    kind: "transfer_pair",
    title: "Possible transfers",
    help: "Check whether two bank transactions are one movement between your accounts.",
  },
  {
    kind: "schedule",
    title: "Recurring schedules",
    help: "Review a pattern before adding it to your forecast.",
  },
  {
    kind: "transfer_link",
    title: "Transfer rules",
    help: "Review a reusable rule; creating a draft does not activate it.",
  },
];
const selectClass = "h-9 min-w-0 max-w-full rounded-md border border-border bg-card px-2 text-sm";

function Cards({
  kind,
  total,
  account,
  sort,
}: {
  kind: Kind;
  total: number;
  account: string;
  sort: "oldest" | "confidence";
}) {
  const pages = useInfiniteQuery({
    queryKey: ["suggestions", kind, account, sort],
    initialPageParam: 0,
    queryFn: async ({ pageParam }) => {
      const { data, error } = await api.GET("/suggestions", {
        params: {
          query: {
            kind,
            limit: 10,
            offset: pageParam,
            sort,
            ...(account ? { account } : {}),
          },
        },
      });
      if (error) throw new Error(errorDetail(error));
      return data ?? [];
    },
    getNextPageParam: (last, all) => {
      const loaded = all.reduce((n, page) => n + page.length, 0);
      return last.length === 10 && loaded < total ? loaded : undefined;
    },
  });
  const cards = pages.data?.pages.flat() ?? [];
  return (
    <div className="mt-3 space-y-3">
      {pages.isPending && <p role="status">Loading suggestions…</p>}
      {pages.isSuccess && !cards.length && (
        <p role="status" className="text-sm text-muted-foreground">
          No open suggestions in this group.
        </p>
      )}
      {pages.error && (
        <>
          <ErrorText>{pages.error.message}</ErrorText>
          <Button variant="outline" onClick={() => pages.refetch()}>
            Retry suggestions
          </Button>
        </>
      )}
      {cards.map((s) => (
        <SuggestionCard key={s.id} suggestion={s} />
      ))}
      {pages.hasNextPage && (
        <Button
          variant="outline"
          disabled={pages.isFetchingNextPage}
          onClick={() => pages.fetchNextPage()}
        >
          Show {Math.min(10, total - cards.length)} more of {total - cards.length}
        </Button>
      )}
    </div>
  );
}

export default function NeedsAttention() {
  const [account, setAccount] = useState("");
  const [sort, setSort] = useState<"oldest" | "confidence">("oldest");
  const [expanded, setExpanded] = useState<Kind | null>(null);
  const counts = useQuery({
    queryKey: ["suggestions", "counts", account],
    queryFn: async () => {
      const { data, error } = await api.GET("/suggestions/counts", {
        params: { query: { ...(account ? { account } : {}) } },
      });
      if (error) throw new Error(errorDetail(error));
      return data!;
    },
  });
  const late = useQuery({
    queryKey: ["late-bills"],
    queryFn: async () => {
      const { data, error } = await api.GET("/matching/unmatched", {
        params: { query: { direction: "expected" } },
      });
      if (error) throw new Error(errorDetail(error));
      return (data ?? []).filter((b) => b.aging_days > 0 && !b.has_proposed_match);
    },
  });
  const review = useQuery({
    queryKey: ["unreviewed", "summary"],
    queryFn: async () => {
      const { data, error } = await api.GET("/transactions/review-summary");
      if (error) throw new Error(errorDetail(error));
      return data!;
    },
  });
  const accounts = useQuery({
    queryKey: ["accounts", "attention"],
    queryFn: async () => {
      const { data, error } = await api.GET("/accounts", {
        params: { query: { include_hidden: true, include_closed: true } },
      });
      if (error) throw new Error(errorDetail(error));
      return data ?? [];
    },
  });
  const queries = [counts, late, review, accounts];
  const loading = queries.some((q) => q.isPending);
  const failed = queries.some((q) => q.isError);
  const bills = (late.data ?? []).filter((b) => !account || b.account_id === account);
  const reviewAccounts = (review.data?.accounts ?? []).filter(
    (a) => !account || a.account_id === account,
  );
  const reviewTotal = account ? (reviewAccounts[0]?.total ?? 0) : (review.data?.total ?? 0);
  const proposals = account
    ? (reviewAccounts[0]?.agent_pending ?? 0)
    : (review.data?.agent_pending ?? 0);
  const allClear =
    !loading && !failed && counts.data?.total === 0 && !bills.length && !reviewTotal && !proposals;

  return (
    <Card className="min-w-0">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <CardTitle className="mb-1">Needs attention</CardTitle>
          <p className="text-sm text-muted-foreground">
            Resolve exceptions, then review suggestions and new transactions.
          </p>
        </div>
        <Link to="/schedules" className="text-sm text-primary underline">
          Manage schedules
        </Link>
      </div>
      <div className="my-4 flex flex-wrap gap-3">
        <label className="flex min-w-0 flex-col gap-1 text-xs">
          Account
          <select
            className={selectClass}
            value={account}
            onChange={(e) => setAccount(e.target.value)}
          >
            <option value="">All accounts</option>
            {accounts.data?.map((a) => (
              <option key={a.id} value={a.id}>
                {a.name}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-xs">
          Suggestion order
          <select
            className={selectClass}
            value={sort}
            onChange={(e) => setSort(e.target.value as typeof sort)}
          >
            <option value="oldest">Oldest first</option>
            <option value="confidence">Strongest pattern first</option>
          </select>
        </label>
      </div>
      {loading && (
        <p role="status" className="text-sm">
          Checking attention items…
        </p>
      )}
      {failed && (
        <div role="alert" className="mb-4 rounded-lg border border-destructive p-3 text-sm">
          <p>Some attention items could not be checked. The results below may be incomplete.</p>
          {queries
            .filter((q) => q.error)
            .map((q, i) => (
              <p key={i}>{q.error!.message}</p>
            ))}
          <Button
            className="mt-2"
            variant="outline"
            onClick={() => queries.forEach((q) => q.refetch())}
          >
            Retry attention checks
          </Button>
        </div>
      )}
      {allClear && (
        <p role="status" className="text-sm">
          All clear.
        </p>
      )}
      {bills.length > 0 && (
        <section className="mb-4 rounded-lg border border-amber-300 p-3">
          <h3 className="font-medium">
            Expected payments not seen <Badge variant="amber">{bills.length}</Badge>
          </h3>
          <p className="mt-1 text-xs text-muted-foreground">
            Past the expected date does not prove a missed payment. Check the statement, estimate
            and bank posting before acting.
          </p>
          <ul className="mt-3 divide-y divide-border">
            {bills.map((b) => (
              <li
                key={b.transaction_id}
                className="flex flex-wrap items-center justify-between gap-2 py-2 text-sm"
              >
                <div className="min-w-0 break-words">
                  <p className="font-medium">{b.description || b.payee || "Expected payment"}</p>
                  <p className="text-xs text-muted-foreground">
                    {b.account_name} · {fmtAmount(b.amount)} · expected {b.date} · {b.aging_days}{" "}
                    days ago
                  </p>
                  {(b.amount_estimated || b.date_estimated) && (
                    <Badge variant="amber">Estimate — verify details</Badge>
                  )}
                  {b.schedule_status === "paused" && <Badge>Schedule paused</Badge>}
                </div>
                <Link
                  className="text-primary underline"
                  to={
                    b.account_id
                      ? `/accounts/${b.account_id}?status=expected&start=${b.date}&end=${b.date}&transaction=${b.transaction_id}`
                      : "/schedules"
                  }
                >
                  Review expectation
                </Link>
              </li>
            ))}
          </ul>
        </section>
      )}
      <div className="divide-y divide-border">
        {groups
          .filter((g) => (counts.data?.[g.kind] ?? 0) > 0)
          .map((g) => (
            <section key={g.kind} className="py-3">
              <button
                className="flex w-full items-center justify-between gap-3 text-left"
                aria-expanded={expanded === g.kind}
                aria-controls={`attention-${g.kind}`}
                onClick={() => setExpanded(expanded === g.kind ? null : g.kind)}
              >
                <span>
                  <span className="font-medium">{g.title}</span>
                  <span className="mt-1 block text-xs text-muted-foreground">{g.help}</span>
                </span>
                <span className="flex shrink-0 gap-2">
                  <Badge>{counts.data![g.kind]}</Badge>
                  <span aria-hidden="true">{expanded === g.kind ? "−" : "+"}</span>
                </span>
              </button>
              {expanded === g.kind && (
                <div id={`attention-${g.kind}`}>
                  <Cards kind={g.kind} total={counts.data![g.kind]} account={account} sort={sort} />
                </div>
              )}
            </section>
          ))}
      </div>
      {(reviewTotal > 0 || proposals > 0) && (
        <details className="mt-3 border-t border-border pt-3">
          <summary className="cursor-pointer font-medium">
            Transaction review <Badge variant="amber">{reviewTotal}</Badge>
            {proposals > 0 && ` · ${proposals} agent proposals`}
          </summary>
          <p className="my-2 text-xs text-muted-foreground">
            Review posted transactions in each register. Pending transactions are separate; a
            transfer can appear on both accounts.
          </p>
          <ul className="space-y-2">
            {reviewAccounts.map((a) => (
              <li key={a.account_id} className="flex flex-wrap justify-between gap-2 text-sm">
                <span>{a.name}</span>
                <span className="flex flex-wrap gap-3">
                  {a.total > a.pending && (
                    <Link
                      className="text-primary underline"
                      to={`/accounts/${a.account_id}?reviewed=false&status=posted`}
                    >
                      {a.total - a.pending} posted
                    </Link>
                  )}
                  {a.pending > 0 && (
                    <Link
                      className="text-primary underline"
                      to={`/accounts/${a.account_id}?reviewed=false&status=pending`}
                    >
                      {a.pending} pending
                    </Link>
                  )}
                  {a.agent_pending > 0 && (
                    <Link
                      className="text-primary underline"
                      to={`/accounts/${a.account_id}?status=pending`}
                    >
                      {a.agent_pending} proposals
                    </Link>
                  )}
                </span>
              </li>
            ))}
          </ul>
        </details>
      )}
    </Card>
  );
}
