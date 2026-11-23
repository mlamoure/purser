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
export type MapTarget = (
  | { account_id: string }
  | { create: { name: string; type: string; path: string; currency: string } }
) & {
  /** Start the account from the bank's reported balance (decision 123). */
  starting_balance?: boolean;
};

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

/** The account types that can start from the bank's reported balance: the
 * ones whose balance is the sum of their transactions (decision 123). A
 * brokerage account's balance includes its holdings, and a loan or an asset
 * is valued, so the option is not offered for them. */
export const STARTING_BALANCE_TYPES = new Set(["checking", "savings", "credit_card", "cash"]);

/** The account type a bank account's name suggests. Banks name accounts
 * after what they are ("Online Savings", "Rewards Visa", "Auto Loan"), and
 * the feed carries no type, so the form starts from this guess and the person
 * corrects it. The words a bank uses for the product win over a brand name:
 * "Discover Online Savings" is savings. Anything unrecognised is checking,
 * the commonest feed account. */
export function guessAccountType(name: string): string {
  const n = name.toLowerCase();
  if (/\b(checking|chequing)\b/.test(n)) return "checking";
  if (/\b(savings?|money market)\b/.test(n)) return "savings";
  if (/\b(mortgage|loan|heloc|line of credit)\b/.test(n)) return "loan";
  if (/\b(brokerage|investments?|investing|ira|roth|401k|401\(k\)|403b|retirement|securities)/.test(n)) return "brokerage";
  if (/\b(credit|card|visa|mastercard|amex|american express)\b/.test(n)) return "credit_card";
  return "checking";
}

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
  const body: MapRequest = { external_id: externalId, starting_balance: false, ...target };
  const { error } = await api.POST("/connections/{connection_id}/map", {
    params: { path: { connection_id: connectionId } },
    body,
  });
  if (error) throw new Error(errorDetail(error));
}

/** Why Purser proposes a type, in words (ProposalOut.type_basis). The feed
 * carries no account type, so the form says what the proposal rests on. */
export const TYPE_BASIS_TEXT: Record<string, string> = {
  holdings: "Proposed because the bank lists holdings for this account.",
  name: "Proposed from the bank's name for this account.",
  balance: "Proposed because the account owes money; the bank's name does not say what it is.",
  default: "The bank's data does not say what kind of account this is. Please check the type.",
};

/** The form behind a feed account's Map button — the setup wizard's mapping
 * step and Settings -> Connections both render it. It starts from Purser's
 * proposal for the account (name, type and path, derived on the server from
 * what the feed states: decision 126) and falls back to the name alone
 * (guessAccountType) when there is none; an existing open account in the same
 * currency can be chosen instead. `onMap` does the request (and any refresh);
 * what it throws shows here. */
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
  const proposal = account.proposal ?? null;
  const [mode, setMode] = useState<"create" | "existing">("create");
  const [name, setName] = useState(proposal?.name ?? bankName);
  const [type, setType] = useState(() => proposal?.type ?? guessAccountType(bankName));
  const [path, setPath] = useState(
    () => proposal?.path ?? defaultPath(guessAccountType(bankName), bankName, account.external_id),
  );
  const [pathEdited, setPathEdited] = useState(false);
  // The proposal's reason holds only while its type stands.
  const typeNote = !proposal
    ? null
    : proposal.note
      ? "The bank also lists investments in this account. If it is an investment account, choose Brokerage."
      : type === proposal.type
        ? (TYPE_BASIS_TEXT[proposal.type_basis] ?? null)
        : null;
  const [accountId, setAccountId] = useState("");
  // On for a new account: the feed brings about three months of rows and no
  // balance from before them. Off for an existing one, whose ledger may be
  // deliberate; the person can still tick it.
  const [startFromBank, setStartFromBank] = useState(true);

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

  const targetType =
    mode === "existing" ? candidates.find((a) => a.id === accountId)?.type : type;
  const canStartFromBank = targetType !== undefined && STARTING_BALANCE_TYPES.has(targetType);
  const starting = canStartFromBank && startFromBank ? { starting_balance: true } : {};

  const target = (): MapTarget =>
    mode === "existing"
      ? { account_id: accountId, ...starting }
      : {
          create: {
            name: name.trim() || bankName,
            type,
            path: path.trim(),
            currency: account.currency,
          },
          ...starting,
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
            onChange={() => {
              setMode("create");
              setStartFromBank(true);
            }}
          />
          A new Purser account
        </label>
        {candidates.length > 0 && (
          <label className="flex items-center gap-2">
            <input
              type="radio"
              name={`${id}-mode`}
              checked={mode === "existing"}
              onChange={() => {
                setMode("existing");
                setStartFromBank(false);
              }}
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
            {typeNote && <p className="text-xs text-muted-foreground">{typeNote}</p>}
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

      {mode === "create" && (
        <p className="text-xs text-muted-foreground">
          The account type cannot be changed after mapping.
        </p>
      )}
      {proposal?.action === "ignore" && (
        <p className="text-xs text-muted-foreground">
          This looks like an account another bank login already delivers
          {proposal.duplicate_of && !proposal.duplicate_of.startsWith("fa_")
            ? ` (${proposal.duplicate_of})`
            : ""}
          . If it is the same account, ignore it instead of mapping it.
        </p>
      )}
      {canStartFromBank && (
        <div className="space-y-1">
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={startFromBank}
              onChange={(e) => setStartFromBank(e.target.checked)}
            />
            Start from the bank&apos;s balance
          </label>
          <p className="text-xs text-muted-foreground">
            The bank sends about three months of transactions and no balance from before them.
            Purser adds one starting entry so the account matches the balance the bank reports,
            and lowers that entry by itself if you import older history later.
          </p>
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
