import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ArrowDown, ArrowUp } from "lucide-react";
import type { components } from "../api/schema";
import { api, errorDetail } from "../lib/api";
import { formatDate } from "../lib/dates";
import { compareMoney, formatMoney, moneySign } from "../lib/money";
import { plotCoordinate } from "../lib/plot";
import { cn } from "../lib/utils";
import { fmtQuantity } from "../pages/Positions";
import { QueryState } from "./QueryState";
import { Badge, Button, Card, CardTitle, Table, Td, Th } from "./ui";

type Summary = components["schemas"]["HoldingsSummaryOut"];
type Line = components["schemas"]["HoldingsSummaryLineOut"];

type SortKey = "symbol" | "quantity" | "price" | "market_value" | "unrealized_gain" | "weight_pct";

const COLUMNS: { key: SortKey; label: string; numeric: boolean; wide?: boolean }[] = [
  { key: "symbol", label: "Holding", numeric: false },
  { key: "quantity", label: "Shares", numeric: true, wide: true },
  { key: "price", label: "Price", numeric: true, wide: true },
  { key: "market_value", label: "Value", numeric: true },
  { key: "unrealized_gain", label: "Gain", numeric: true },
  { key: "weight_pct", label: "Weight", numeric: true, wide: true },
];

/** Shares, Price and Weight step aside at phone width (the bar above shows
 * the weights), leaving each holding's value and gain. */
const WIDE = "hidden sm:table-cell";

/** "12.50" -> "12.5%", "-3.00" -> "-3%": a percent the API sends at two places. */
export function pctText(value: string | null | undefined, { sign = false } = {}): string {
  if (value == null) return "—";
  const trimmed = value.includes(".") ? value.replace(/0+$/, "").replace(/\.$/, "") : value;
  return `${sign && moneySign(value) > 0 ? "+" : ""}${trimmed}%`;
}

/** Lines in the chosen order; a missing value sorts last either way. */
export function sortLines(lines: Line[], key: SortKey, descending: boolean): Line[] {
  const direction = descending ? -1 : 1;
  return [...lines].sort((a, b) => {
    if (key === "symbol") return direction * a.symbol.localeCompare(b.symbol);
    const x = a[key];
    const y = b[key];
    if (x == null || y == null) return x == null ? (y == null ? 0 : 1) : -1;
    return direction * compareMoney(x, y) || a.symbol.localeCompare(b.symbol);
  });
}

export function GainText({ amount, pct, currency }: { amount: string | null; pct: string | null; currency: string }) {
  if (amount == null) return <span className="text-muted-foreground">—</span>;
  const sign = moneySign(amount);
  return (
    <span className={cn(sign > 0 && "text-success", sign < 0 && "text-destructive")}>
      {formatMoney(amount, { currency, signDisplay: "always" })}
      {pct != null && (
        <span className="ml-1 hidden text-xs opacity-80 sm:inline">{pctText(pct, { sign: true })}</span>
      )}
    </span>
  );
}

/** A brokerage account's holdings (#595), from SimpleFIN or entered in Purser:
 * the market value, unrealized gain and cash up top, an allocation bar, and
 * a sortable table with a cash row and the total, which is the account's
 * balance. Every figure comes from the server; this only formats strings.
 * An account whose holdings are entered in Purser edits its positions on the Positions tab, which
 * `onEditPositions` opens. */
export function HoldingsCard({
  accountId,
  currency,
  onEditPositions,
}: {
  accountId: string;
  currency: string;
  onEditPositions?: () => void;
}) {
  const [sort, setSort] = useState<{ key: SortKey; descending: boolean }>({
    key: "market_value",
    descending: true,
  });
  const summary = useQuery({
    queryKey: ["holdings-summary", accountId],
    queryFn: async () => {
      const { data, error } = await api.GET("/accounts/{account_id}/holdings/summary", {
        params: { path: { account_id: accountId } },
      });
      if (error) throw new Error(errorDetail(error));
      return data as Summary;
    },
  });
  const money = (v: string | null | undefined) => formatMoney(v, { currency });
  const toggle = (key: SortKey) =>
    setSort((s) => (s.key === key ? { key, descending: !s.descending } : { key, descending: key !== "symbol" }));

  return (
    <Card role="region" aria-label="Holdings">
      <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
        <div>
          <CardTitle className="mb-0">Holdings</CardTitle>
          {summary.data?.as_of && (
            <p className="mt-0.5 text-xs text-muted-foreground">
              As of {formatDate(summary.data.as_of)} ·{" "}
              {summary.data.source === "manual" ? "entered in Purser" : "from SimpleFIN"}
              {summary.data.closed && " · account closed"}
            </p>
          )}
        </div>
        {onEditPositions && (
          <Button size="sm" variant="outline" onClick={onEditPositions}>
            Edit positions
          </Button>
        )}
      </div>
      <QueryState
        query={summary}
        what="holdings"
        isEmpty={(data) => data.lines.length === 0}
        empty="No holdings reported for this account yet."
      >
        {(data) => (
          <>
            <dl className="mb-4 grid grid-cols-3 gap-2 sm:gap-3">
              <div className="min-w-0 rounded-lg bg-muted/50 px-2 py-2 sm:px-3">
                <dt className="text-xs text-muted-foreground">Securities</dt>
                <dd className="font-mono text-sm font-semibold sm:text-lg">{money(data.market_value)}</dd>
              </div>
              <div className="min-w-0 rounded-lg bg-muted/50 px-2 py-2 sm:px-3">
                <dt className="text-xs text-muted-foreground">Unrealized gain</dt>
                <dd className="font-mono text-sm font-semibold sm:text-lg">
                  <GainText amount={data.unrealized_gain} pct={data.gain_pct} currency={currency} />
                </dd>
              </div>
              <div className="min-w-0 rounded-lg bg-muted/50 px-2 py-2 sm:px-3">
                <dt className="text-xs text-muted-foreground">Cash</dt>
                <dd className="font-mono text-sm font-semibold sm:text-lg">{money(data.cash)}</dd>
              </div>
            </dl>

            <AllocationBar data={data} />

            <Table>
              <thead>
                <tr>
                  {COLUMNS.map((c) => (
                    <Th
                      key={c.key}
                      className={cn(c.numeric && "text-right", c.wide && WIDE)}
                      aria-sort={sort.key === c.key ? (sort.descending ? "descending" : "ascending") : "none"}
                    >
                      <button
                        type="button"
                        onClick={() => toggle(c.key)}
                        className={cn(
                          "inline-flex items-center gap-1 hover:text-foreground",
                          sort.key === c.key && "text-foreground",
                        )}
                      >
                        {c.label}
                        {sort.key === c.key &&
                          (sort.descending ? (
                            <ArrowDown aria-hidden="true" className="h-3 w-3" />
                          ) : (
                            <ArrowUp aria-hidden="true" className="h-3 w-3" />
                          ))}
                      </button>
                    </Th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {sortLines(data.lines, sort.key, sort.descending).map((line) => (
                  <HoldingRow key={`${line.symbol}-${line.position_id ?? ""}`} line={line} currency={currency} />
                ))}
              </tbody>
              <tfoot>
                <tr>
                  <Td className="text-muted-foreground">Cash</Td>
                  <Td className={WIDE} />
                  <Td className={WIDE} />
                  <Td className="text-right font-mono">{money(data.cash)}</Td>
                  <Td />
                  <Td className={cn("text-right font-mono text-muted-foreground", WIDE)}>
                    {pctText(data.cash_weight_pct)}
                  </Td>
                </tr>
                <tr className="font-semibold">
                  <Td>Total</Td>
                  <Td className={WIDE} />
                  <Td className={WIDE} />
                  <Td className="text-right font-mono">{money(data.total)}</Td>
                  <Td className="text-right font-mono">
                    <GainText amount={data.unrealized_gain} pct={data.gain_pct} currency={currency} />
                  </Td>
                  <Td className={WIDE} />
                </tr>
              </tfoot>
            </Table>
          </>
        )}
      </QueryState>
    </Card>
  );
}

function HoldingRow({ line, currency }: { line: Line; currency: string }) {
  const money = (v: string | null | undefined) => formatMoney(v, { currency });
  return (
    <tr className="hover:bg-muted/50">
      <Td>
        <div className="font-medium">{line.symbol}</div>
        {line.name && <div className="text-xs text-muted-foreground">{line.name}</div>}
        <div className="mt-0.5 flex flex-wrap gap-1">
          {line.stale_price && <Badge variant="amber">stale price</Badge>}
          {line.no_price && <Badge variant="amber">no price</Badge>}
          {line.unpriceable && <Badge variant="amber">no ticker</Badge>}
          {line.value_out_of_range && <Badge variant="amber">value too large: check shares</Badge>}
          {line.in_transit && <Badge>{`${money(line.in_transit)} in transit`}</Badge>}
        </div>
      </Td>
      <Td className={cn("text-right font-mono", WIDE)}>{fmtQuantity(line.quantity)}</Td>
      <Td className={cn("text-right font-mono", WIDE)}>
        {line.price != null ? money(line.price) : "—"}
        {line.price_as_of && <div className="text-xs text-muted-foreground">{formatDate(line.price_as_of)}</div>}
      </Td>
      <Td className="text-right font-mono">{money(line.market_value)}</Td>
      <Td className="text-right font-mono">
        <GainText amount={line.unrealized_gain} pct={line.gain_pct} currency={currency} />
      </Td>
      <Td className={cn("text-right font-mono text-muted-foreground", WIDE)}>{pctText(line.weight_pct)}</Td>
    </tr>
  );
}

/** Each holding's share of the account as one segmented bar, largest first,
 * cash last in the muted colour. Widths are pixel geometry only (plot.ts);
 * the labels show the API's strings. */
function AllocationBar({ data }: { data: Summary }) {
  const lines = data.lines.filter((l) => l.weight_pct != null && moneySign(l.weight_pct) > 0);
  const cash = data.cash_weight_pct != null && moneySign(data.cash_weight_pct) > 0 ? data.cash_weight_pct : null;
  if (lines.length === 0 && cash == null) return null;
  // Successive holdings fade so neighbours stay distinct without a palette.
  const opacity = (i: number) => Math.max(0.25, 1 - i * 0.25);
  return (
    <div className="mb-4">
      <div className="flex h-2.5 w-full overflow-hidden rounded-full bg-muted" aria-hidden="true">
        {lines.map((l, i) => (
          <div
            key={`${l.symbol}-${l.position_id ?? ""}`}
            className="h-full border-r-2 border-card bg-primary last:border-r-0"
            style={{ width: `${plotCoordinate(l.weight_pct)}%`, opacity: opacity(i) }}
            title={`${l.symbol} ${pctText(l.weight_pct)}`}
          />
        ))}
        {cash && (
          <div className="h-full bg-muted-foreground/40" style={{ width: `${plotCoordinate(cash)}%` }} title={`Cash ${pctText(cash)}`} />
        )}
      </div>
      <ul className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
        {lines.slice(0, 6).map((l, i) => (
          <li key={`${l.symbol}-${l.position_id ?? ""}`} className="flex items-center gap-1.5">
            <span className="inline-block h-2 w-2 rounded-full bg-primary" style={{ opacity: opacity(i) }} />
            {l.symbol} {pctText(l.weight_pct)}
          </li>
        ))}
        {cash && (
          <li className="flex items-center gap-1.5">
            <span className="inline-block h-2 w-2 rounded-full bg-muted-foreground/40" />
            Cash {pctText(cash)}
          </li>
        )}
      </ul>
    </div>
  );
}
