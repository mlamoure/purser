import { cn } from "../lib/utils";

/* The source offer AGPL-3.0 section 13 asks of a network service (#429):
 * every screen, signed in or not, links to the instance's source code. Both
 * targets are server routes, not SPA routes: /source redirects to
 * PURSER_SOURCE_URL, and /third-party-licenses.txt is written beside the
 * bundle by the Vite build. */
export default function SourceFooter({ className }: { className?: string }) {
  return (
    <footer className={cn("px-6 py-4 text-center text-xs text-muted-foreground", className)}>
      Purser is free software under the{" "}
      <a
        className="underline"
        href="https://www.gnu.org/licenses/agpl-3.0.html"
        target="_blank"
        rel="noreferrer"
      >
        GNU AGPL v3
      </a>
      .{" "}
      <a className="underline" href="/source" target="_blank" rel="noreferrer">
        Source code
      </a>
      {" · "}
      <a className="underline" href="/third-party-licenses.txt" target="_blank" rel="noreferrer">
        Third-party licences
      </a>
    </footer>
  );
}
