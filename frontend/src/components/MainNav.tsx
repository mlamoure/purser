import { useId, useState } from "react";
import { NavLink } from "react-router-dom";
import { api } from "../lib/api";
import { cn } from "../lib/utils";

/** The primary navigation (#542): the four places people work, then one
 * Settings area (its sections, and their admin gating, are its own
 * sub-navigation). The same for every session. */
export const MAIN_LINKS: [string, string][] = [
  ["/", "Dashboard"],
  ["/accounts", "Accounts"],
  ["/schedules", "Scheduled transactions"],
  ["/import", "Import"],
  ["/settings", "Settings"],
];

export default function MainNav() {
  // Below md the links fold behind a Menu button instead of wrapping into
  // rows; from md up they sit in one row and the button is hidden.
  const [open, setOpen] = useState(false);
  const menuId = useId();

  return (
    <nav aria-label="Main" className="border-b border-border bg-card px-4 py-3 sm:px-6">
      <div className="flex flex-wrap items-center gap-x-1 gap-y-2">
        <span className="mr-4 text-lg font-bold text-primary sm:mr-6">Purser</span>
        <button
          type="button"
          className="ml-auto inline-flex items-center gap-2 rounded-md px-3 py-1.5 text-sm font-medium text-muted-foreground hover:bg-muted md:hidden"
          aria-expanded={open}
          aria-controls={menuId}
          onClick={() => setOpen((o) => !o)}
          onKeyDown={(e) => {
            if (e.key === "Escape") setOpen(false);
          }}
        >
          <svg aria-hidden="true" viewBox="0 0 20 20" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="2">
            {open ? <path d="M5 5l10 10M15 5L5 15" /> : <path d="M3 5h14M3 10h14M3 15h14" />}
          </svg>
          Menu
        </button>
        <div
          id={menuId}
          className={cn(
            "w-full flex-col gap-1 md:flex md:w-auto md:flex-1 md:flex-row md:items-center",
            open ? "flex" : "hidden",
          )}
        >
          {MAIN_LINKS.map(([to, label]) => (
            // NavLink marks the active item with aria-current="page"; "/" must
            // match exactly or Dashboard would stay active everywhere, while
            // /accounts and /settings stay active on their sub-pages.
            <NavLink
              key={to}
              to={to}
              end={to === "/"}
              onClick={() => setOpen(false)}
              className={({ isActive }) =>
                cn(
                  "rounded-md px-3 py-1.5 text-sm font-medium",
                  isActive
                    ? "bg-primary/10 font-semibold text-primary ring-1 ring-inset ring-primary/30"
                    : "text-muted-foreground hover:bg-muted hover:text-foreground",
                )
              }
            >
              {label}
            </NavLink>
          ))}
          <button
            type="button"
            className="rounded-md px-3 py-1.5 text-left text-sm text-muted-foreground hover:bg-muted md:ml-auto"
            onClick={async () => {
              await api.POST("/auth/logout");
              window.location.href = "/login";
            }}
          >
            Log out
          </button>
        </div>
      </div>
    </nav>
  );
}
