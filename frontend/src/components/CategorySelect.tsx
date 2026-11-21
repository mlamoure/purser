import { useQuery } from "@tanstack/react-query";
import type { components } from "../api/schema";
import { api, errorDetail } from "../lib/api";
import { categoriesQuery } from "../lib/names";
import { pathLeafLabel } from "../lib/labels";

type AccountOut = components["schemas"]["AccountOut"];

const selectClass =
  "h-8 w-full min-w-44 rounded-md border border-border bg-card px-2 text-xs";

export type CategoryGroup = { label: string; options: { path: string; name: string }[] };

/** The category list as picker groups (#541): every category that has
 * children heads a group of its own (itself first, then its childless
 * children), so "Groceries" sits under "Food & drink" rather than reading as
 * `expenses.food.groceries`. Order follows the API's path order. A group's
 * label is the head's name, with its ancestors below the root for a deeper
 * tree ("Housing › Repairs"). */
export function groupCategories(categories: Pick<AccountOut, "path" | "name">[]): CategoryGroup[] {
  const byPath = new Map(categories.map((c) => [c.path, c]));
  const parentOf = (path: string) => path.split(".").slice(0, -1).join(".");
  const hasChildren = new Set(categories.map((c) => parentOf(c.path)).filter((p) => byPath.has(p)));
  const groups = new Map<string, CategoryGroup>();
  const groupLabel = (head: string) => {
    const labels = head.split(".");
    if (labels.length === 1) return byPath.get(head)?.name ?? pathLeafLabel(head);
    return labels
      .slice(1)
      .map((_, i) => {
        const ancestor = labels.slice(0, i + 2).join(".");
        return byPath.get(ancestor)?.name ?? pathLeafLabel(ancestor);
      })
      .join(" › ");
  };
  for (const c of categories) {
    const head = hasChildren.has(c.path) || !byPath.has(parentOf(c.path)) ? c.path : parentOf(c.path);
    let group = groups.get(head);
    if (!group) {
      group = { label: groupLabel(head), options: [] };
      groups.set(head, group);
    }
    group.options.push({ path: c.path, name: c.name });
  }
  return [...groups.values()];
}

/** The register's category cell (DESIGN §3.3). The value is what the API
 * takes — a category path, or `[account path]` for a Quicken-style transfer
 * target, where choosing one converts the row into a transfer to that
 * account — and the options read as names (#541): "Groceries" under its
 * group, "Transfer to Savings". */
export default function CategorySelect({
  value,
  currentAccountPath,
  onChange,
  disabled,
  id,
  "aria-label": ariaLabel = "Category",
}: {
  value: string; // category path, or "[other.account.path]" for transfers
  currentAccountPath: string;
  onChange: (next: string) => void;
  disabled?: boolean;
  id?: string;
  "aria-label"?: string;
}) {
  const categories = useQuery(categoriesQuery);
  const accounts = useQuery({
    // Transfer targets include HIDDEN accounts (you can still move money to a
    // decluttered account) but not CLOSED ones (they take no new postings).
    queryKey: ["accounts", "transfer-targets"],
    queryFn: async () => {
      const { data, error } = await api.GET("/accounts", {
        params: { query: { include_balances: true, include_hidden: true } },
      });
      if (error) throw new Error(errorDetail(error));
      return data ?? [];
    },
  });

  const categoryList = categories.data ?? [];
  const groups = groupCategories(categoryList);
  const transferOptions = (accounts.data ?? []).filter(
    (a) => a.path !== currentAccountPath,
  );
  const known =
    value === "" || categoryList.some((c) => c.path === value) ||
    transferOptions.some((a) => `[${a.path}]` === value);
  // A value neither list offers (a closed account's transfer, a category
  // still loading) is kept selectable, named from its path, never shown raw.
  const unknownLabel = /^\[(.*)\]$/.test(value)
    ? `Transfer to ${pathLeafLabel(value.slice(1, -1))}`
    : pathLeafLabel(value);

  return (
    <select
      id={id}
      aria-label={id ? undefined : ariaLabel}
      className={selectClass}
      value={value}
      disabled={disabled}
      onChange={(e) => {
        if (e.target.value !== value) onChange(e.target.value);
      }}
    >
      {value === "" && <option value="">—</option>}
      {!known && <option value={value}>{unknownLabel}</option>}
      {/* One select per register row: a failed load is said inside the
          list (#540) rather than as an error on every row. */}
      {categories.error && (
        <optgroup label="Categories">
          <option disabled>Could not load categories</option>
        </optgroup>
      )}
      {groups.map((g) => (
        <optgroup key={g.options[0].path} label={g.label}>
          {g.options.map((c) => (
            <option key={c.path} value={c.path}>
              {c.name}
            </option>
          ))}
        </optgroup>
      ))}
      <optgroup label="Transfers">
        {accounts.error && <option disabled>Could not load accounts</option>}
        {transferOptions.map((a) => (
          <option key={a.id} value={`[${a.path}]`}>
            {`Transfer to ${a.name}`}
          </option>
        ))}
      </optgroup>
    </select>
  );
}
