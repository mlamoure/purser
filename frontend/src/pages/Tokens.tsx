import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, errorDetail } from "../lib/api";
import { Badge, Button, Card, CardTitle, ErrorText, Input, Label, Table, Td, Th } from "../components/ui";

const SCOPES = [
  ["read", "Read everything (accounts, transactions, reports)"],
  ["write", "Act for you: create and edit transactions, rules, schedules and matches (audited, reversible)"],
  ["admin", "Manage users, tokens, connections"],
] as const;

const EXPIRY_CHOICES = [
  ["never", "Never"],
  ["30", "30 days"],
  ["90", "90 days"],
  ["365", "1 year"],
] as const;

type Token = {
  id: string;
  name: string;
  kind: string;
  scopes: string[];
  expires_at: string | null;
  last_used_at: string | null;
  created_at: string;
  is_current: boolean;
};

const fmtWhen = (iso: string | null | undefined) => (iso ? new Date(iso).toLocaleString() : "never");
const isExpired = (t: Token) => !!t.expires_at && new Date(t.expires_at) <= new Date();
// Session names are server-minted as `session:{method}:{email}`.
const signInMethod = (t: Token) => (t.name.split(":")[1] === "oidc" ? "SSO" : "Password");

export default function Tokens() {
  const qc = useQueryClient();
  const [name, setName] = useState("");
  // The recommended default for agents (DESIGN §9): read + write.
  const [scopes, setScopes] = useState<string[]>(["read", "write"]);
  const [expiry, setExpiry] = useState<string>("never");
  const [showExpired, setShowExpired] = useState(false);
  const [minted, setMinted] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const tokens = useQuery({
    queryKey: ["tokens", showExpired],
    queryFn: async () =>
      (await api.GET("/tokens", { params: { query: { include_expired: showExpired } } })).data ?? [],
  });
  const create = useMutation({
    mutationFn: async () => {
      const expires_at =
        expiry === "never"
          ? undefined
          : new Date(Date.now() + Number(expiry) * 86_400_000).toISOString();
      const { data, error: err } = await api.POST("/tokens", { body: { name, scopes, expires_at } });
      if (err) throw new Error(errorDetail(err));
      return data!;
    },
    onSuccess: (data) => {
      setMinted(data.token);
      setName("");
      qc.invalidateQueries({ queryKey: ["tokens"] });
    },
    onError: (e) => setError(e.message),
  });

  const revoke = async (t: Token) => {
    if (t.is_current) {
      // Revoking the session you are using IS logging out.
      await api.POST("/auth/logout");
      window.location.href = "/login";
      return;
    }
    await api.DELETE("/tokens/{token_id}", { params: { path: { token_id: t.id } } });
    qc.invalidateQueries({ queryKey: ["tokens"] });
  };

  const all: Token[] = tokens.data ?? [];
  const apiTokens = all.filter((t) => t.kind === "api");
  const sessions = all.filter((t) => t.kind === "session");

  return (
    <div className="space-y-6">
      <Card>
        <CardTitle>Create an API token</CardTitle>
        <p className="mb-4 text-sm text-muted-foreground">
          For agents, the recommended scopes are <b>read + write</b>: your agent acts for
          you. Every write it makes is attributed to its token in the audit log and can be
          reversed, and when it acts on its own idea it proposes instead, so you decide.
          Only this admin page mints tokens — an agent can never create or escalate its
          own access.
        </p>
        <p className="mb-4 text-sm text-muted-foreground">
          Documentation:{" "}
          <a href="/docs" target="_blank" rel="noreferrer" className="underline">
            API reference (Swagger)
          </a>
          ,{" "}
          <a href="/redoc" target="_blank" rel="noreferrer" className="underline">
            ReDoc
          </a>
          ,{" "}
          <a href="/llms.txt" target="_blank" rel="noreferrer" className="underline">
            llms.txt (agent guide)
          </a>
          . MCP clients connect to <code>/mcp</code> using the same Bearer tokens.
        </p>
        <div className="mb-4 space-y-1">
          <Label>Token name</Label>
          <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="my-agent" />
        </div>
        <div className="mb-4 space-y-2">
          {SCOPES.map(([scope, help]) => (
            <label key={scope} className="flex items-start gap-2 text-sm">
              <input
                type="checkbox"
                className="mt-1"
                checked={scopes.includes(scope)}
                onChange={(e) =>
                  setScopes(e.target.checked ? [...scopes, scope] : scopes.filter((s) => s !== scope))
                }
              />
              <span>
                <code className="rounded bg-muted px-1">{scope}</code>{" "}
                <span className="text-muted-foreground">— {help}</span>
              </span>
            </label>
          ))}
        </div>
        <div className="mb-4 space-y-1">
          <Label>Expires</Label>
          <select
            value={expiry}
            onChange={(e) => setExpiry(e.target.value)}
            className="flex h-9 w-full rounded-md border border-border bg-card px-3 py-1 text-sm shadow-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            {EXPIRY_CHOICES.map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </div>
        <Button disabled={!name || scopes.length === 0 || create.isPending} onClick={() => { setError(null); setMinted(null); create.mutate(); }}>
          Create token
        </Button>
        <ErrorText>{error}</ErrorText>
        {minted && (
          <div className="mt-4 rounded-md border border-emerald-300 bg-emerald-50 p-3">
            <p className="mb-1 text-sm font-medium text-emerald-900">Copy it now — shown exactly once:</p>
            <code className="break-all text-xs">{minted}</code>
          </div>
        )}
      </Card>

      <Card>
        <CardTitle>API tokens</CardTitle>
        <p className="mb-4 text-sm text-muted-foreground">
          Long-lived credentials for agents and integrations (MCP, scripts).
        </p>
        <Table>
          <thead>
            <tr><Th>Name</Th><Th>Scopes</Th><Th>Created</Th><Th>Last used</Th><Th>Expires</Th><Th /></tr>
          </thead>
          <tbody>
            {apiTokens.map((t) => (
              <tr key={t.id}>
                <Td>{t.name}</Td>
                <Td className="space-x-1">{t.scopes.map((s) => <Badge key={s}>{s}</Badge>)}</Td>
                <Td>{fmtWhen(t.created_at)}</Td>
                <Td>{fmtWhen(t.last_used_at)}</Td>
                <Td className="space-x-1">
                  <span>{t.expires_at ? new Date(t.expires_at).toLocaleString() : "—"}</span>
                  {isExpired(t) && <Badge variant="red">expired</Badge>}
                </Td>
                <Td>
                  <Button size="sm" variant="destructive" onClick={() => revoke(t)}>
                    Revoke
                  </Button>
                </Td>
              </tr>
            ))}
          </tbody>
        </Table>
        {apiTokens.length === 0 && (
          <p className="text-sm text-muted-foreground">No API tokens yet.</p>
        )}
      </Card>

      <Card>
        <CardTitle>Login sessions</CardTitle>
        <p className="mb-4 text-sm text-muted-foreground">
          Each browser sign-in creates a session that expires automatically after a period of
          inactivity. Dead sessions are removed by a background job; revoking one signs that
          browser out.
        </p>
        <Table>
          <thead>
            <tr><Th>Signed in via</Th><Th>Created</Th><Th>Last used</Th><Th>Expires</Th><Th /></tr>
          </thead>
          <tbody>
            {sessions.map((t) => (
              <tr key={t.id}>
                <Td className="space-x-2">
                  <span>{signInMethod(t)}</span>
                  {t.is_current && <Badge variant="green">current</Badge>}
                  {isExpired(t) && <Badge variant="red">expired</Badge>}
                </Td>
                <Td>{fmtWhen(t.created_at)}</Td>
                <Td>{fmtWhen(t.last_used_at)}</Td>
                <Td>{t.expires_at ? new Date(t.expires_at).toLocaleString() : "—"}</Td>
                <Td>
                  <Button size="sm" variant={t.is_current ? "outline" : "destructive"} onClick={() => revoke(t)}>
                    {t.is_current ? "Sign out" : "Revoke"}
                  </Button>
                </Td>
              </tr>
            ))}
          </tbody>
        </Table>
        {sessions.length === 0 && (
          <p className="text-sm text-muted-foreground">No active sessions.</p>
        )}
      </Card>

      <label className="flex items-center gap-2 text-sm text-muted-foreground">
        <input
          type="checkbox"
          checked={showExpired}
          onChange={(e) => setShowExpired(e.target.checked)}
        />
        Show expired and revoked
      </label>
    </div>
  );
}
