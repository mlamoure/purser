/** The search syntax, one click from the search box (#141). A native
 * <details>: keyboard and screen-reader friendly with no script, and themed
 * only through the CSS variables. */
const EXAMPLES: [string, string][] = [
  ["coffee", "payee, description or memo contains it"],
  ["whole foods", "both words, anywhere"],
  ['"whole foods"', "the exact phrase"],
  ["-refund", "leave out rows that match"],
  ["payee:costco", "only the payee (also desc:, memo:)"],
  ["account:checking", "account name or path"],
  ["category:dining", "category, with its subcategories"],
  ["amount:>100", "size of the amount (also 42.10, 10..20)"],
  ["is:debit", "money out (is:credit for money in)"],
  ["date:2026-10", "a year, month or day; also 2026-01..2026-03"],
  ["status:pending", "pending, posted, expected or void"],
  ["is:needs-review", "needs review: no category, or a reconciliation adjustment"],
];

export function SearchHelp() {
  return (
    <details className="relative">
      <summary
        className="flex h-9 cursor-pointer list-none items-center rounded-md border border-border px-2 text-sm text-muted-foreground hover:text-foreground"
        aria-label="Search syntax"
        title="Search syntax"
      >
        ?
      </summary>
      <div className="absolute right-0 z-20 mt-1 w-80 max-w-[calc(100vw-2rem)] rounded-md border border-border bg-card p-3 text-sm shadow-md">
        <p className="mb-2 text-muted-foreground">
          Every part must match. Case, accents and punctuation are ignored.
        </p>
        <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
          {EXAMPLES.map(([example, meaning]) => (
            <div key={example} className="contents">
              <dt>
                <code className="text-foreground">{example}</code>
              </dt>
              <dd className="text-muted-foreground">{meaning}</dd>
            </div>
          ))}
        </dl>
      </div>
    </details>
  );
}
