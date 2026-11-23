import { useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { ChevronRight } from "lucide-react";
import type { components } from "../api/schema";
import { api, errorDetail } from "../lib/api";
import { formatMoney, sumMoney } from "../lib/money";
import {
  ACCOUNT_GROUPS,
  OTHER_ACCOUNT_GROUP,
  accountGroupOf,
  accountTypeLabel,
  type AccountGroupKey,
} from "../lib/labels";
import { cn } from "../lib/utils";
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

/** An account's type in words, "Asset · Vehicle" for a property account,
 * with its hidden/closed badges (#541). */
function AccountTypeBadges({ account: a }: { account: AccountOut }) {
  return (
    <span className="flex flex-wrap items-center gap-1">
      <Badge>
        {a.type === "property"
          ? `${accountTypeLabel(a.type)} · ${assetKindLabel(a.asset?.kind)}`
          : accountTypeLabel(a.type)}
      </Badge>
      {a.hidden_at && <Badge>hidden</Badge>}
      {a.closed_at && <Badge variant="amber">closed</Badge>}
    </span>
  );
}

export type AccountGroup = {
  key: AccountGroupKey;
  label: string;
  rows: AccountOut[];
  /** The exact sum of the rows' balances, or null when they mix currencies
   * (adding dollars to euros would be a number nobody holds). */
  subtotal: string | null;
  currency: string;
};

/** Rows into the Dashboard's groups (#593), in display order, empty groups
 * dropped. Every row lands in exactly one group, keeping the API's order
 * inside it. */
export function groupAccounts(rows: AccountOut[]): AccountGroup[] {
  const defs = [...ACCOUNT_GROUPS, OTHER_ACCOUNT_GROUP];
  return defs
    .map((def) => {
      const members = rows.filter((a) => accountGroupOf(a.type) === def.key);
      const currencies = new Set(members.map((a) => a.currency.trim().toUpperCase()));
      const currency = members[0]?.currency ?? "USD";
      return {
        key: def.key,
        label: def.label,
        rows: members,
        subtotal: currencies.size <= 1 ? sumMoney(members.map((a) => a.balance)) : null,
        currency,
      };
    })
    .filter((g) => g.rows.length > 0);
}

export type GroupedAccountsVariant = "summary" | "detailed";

/** Where each page remembers its collapsed groups: the Dashboard and the
 * Accounts page fold independently (#617). */
export const COLLAPSED_GROUPS_KEYS: Record<GroupedAccountsVariant, string> = {
  summary: "purser.dashboard.collapsedGroups",
  detailed: "purser.accounts.collapsedGroups",
};

/** The groups this browser collapsed: a per-viewer convenience, so a
 * blocked or empty store just means every group starts open. */
function readCollapsed(storageKey: string): Set<string> {
  try {
    const raw = window.localStorage.getItem(storageKey);
    const parsed: unknown = raw ? JSON.parse(raw) : [];
    return new Set(Array.isArray(parsed) ? parsed.filter((k) => typeof k === "string") : []);
  } catch {
    return new Set();
  }
}

function writeCollapsed(storageKey: string, keys: Set<string>) {
  try {
    window.localStorage.setItem(storageKey, JSON.stringify([...keys]));
  } catch {
    // Storage unavailable (private window, blocked site data): state stays in memory.
  }
}

/** Accounts grouped by kind with a subtotal per group (#593): Banking,
 * Credit cards, Investments, Property, Loans. A group's header toggles its
 * rows; the subtotal stays visible while collapsed, so a folded group still
 * reads as one line of the balance sheet, and it sums exactly the rows given.
 *
 * Two variants share the groups and the rendering (#617):
 * - `summary` (the Dashboard): name and balance, a secured asset's equity
 *   under its name.
 * - `detailed` (the Accounts page): adds the Type column with the hidden and
 *   closed badges, and the storage path as a quiet second line (#541).
 * Each variant remembers its collapsed groups under its own storage key. */
export function GroupedAccountsTable({
  rows,
  variant = "summary",
}: {
  rows: AccountOut[];
  variant?: GroupedAccountsVariant;
}) {
  const detailed = variant === "detailed";
  const storageKey = COLLAPSED_GROUPS_KEYS[variant];
  const [collapsed, setCollapsed] = useState<Set<string>>(() => readCollapsed(storageKey));
  const groups = groupAccounts(rows);
  const toggle = (key: string) =>
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      writeCollapsed(storageKey, next);
      return next;
    });

  return (
    <Table>
      <thead>
        <tr>
          <Th>Account</Th>
          {detailed && <Th>Type</Th>}
          <Th className="text-right">Balance</Th>
        </tr>
      </thead>
      {groups.map((g) => {
        const open = !collapsed.has(g.key);
        return (
          <tbody key={g.key} aria-label={g.label}>
            <tr className="bg-muted/40">
              <Td className="py-1.5" colSpan={detailed ? 2 : undefined}>
                <button
                  type="button"
                  aria-expanded={open}
                  onClick={() => toggle(g.key)}
                  className="group flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-muted-foreground hover:text-foreground"
                >
                  <ChevronRight
                    aria-hidden="true"
                    className={cn("h-3.5 w-3.5 transition-transform duration-200", open && "rotate-90")}
                  />
                  {g.label}
                  <span className="font-normal normal-case tracking-normal">· {g.rows.length}</span>
                </button>
              </Td>
              <Td className="py-1.5 text-right font-mono text-xs font-semibold text-muted-foreground">
                {g.subtotal == null ? "Mixed currencies" : formatMoney(g.subtotal, { currency: g.currency })}
              </Td>
            </tr>
            {open &&
              g.rows.map((a) => (
                <tr key={a.id} className="hover:bg-muted/50">
                  <Td>
                    <span className="flex items-center gap-2 pl-2">
                      <span className="text-muted-foreground">
                        <AccountIcon type={a.type} kind={a.asset?.kind} />
                      </span>
                      <Link to={`/accounts/${a.id}`} className="font-medium hover:underline">
                        {a.name}
                      </Link>
                    </span>
                    {detailed ? (
                      <div className="pl-8 font-mono text-xs text-muted-foreground">{a.path}</div>
                    ) : (
                      a.secured_loan_ids.length > 0 &&
                      a.equity != null && (
                        <div className="pl-8 text-xs text-muted-foreground">
                          Equity {formatMoney(a.equity, { currency: a.currency })}
                        </div>
                      )
                    )}
                  </Td>
                  {detailed && (
                    <Td>
                      <AccountTypeBadges account={a} />
                    </Td>
                  )}
                  <Td className="text-right font-mono">{formatMoney(a.balance, { currency: a.currency })}</Td>
                </tr>
              ))}
          </tbody>
        );
      })}
    </Table>
  );
}
