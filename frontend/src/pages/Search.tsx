import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, errorDetail } from "../lib/api";
import { useDebouncedValue } from "../lib/useDebouncedValue";
import { formatMoney } from "../lib/money";
import { formatDate } from "../lib/dates";
import { accountNamesQuery, usePathNames } from "../lib/names";
import { Badge, Button, Card, Input, Table, Td, Th } from "../components/ui";
import { QueryState } from "../components/QueryState";
import { SearchHelp } from "../components/SearchHelp";

const PAGE_SIZE = 50;
// As the register's box: one search once typing pauses (#141).
const SEARCH_DEBOUNCE_MS = 250;

/** Search every account at once (#141): the register's search syntax, with
 * the query in the address so a search can be bookmarked or shared. */
export default function Search() {
  const [params, setParams] = useSearchParams();
  const [q, setQ] = useState(params.get("q") ?? "");
  const offset = Number(params.get("offset") ?? "0") || 0;
  const searchQ = useDebouncedValue(q.trim(), SEARCH_DEBOUNCE_MS);
  const names = usePathNames();
  const accounts = useQuery(accountNamesQuery);
  const accountIds = useMemo(
    () => new Map((accounts.data ?? []).map((a) => [a.path, a.id])),
    [accounts.data],
  );

  useEffect(() => {
    // The address follows the debounced query (a new query starts at page 1).
    const current = params.get("q") ?? "";
    if (searchQ !== current) {
      setParams(searchQ ? { q: searchQ } : {}, { replace: true });
    }
  }, [searchQ, params, setParams]);

  const page = useQuery({
    queryKey: ["search", searchQ, offset],
    enabled: searchQ !== "",
    queryFn: async () => {
      const { data, error } = await api.GET("/transactions", {
        params: { query: { q: searchQ, limit: PAGE_SIZE, offset } },
      });
      if (error) throw new Error(errorDetail(error));
      return data;
    },
  });

  const goTo = (next: number) =>
    setParams(next > 0 ? { q: searchQ, offset: String(next) } : { q: searchQ });

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-semibold">Search</h1>
      <div className="flex gap-1">
        <Input
          autoFocus
          aria-label="Search all accounts"
          placeholder="Search payee, description, memo… across every account"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
        <SearchHelp />
      </div>
      {searchQ === "" ? (
        <p className="text-sm text-muted-foreground">
          Type to search every account. Every word must match; open ? for the syntax.
        </p>
      ) : (
        <QueryState
          query={page}
          what="results"
          isEmpty={(data) => data.items.length === 0}
          empty={
            <p className="text-sm text-muted-foreground">
              No transactions match.
              {page.data?.did_you_mean && (
                <>
                  {" "}
                  Did you mean{" "}
                  <button
                    type="button"
                    className="font-medium text-primary underline"
                    onClick={() => setQ(page.data?.did_you_mean ?? "")}
                  >
                    {page.data.did_you_mean}
                  </button>
                  ?
                </>
              )}
            </p>
          }
        >
          {(data) => (
            <Card className="space-y-3 overflow-x-auto p-0">
              <p className="px-4 pt-3 text-sm text-muted-foreground" role="status">
                {data.total === 1 ? "1 transaction" : `${data.total} transactions`}
              </p>
              <Table>
                <thead>
                  <tr>
                    <Th>Date</Th>
                    <Th>Account</Th>
                    <Th>Payee</Th>
                    <Th>Description</Th>
                    <Th>Category</Th>
                    <Th className="text-right">Amount</Th>
                  </tr>
                </thead>
                <tbody>
                  {data.items.map((t) => {
                    const accountId = t.account_path ? accountIds.get(t.account_path) : undefined;
                    return (
                      <tr key={t.id}>
                        <Td className="whitespace-nowrap">{formatDate(t.date)}</Td>
                        <Td>
                          {accountId ? (
                            <Link className="text-primary hover:underline" to={`/accounts/${accountId}`}>
                              {names(t.account_path)}
                            </Link>
                          ) : (
                            names(t.account_path)
                          )}
                        </Td>
                        <Td>{t.payee ?? "—"}</Td>
                        <Td>{t.description ?? "—"}</Td>
                        <Td>
                          {t.is_transfer
                            ? `Transfer · ${names(t.transfer_account_path)}`
                            : t.split_lines
                              ? `Split (${t.split_lines})`
                              : names(t.category_path)}
                        </Td>
                        <Td className="whitespace-nowrap text-right tabular-nums">
                          {formatMoney(t.amount)}
                          {t.status !== "posted" && (
                            <Badge className="ml-2">
                              {t.status}
                            </Badge>
                          )}
                        </Td>
                      </tr>
                    );
                  })}
                </tbody>
              </Table>
              {data.total > PAGE_SIZE && (
                <div className="flex items-center justify-between px-4 pb-3 text-sm">
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={offset === 0}
                    onClick={() => goTo(Math.max(0, offset - PAGE_SIZE))}
                  >
                    Previous
                  </Button>
                  <span className="text-muted-foreground">
                    {offset + 1}–{Math.min(offset + PAGE_SIZE, data.total)} of {data.total}
                  </span>
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={offset + PAGE_SIZE >= data.total}
                    onClick={() => goTo(offset + PAGE_SIZE)}
                  >
                    Next
                  </Button>
                </div>
              )}
            </Card>
          )}
        </QueryState>
      )}
    </div>
  );
}
