import { useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, errorDetail } from "../lib/api";
import { fmtAmount } from "../lib/utils";
import { Badge, Card, CardTitle, Table, Td, Th } from "../components/ui";
import { SuggestionCard } from "../components/SuggestionCard";

export default function Dashboard() {
  const [showHidden, setShowHidden] = useState(false);
  const [showClosed, setShowClosed] = useState(false);
  const accounts = useQuery({
    queryKey: ["accounts", showHidden, showClosed],
    queryFn: async () =>
      (
        await api.GET("/accounts", {
          params: {
            query: {
              include_balances: true,
              include_hidden: showHidden,
              include_closed: showClosed,
            },
          },
        })
      ).data ?? [],
  });
  const networth = useQuery({
    queryKey: ["networth"],
    queryFn: async () => (await api.GET("/networth", { params: { query: {} } })).data,
  });

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-3 gap-4">
        <Card>
          <p className="text-sm text-muted-foreground">Net worth</p>
          <p className="text-2xl font-bold">{fmtAmount(networth.data?.net_worth)}</p>
        </Card>
        <Card>
          <p className="text-sm text-muted-foreground">Assets</p>
          <p className="text-2xl font-bold text-emerald-700">{fmtAmount(networth.data?.assets)}</p>
        </Card>
        <Card>
          <p className="text-sm text-muted-foreground">Liabilities</p>
          <p className="text-2xl font-bold text-red-700">{fmtAmount(networth.data?.liabilities)}</p>
        </Card>
      </div>

      <NeedsAttention />

      <Card>
        <div className="mb-4 flex items-center justify-between">
          <CardTitle className="mb-0">Accounts</CardTitle>
          <Link to="/import" className="text-sm font-medium text-primary hover:underline">
            Import QIF
          </Link>
        </div>
        <Table>
          <thead>
            <tr>
              <Th>Path</Th>
              <Th>Name</Th>
              <Th>Type</Th>
              <Th className="text-right">Balance</Th>
            </tr>
          </thead>
          <tbody>
            {(accounts.data ?? []).map((a) => (
              <tr key={a.id} className="hover:bg-muted/50">
                <Td className="font-mono text-xs">
                  <Link to={`/accounts/${a.id}`} className="hover:underline">
                    {a.path}
                  </Link>
                </Td>
                <Td>
                  <Link to={`/accounts/${a.id}`} className="hover:underline">
                    {a.name}
                  </Link>
                </Td>
                <Td>
                  <span className="flex flex-wrap items-center gap-1">
                    <Badge>{a.type}</Badge>
                    {a.hidden_at && <Badge>hidden</Badge>}
                    {a.closed_at && <Badge variant="amber">closed</Badge>}
                  </span>
                </Td>
                <Td className="text-right font-mono">{fmtAmount(a.balance)}</Td>
              </tr>
            ))}
          </tbody>
        </Table>
        {accounts.data?.length === 0 && (
          <p className="text-sm text-muted-foreground">
            No accounts yet — connect a feed or create accounts via the API.
          </p>
        )}
        <div className="mt-3 flex gap-4">
          <label className="flex items-center gap-2 text-sm text-muted-foreground">
            <input
              type="checkbox"
              checked={showHidden}
              onChange={(e) => setShowHidden(e.target.checked)}
            />
            Show hidden
          </label>
          <label className="flex items-center gap-2 text-sm text-muted-foreground">
            <input
              type="checkbox"
              checked={showClosed}
              onChange={(e) => setShowClosed(e.target.checked)}
            />
            Show closed
          </label>
        </div>
      </Card>
    </div>
  );
}

/** The review-queue leftovers that are not per-transaction (issue #112):
 * the suggestions inbox and the late-bills report — plus counts that point
 * into the registers. Everything else lives on the rows themselves. */
function NeedsAttention() {
  // The suggestions inbox (#233/#236): detected recurring bills and
  // transfers, agent-proposed schedules, payments for a newly added card,
  // transfer-link rules your manual transfer confirms proposed (#271), and
  // the matcher's proposed matches (#246).
  const suggestions = useQuery({
    queryKey: ["suggestions"],
    queryFn: async () => (await api.GET("/suggestions", { params: { query: {} } })).data ?? [],
  });
  const lateBills = useQuery({
    queryKey: ["late-bills"],
    queryFn: async () =>
      // The report lists every live expectation with its aging; only the
      // ones past their date are late — upcoming instances live on the
      // Schedules page and in the forecast, not here (#236). A late bill
      // with a proposed match is a match card in the inbox above (#246).
      (
        (await api.GET("/matching/unmatched", { params: { query: { direction: "expected" } } }))
          .data ?? []
      ).filter((b) => b.aging_days > 0 && !b.has_proposed_match),
  });
  const candidates = useQuery({
    queryKey: ["transfer-candidates"],
    queryFn: async () => (await api.GET("/matching/transfer-candidates")).data ?? [],
  });
  // The unreviewed set (#175). limit=1 because only the total is wanted —
  // this is a count, not a second row source.
  const unreviewed = useQuery({
    queryKey: ["unreviewed"],
    queryFn: async () => {
      const { data, error } = await api.GET("/transactions", {
        params: { query: { reviewed: false, limit: 1 } },
      });
      if (error) throw new Error(errorDetail(error));
      return data;
    },
  });

  const agentPending = useQuery({
    queryKey: ["agent-pending"],
    queryFn: async () => {
      const { data } = await api.GET("/transactions", {
        params: { query: { status: ["pending"], limit: 200 } },
      });
      return (data?.items ?? []).filter((t) => t.origin === "agent");
    },
  });


  const unreviewedTotal = unreviewed.data?.total ?? 0;

  const nothing =
    (suggestions.data?.length ?? 0) === 0 &&
    (lateBills.data?.length ?? 0) === 0 &&
    (candidates.data?.length ?? 0) === 0 &&
    (agentPending.data?.length ?? 0) === 0 &&
    unreviewedTotal === 0;

  return (
    <Card>
      <CardTitle>Needs attention</CardTitle>
      {nothing && <p className="text-sm text-muted-foreground">All clear.</p>}
      {(suggestions.data?.length ?? 0) > 0 && (
        <div className="mt-2 space-y-2">
          <p className="text-sm font-medium">
            Suggestions — schedules, transfer links and bill matches Purser thinks you have{" "}
            <Link to="/schedules" className="font-normal text-muted-foreground underline">
              manage schedules
            </Link>{" "}
            <Link to="/config/rules" className="font-normal text-muted-foreground underline">
              rules
            </Link>
          </p>
          {suggestions.data!.map((s) => (
            <SuggestionCard key={s.id} suggestion={s} />
          ))}
        </div>
      )}
      {(lateBills.data?.length ?? 0) > 0 && (
        <div className="mt-4 space-y-1">
          <p className="text-sm font-medium">Late bills (expected, not seen)</p>
          {lateBills.data!.map((b) => (
            <p key={b.transaction_id} className="text-sm text-muted-foreground">
              {b.payee ?? b.description ?? "(unknown)"} ·{" "}
              <span className="font-mono">{fmtAmount(b.amount)}</span> · due {b.date} ·{" "}
              <Badge variant={b.aging_days > 7 ? "red" : "amber"}>{b.aging_days}d late</Badge>
            </p>
          ))}
        </div>
      )}
      {unreviewedTotal > 0 && (
        <p className="mt-4 text-sm">
          <Badge variant="amber">{unreviewedTotal}</Badge>{" "}
          <span className="text-muted-foreground">
            transaction{unreviewedTotal === 1 ? "" : "s"} awaiting review — they arrived
            without human eyes.
          </span>
        </p>
      )}
      {((candidates.data?.length ?? 0) > 0 || (agentPending.data?.length ?? 0) > 0) && (
        <p className="mt-4 text-sm text-muted-foreground">
          {(candidates.data?.length ?? 0) > 0 && (
            <>
              {candidates.data!.length} possible transfer pair
              {candidates.data!.length === 1 ? "" : "s"} —{" "}
            </>
          )}
          {(agentPending.data?.length ?? 0) > 0 && (
            <>
              {agentPending.data!.length} agent proposal
              {agentPending.data!.length === 1 ? "" : "s"} awaiting review —{" "}
            </>
          )}
          open the account registers below to act on them.
        </p>
      )}
    </Card>
  );
}
