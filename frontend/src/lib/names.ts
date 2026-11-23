/* Names for ltree paths (issue #541).
 *
 * Accounts and categories are stored and sent by path ("assets.checking",
 * "expenses.food.groceries", DESIGN §3.2); people know them by name
 * ("Checking", "Groceries"). These hooks read the two lists the API already
 * serves and turn a path into its name for display. A page keeps sending the
 * path; only what it shows changes. */

import { useMemo } from "react";
import { queryOptions, useQuery } from "@tanstack/react-query";
import type { components } from "../api/schema";
import { api, errorDetail } from "./api";
import { pathLeafLabel } from "./labels";

type AccountOut = components["schemas"]["AccountOut"];

/** Every category (GET /categories, ordered by path). The category picker,
 * the register and the name lookups share these options, so one fetch
 * serves them all. */
export const categoriesQuery = queryOptions({
  queryKey: ["categories"],
  queryFn: async (): Promise<AccountOut[]> => {
    const { data, error } = await api.GET("/categories");
    if (error) throw new Error(errorDetail(error));
    return data ?? [];
  },
});

/** Every real account, hidden and closed included: a rule, a schedule or an
 * old transfer can still name a closed account, and it should still read by
 * its name. */
export const accountNamesQuery = queryOptions({
  queryKey: ["accounts", "names"],
  queryFn: async (): Promise<AccountOut[]> => {
    const { data, error } = await api.GET("/accounts", {
      params: { query: { include_hidden: true, include_closed: true } },
    });
    if (error) throw new Error(errorDetail(error));
    return data ?? [];
  },
});

/** path -> display name. A path neither list knows (still loading, a load
 * that failed, a system account such as equity.securities) shows its last
 * label as words, never the raw path; null/"" shows "—". */
export type PathName = (path: string | null | undefined) => string;

export function namesFrom(lists: (AccountOut[] | undefined)[]): PathName {
  const byPath = new Map<string, string>();
  for (const list of lists) for (const a of list ?? []) byPath.set(a.path, a.name);
  return (path) => {
    if (path == null || path === "") return "—";
    return byPath.get(path) ?? pathLeafLabel(path);
  };
}

/** Names for category paths only (one GET /categories). */
export function useCategoryNames(): PathName {
  const categories = useQuery(categoriesQuery);
  return useMemo(() => namesFrom([categories.data]), [categories.data]);
}

/** Names for account paths only (one GET /accounts). */
export function useAccountNames(): PathName {
  const accounts = useQuery(accountNamesQuery);
  return useMemo(() => namesFrom([accounts.data]), [accounts.data]);
}

/** Names for any path, account or category (a rule, a split line). */
export function usePathNames(): PathName {
  const categories = useQuery(categoriesQuery);
  const accounts = useQuery(accountNamesQuery);
  return useMemo(
    () => namesFrom([categories.data, accounts.data]),
    [categories.data, accounts.data],
  );
}

/** Which way a row's money moves on the account being viewed: "credit" is
 * money in, "debit" money out (#589). */
export type MoneyDirection = "credit" | "debit";

/** A transfer reads from the viewed account's side (#589): money in is
 * "Transfer from <account>", money out "Transfer to <account>". */
export function transferLabel(accountName: string, direction: MoneyDirection = "debit"): string {
  return direction === "credit" ? `Transfer from ${accountName}` : `Transfer to ${accountName}`;
}

/** A category-cell value for display: a category path reads as its name,
 * a "[account.path]" transfer target as "Transfer to <account name>", or
 * "Transfer from <account name>" on a credit row (#589). */
export function categoryValueLabel(
  value: string | null | undefined,
  name: PathName,
  direction: MoneyDirection = "debit",
): string {
  if (value == null || value === "") return "—";
  // The register's "Securities trade" choice (CategorySelect's
  // SECURITIES_TRADE_VALUE, #591): not a category path.
  if (value === ":securities_trade") return "Securities trade";
  const transfer = /^\[(.*)\]$/.exec(value);
  return transfer ? transferLabel(name(transfer[1]), direction) : name(value);
}
