import { useCallback } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, errorDetail } from "./api";
import { formatTimestamp } from "./dates";

/** GET /clock (issue #471, decision 105): the ledger day and the household
 * time zone it follows. One cached query for the whole app. */
export function useClock() {
  return useQuery({
    queryKey: ["clock"],
    queryFn: async () => {
      const { data, error } = await api.GET("/clock");
      if (error) throw new Error(errorDetail(error));
      return data;
    },
    staleTime: 60_000,
  });
}

/** Today in the browser's zone: only a stand-in until GET /clock answers. */
export function localToday(): string {
  const d = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

/** The server's "today": the household time zone decides it, not UTC or the
 * browser, and the server refuses an event or an opening count dated after
 * it. */
export function useLedgerToday(): string {
  return useClock().data?.ledger_today ?? localToday();
}

/** Formats a timestamp in the household time zone (see dates.ts). Until
 * GET /clock answers (or if it fails) the browser's zone stands in, which is
 * the same zone for a household browsing at home. */
export function useFormatTimestamp(): (iso: string | null | undefined) => string {
  const timeZone = useClock().data?.timezone;
  return useCallback((iso) => formatTimestamp(iso, timeZone), [timeZone]);
}
