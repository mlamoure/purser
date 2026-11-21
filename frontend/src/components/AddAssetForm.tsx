import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, errorDetail } from "../lib/api";
import { useLedgerToday } from "../lib/clock";
import { Button, Card, CardTitle, ErrorText, Input } from "./ui";
import { ASSET_KINDS, AccountIcon, assetKindLabel, type AssetKind } from "./AccountIcon";

const selectClass = "h-9 rounded-md border border-border bg-card px-2 text-sm";
const PRICE = /^\d{1,16}(\.\d{1,4})?$/;

/** An ltree label for a name: "Main Street House" → "main_street_house". */
export function assetPathFor(name: string): string {
  const label = name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .slice(0, 60);
  return `assets.${label || "asset"}`;
}

/** "Add asset" (#567): a hand-valued asset (type property) with its kind,
 * purchase date and price. The price becomes money put in on the purchase
 * date (against equity.capital), so the asset starts at what was paid and
 * later changes in value are gains or losses. */
export function AddAssetForm({ onCancel }: { onCancel: () => void }) {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const today = useLedgerToday();
  const [name, setName] = useState("");
  const [kind, setKind] = useState<AssetKind>("house");
  const [path, setPath] = useState("");
  const [acquired, setAcquired] = useState("");
  const [price, setPrice] = useState("");
  const [description, setDescription] = useState("");
  const [error, setError] = useState<string | null>(null);

  const effectivePath = path.trim() || assetPathFor(name);
  const create = useMutation({
    mutationFn: async () => {
      const { data, error: err } = await api.POST("/accounts", {
        body: {
          name: name.trim(),
          type: "property",
          path: effectivePath,
          currency: "USD",
          asset_kind: kind,
          ...(acquired ? { acquired_on: acquired } : {}),
          ...(description.trim() ? { asset_description: description.trim() } : {}),
        },
      });
      if (err || !data) throw new Error(errorDetail(err));
      if (price.trim()) {
        const { error: priced } = await api.POST("/accounts/{account_id}/value-changes", {
          params: { path: { account_id: data.id } },
          body: {
            as_of: acquired || today,
            amount: price.trim(),
            kind: "capital",
            note: "Purchase price",
          },
        });
        if (priced) {
          throw new Error(
            `The asset was created, but its purchase price was not recorded: ${errorDetail(priced)}. Record it from the asset's page.`,
          );
        }
      }
      return data;
    },
    onSuccess: (data) => {
      qc.invalidateQueries({ queryKey: ["accounts"] });
      qc.invalidateQueries({ queryKey: ["networth"] });
      navigate(`/accounts/${data.id}`);
    },
    onError: (e) => {
      setError(e.message);
      qc.invalidateQueries({ queryKey: ["accounts"] });
    },
  });

  const priceOk = !price.trim() || PRICE.test(price.trim());
  const valid = name.trim().length > 0 && priceOk;
  return (
    <Card role="region" aria-label="Add asset">
      <CardTitle>Add asset</CardTitle>
      <form
        className="grid gap-3 text-sm sm:grid-cols-2"
        onSubmit={(e) => {
          e.preventDefault();
          if (valid) create.mutate();
        }}
      >
        <label className="space-y-1">
          <span className="block text-muted-foreground">Name</span>
          <Input
            aria-label="Asset name"
            placeholder="Main house"
            value={name}
            onChange={(e) => setName(e.target.value)}
            autoFocus
          />
        </label>
        <label className="space-y-1">
          <span className="block text-muted-foreground">Kind</span>
          <span className="flex items-center gap-2">
            <AccountIcon type="property" kind={kind} />
            <select
              aria-label="Asset kind"
              className={`${selectClass} flex-1`}
              value={kind}
              onChange={(e) => setKind(e.target.value as AssetKind)}
            >
              {ASSET_KINDS.map((k) => (
                <option key={k} value={k}>
                  {assetKindLabel(k)}
                </option>
              ))}
            </select>
          </span>
        </label>
        <label className="space-y-1">
          <span className="block text-muted-foreground">Purchased on (optional)</span>
          <Input
            aria-label="Purchased on"
            type="date"
            max={today}
            value={acquired}
            onChange={(e) => setAcquired(e.target.value)}
          />
        </label>
        <label className="space-y-1">
          <span className="block text-muted-foreground">Purchase price (optional)</span>
          <Input
            aria-label="Purchase price"
            className="font-mono"
            inputMode="decimal"
            placeholder="450000.00"
            value={price}
            onChange={(e) => setPrice(e.target.value)}
          />
        </label>
        <label className="space-y-1 sm:col-span-2">
          <span className="block text-muted-foreground">Description (optional, stored encrypted)</span>
          <Input
            aria-label="Asset description"
            placeholder="Address, VIN, notes"
            maxLength={500}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
          />
        </label>
        <label className="space-y-1 sm:col-span-2">
          <span className="block text-muted-foreground">Path</span>
          <Input
            aria-label="Asset path"
            className="font-mono"
            placeholder={assetPathFor(name)}
            value={path}
            onChange={(e) => setPath(e.target.value)}
          />
        </label>
        {!priceOk && (
          <p className="text-xs text-warning sm:col-span-2">Enter a price such as 450000.00.</p>
        )}
        <p className="text-xs text-muted-foreground sm:col-span-2">
          The purchase price is recorded as money put in on the purchase date. Record its value
          later from the asset's page (an appraisal or an estimate); the difference counts as a
          change in value.
        </p>
        <div className="flex gap-2 sm:col-span-2">
          <Button type="submit" size="sm" disabled={!valid || create.isPending}>
            Add asset
          </Button>
          <Button type="button" size="sm" variant="ghost" onClick={onCancel}>
            Cancel
          </Button>
        </div>
        <div className="sm:col-span-2">
          <ErrorText>{error}</ErrorText>
        </div>
      </form>
    </Card>
  );
}
