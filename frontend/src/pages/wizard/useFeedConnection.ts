import { useState } from "react";
import type { components } from "../../api/schema";
import { api, errorDetail } from "../../lib/api";

export type Discovered = components["schemas"]["DiscoveredOut"];

/** Ledger label the wizard gives a bank account it maps: the account's name
 * (or external id) lower-cased, other characters collapsed to `_`. */
export function accountLabel(d: Pick<Discovered, "name" | "external_id">): string {
  return (d.name ?? d.external_id)
    .toLowerCase()
    .replace(/[^a-z0-9_]+/g, "_")
    .replace(/^_+|_+$/g, "");
}

/** The wizard's SimpleFIN connection and the bank accounts it discovered —
 * the state steps 3 (connect) and 4 (map) share. `connect` and `mapAccount`
 * throw the API's error detail for the calling step to show. */
export function useFeedConnection() {
  const [connectionId, setConnectionId] = useState<string | null>(null);
  const [discovered, setDiscovered] = useState<Discovered[]>([]);

  const refreshDiscovered = async (cid: string) => {
    const { data, error } = await api.GET("/connections/{connection_id}/discovered", {
      params: { path: { connection_id: cid } },
    });
    if (error) throw new Error(errorDetail(error));
    setDiscovered(data ?? []);
  };

  /** Exchange a setup token (once — the access URL is stored encrypted), run
   * the first fetch, and list the accounts it found. */
  const connect = async (setupToken: string) => {
    const { data, error } = await api.POST("/connections", {
      body: { setup_token: setupToken },
    });
    if (error) throw new Error(errorDetail(error));
    const cid = data!.id;
    setConnectionId(cid);
    const { error: syncError } = await api.POST("/connections/{connection_id}/sync", {
      params: { path: { connection_id: cid } },
    });
    if (syncError) throw new Error(errorDetail(syncError));
    await refreshDiscovered(cid);
  };

  /** Create a checking account under `assets.` for a discovered bank account
   * and map it; its archived transactions import on the spot. */
  const mapAccount = async (d: Discovered) => {
    if (!connectionId) throw new Error("connect a SimpleFIN feed first");
    const { error } = await api.POST("/connections/{connection_id}/map", {
      params: { path: { connection_id: connectionId } },
      body: {
        external_id: d.external_id,
        create: {
          name: d.name ?? d.external_id,
          type: "checking",
          path: `assets.${accountLabel(d)}`,
        },
      },
    });
    if (error) throw new Error(errorDetail(error));
    await refreshDiscovered(connectionId);
  };

  return { connectionId, discovered, connect, mapAccount };
}
