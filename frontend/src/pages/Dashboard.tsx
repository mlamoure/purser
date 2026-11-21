import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import type { components } from "../api/schema";
import { api, errorDetail } from "../lib/api";
import { formatMoney, negateMoney } from "../lib/money";
import { formatDate } from "../lib/dates";
import { Card, CardTitle, ErrorText } from "../components/ui";
import { AccountsTable, NO_ACCOUNTS, useAccounts } from "../components/AccountsTable";
import { QueryState } from "../components/QueryState";
import NeedsAttention from "../components/NeedsAttention";
import FirstRunChecklist, { NewBankAccounts } from "../components/FirstRunChecklist";
import { useIsAdmin } from "../lib/useIsAdmin";

export default function Dashboard() {
  const isAdmin = useIsAdmin();
  const accounts = useAccounts();
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
      {isAdmin && <FirstRunChecklist />}
      {networth.error && <ErrorText>{networth.error.message}</ErrorText>}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
        <Card role="region" aria-label="Net worth">
          <p className="text-sm text-muted-foreground">Net worth</p>
          <p className="font-mono text-2xl font-bold">{formatMoney(networth.data?.net_worth)}</p>
          <UnvestedValue value={future.data} total={future.data?.potential_net_worth} />
        </Card>
        <Card role="region" aria-label="Assets">
          <p className="text-sm text-muted-foreground">Assets</p>
          <p className="font-mono text-2xl font-bold text-success">{formatMoney(networth.data?.assets)}</p>
          <UnvestedValue value={future.data} total={future.data?.potential_assets} details />
        </Card>
        <Card role="region" aria-label="Liabilities">
          <p className="text-sm text-muted-foreground">Liabilities</p>
          {/* The API sends liabilities as a positive total (what is owed),
              while each liability account's balance below is negative. The
              tile shows the same sign as those rows (-$380.00, not $380.00)
              so the two never read as different amounts, and red marks it
              as owed; Net worth is Assets plus this. */}
          <p className="font-mono text-2xl font-bold text-destructive">
            {networth.data ? formatMoney(negateMoney(networth.data.liabilities)) : "—"}
          </p>
        </Card>
      </div>

      {future.error && <ErrorText>{`Unvested values unavailable: ${future.error.message}`}</ErrorText>}

      {isAdmin && <NewBankAccounts />}
      <NeedsAttention />

      <Card>
        <div className="mb-4 flex items-center justify-between">
          <CardTitle className="mb-0">Accounts</CardTitle>
          <Link to="/accounts" className="text-sm font-medium text-primary hover:underline">
            All accounts
          </Link>
        </div>
        {/* A compact summary (#542): the Accounts page has types, paths and
            the hidden/closed toggles. */}
        <QueryState query={accounts} what="accounts" empty={NO_ACCOUNTS}>
          {(rows) => <AccountsTable rows={rows} compact />}
        </QueryState>
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
        <span className="font-mono font-semibold">{formatMoney(total)}</span>
      </p>
      {!value.complete && (
        <p className="mt-1 text-xs text-warning">Valuation incomplete</p>
      )}
      {value.stale_price_symbols.length > 0 && (
        <p className="mt-1 text-xs text-warning">
          Stale prices: {value.stale_price_symbols.join(", ")}.
        </p>
      )}
      {details && (
        <details className="mt-2 text-xs text-muted-foreground">
          <summary className="cursor-pointer">
            Unvested stock: {formatMoney(value.unvested_value)}
          </summary>
          <p className="mt-2">
            If all captured awards vest, at recorded stock prices and before tax withholding.
          </p>
          {!value.complete && (
            <p className="mt-2">
              Known priced awards: {formatMoney(value.known_unvested_value)}.
              {value.unknown_quantity_awards > 0 && ` ${value.unknown_quantity_awards} award(s) have unknown remaining units.`}
              {value.unpriced_symbols.length > 0 && ` No usable price for ${value.unpriced_symbols.join(", ")}.`}
              {value.unsupported_currency_awards > 0 && " Some awards need currency conversion."}
            </p>
          )}
          <p className="mt-2">
            {value.oldest_price_as_of && `Oldest price: ${formatDate(value.oldest_price_as_of)}`}
            {value.next_vest_date && ` · Next scheduled vest: ${formatDate(value.next_vest_date)}`}
          </p>
        </details>
      )}
    </div>
  );
}
