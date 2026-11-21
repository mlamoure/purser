import { useRef, useState } from "react";
import { api, errorDetail } from "../../lib/api";
import { mapFeedAccount, type Discovered, type MapTarget } from "./MapFeedAccountForm";

export type { Discovered } from "./MapFeedAccountForm";

/** The wizard's SimpleFIN connection and the bank accounts it discovered —
 * the state steps 3 (connect) and 4 (map) share. `connect` and `mapAccount`
 * throw the API's error detail for the calling step to show. */
export function useFeedConnection() {
  const [connectionId, setConnectionId] = useState<string | null>(null);
  const claimed = useRef<{ setupToken: string; id: string } | null>(null);
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
    // A claim is one-time. If the fetch failed, retry the saved connection;
    // claiming the same setup token again would strand the wizard.
    if (claimed.current?.setupToken !== setupToken) {
      const { data, error } = await api.POST("/connections", {
        body: { setup_token: setupToken },
      });
      if (error) throw new Error(errorDetail(error));
      claimed.current = { setupToken, id: data!.id };
    }
    const cid = claimed.current.id;
    setConnectionId(cid);
    const { error: syncError } = await api.POST("/connections/{connection_id}/sync", {
      params: { path: { connection_id: cid } },
    });
    if (syncError) throw new Error(errorDetail(syncError));
    await refreshDiscovered(cid);
  };

  /** Map a discovered bank account onto `target` (a new or existing Purser
   * account, from MapFeedAccountForm); its archived transactions import on
   * the spot. */
  const mapAccount = async (d: Discovered, target: MapTarget) => {
    if (!connectionId) throw new Error("connect a SimpleFIN feed first");
    await mapFeedAccount(connectionId, d.external_id, target);
    await refreshDiscovered(connectionId);
  };

  return { connectionId, discovered, connect, mapAccount };
}
