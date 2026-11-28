import { useState } from "react";
import { Link } from "react-router-dom";
import { useQueries, useQuery } from "@tanstack/react-query";
import { api, errorDetail } from "../lib/api";
import { configQuery } from "../lib/config";
import { Badge, Button, Card, CardTitle } from "./ui";

/** Browser-local "don't show again" for the card (a convenience only: the
 * items themselves come from the server). */
const DISMISS_KEY = "purser.firstRunChecklist.dismissed";

function readDismissed(): boolean {
  try {
    return window.localStorage.getItem(DISMISS_KEY) === "1";
  } catch {
    return false; // storage blocked (private window, sandbox): show the card
  }
}

function writeDismissed(): void {
  try {
    window.localStorage.setItem(DISMISS_KEY, "1");
  } catch {
    // Not persisted; the card stays hidden until the page reloads.
  }
}

export type ChecklistItem = {
  id: "key" | "feed" | "map" | "agent";
  label: string;
  /** Why the item is open, in a sentence (shown while it is to do). */
  detail?: string;
  done: boolean;
  to: string;
  action: string;
};

export type WaitingFeedAccount = {
  connectionId: string;
  externalId: string;
  name: string;
  bank: string | null;
};

/** The feed accounts no Purser account is mapped to and no admin ignored
 * (#560), with how many are mapped. `ready` is false while a read is pending
 * or failed: a failed read is never taken as "nothing waiting". Admin-scoped
 * reads, under their own keys so the pages' lists are not refetched. */
export function useFeedMapping(): {
  ready: boolean;
  loading: boolean;
  connections: number | null;
  mapped: number;
  waiting: WaitingFeedAccount[];
} {
  const connections = useQuery({
    queryKey: ["connections", "first-run"],
    queryFn: async () => {
      const { data, error } = await api.GET("/connections");
      if (error) throw new Error(errorDetail(error));
      return data ?? [];
    },
  });
  const discovered = useQueries({
    queries: (connections.data ?? []).map((c) => ({
      queryKey: ["discovered", c.id, "first-run"],
      // Before the first fetch the server answers "no fetches yet", which is
      // not a failure: there is nothing to map.
      enabled: !!c.last_fetch_at,
      queryFn: async () => {
        const { data, error } = await api.GET("/connections/{connection_id}/discovered", {
          params: { path: { connection_id: c.id } },
        });
        if (error) throw new Error(errorDetail(error));
        return data ?? [];
      },
    })),
  });
  const fetched = (connections.data ?? []).map((c, i) => ({ c, q: discovered[i] }))
    .filter(({ c }) => !!c.last_fetch_at);
  const rows = fetched.flatMap(({ c, q }) => (q?.data ?? []).map((d) => ({ c, d })));
  return {
    ready: connections.isSuccess && fetched.every(({ q }) => q?.isSuccess),
    loading: connections.isPending || fetched.some(({ q }) => q?.isPending),
    connections: connections.data ? connections.data.length : null,
    mapped: rows.filter(({ d }) => d.mapped_account_id).length,
    waiting: rows
      .filter(({ d }) => !d.mapped_account_id && !d.ignored)
      .map(({ c, d }) => ({
        connectionId: c.id,
        externalId: d.external_id,
        name: d.name ?? d.external_id,
        bank: d.org ?? null,
      })),
  };
}

/** Where a waiting feed account is mapped or ignored: its row on
 * Settings -> Connections, opened with the Map form. */
export function mapLink(account: Pick<WaitingFeedAccount, "externalId">): string {
  return `/settings/connections?map=${encodeURIComponent(account.externalId)}`;
}

function names(accounts: WaitingFeedAccount[]): string {
  const shown = accounts.slice(0, 3).map((a) => (a.bank ? `${a.name} (${a.bank})` : a.name));
  const more = accounts.length - shown.length;
  return shown.join(", ") + (more > 0 ? ` and ${more} more` : "");
}

/** What the first-run wizard set up, derived from server state (#539). The
 * wizard's progress lives only in its component state, so once the first
 * admin exists a reload drops the remaining steps; this card resumes them.
 * Each item comes from an existing admin read:
 *   key   — GET /config: encryption.key_acknowledged_at
 *   feed  — GET /connections: any connection
 *   map   — GET /connections/{id}/discovered: open until the first bank
 *           account is mapped (accounts a bank adds later are raised by
 *           NewBankAccounts, not here; #560)
 *   agent — GET /tokens: any live API token of this user
 * An item whose read fails is left out rather than guessed. Rendered for
 * admins only (Dashboard); the reads are admin-scoped. */
export function useFirstRunChecklist(): { items: ChecklistItem[]; loading: boolean } {
  // The shared configQuery (Config -> Settings, Market data and SimpleFIN use it
  // too), so this shares their cache and their invalidations.
  const config = useQuery(configQuery);
  const feed = useFeedMapping();
  // Own key under the shared prefix; like the pages' reads it throws on error
  // (a failed read must not read as "no agent connected").
  const tokens = useQuery({
    queryKey: ["tokens", "first-run"],
    queryFn: async () => {
      const { data, error } = await api.GET("/tokens");
      if (error) throw new Error(errorDetail(error));
      return data ?? [];
    },
  });
  const loading = config.isPending || feed.loading || tokens.isPending;
  const items: ChecklistItem[] = [];
  if (config.data) {
    items.push({
      id: "key",
      label: "Record your encryption key",
      detail:
        "Nobody has confirmed saving this instance's encryption key. Without it a restored backup cannot be read.",
      done: config.data.encryption.key_acknowledged_at != null,
      to: "/settings/security",
      action: "Show and record the key",
    });
  }
  if (feed.connections != null) {
    items.push({
      id: "feed",
      label: "Connect a SimpleFIN bank feed (optional)",
      detail: "No bank feed is connected, so nothing imports on its own.",
      done: feed.connections > 0,
      to: "/settings/connections",
      action: "Connect a feed",
    });
    // First-run mapping only: the step is open until one bank account is
    // mapped. An account a bank delivers after that is not unfinished setup;
    // the Dashboard raises it on its own (NewBankAccounts).
    // Left out while nothing is delivered yet (no fetch).
    const waiting = feed.waiting;
    if (feed.ready && (feed.mapped === 0) !== (waiting.length === 0)) {
      items.push({
        id: "map",
        label: "Map your bank accounts",
        detail: waiting.length
          ? `Not in Purser yet: ${names(waiting)}. Map each to a Purser account, or ignore it.`
          : undefined,
        done: waiting.length === 0,
        to: waiting.length ? mapLink(waiting[0]) : "/settings/connections",
        action: "Map accounts",
      });
    }
  }
  if (tokens.data) {
    items.push({
      id: "agent",
      label: "Connect your AI agent",
      detail: "No agent token exists yet, so no agent can read or write this ledger.",
      done: tokens.data.some((t) => t.kind === "api"),
      to: "/settings/agents",
      action: "Create a token",
    });
  }
  return { items, loading };
}

/** "Finish setting up" — shown on the Dashboard to admins while any item is
 * undone; gone once all are done, or after Dismiss (per browser). */
export default function FirstRunChecklist() {
  const [dismissed, setDismissed] = useState(readDismissed);
  if (dismissed) return null; // dismissed: not even the reads
  return (
    <ChecklistCard
      onDismiss={() => {
        writeDismissed();
        setDismissed(true);
      }}
    />
  );
}

function ChecklistCard({ onDismiss }: { onDismiss: () => void }) {
  const { items, loading } = useFirstRunChecklist();

  if (loading || items.every((i) => i.done)) return null;
  return (
    <Card role="region" aria-label="Finish setting up">
      <div className="mb-2 flex items-center justify-between gap-2">
        <CardTitle className="mb-0">Finish setting up</CardTitle>
        <Button
          size="sm"
          variant="ghost"
          onClick={onDismiss}
        >
          Dismiss
        </Button>
      </div>
      <p className="mb-3 text-sm text-muted-foreground">
        These setup steps are still open on this instance. The card goes away when all are
        done.
      </p>
      <ul className="space-y-2">
        {items.map((item) => (
          <li key={item.id} className="text-sm">
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant={item.done ? "green" : "amber"}>{item.done ? "done" : "to do"}</Badge>
              <span className={item.done ? "text-muted-foreground line-through" : undefined}>
                {item.label}
              </span>
              {!item.done && (
                <Link to={item.to} className="ml-auto font-medium text-primary hover:underline">
                  {item.action}
                </Link>
              )}
            </div>
            {!item.done && item.detail && (
              <p className="mt-1 text-xs text-muted-foreground">{item.detail}</p>
            )}
          </li>
        ))}
      </ul>
    </Card>
  );
}

/** Bank accounts delivered after setup that are neither mapped nor ignored
 * (#560): shown to admins on the Dashboard until each is mapped or ignored.
 * Nothing while setup's own mapping step is still open (the checklist has
 * it), while a read is pending, or when a read failed. */
export function NewBankAccounts() {
  const feed = useFeedMapping();
  if (!feed.ready || feed.mapped === 0 || feed.waiting.length === 0) return null;
  const n = feed.waiting.length;
  return (
    <Card role="region" aria-label="New bank accounts">
      <CardTitle className="mb-1">
        {n} bank account{n === 1 ? "" : "s"} not in Purser yet
      </CardTitle>
      <p className="mb-3 text-sm text-muted-foreground">
        Your bank feed delivers {n === 1 ? "this account" : "these accounts"}, and no Purser
        account receives {n === 1 ? "it" : "them"}. Map {n === 1 ? "it" : "each"} to import its
        transactions, or ignore {n === 1 ? "it" : "one"} if it is a duplicate or does not belong
        here.
      </p>
      <ul className="divide-y divide-border">
        {feed.waiting.map((a) => (
          <li
            key={`${a.connectionId} ${a.externalId}`}
            className="flex flex-wrap items-center justify-between gap-2 py-2 text-sm"
          >
            <span className="min-w-0 break-words">
              <span className="font-medium">{a.name}</span>
              {a.bank ? <span className="text-muted-foreground"> · {a.bank}</span> : null}
            </span>
            <Link to={mapLink(a)} className="font-medium text-primary hover:underline">
              Map or ignore
            </Link>
          </li>
        ))}
      </ul>
    </Card>
  );
}
