import { Navigate, NavLink, Outlet, useLocation } from "react-router-dom";
import { cn } from "../../lib/utils";
import { useIsAdmin } from "../../lib/useIsAdmin";
import { Card } from "../../components/ui";

export type SettingsSection = { path: string; label: string; adminOnly: boolean };

/** Settings' sections, in sub-navigation order (#542). `adminOnly` sections
 * read admin-scoped endpoints throughout (GET/PUT /config, /users, /audit),
 * so a member session neither sees them nor calls them. The others work with
 * a member's read + write scopes; their admin-only parts are gated inside the
 * page (Connections, Prices, Categories, Agents & API). */
export const SETTINGS_SECTIONS: SettingsSection[] = [
  { path: "general", label: "General", adminOnly: true },
  { path: "connections", label: "Connections", adminOnly: false },
  { path: "categories", label: "Categories", adminOnly: false },
  { path: "rules", label: "Rules", adminOnly: false },
  { path: "agent-memory", label: "Agent memory", adminOnly: false },
  { path: "prices", label: "Prices", adminOnly: false },
  { path: "agents", label: "Agents & API", adminOnly: false },
  { path: "security", label: "Security", adminOnly: true },
  { path: "users", label: "Users", adminOnly: true },
  { path: "audit", label: "Audit log", adminOnly: true },
];

/** The section a /settings/... path opens, if any. */
export function settingsSection(pathname: string): SettingsSection | undefined {
  const segment = pathname.split("/")[2];
  return SETTINGS_SECTIONS.find((s) => s.path === segment);
}

function useVisibleSections(): SettingsSection[] {
  const isAdmin = useIsAdmin();
  return SETTINGS_SECTIONS.filter((s) => isAdmin || !s.adminOnly);
}

/** /settings opens the first section this session can use. */
export function SettingsIndex() {
  const [first] = useVisibleSections();
  return <Navigate to={first.path} replace />;
}

export default function SettingsLayout() {
  const isAdmin = useIsAdmin();
  const visible = useVisibleSections();
  const section = settingsSection(useLocation().pathname);
  const blocked = section?.adminOnly && !isAdmin;

  return (
    <div className="space-y-4 md:grid md:grid-cols-[11rem_minmax(0,1fr)] md:items-start md:gap-6 md:space-y-0">
      {/* A row that scrolls sideways on a phone, a column beside the page
          from md up: ten sections never wrap into a stack of rows. */}
      <nav
        aria-label="Settings"
        className="-mx-3 overflow-x-auto border-b border-border px-3 sm:mx-0 sm:px-0 md:mx-0 md:overflow-visible md:border-b-0 md:px-0"
      >
        <ul className="flex gap-1 whitespace-nowrap md:flex-col md:whitespace-normal">
          {visible.map(({ path, label }) => (
            <li key={path}>
              <NavLink
                to={path}
                className={({ isActive }) =>
                  cn(
                    "-mb-px block border-b-2 border-transparent px-3 py-2 text-sm font-medium text-muted-foreground hover:text-foreground",
                    "md:mb-0 md:rounded-md md:border-b-0 md:py-1.5 md:hover:bg-muted",
                    isActive && "border-primary text-foreground md:bg-primary/10 md:text-primary",
                  )
                }
              >
                {label}
              </NavLink>
            </li>
          ))}
        </ul>
      </nav>
      <div className="min-w-0">
        {blocked ? (
          <Card role="status">
            <p className="text-sm text-muted-foreground">
              {section.label} is managed by an administrator. Ask an admin of this Purser to
              change it.
            </p>
          </Card>
        ) : (
          <Outlet />
        )}
      </div>
    </div>
  );
}
