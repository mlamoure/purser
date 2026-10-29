import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, errorDetail } from "../lib/api";
import { fmtAmount } from "../lib/utils";
import { Badge, Button, Card, CardTitle, ErrorText, Input, Table, Td, Th } from "../components/ui";
import type { components } from "../api/schema";

type PositionOut = components["schemas"]["PositionOut"];

/** Today in UTC: the server refuses an event dated after it (issue #390). */
export const utcToday = () => new Date().toISOString().slice(0, 10);

/** A quantity or price without trailing zeros, as a string (no float). */
export function fmtQuantity(value: string | null | undefined): string {
  if (value == null) return "—";
  return value.includes(".") ? value.replace(/0+$/, "").replace(/\.$/, "") : value;
}

/** The Positions tab of a brokerage account whose holdings are tracked by
 * hand (issue #390, decision 88): each position valued at the newest close,
 * an inline quantity editor (a `set_quantity` event), and "+ position". */
export default function PositionsPanel({ accountId }: { accountId: string }) {
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
  };
  const rows = positions.data ?? [];

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
            <PositionRow key={p.id} p={p} onChange={refresh} />
          ))}
        </tbody>
      </Table>
      {positions.error && <ErrorText>{positions.error.message}</ErrorText>}
      {positions.data && rows.length === 0 && (
        <p className="mt-2 text-sm text-muted-foreground">
          No positions yet. Add one with + position.
        </p>
      )}
      <p className="mt-3 text-xs text-muted-foreground">
        Each position is valued at the newest close for its symbol: a price you record, the
        daily provider fetch (Config, Prices), or a price implied by a feed snapshot of the
        same symbol. A position with no price yet adds nothing to the balance, and neither does
        one whose value at its close is too large to store: check its share count.
      </p>
    </Card>
  );
}

function PositionRow({ p, onChange }: { p: PositionOut; onChange: () => void }) {
  const [editing, setEditing] = useState(false);
  const [quantity, setQuantity] = useState(fmtQuantity(p.quantity));
  const [asOf, setAsOf] = useState(utcToday());
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
          occurred_on: asOf,
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
                max={utcToday()}
                value={asOf}
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
            <span title={`${p.price_source ?? ""} close of ${p.price_as_of ?? ""}`}>
              {fmtQuantity(p.price)}
              <span className="ml-1 text-xs text-muted-foreground">{p.price_as_of}</span>
            </span>
          ) : (
            "—"
          )}
        </Td>
        <Td className="text-right font-mono">
          {p.market_value != null ? (
            fmtAmount(p.market_value)
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
          {p.unrealized_gain != null ? fmtAmount(p.unrealized_gain) : "—"}
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
                  setAsOf(utcToday());
                  setCostBasis("");
                  setEditing(true);
                }}
              >
                Set quantity
              </Button>
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
  const [asOf, setAsOf] = useState(utcToday());
  const [costBasis, setCostBasis] = useState("");
  const [error, setError] = useState<string | null>(null);
  const create = useMutation({
    mutationFn: async () => {
      const { error: err } = await api.POST("/accounts/{account_id}/positions", {
        params: { path: { account_id: accountId } },
        body: {
          symbol: symbol.trim(),
          quantity: quantity.trim(),
          as_of: asOf,
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
          max={utcToday()}
          value={asOf}
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
