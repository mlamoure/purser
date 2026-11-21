import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, errorDetail } from "../../lib/api";
import type { components } from "../../api/schema";

/* What the Settings sections that edit GET/PUT /config share (#542): each
 * card keeps its own Save, which sends only that card's keys. */

export type ConfigOut = components["schemas"]["ConfigOut"];
export type ConfigUpdate = components["schemas"]["ConfigUpdate"];

export const selectClass = "h-9 w-full rounded-md border border-border bg-card px-2 text-sm";

export function useSaveConfig(onError: (message: string) => void) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (body: ConfigUpdate) => {
      const { error: err } = await api.PUT("/config", { body });
      if (err) throw new Error(errorDetail(err));
    },
    onSuccess: (_data, body) => {
      qc.invalidateQueries({ queryKey: ["config"] });
      // The household time zone decides the ledger day and how every
      // timestamp is shown (GET /clock).
      if (body.timezone !== undefined) qc.invalidateQueries({ queryKey: ["clock"] });
    },
    onError: (e) => onError(e.message),
  });
}

export function Saved({ visible }: { visible: boolean }) {
  return visible ? <p className="mt-2 text-sm text-success">Saved.</p> : null;
}

