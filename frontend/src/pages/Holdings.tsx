import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import type { components } from "../api/schema";
import { api, errorDetail } from "../lib/api";
import { formatDate } from "../lib/dates";
import { formatMoney } from "../lib/money";
import { cn } from "../lib/utils";
import { GainText, pctText } from "../components/HoldingsCard";
import { QueryState } from "../components/QueryState";
import { Badge, Card, CardTitle, Table, Td, Th } from "../components/ui";
import { fmtQuantity } from "./Positions";

type Portfolio = components["schemas"]["PortfolioOut"];
type PortfolioAccount = components["schemas"]["PortfolioAccountOut"];
type Line = components["schemas"]["PortfolioLineOut"];

/** Shares, Price, As of and Weight step aside at phone width. */
const WIDE = "hidden sm:table-cell";

export const portfolioQuery = {
  queryKey: ["holdings-portfolio"],
  queryFn: async (): Promise<Portfolio> => {
    const { data, error } = await api.GET("/holdings/portfolio");
    if (error || !data) throw new Error(errorDetail(error));
    return data;
  },
};

/** A line's tags. Where a price comes from is not shown (#689): a
 * SimpleFIN account is valued by the bank, any other by Market data. Only a
 * price entered by hand is tagged. */
function Flags({ line }: { line: Line }) {
  return (
    <>
      {line.manual_price && (
        <Badge className="ml-2" title="This price was entered by hand">
          manual price
        </Badge>
      )}
      {line.stale_price && (
        <Badge variant="amber" className="ml-2">
          stale
        </Badge>
      )}
      {line.no_price && (
        <Badge variant="amber" className="ml-2">
          no price
        </Badge>
      )}
      {line.unpriceable && (
        <Badge variant="amber" className="ml-2" title="The feed sent a description, not a ticker">
          not a ticker
        </Badge>
      )}
      {line.value_out_of_range && (
        <Badge variant="amber" className="ml-2">
          check the share count
        </Badge>
      )}
    </>
  );
}

function AccountHoldings({ account }: { account: PortfolioAccount }) {
  const money = (v: string | null | undefined) => formatMoney(v, { currency: account.currency });
  return (
    <Card role="region" aria-label={account.name}>
      <div className="mb-3 flex flex-wrap items-start justify-between gap-3">
        <div>
          <CardTitle className="mb-0">
            <Link to={`/accounts/${account.account_id}`} className="hover:underline">
              {account.name}
            </Link>
          </CardTitle>
          <p className="mt-0.5 text-xs text-muted-foreground">
            {account.source === "manual" ? "Entered in Purser" : "From SimpleFIN"}
            {account.as_of && ` · as of ${formatDate(account.as_of)}`}
          </p>
        </div>
        <p className="font-mono text-lg font-semibold">{money(account.total)}</p>
      </div>
      {account.lines.length === 0 ? (
        <p className="text-sm text-muted-foreground">No holdings reported for this account yet.</p>
      ) : (
        <Table>
          <thead>
            <tr>
              <Th>Holding</Th>
              <Th className={cn("text-right", WIDE)}>Shares</Th>
              <Th className={cn("text-right", WIDE)}>Price</Th>
              <Th className={WIDE}>As of</Th>
              <Th className="text-right">Value</Th>
              <Th className="text-right">Gain</Th>
              <Th className={cn("text-right", WIDE)}>Weight</Th>
            </tr>
          </thead>
          <tbody>
            {account.lines.map((line) => (
              <tr key={`${line.symbol}-${line.position_id ?? ""}`}>
                <Td>
                  <span className="font-mono">{line.symbol}</span>
                  {line.name && (
                    <span className="ml-2 hidden text-xs text-muted-foreground sm:inline">{line.name}</span>
                  )}
                  <Flags line={line} />
                </Td>
                <Td className={cn("text-right font-mono", WIDE)}>{fmtQuantity(line.quantity)}</Td>
                <Td className={cn("text-right font-mono", WIDE)}>{line.price != null ? money(line.price) : "—"}</Td>
                <Td className={cn("text-muted-foreground", WIDE)}>
                  {line.price_as_of ? formatDate(line.price_as_of) : account.as_of ? formatDate(account.as_of) : "—"}
                </Td>
                <Td className="text-right font-mono">{money(line.market_value)}</Td>
                <Td className="text-right font-mono">
                  <GainText amount={line.unrealized_gain} pct={line.gain_pct} currency={account.currency} />
                </Td>
                <Td className={cn("text-right font-mono text-muted-foreground", WIDE)}>{pctText(line.weight_pct)}</Td>
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr>
              <Td className="text-muted-foreground">Cash</Td>
              <Td className={WIDE} />
              <Td className={WIDE} />
              <Td className={WIDE} />
              <Td className="text-right font-mono">{money(account.cash)}</Td>
              <Td />
              <Td className={WIDE} />
            </tr>
          </tfoot>
        </Table>
      )}
    </Card>
  );
}

/** Every brokerage account's holdings in one place (#684): totals per
 * currency, then each open account largest first. Every figure comes from
 * the server; this only formats. */
export default function Holdings() {
  const portfolio = useQuery(portfolioQuery);
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold">Holdings</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Every brokerage account&apos;s newest holdings. SimpleFIN accounts are valued by your
          bank, and other accounts by Market data. A price entered by hand is marked{" "}
          <em>manual price</em>.
        </p>
      </div>
      <QueryState
        query={portfolio}
        what="holdings"
        isEmpty={(data) => data.accounts.length === 0}
        empty="No brokerage accounts yet."
      >
        {(data) => (
          <>
            <dl className="grid grid-cols-1 gap-3 sm:grid-cols-3">
              {data.totals.map((t) => (
                <div key={t.currency} className="contents">
                  <div className="rounded-lg bg-muted/50 px-3 py-2">
                    <dt className="text-xs text-muted-foreground">Securities{data.totals.length > 1 && ` (${t.currency})`}</dt>
                    <dd className="font-mono text-lg font-semibold">{formatMoney(t.market_value, { currency: t.currency })}</dd>
                  </div>
                  <div className="rounded-lg bg-muted/50 px-3 py-2">
                    <dt className="text-xs text-muted-foreground">Cash{data.totals.length > 1 && ` (${t.currency})`}</dt>
                    <dd className="font-mono text-lg font-semibold">{formatMoney(t.cash, { currency: t.currency })}</dd>
                  </div>
                  <div className="rounded-lg bg-muted/50 px-3 py-2">
                    <dt className="text-xs text-muted-foreground">Total{data.totals.length > 1 && ` (${t.currency})`}</dt>
                    <dd className="font-mono text-lg font-semibold">{formatMoney(t.total, { currency: t.currency })}</dd>
                  </div>
                </div>
              ))}
            </dl>
            {(data.stale_price > 0 || data.no_price > 0) && (
              <p className="text-sm" role="note">
                {data.stale_price > 0 && `${data.stale_price} holding(s) with a stale price. `}
                {data.no_price > 0 && `${data.no_price} holding(s) with no price. `}
                Prices come from the provider in{" "}
                <Link to="/settings/market-data" className="underline">
                  Settings, Market data
                </Link>
                , or are entered by hand.
              </p>
            )}
            {data.accounts.map((account) => (
              <AccountHoldings key={account.account_id} account={account} />
            ))}
          </>
        )}
      </QueryState>
    </div>
  );
}
