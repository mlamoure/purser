import { useId, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, errorDetail } from "../../lib/api";
import { useFormatTimestamp } from "../../lib/clock";
import { QueryState } from "../../components/QueryState";
import {
  Badge,
  Button,
  Card,
  CardTitle,
  ErrorText,
  Input,
  Label,
  Table,
  Td,
  Th,
} from "../../components/ui";
import { useIsAdmin } from "../../lib/useIsAdmin";
import { ClientInstructions } from "../wizard/ClientInstructions";
import { CopyButton } from "../wizard/CopyButton";

const SCOPES = [
  ["read", "Read everything (accounts, transactions, reports)"],
  [
    "write",
    "Act for you: create and edit transactions, rules, scheduled transactions and matches (audited, reversible)",
  ],
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

const isExpired = (t: Token) =>
  !!t.expires_at && new Date(t.expires_at) <= new Date();
// Session names are server-minted as `session:{method}:{email}`.
const signInMethod = (t: Token) =>
  t.name.split(":")[1] === "oidc" ? "SSO" : "Password";

/** What revoking `t` does, said before it happens (#538). */
function revokePrompt(t: Token, fmtWhen: (iso: string) => string): string {
  if (t.is_current)
    return "Sign out? This is the session you are using: revoking it signs this browser out.";
  if (t.kind === "session")
    return `Revoke this ${signInMethod(t)} session (signed in ${fmtWhen(t.created_at)})? That browser is signed out.`;
  return `Revoke the token "${t.name}"? Anything using it loses access immediately. This cannot be undone.`;
}

export default function Tokens() {
  const qc = useQueryClient();
  const isAdmin = useIsAdmin();
  const nameId = useId();
  const expiryId = useId();
  const createHintId = useId();
  // Served by this same origin (the SPA calls the API relatively).
  const mcpUrl = `${window.location.origin}/mcp`;
  const [name, setName] = useState("");
  // The recommended default for agents (DESIGN §9): read + write.
  const [scopes, setScopes] = useState<string[]>(["read", "write"]);
  const [expiry, setExpiry] = useState<string>("never");
  const [showExpired, setShowExpired] = useState(false);
  const [replaces, setReplaces] = useState("");
  const [minted, setMinted] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [revokeError, setRevokeError] = useState<{ kind: string; message: string } | null>(null);

  const formatTimestamp = useFormatTimestamp();
  const fmtWhen = (iso: string | null | undefined) => (iso ? formatTimestamp(iso) : "never");
  const tokens = useQuery({
    queryKey: ["tokens", showExpired],
    queryFn: async () => {
      const { data, error: err } = await api.GET("/tokens", {
        params: { query: { include_expired: showExpired } },
      });
      if (err) throw new Error(errorDetail(err));
      return data ?? [];
    },
  });
  const create = useMutation({
    mutationFn: async () => {
      const expires_at =
        expiry === "never"
          ? undefined
          : new Date(Date.now() + Number(expiry) * 86_400_000).toISOString();
      const { data, error: err } = await api.POST("/tokens", {
        body: {
          name,
          scopes,
          expires_at,
          ...(replaces ? { replaces_token_id: replaces } : {}),
        },
      });
      if (err) throw new Error(errorDetail(err));
      return data!;
    },
    onSuccess: (data) => {
      setMinted(data.token);
      setName("");
      setReplaces("");
      qc.invalidateQueries({ queryKey: ["tokens"] });
    },
    onError: (e) => setError(e.message),
  });

  const revoke = async (t: Token) => {
    if (!window.confirm(revokePrompt(t, fmtWhen))) return;
    setRevokeError(null);
    try {
      if (t.is_current) {
        // Revoking the session you are using IS logging out.
        const { error: err } = await api.POST("/auth/logout");
        if (err) throw new Error(errorDetail(err));
        window.location.href = "/login";
        return;
      }
      const { error: err } = await api.DELETE("/tokens/{token_id}", {
        params: { path: { token_id: t.id } },
      });
      if (err) throw new Error(errorDetail(err));
    } catch (e) {
      setRevokeError({
        kind: t.kind,
        message: `Could not revoke: ${e instanceof Error ? e.message : String(e)}`,
      });
      return;
    }
    qc.invalidateQueries({ queryKey: ["tokens"] });
  };

  const all: Token[] = tokens.data ?? [];
  const apiTokens = all.filter((t) => t.kind === "api");
  const sessions = all.filter((t) => t.kind === "session");
  // Why "Create token" is disabled, so it never disables silently.
  const createHint = !name.trim()
    ? "Enter a token name."
    : scopes.length === 0
      ? "Choose at least one scope."
      : null;

  return (
    <div className="space-y-6">
      {isAdmin ? (
        <Card>
          <CardTitle>Create an API token</CardTitle>
          <p className="mb-4 text-sm text-muted-foreground">
            For agents, the recommended scopes are <b>read + write</b>: your agent
            acts for you. Every write it makes is attributed to its token in the
            audit log and can be reversed, and when it acts on its own idea it
            proposes instead, so you decide. Only this admin page mints tokens —
            an agent can never create or escalate its own access.
          </p>
          <p className="mb-4 text-sm text-muted-foreground">
            Documentation:{" "}
            <a
              href="/docs"
              target="_blank"
              rel="noreferrer"
              className="underline"
            >
              API reference (Swagger)
            </a>
            ,{" "}
            <a
              href="/redoc"
              target="_blank"
              rel="noreferrer"
              className="underline"
            >
              ReDoc
            </a>
            ,{" "}
            <a
              href="/llms.txt"
              target="_blank"
              rel="noreferrer"
              className="underline"
            >
              llms.txt (agent guide)
            </a>
            . MCP clients connect to{" "}
            <code className="break-all rounded bg-muted px-1">{mcpUrl}</code>{" "}
            using the same Bearer tokens.{" "}
            <CopyButton text={mcpUrl} label="Copy MCP URL" />
          </p>
          <div className="mb-4 space-y-1">
            <Label htmlFor={nameId}>Token name</Label>
            <Input
              id={nameId}
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="my-agent"
            />
          </div>
          <div className="mb-4 space-y-2">
            <label className="block space-y-1 text-sm">
              <span>Integration identity</span>
              <select
                className="h-9 w-full rounded-md border border-border bg-card px-3"
                value={replaces}
                onChange={(e) => setReplaces(e.target.value)}
              >
                <option value="">New integration</option>
                {apiTokens.map((t) => (
                  <option key={t.id} value={t.id}>
                    Replace credential for {t.name}
                  </option>
                ))}
              </select>
            </label>
            {replaces && (
              <p className="text-sm text-muted-foreground">
                The new credential keeps the same integration in audit history.
                Update the integrator, then revoke the old credential; it remains
                valid until you do.
              </p>
            )}
            {SCOPES.map(([scope, help]) => (
              <label key={scope} className="flex items-start gap-2 text-sm">
                <input
                  type="checkbox"
                  className="mt-1"
                  checked={scopes.includes(scope)}
                  onChange={(e) =>
                    setScopes(
                      e.target.checked
                        ? [...scopes, scope]
                        : scopes.filter((s) => s !== scope),
                    )
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
            <Label htmlFor={expiryId}>Expires</Label>
            <select
              id={expiryId}
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
          <Button
            disabled={!!createHint || create.isPending}
            aria-describedby={createHint ? createHintId : undefined}
            onClick={() => {
              setError(null);
              setMinted(null);
              create.mutate();
            }}
          >
            Create token
          </Button>
          {createHint && (
            <p id={createHintId} className="mt-2 text-xs text-muted-foreground">
              {createHint}
            </p>
          )}
          <ErrorText>{error}</ErrorText>
          {minted && (
            <div className="mt-4 rounded-md border border-success/40 bg-success-soft p-3">
              <p className="mb-1 text-sm font-medium text-foreground">
                Copy it now — shown exactly once:
              </p>
              <div className="flex flex-wrap items-center gap-2">
                <code className="min-w-0 break-all text-xs">{minted}</code>
                <CopyButton text={minted} label="Copy token" />
              </div>
              <p className="mb-2 mt-4 text-sm font-medium text-foreground">
                Connect an agent with it:
              </p>
              <ClientInstructions token={minted} />
            </div>
          )}
        </Card>
      ) : (
        <Card>
          <CardTitle>Connect an agent</CardTitle>
          <p className="text-sm text-muted-foreground">
            Only an admin can create API tokens. MCP clients connect to{" "}
            <code className="break-all rounded bg-muted px-1">{mcpUrl}</code>{" "}
            with one. Below you can sign out your other browser sessions.
          </p>
        </Card>
      )}

      <Card>
        <CardTitle>API tokens</CardTitle>
        <p className="mb-4 text-sm text-muted-foreground">
          Long-lived credentials for agents and integrations (MCP, scripts).
        </p>
        <Table>
          <thead>
            <tr>
              <Th>Name</Th>
              <Th>Scopes</Th>
              <Th>Created</Th>
              <Th>Last used</Th>
              <Th>Expires</Th>
              <Th />
            </tr>
          </thead>
          <tbody>
            {apiTokens.map((t) => (
              <tr key={t.id}>
                <Td>{t.name}</Td>
                <Td className="space-x-1">
                  {t.scopes.map((s) => (
                    <Badge key={s}>{s}</Badge>
                  ))}
                </Td>
                <Td>{fmtWhen(t.created_at)}</Td>
                <Td>{fmtWhen(t.last_used_at)}</Td>
                <Td className="space-x-1">
                  <span>
                    {t.expires_at ? formatTimestamp(t.expires_at) : "—"}
                  </span>
                  {isExpired(t) && <Badge variant="red">expired</Badge>}
                </Td>
                <Td>
                  <Button
                    size="sm"
                    variant="destructive"
                    onClick={() => revoke(t)}
                  >
                    Revoke
                  </Button>
                </Td>
              </tr>
            ))}
          </tbody>
        </Table>
        <QueryState
          query={tokens}
          what="API tokens"
          empty="No API tokens yet."
          isEmpty={() => apiTokens.length === 0}
        />
        <ErrorText>{revokeError?.kind === "api" ? revokeError.message : null}</ErrorText>
      </Card>

      <Card>
        <CardTitle>Login sessions</CardTitle>
        <p className="mb-4 text-sm text-muted-foreground">
          Each browser sign-in creates a session that expires automatically
          after a period of inactivity. Dead sessions are removed by a
          background job; revoking one signs that browser out.
          {isAdmin && (
            <>
              {" "}
              How long a sign-in lasts is set under{" "}
              <Link to="/settings/security" className="underline">
                Security
              </Link>
              .
            </>
          )}
        </p>
        <Table>
          <thead>
            <tr>
              <Th>Signed in via</Th>
              <Th>Created</Th>
              <Th>Last used</Th>
              <Th>Expires</Th>
              <Th />
            </tr>
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
                <Td>
                  {t.expires_at ? formatTimestamp(t.expires_at) : "—"}
                </Td>
                <Td>
                  <Button
                    size="sm"
                    variant={t.is_current ? "outline" : "destructive"}
                    onClick={() => revoke(t)}
                  >
                    {t.is_current ? "Sign out" : "Revoke"}
                  </Button>
                </Td>
              </tr>
            ))}
          </tbody>
        </Table>
        <QueryState
          query={tokens}
          what="login sessions"
          empty="No active sessions."
          isEmpty={() => sessions.length === 0}
        />
        <ErrorText>{revokeError?.kind === "session" ? revokeError.message : null}</ErrorText>
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
