import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, errorDetail } from "../lib/api";
import { formatMoney } from "../lib/money";
import { formatDate } from "../lib/dates";
import { useLedgerToday } from "../lib/clock";
import { Badge, Button, Card, CardTitle, ErrorText, Input, Table, Td, Th } from "../components/ui";
import { QueryState } from "../components/QueryState";
import type { components } from "../api/schema";

type PositionOut = components["schemas"]["PositionOut"];

/** A quantity or price without trailing zeros, as a string (no float). */
export function fmtQuantity(value: string | null | undefined): string {
  if (value == null) return "—";
  return value.includes(".") ? value.replace(/0+$/, "").replace(/\.$/, "") : value;
}

/** The Positions tab of a brokerage account whose holdings are tracked by
 * hand (issue #390, decision 88): each position valued at the newest close,
 * an inline quantity editor (a `set_quantity` event), "Record split" (a
 * `split` event, #624) and "+ position". */
export default function PositionsPanel({
  accountId,
  currency,
}: {
  accountId: string;
  /** The account's currency, which its positions are valued in. */
  currency?: string | null;
}) {
  const qc = useQueryClient();
  const [adding, setAdding] = useState(false);
  const positions = useQuery({
    queryKey: ["positions", accountId],
    queryFn: async () => {
      const { data, error } = await api.GET("/accounts/{account_id}/positions", {
        params: { path: { account_id: accountId } },
      });
      if (error) throw new Error(errorDetail(error));
      return data;
    },
  });
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["positions", accountId] });
    qc.invalidateQueries({ queryKey: ["account", accountId] });
    // The holdings table and the balance chart read the same positions (#595).
    qc.invalidateQueries({ queryKey: ["holdings-summary", accountId] });
    qc.invalidateQueries({ queryKey: ["balance-history", accountId] });
  };

  return (
    <Card>
      <div className="mb-4 flex items-center justify-between gap-4">
        <CardTitle className="mb-0">Positions</CardTitle>
        {!adding && (
          <Button size="sm" variant="outline" onClick={() => setAdding(true)}>
            + position
          </Button>
        )}
      </div>
      {adding && (
        <AddPosition
          accountId={accountId}
          onDone={() => {
            setAdding(false);
            refresh();
          }}
          onCancel={() => setAdding(false)}
        />
      )}
      <QueryState
        query={positions}
        what="positions"
        empty="No positions yet. Add one with + position."
      >
        {(rows) => (
          <Table>
            <thead>
              <tr>
                <Th>Symbol</Th>
                <Th className="text-right">Quantity</Th>
                <Th className="text-right">Price</Th>
                <Th className="text-right">Value</Th>
                <Th className="text-right">Gain</Th>
                <Th>Actions</Th>
              </tr>
            </thead>
            <tbody>
              {rows.map((p) => (
                <PositionRow key={p.id} p={p} currency={currency} onChange={refresh} />
              ))}
            </tbody>
          </Table>
        )}
      </QueryState>
      <p className="mt-3 text-xs text-muted-foreground">
        Each position is valued at the newest close for its symbol: a price you record, the
        daily provider fetch (Settings, Market data), or a price implied by a feed snapshot of the
        same symbol. A position with no price yet adds nothing to the balance, and neither does
        one whose value at its close is too large to store: check its share count.
      </p>
    </Card>
  );
}

function PositionRow({
  p,
  currency,
  onChange,
}: {
  p: PositionOut;
  currency?: string | null;
  onChange: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [splitting, setSplitting] = useState(false);
  const [quantity, setQuantity] = useState(fmtQuantity(p.quantity));
  const today = useLedgerToday();
  // Empty means "today": the ledger day may arrive after the form opens.
  const [asOf, setAsOf] = useState("");
  // The basis is sent only when typed: without one, the server keeps the
  // basis for a re-count of the same count, makes it unknown for a changed
  // count, and zero for 0 shares. The current basis is a hint, not a value
  // (prefilled, it would ride along with a sell-out or a backdated count).
  const [costBasis, setCostBasis] = useState("");
  const basisHint = p.cost_basis != null ? `Basis (now ${fmtQuantity(p.cost_basis)})` : "Basis (unknown)";
  const [error, setError] = useState<string | null>(null);
  const save = useMutation({
    mutationFn: async () => {
      const { error: err } = await api.POST("/positions/{position_id}/events", {
        params: { path: { position_id: p.id } },
        body: {
          kind: "set_quantity",
          occurred_on: asOf || today,
          quantity: quantity.trim(),
          ...(costBasis.trim() ? { cost_basis: costBasis.trim() } : {}),
        },
      });
      if (err) throw new Error(errorDetail(err));
    },
    onSuccess: () => {
      setEditing(false);
      setError(null);
      onChange();
    },
    onError: (e) => setError(e.message),
  });
  // A position sold down to nothing can be closed: it then leaves this list
  // and stays as read-only history (the server refuses while it holds shares).
  // (A string test, like fmtQuantity: no float near a quantity.)
  const holdsNothing = p.closed_at == null && /^0+(\.0*)?$/.test(p.quantity ?? "");
  const close = useMutation({
    mutationFn: async () => {
      const { error: err } = await api.PATCH("/positions/{position_id}", {
        params: { path: { position_id: p.id } },
        body: { closed: true },
      });
      if (err) throw new Error(errorDetail(err));
    },
    onSuccess: () => {
      setError(null);
      onChange();
    },
    onError: (e) => setError(e.message),
  });

  return (
    <>
      <tr>
        <Td>
          <span className="font-mono font-medium">{p.symbol}</span>
          {p.name && <span className="ml-2 text-xs text-muted-foreground">{p.name}</span>}
        </Td>
        <Td className="text-right font-mono">
          {editing ? (
            <span className="flex justify-end gap-1">
              <Input
                aria-label={`Quantity of ${p.symbol}`}
                className="w-28"
                value={quantity}
                onChange={(e) => setQuantity(e.target.value)}
              />
              <Input
                aria-label={`As of for ${p.symbol}`}
                type="date"
                className="w-36"
                max={today}
                value={asOf || today}
                onChange={(e) => setAsOf(e.target.value)}
              />
              <Input
                aria-label={`Cost basis of ${p.symbol} (total, optional)`}
                title="The total cost basis of the shares after this count. Left empty, a re-count of the same number of shares keeps the basis, a new count makes it unknown, and 0 shares have none."
                className="w-36"
                placeholder={basisHint}
                value={costBasis}
                onChange={(e) => setCostBasis(e.target.value)}
              />
            </span>
          ) : (
            fmtQuantity(p.quantity)
          )}
        </Td>
        <Td className="text-right font-mono">
          {p.price ? (
            <span title={`${p.price_source ?? ""} close of ${formatDate(p.price_as_of)}`}>
              {fmtQuantity(p.price)}
              <span className="ml-1 text-xs text-muted-foreground">{formatDate(p.price_as_of)}</span>
            </span>
          ) : (
            "—"
          )}
        </Td>
        <Td className="text-right font-mono">
          {p.market_value != null ? (
            formatMoney(p.market_value, { currency })
          ) : p.value_out_of_range ? (
            <Badge
              variant="amber"
              title="At this close the value is too large to store, so it adds nothing to the balance. Check the share count."
            >
              check the share count
            </Badge>
          ) : (
            <Badge variant="amber">no price</Badge>
          )}
          {p.stale_price && (
            <Badge variant="amber" className="ml-1" title="The newest close is older than the staleness threshold">
              stale
            </Badge>
          )}
        </Td>
        <Td className="text-right font-mono text-muted-foreground">
          {p.unrealized_gain != null ? formatMoney(p.unrealized_gain, { currency }) : "—"}
        </Td>
        <Td>
          {editing ? (
            <span className="flex gap-1">
              <Button size="sm" disabled={save.isPending || !quantity.trim()} onClick={() => save.mutate()}>
                Save
              </Button>
              <Button size="sm" variant="ghost" onClick={() => setEditing(false)}>
                Cancel
              </Button>
            </span>
          ) : (
            <span className="flex gap-1">
              <Button
                size="sm"
                variant="outline"
                title="Record the share count on a date (a set_quantity event)"
                onClick={() => {
                  setQuantity(fmtQuantity(p.quantity));
                  setAsOf("");
                  setCostBasis("");
                  setSplitting(false);
                  setEditing(true);
                }}
              >
                Set quantity
              </Button>
              {p.closed_at == null && !holdsNothing && (
                <Button
                  size="sm"
                  variant="ghost"
                  aria-label={`Record split of ${p.symbol}`}
                  title="Record a stock split: every share becomes a ratio of shares on its effective date"
                  onClick={() => {
                    setEditing(false);
                    setSplitting(true);
                  }}
                >
                  Record split
                </Button>
              )}
              {holdsNothing && (
                <Button
                  size="sm"
                  variant="ghost"
                  aria-label={`Close ${p.symbol}`}
                  title="This position holds nothing: close it, keeping its history read-only"
                  disabled={close.isPending}
                  onClick={() => close.mutate()}
                >
                  Close
                </Button>
              )}
            </span>
          )}
        </Td>
      </tr>
      {splitting && (
        <tr>
          <Td colSpan={6}>
            <RecordSplit
              p={p}
              onDone={() => {
                setSplitting(false);
                onChange();
              }}
              onCancel={() => setSplitting(false)}
            />
          </Td>
        </tr>
      )}
      {error && (
        <tr>
          <Td colSpan={6}>
            <ErrorText>{error}</ErrorText>
          </Td>
        </tr>
      )}
    </>
  );
}

const selectClass = "h-9 rounded-md border border-border bg-card px-2 text-sm";

/** A stock split (#624, decision 127): new shares for old, effective on the
 * first day the shares trade split. Every share held the day before becomes
 * that ratio; each lot keeps its total basis and term. The broker's cash for
 * a fractional share, when it paid any, is recorded with the split. */
function RecordSplit({
  p,
  onDone,
  onCancel,
}: {
  p: PositionOut;
  onDone: () => void;
  onCancel: () => void;
}) {
  const today = useLedgerToday();
  const [numerator, setNumerator] = useState("");
  const [denominator, setDenominator] = useState("1");
  const [effective, setEffective] = useState(""); // empty means "today"
  const [cashInLieu, setCashInLieu] = useState("");
  const [lotMethod, setLotMethod] = useState("fifo");
  const [error, setError] = useState<string | null>(null);
  const whole = (v: string) => /^[1-9][0-9]*$/.test(v.trim());
  const record = useMutation({
    mutationFn: async () => {
      const lieu = cashInLieu.trim();
      const { error: err } = await api.POST("/positions/{position_id}/events", {
        params: { path: { position_id: p.id } },
        body: {
          kind: "split",
          occurred_on: effective || today,
          split_numerator: Number(numerator.trim()),
          split_denominator: Number(denominator.trim()),
          ...(lieu ? { cash_in_lieu: lieu } : {}),
          ...(lieu && p.tracks_lots ? { lot_method: lotMethod as "fifo" | "lifo" | "hifo" } : {}),
        },
      });
      if (err) throw new Error(errorDetail(err));
    },
    onSuccess: () => {
      setError(null);
      onDone();
    },
    onError: (e) => setError(e.message),
  });

  return (
    <div className="space-y-2 rounded-md border border-border p-3">
      <div className="flex flex-wrap items-end gap-2">
        <Input
          aria-label={`New shares for ${p.symbol}`}
          className="w-24"
          inputMode="numeric"
          placeholder="New"
          value={numerator}
          onChange={(e) => setNumerator(e.target.value)}
        />
        <span className="pb-2 text-sm text-muted-foreground">for</span>
        <Input
          aria-label={`Old shares for ${p.symbol}`}
          className="w-24"
          inputMode="numeric"
          placeholder="Old"
          value={denominator}
          onChange={(e) => setDenominator(e.target.value)}
        />
        <Input
          aria-label={`Split effective date for ${p.symbol}`}
          type="date"
          className="w-40"
          max={today}
          value={effective || today}
          onChange={(e) => setEffective(e.target.value)}
        />
        <Input
          aria-label={`Cash in lieu for ${p.symbol} (optional)`}
          title="The cash the broker paid for the fractional share the split left, if any"
          className="w-40"
          placeholder="Cash in lieu (optional)"
          value={cashInLieu}
          onChange={(e) => setCashInLieu(e.target.value)}
        />
        {p.tracks_lots && cashInLieu.trim() && (
          <select
            aria-label="Lots the fractional share comes from"
            className={selectClass}
            value={lotMethod}
            onChange={(e) => setLotMethod(e.target.value)}
          >
            <option value="fifo">First in, first out</option>
            <option value="lifo">Last in, first out</option>
            <option value="hifo">Highest basis first</option>
          </select>
        )}
        <Button
          size="sm"
          disabled={record.isPending || !whole(numerator) || !whole(denominator)}
          onClick={() => record.mutate()}
        >
          Record split
        </Button>
        <Button size="sm" variant="ghost" onClick={onCancel}>
          Cancel
        </Button>
      </div>
      <p className="text-xs text-muted-foreground">
        2 new for 1 old is a 2-for-1 split; 1 new for 10 old is a reverse split. The effective
        date is the first day the shares trade split. Each lot keeps its total cost basis and
        holding period; a wrong split is undone by reversing it.
      </p>
      <ErrorText>{error}</ErrorText>
    </div>
  );
}

function AddPosition({
  accountId,
  onDone,
  onCancel,
}: {
  accountId: string;
  onDone: () => void;
  onCancel: () => void;
}) {
  const [symbol, setSymbol] = useState("");
  const [quantity, setQuantity] = useState("");
  const today = useLedgerToday();
  const [asOf, setAsOf] = useState(""); // empty means "today"
  const [costBasis, setCostBasis] = useState("");
  const [error, setError] = useState<string | null>(null);
  const create = useMutation({
    mutationFn: async () => {
      const { error: err } = await api.POST("/accounts/{account_id}/positions", {
        params: { path: { account_id: accountId } },
        body: {
          symbol: symbol.trim(),
          quantity: quantity.trim(),
          as_of: asOf || today,
          ...(costBasis.trim() ? { cost_basis: costBasis.trim() } : {}),
        },
      });
      if (err) throw new Error(errorDetail(err));
    },
    onSuccess: onDone,
    onError: (e) => setError(e.message),
  });

  return (
    <div className="mb-4 space-y-2 rounded-md border border-border p-3">
      <div className="flex flex-wrap items-end gap-2">
        <Input
          aria-label="Symbol"
          className="w-28"
          placeholder="Symbol"
          value={symbol}
          onChange={(e) => setSymbol(e.target.value)}
        />
        <Input
          aria-label="Quantity"
          className="w-28"
          placeholder="Quantity"
          value={quantity}
          onChange={(e) => setQuantity(e.target.value)}
        />
        <Input
          aria-label="As of"
          type="date"
          className="w-40"
          max={today}
          value={asOf || today}
          onChange={(e) => setAsOf(e.target.value)}
        />
        <Input
          aria-label="Cost basis (total, optional)"
          className="w-40"
          placeholder="Cost basis (optional)"
          value={costBasis}
          onChange={(e) => setCostBasis(e.target.value)}
        />
        <Button
          size="sm"
          disabled={create.isPending || !symbol.trim() || !quantity.trim()}
          onClick={() => create.mutate()}
        >
          Add
        </Button>
        <Button size="sm" variant="ghost" onClick={onCancel}>
          Cancel
        </Button>
      </div>
      <ErrorText>{error}</ErrorText>
    </div>
  );
}
