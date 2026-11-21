import { queryOptions } from "@tanstack/react-query";
import { api, errorDetail } from "./api";

/** GET /config, shared by the admin parts of Settings (General, Security,
 * Connections, Prices, Categories, Agents & API) and the
 * first-run checklist: one ["config"] key with one queryFn, so they share the
 * cache and its invalidations. A failed read throws (#540): it shows as
 * "Could not load …: <detail>", never as an empty page. */
export const configQuery = queryOptions({
  queryKey: ["config"],
  queryFn: async () => {
    const { data, error } = await api.GET("/config");
    if (error) throw new Error(errorDetail(error));
    return data!;
  },
});
