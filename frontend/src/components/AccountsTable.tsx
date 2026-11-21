import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import type { components } from "../api/schema";
import { api, errorDetail } from "../lib/api";
import { formatMoney } from "../lib/money";
import { accountTypeLabel } from "../lib/labels";
import { Badge, Table, Td, Th } from "./ui";
import { AccountIcon, assetKindLabel } from "./AccountIcon";

type AccountOut = components["schemas"]["AccountOut"];

/** The ledger accounts with balances (GET /accounts), shared by the Accounts
 * page and the Dashboard's summary (#542): one query key, so both read one
 * cache. Hidden and closed accounts load only when asked for. */
export function useAccounts({ showHidden = false, showClosed = false } = {}) {
  return useQuery({
    queryKey: ["accounts", showHidden, showClosed],
    queryFn: async () => {
      // A failed load must not read as "No accounts yet".
      const { data, error } = await api.GET("/accounts", {
        params: {
          query: {
            include_balances: true,
            include_hidden: showHidden,
            include_closed: showClosed,
          },
        },
      });
      if (error) throw new Error(errorDetail(error));
      return data ?? [];
    },
  });
}

export const NO_ACCOUNTS = "No accounts yet — connect a feed or create accounts via the API.";

/** Accounts by name, each a link to its register. The full table (Accounts
 * page) adds the type in words, hidden/closed badges and the storage path as
 * a quiet second line (#541); `compact` (Dashboard) is name and balance. */
export function AccountsTable({ rows, compact = false }: { rows: AccountOut[]; compact?: boolean }) {
  return (
    <Table>
      <thead>
        <tr>
          <Th>Account</Th>
          {!compact && <Th>Type</Th>}
          <Th className="text-right">Balance</Th>
        </tr>
      </thead>
      <tbody>
        {rows.map((a) => (
          <tr key={a.id} className="hover:bg-muted/50">
            <Td>
              <span className="flex items-center gap-2">
                <span className="text-muted-foreground">
                  <AccountIcon type={a.type} kind={a.asset?.kind} />
                </span>
                <Link to={`/accounts/${a.id}`} className="font-medium hover:underline">
                  {a.name}
                </Link>
              </span>
              {!compact && (
                <div className="pl-6 font-mono text-xs text-muted-foreground">{a.path}</div>
              )}
            </Td>
            {!compact && (
              <Td>
                <span className="flex flex-wrap items-center gap-1">
                  <Badge>
                    {a.type === "property"
                      ? `${accountTypeLabel(a.type)} · ${assetKindLabel(a.asset?.kind)}`
                      : accountTypeLabel(a.type)}
                  </Badge>
                  {a.hidden_at && <Badge>hidden</Badge>}
                  {a.closed_at && <Badge variant="amber">closed</Badge>}
                </span>
              </Td>
            )}
            <Td className="text-right font-mono">{formatMoney(a.balance, { currency: a.currency })}</Td>
          </tr>
        ))}
      </tbody>
    </Table>
  );
}
