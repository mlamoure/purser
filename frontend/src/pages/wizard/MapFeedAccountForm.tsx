import { useId, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import type { components } from "../../api/schema";
import { Button, ErrorText, Input, Label } from "../../components/ui";
import { api, errorDetail } from "../../lib/api";
import { useStepRunner } from "./useStepRunner";

export type Discovered = components["schemas"]["DiscoveredOut"];
type MapRequest = components["schemas"]["MapRequest"];

/** Where a feed account goes: an existing Purser account, or a new one. The
 * server takes exactly one of `account_id` / `create` (MapRequest); `create`
 * carries the fields `ledger.create_account` reads (name, type, path,
 * currency). */
export type MapTarget =
  | { account_id: string }
  | { create: { name: string; type: string; path: string; currency: string } };

/** The real (non-category) account types the ledger accepts, in the order a
 * bank feed most often needs them. */
export const MAP_ACCOUNT_TYPES: [string, string][] = [
  ["checking", "Checking"],
  ["savings", "Savings"],
  ["credit_card", "Credit card"],
  ["cash", "Cash"],
  ["brokerage", "Brokerage"],
  ["loan", "Loan"],
  ["property", "Asset (house, car, …)"],
];

const LIABILITY_TYPES = new Set(["credit_card", "loan"]);

/** Ledger label for a bank account: its name (or external id) lower-cased,
 * other characters collapsed to `_`. */
export function accountLabel(d: Pick<Discovered, "name" | "external_id">): string {
  return (d.name || d.external_id)
    .toLowerCase()
    .replace(/[^a-z0-9_]+/g, "_")
    .replace(/^_+|_+$/g, "");
}

/** The path a new account of `type` named `name` gets unless the user edits
 * it: cards and loans under `liabilities.`, everything else under `assets.`. */
export function defaultPath(type: string, name: string, externalId: string): string {
  const root = LIABILITY_TYPES.has(type) ? "liabilities" : "assets";
  return `${root}.${accountLabel({ name, external_id: externalId })}`;
}

/** Map one feed account (`POST /connections/{id}/map`, admin scope). Throws
 * the server's reason; the archived transactions import on the server before
 * it answers. */
export async function mapFeedAccount(
  connectionId: string,
  externalId: string,
  target: MapTarget,
): Promise<void> {
  const body: MapRequest = { external_id: externalId, ...target };
  const { error } = await api.POST("/connections/{connection_id}/map", {
    params: { path: { connection_id: connectionId } },
    body,
  });
  if (error) throw new Error(errorDetail(error));
}

/** The form behind a feed account's Map button — the setup wizard's mapping
 * step and Settings -> Connections both render it. It defaults to a new checking
 * account named after the bank account (the wizard's original one-click
 * mapping); an existing open account in the same currency can be chosen
 * instead. `onMap` does the request (and any refresh); what it throws shows
 * here. */
export function MapFeedAccountForm({
  account,
  onMap,
  onCancel,
}: {
  account: Discovered;
  onMap: (target: MapTarget) => Promise<void>;
  onCancel: () => void;
}) {
  const id = useId();
  const { busy, error, run } = useStepRunner();
  const bankName = account.name ?? account.external_id;
  const [mode, setMode] = useState<"create" | "existing">("create");
  const [name, setName] = useState(bankName);
  const [type, setType] = useState("checking");
  const [path, setPath] = useState(defaultPath("checking", bankName, account.external_id));
  const [pathEdited, setPathEdited] = useState(false);
  const [accountId, setAccountId] = useState("");

  // Mapping onto an existing account: open real accounts in the feed's
  // currency. One already fed by another bank account is listed too (a
  // replaced card number); the server moves the feed over and audits it.
  const accounts = useQuery({
    queryKey: ["accounts", "map-targets"],
    queryFn: async () => {
      const { data, error: err } = await api.GET("/accounts", {
        params: { query: { include_hidden: true } },
      });
      if (err) throw new Error(errorDetail(err));
      return data ?? [];
    },
  });
  const candidates = (accounts.data ?? []).filter(
    (a) => !a.is_virtual && !a.closed_at && a.currency === account.currency,
  );

  const rederive = (nextType: string, nextName: string) => {
    if (!pathEdited) setPath(defaultPath(nextType, nextName, account.external_id));
  };

  const target = (): MapTarget =>
    mode === "existing"
      ? { account_id: accountId }
      : {
          create: {
            name: name.trim() || bankName,
            type,
            path: path.trim(),
            currency: account.currency,
          },
        };

  const ready = mode === "existing" ? accountId !== "" : path.trim() !== "";

  return (
    <form
      className="space-y-3 rounded-md border border-border bg-muted/30 p-3"
      aria-label={`Map ${bankName}`}
      onSubmit={(e) => {
        e.preventDefault();
        run(() => onMap(target()));
      }}
    >
      <fieldset className="flex flex-wrap gap-4 text-sm">
        <legend className="sr-only">Map to</legend>
        <label className="flex items-center gap-2">
          <input
            type="radio"
            name={`${id}-mode`}
            checked={mode === "create"}
            onChange={() => setMode("create")}
          />
          A new Purser account
        </label>
        {candidates.length > 0 && (
          <label className="flex items-center gap-2">
            <input
              type="radio"
              name={`${id}-mode`}
              checked={mode === "existing"}
              onChange={() => setMode("existing")}
            />
            An existing account
          </label>
        )}
      </fieldset>

      {mode === "create" ? (
        <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
          <div className="space-y-1">
            <Label htmlFor={`${id}-name`}>Account name</Label>
            <Input
              id={`${id}-name`}
              value={name}
              onChange={(e) => {
                setName(e.target.value);
                rederive(type, e.target.value);
              }}
            />
          </div>
          <div className="space-y-1">
            <Label htmlFor={`${id}-type`}>Account type</Label>
            <select
              id={`${id}-type`}
              className="h-9 w-full rounded-md border border-border bg-card px-2 text-sm"
              value={type}
              onChange={(e) => {
                setType(e.target.value);
                rederive(e.target.value, name);
              }}
            >
              {MAP_ACCOUNT_TYPES.map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </div>
          <div className="space-y-1">
            <Label htmlFor={`${id}-path`}>Ledger path</Label>
            <Input
              id={`${id}-path`}
              className="font-mono"
              value={path}
              onChange={(e) => {
                setPath(e.target.value);
                setPathEdited(true);
              }}
            />
          </div>
        </div>
      ) : (
        <div className="space-y-1">
          <Label htmlFor={`${id}-account`}>Purser account</Label>
          <select
            id={`${id}-account`}
            className="h-9 w-full rounded-md border border-border bg-card px-2 text-sm"
            value={accountId}
            onChange={(e) => setAccountId(e.target.value)}
          >
            <option value="">Choose an account…</option>
            {candidates.map((a) => (
              <option key={a.id} value={a.id}>
                {a.path} — {a.name}
                {a.external_id ? " (replaces its current feed)" : ""}
              </option>
            ))}
          </select>
        </div>
      )}

      <p className="text-xs text-muted-foreground">
        Transactions already fetched for this bank account import as soon as it is mapped.
      </p>
      <div className="flex gap-2">
        <Button type="submit" size="sm" disabled={busy || !ready}>
          Map account
        </Button>
        <Button type="button" size="sm" variant="ghost" disabled={busy} onClick={onCancel}>
          Cancel
        </Button>
      </div>
      <ErrorText>{error}</ErrorText>
    </form>
  );
}
