import { useEffect } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Navigate, Route, Routes, useLocation } from "react-router-dom";
import { api, setOnUnauthorized } from "./lib/api";
import { meQuery } from "./lib/me";
import { useDocumentTitle } from "./lib/useDocumentTitle";
import SourceFooter from "./components/SourceFooter";
import MainNav from "./components/MainNav";
import Login from "./pages/Login";
import Wizard from "./pages/Wizard";
import Dashboard from "./pages/Dashboard";
import Accounts from "./pages/Accounts";
import AccountRegister from "./pages/AccountRegister";
import Search from "./pages/Search";
import Schedules from "./pages/Schedules";
import ImportQif from "./pages/ImportQif";
import Selection from "./pages/Selection";
import SettingsLayout, {
  SettingsIndex,
  SettingsTabs,
  settingsSection,
  settingsTab,
} from "./pages/settings/Layout";
import General from "./pages/settings/General";
import Connections from "./pages/settings/Connections";
import Categories from "./pages/settings/Categories";
import Rules from "./pages/settings/Rules";
import AiCategorization from "./pages/settings/AiCategorization";
import Memories from "./pages/settings/Memories";
import MarketData from "./pages/settings/MarketData";
import Holdings from "./pages/Holdings";
import AgentsApi from "./pages/settings/AgentsApi";
import Security from "./pages/settings/Security";
import Users from "./pages/settings/Users";
import AuditLog from "./pages/settings/AuditLog";

// Browser-tab names. Account registers name their own tab after the account
// (AccountRegister), so only the list at exactly /accounts is named here.
const PAGE_TITLES: [string, string][] = [
  ["/holdings", "Holdings"],
  ["/search", "Search"],
  ["/schedules", "Scheduled transactions"],
  ["/import", "Import"],
  ["/selection", "Selected transactions"],
];

function pageTitle(pathname: string): string | null {
  if (pathname === "/") return "Dashboard";
  if (pathname === "/accounts") return "Accounts";
  if (pathname === "/settings" || pathname.startsWith("/settings/")) {
    const section = settingsSection(pathname);
    if (!section) return "Settings";
    // A tab other than the section's own page names itself first (#694).
    const tab = settingsTab(pathname);
    return tab?.path
      ? `${tab.label} · ${section.label} · Settings`
      : `${section.label} · Settings`;
  }
  const hit = PAGE_TITLES.find(([prefix]) => pathname === prefix || pathname.startsWith(`${prefix}/`));
  return hit ? hit[1] : null;
}

/** Where each pre-#542 route lives now. Bookmarks and links already sent
 * (an agent's, the docs') keep working, with their query string: an audit
 * log link carries its filters. */
const MOVED_ROUTES: [string, string][] = [
  ["/config", "/settings"],
  ["/config/simplefin", "/settings/connections"],
  ["/config/categories", "/settings/categories"],
  ["/config/rules", "/settings/categories/rules"],
  ["/config/memories", "/settings/agent-memory"],
  ["/config/prices", "/settings/market-data"],
  // Settings, Prices was renamed Market data (#678).
  ["/settings/prices", "/settings/market-data"],
  // Rules became a tab of Settings, Categories (#694).
  ["/settings/rules", "/settings/categories/rules"],
  ["/config/settings", "/settings/general"],
  ["/connections", "/settings/connections"],
  ["/tokens", "/settings/agents"],
  ["/users", "/settings/users"],
  ["/audit", "/settings/audit"],
  // The review queue dissolved into the registers (issue #112).
  ["/review", "/"],
];

function Moved({ to }: { to: string }) {
  const { search, hash } = useLocation();
  return <Navigate to={{ pathname: to, search, hash }} replace />;
}

export default function App() {
  const queryClient = useQueryClient();
  // Mid-session expiry (#220): any non-/auth 401 invalidates the session
  // query (its key from meQuery, never spelled out here), so the !me.data
  // gate below takes over and shows the login page.
  useEffect(() => {
    setOnUnauthorized(() => queryClient.invalidateQueries({ queryKey: meQuery.queryKey }));
    return () => setOnUnauthorized(null);
  }, [queryClient]);
  const setup = useQuery({
    queryKey: ["setup-status"],
    queryFn: async () => (await api.GET("/auth/setup/status")).data,
  });
  const me = useQuery({ ...meQuery, enabled: setup.data?.setup_needed === false });
  const location = useLocation();
  useDocumentTitle(
    setup.isLoading || me.isLoading
      ? null
      : setup.data?.setup_needed
        ? "Setup"
        : !me.data
          ? "Sign in"
          : pageTitle(location.pathname),
  );

  if (setup.isLoading) return <div className="p-10 text-muted-foreground">Loading…</div>;
  if (setup.data?.setup_needed) {
    return (
      <>
        <Wizard />
        <SourceFooter />
      </>
    );
  }
  if (me.isLoading) return <div className="p-10 text-muted-foreground">Loading…</div>;
  if (!me.data) {
    // The sign-in page fills the viewport; the footer sits in its empty bottom.
    return (
      <div className="relative">
        <Routes>
          <Route path="*" element={<Login onLogin={() => me.refetch()} />} />
        </Routes>
        <SourceFooter className="absolute inset-x-0 bottom-0" />
      </div>
    );
  }

  return (
    <div className="min-h-screen">
      <MainNav />
      <main className="mx-auto min-w-0 max-w-6xl p-3 sm:p-6">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/accounts" element={<Accounts />} />
          <Route path="/accounts/:accountId" element={<AccountRegister />} />
          <Route path="/holdings" element={<Holdings />} />
          <Route path="/search" element={<Search />} />
          <Route path="/schedules" element={<Schedules />} />
          <Route path="/import" element={<ImportQif />} />
          <Route path="/selection" element={<Selection />} />
          <Route path="/settings" element={<SettingsLayout />}>
            <Route index element={<SettingsIndex />} />
            <Route path="general" element={<General />} />
            <Route path="connections" element={<Connections />} />
            <Route path="categories" element={<SettingsTabs />}>
              <Route index element={<Categories />} />
              <Route path="rules" element={<Rules />} />
              <Route path="ai" element={<AiCategorization />} />
            </Route>
            <Route path="agent-memory" element={<Memories />} />
            <Route path="market-data" element={<MarketData />} />
            <Route path="agents" element={<AgentsApi />} />
            <Route path="security" element={<Security />} />
            <Route path="users" element={<Users />} />
            <Route path="audit" element={<AuditLog />} />
          </Route>
          {MOVED_ROUTES.map(([from, to]) => (
            <Route key={from} path={from} element={<Moved to={to} />} />
          ))}
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
      <SourceFooter />
    </div>
  );
}
