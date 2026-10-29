import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";

const selectClass =
  "h-8 w-full min-w-44 rounded-md border border-border bg-card px-2 text-xs";

/** The register's category cell (DESIGN §3.3): plain ltree paths are
 * categories; `[account path]` entries are Quicken-style transfer targets —
 * choosing one converts the row into a transfer to that account. */
export default function CategorySelect({
  value,
  currentAccountPath,
  onChange,
  disabled,
}: {
  value: string; // category path, or "[other.account.path]" for transfers
  currentAccountPath: string;
  onChange: (next: string) => void;
  disabled?: boolean;
}) {
  const categories = useQuery({
    queryKey: ["categories"],
    queryFn: async () => (await api.GET("/categories")).data ?? [],
  });
  const accounts = useQuery({
    // Transfer targets include HIDDEN accounts (you can still move money to a
    // decluttered account) but not CLOSED ones (they take no new postings).
    queryKey: ["accounts", "transfer-targets"],
    queryFn: async () =>
      (
        await api.GET("/accounts", {
          params: { query: { include_balances: true, include_hidden: true } },
        })
      ).data ?? [],
  });

  const categoryOptions = (categories.data ?? []).map((c) => c.path);
  const transferOptions = (accounts.data ?? []).filter(
    (a) => a.path !== currentAccountPath,
  );
  const known =
    value === "" || categoryOptions.includes(value) ||
    transferOptions.some((a) => `[${a.path}]` === value);

  return (
    <select
      className={selectClass}
      value={value}
      disabled={disabled}
      onChange={(e) => {
        if (e.target.value !== value) onChange(e.target.value);
      }}
    >
      {value === "" && <option value="">—</option>}
      {!known && <option value={value}>{value}</option>}
      <optgroup label="Categories">
        {categoryOptions.map((path) => (
          <option key={path} value={path}>
            {path}
          </option>
        ))}
      </optgroup>
      <optgroup label="Transfer to…">
        {transferOptions.map((a) => (
          <option key={a.id} value={`[${a.path}]`}>
            [{a.name}]
          </option>
        ))}
      </optgroup>
    </select>
  );
}
