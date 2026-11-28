import { Navigate, NavLink, Outlet, useLocation } from "react-router-dom";
import { cn } from "../../lib/utils";
import { useIsAdmin } from "../../lib/useIsAdmin";
import { Card } from "../../components/ui";

/** A tab of a section, at /settings/<section>/<path> ("" is the section's
 * own URL). An `adminOnly` tab works like an admin-only section. */
export type SettingsTab = { path: string; label: string; adminOnly: boolean };

export type SettingsSection = {
  path: string;
  label: string;
  adminOnly: boolean;
  tabs?: SettingsTab[];
};

/** Settings' sections, in sub-navigation order (#542). `adminOnly` sections
 * read admin-scoped endpoints throughout (GET/PUT /config, /users, /audit),
 * so a member session neither sees them nor calls them. The others work with
 * a member's read + write scopes; their admin-only parts are gated inside the
 * page (Connections, Market data, Categories, Agents & API) or are a tab of
 * their own (Categories' AI categorization). */
export const SETTINGS_SECTIONS: SettingsSection[] = [
  { path: "general", label: "General", adminOnly: true },
  { path: "connections", label: "Connections", adminOnly: false },
  {
    path: "categories",
    label: "Categories",
    adminOnly: false,
    // How transactions get their categories, in one place (#694): the
    // category set, the rules that file rows (once a section of its own) and
    // the optional AI categorizer.
    tabs: [
      { path: "", label: "Categories", adminOnly: false },
      { path: "rules", label: "Rules", adminOnly: false },
      { path: "ai", label: "AI categorization", adminOnly: true },
    ],
  },
  { path: "agent-memory", label: "Agent memory", adminOnly: false },
  { path: "market-data", label: "Market data", adminOnly: false },
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

/** The tab a /settings/<section>/... path opens, for a section with tabs. */
export function settingsTab(pathname: string): SettingsTab | undefined {
  const segment = pathname.split("/")[3] ?? "";
  return settingsSection(pathname)?.tabs?.find((t) => t.path === segment);
}

/** What a member sees in place of an admin-only section or tab: nothing of
 * it is read, so no admin-scoped call fails. */
function AdminOnly({ label }: { label: string }) {
  return (
    <Card role="status">
      <p className="text-sm text-muted-foreground">
        {label} is managed by an administrator. Ask an admin of this Purser to change it.
      </p>
    </Card>
  );
}

/** A section's tabs (#694), each its own URL, so a tab can be linked to and
 * a bookmark opens it. They are links, not an in-page tab widget: the
 * current one is marked aria-current="page", like the section list. A
 * member sees no admin-only tab and, sent to one, the admin-only notice. */
export function SettingsTabs() {
  const isAdmin = useIsAdmin();
  const { pathname } = useLocation();
  const section = settingsSection(pathname);
  const tab = settingsTab(pathname);
  if (!section?.tabs) return <Outlet />;
  const visible = section.tabs.filter((t) => isAdmin || !t.adminOnly);

  return (
    <div className="space-y-4">
      <nav
        aria-label={section.label}
        className="-mx-3 overflow-x-auto border-b border-border px-3 sm:mx-0 sm:px-0"
      >
        <ul className="flex gap-1 whitespace-nowrap">
          {visible.map((t) => (
            <li key={t.path}>
              <NavLink
                to={`/settings/${section.path}${t.path ? `/${t.path}` : ""}`}
                end
                className={({ isActive }) =>
                  cn(
                    "-mb-px block border-b-2 border-transparent px-3 py-2 text-sm font-medium text-muted-foreground hover:text-foreground",
                    isActive && "border-primary text-foreground",
                  )
                }
              >
                {t.label}
              </NavLink>
            </li>
          ))}
        </ul>
      </nav>
      {tab?.adminOnly && !isAdmin ? <AdminOnly label={tab.label} /> : <Outlet />}
    </div>
  );
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
        {blocked ? <AdminOnly label={section.label} /> : <Outlet />}
      </div>
    </div>
  );
}
