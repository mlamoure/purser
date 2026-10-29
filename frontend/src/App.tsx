import { useEffect } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { api, setOnUnauthorized } from "./lib/api";
import { meQuery } from "./lib/me";
import { cn } from "./lib/utils";
import SourceFooter from "./components/SourceFooter";
import Login from "./pages/Login";
import Wizard from "./pages/Wizard";
import Dashboard from "./pages/Dashboard";
import Tokens from "./pages/Tokens";
import Users from "./pages/Users";
import ConfigLayout from "./pages/config/Layout";
import SimpleFin from "./pages/config/SimpleFin";
import Categories from "./pages/config/Categories";
import Rules from "./pages/config/Rules";
import Memories from "./pages/config/Memories";
import Settings from "./pages/config/Settings";
import Prices from "./pages/config/Prices";
import AccountRegister from "./pages/AccountRegister";
import Schedules from "./pages/Schedules";
import AuditLog from "./pages/AuditLog";
import Selection from "./pages/Selection";
import ImportQif from "./pages/ImportQif";

function Nav({ isAdmin }: { isAdmin: boolean }) {
  const location = useLocation();
  const links = [
    ["/", "Dashboard"],
    ["/schedules", "Schedules"],
    ["/import", "Import"],
    ["/tokens", "API & MCP Access"],
    ["/users", "Users"],
    // GET /audit is admin-scoped; hide the tab from non-admin sessions
    // (the page itself still renders a clear 403 if reached directly).
    ...(isAdmin ? ([["/audit", "Audit Log"]] as const) : []),
    ["/config", "Config"],
  ] as const;
  return (
    <nav className="flex flex-wrap items-center gap-1 border-b border-border bg-card px-4 py-3 sm:px-6">
      <span className="mr-4 text-lg font-bold text-primary sm:mr-6">Purser</span>
      {links.map(([to, label]) => (
        <Link
          key={to}
          to={to}
          className={cn(
            "rounded-md px-3 py-1.5 text-sm font-medium hover:bg-muted",
            (to === "/" ? location.pathname === "/" : location.pathname.startsWith(to)) &&
              "bg-muted",
          )}
        >
          {label}
        </Link>
      ))}
      <button
        className="ml-auto rounded-md px-3 py-1.5 text-sm text-muted-foreground hover:bg-muted"
        onClick={async () => {
          await api.POST("/auth/logout");
          window.location.href = "/login";
        }}
      >
        Log out
      </button>
    </nav>
  );
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

  const scopes = ((me.data as { scopes?: string[] }).scopes ?? []) as string[];

  return (
    <div className="min-h-screen">
      <Nav isAdmin={scopes.includes("admin")} />
      <main className="mx-auto max-w-6xl p-6">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/accounts/:accountId" element={<AccountRegister />} />
          <Route path="/schedules" element={<Schedules />} />
          {/* The review queue dissolved into the registers (issue #112). */}
          <Route path="/review" element={<Navigate to="/" replace />} />
          <Route path="/config" element={<ConfigLayout />}>
            <Route index element={<Navigate to="simplefin" replace />} />
            <Route path="simplefin" element={<SimpleFin />} />
            <Route path="categories" element={<Categories />} />
            <Route path="rules" element={<Rules />} />
            <Route path="memories" element={<Memories />} />
            <Route path="prices" element={<Prices />} />
            <Route path="settings" element={<Settings />} />
          </Route>
          <Route path="/connections" element={<Navigate to="/config/simplefin" replace />} />
          <Route path="/import" element={<ImportQif />} />
          <Route path="/tokens" element={<Tokens />} />
          <Route path="/audit" element={<AuditLog />} />
          <Route path="/selection" element={<Selection />} />
          <Route path="/users" element={<Users />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
      <SourceFooter />
    </div>
  );
}
