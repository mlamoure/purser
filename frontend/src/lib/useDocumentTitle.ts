import { useEffect } from "react";

/** Name the browser tab after the page: "Scheduled transactions · Purser".
 * A null or empty title leaves the current one alone, so a page that knows a
 * better name (an account register) can set it without the shell undoing it. */
export function useDocumentTitle(title: string | null | undefined): void {
  useEffect(() => {
    if (title) document.title = `${title} · Purser`;
  }, [title]);
}
