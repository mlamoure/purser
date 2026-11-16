import { useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import type { components } from "../api/schema";
import { api, errorDetail } from "../lib/api";
import { fmtAmount } from "../lib/utils";
import { Badge, Card, CardTitle, ErrorText, Table, Td, Th } from "../components/ui";
import NeedsAttention from "../components/NeedsAttention";

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
    queryFn: async () => {
      const { data, error } = await api.GET("/networth", { params: { query: {} } });
      if (error) throw new Error(errorDetail(error));
      return data;
    },
  });

  const future = useQuery({
    queryKey: ["networth", "potential"],
    queryFn: async () => {
      const { data, error } = await api.GET("/networth/potential");
      if (error) throw new Error(errorDetail(error));
      return data;
    },
  });

  return (
    <div className="space-y-6">
      {networth.error && <ErrorText>{networth.error.message}</ErrorText>}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
        <Card role="region" aria-label="Net worth">
          <p className="text-sm text-muted-foreground">Net worth</p>
          <p className="text-2xl font-bold">{fmtAmount(networth.data?.net_worth)}</p>
          <UnvestedValue value={future.data} total={future.data?.potential_net_worth} />
        </Card>
        <Card role="region" aria-label="Assets">
          <p className="text-sm text-muted-foreground">Assets</p>
          <p className="text-2xl font-bold text-emerald-700">{fmtAmount(networth.data?.assets)}</p>
          <UnvestedValue value={future.data} total={future.data?.potential_assets} details />
        </Card>
        <Card>
          <p className="text-sm text-muted-foreground">Liabilities</p>
          <p className="text-2xl font-bold text-red-700">{fmtAmount(networth.data?.liabilities)}</p>
        </Card>
      </div>

      {future.error && <ErrorText>{`Unvested values unavailable: ${future.error.message}`}</ErrorText>}

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

/** Keep the optional scenario inside the owned totals, without an empty card. */
function UnvestedValue({ value, total, details = false }: {
  value: components["schemas"]["PotentialNetWorthOut"] | undefined;
  total: string | null | undefined;
  details?: boolean;
}) {
  if (!value || (value.complete && value.unvested_awards === 0)) return null;
  return (
    <div className="mt-3 border-t border-border pt-3 text-sm">
      <p className="flex flex-wrap items-baseline justify-between gap-x-2">
        <span className="text-muted-foreground">Including unvested</span>
        <span className="font-semibold">{fmtAmount(total)}</span>
      </p>
      {!value.complete && (
        <p className="mt-1 text-xs text-amber-700">Valuation incomplete</p>
      )}
      {value.stale_price_symbols.length > 0 && (
        <p className="mt-1 text-xs text-amber-700">
          Stale prices: {value.stale_price_symbols.join(", ")}.
        </p>
      )}
      {details && (
        <details className="mt-2 text-xs text-muted-foreground">
          <summary className="cursor-pointer">
            Unvested stock: {fmtAmount(value.unvested_value)}
          </summary>
          <p className="mt-2">
            If all captured awards vest, at recorded stock prices and before tax withholding.
          </p>
          {!value.complete && (
            <p className="mt-2">
              Known priced awards: {fmtAmount(value.known_unvested_value)}.
              {value.unknown_quantity_awards > 0 && ` ${value.unknown_quantity_awards} award(s) have unknown remaining units.`}
              {value.unpriced_symbols.length > 0 && ` No usable price for ${value.unpriced_symbols.join(", ")}.`}
              {value.unsupported_currency_awards > 0 && " Some awards need currency conversion."}
            </p>
          )}
          <p className="mt-2">
            {value.oldest_price_as_of && `Oldest price: ${value.oldest_price_as_of}`}
            {value.next_vest_date && ` · Next scheduled vest: ${value.next_vest_date}`}
          </p>
        </details>
      )}
    </div>
  );
}
