import { Fragment, useState } from "react";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { api, errorDetail } from "../lib/api";
import { Badge, Button, Card, CardTitle, ErrorText, Table, Td, Th } from "../components/ui";
import type { components } from "../api/schema";

type SessionRow = components["schemas"]["AuditSessionOut"];
type AuditRow = components["schemas"]["AuditOut"];

const selectClass =
  "flex h-9 rounded-md border border-border bg-card px-3 py-1 text-sm shadow-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";

const fmt = (iso: string) => new Date(iso).toLocaleString();
const fmtTime = (iso: string) => new Date(iso).toLocaleTimeString();

const RANGES = [
  ["1", "Last 24 hours"],
  ["7", "Last 7 days"],
  ["30", "Last 30 days"],
] as const;

function KindBadge({ kind }: { kind: string }) {
  const variant = kind === "write" ? "amber" : kind === "change" ? "green" : "default";
  return <Badge variant={variant}>{kind}</Badge>;
}

/** Group a session's rows: one group per request_id (call row + its change
 * rows); rows without a request_id (legacy/system) stand alone. */
function groupRows(rows: AuditRow[]) {
  const groups: { key: string; call: AuditRow | null; changes: AuditRow[] }[] = [];
  const byRequest = new Map<string, (typeof groups)[number]>();
  for (const row of rows) {
    if (row.request_id) {
      let group = byRequest.get(row.request_id);
      if (!group) {
        group = { key: row.request_id, call: null, changes: [] };
        byRequest.set(row.request_id, group);
        groups.push(group);
      }
      if (row.kind === "change") group.changes.push(row);
      else group.call = row;
    } else if (row.kind === "change") {
      groups.push({ key: row.id, call: null, changes: [row] });
    } else {
      groups.push({ key: row.id, call: row, changes: [] });
    }
  }
  return groups;
}

function ChangeRow({ row }: { row: AuditRow }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="ml-6 border-l border-border pl-3 text-xs">
      <button
        type="button"
        className="text-left hover:underline"
        onClick={() => setOpen(!open)}
      >
        <span className="text-muted-foreground">{open ? "▾" : "▸"}</span>{" "}
        <code>{row.action}</code>{" "}
        <span className="text-muted-foreground">
          {row.entity_type}
          {row.entity_id ? ` ${row.entity_id.slice(0, 8)}…` : ""}
        </span>
      </button>
      {open && (
        <pre className="mt-1 max-h-64 overflow-auto rounded bg-muted p-2 text-[11px]">
          {JSON.stringify(row.before_after, null, 2)}
        </pre>
      )}
    </div>
  );
}

function CallGroup({ group }: { group: ReturnType<typeof groupRows>[number] }) {
  const call = group.call;
  const detail = (call?.detail ?? null) as {
    method?: string;
    path?: string;
    tool?: string;
    status?: number;
    ok?: boolean;
    ms?: number;
    rows?: number;
    error?: string;
  } | null;
  return (
    <div className="space-y-1 py-1">
      {call ? (
        <div className="flex items-baseline gap-2 text-sm">
          <span className="w-20 shrink-0 text-xs text-muted-foreground">{fmtTime(call.at)}</span>
          <KindBadge kind={call.kind} />
          <code className="text-xs">
            {detail?.tool ? `tool ${detail.tool}` : `${detail?.method ?? ""} ${detail?.path ?? call.action}`}
          </code>
          {detail?.status !== undefined && (
            <span className={detail.status >= 400 ? "text-xs text-red-600" : "text-xs text-muted-foreground"}>
              {detail.status}
            </span>
          )}
          {detail?.ok === false && <Badge variant="red">error</Badge>}
          {call.disclosed && (
            <Badge variant="amber">disclosed {call.disclosed.join(", ")}</Badge>
          )}
          {detail?.rows !== undefined && (
            <span className="text-xs text-muted-foreground">{detail.rows} rows</span>
          )}
          {detail?.ms !== undefined && (
            <span className="text-xs text-muted-foreground">{detail.ms} ms</span>
          )}
        </div>
      ) : (
        <div className="flex items-baseline gap-2 text-sm">
          <span className="w-20 shrink-0 text-xs text-muted-foreground">
            {group.changes[0] ? fmtTime(group.changes[0].at) : ""}
          </span>
          <span className="text-xs text-muted-foreground">(no call row)</span>
        </div>
      )}
      {detail?.error && <p className="ml-24 text-xs text-red-600">{detail.error}</p>}
      {group.changes.map((row) => (
        <ChangeRow key={row.id} row={row} />
      ))}
    </div>
  );
}

function SessionCalls({ session }: { session: SessionRow }) {
  const calls = useQuery({
    queryKey: ["audit-calls", session.actor_token_id, session.started_at, session.ended_at],
    queryFn: async () => {
      const { data, error } = await api.GET("/audit", {
        params: {
          query: {
            ...(session.actor_token_id
              ? { actor_token_id: session.actor_token_id }
              : { unattributed: true }),
            since: session.started_at,
            until: session.ended_at,
            order: "asc",
            limit: 500,
          },
        },
      });
      if (error) throw error;
      return data;
    },
  });

  if (calls.isLoading) return <p className="p-3 text-sm text-muted-foreground">Loading…</p>;
  if (calls.error) return <ErrorText>{errorDetail(calls.error)}</ErrorText>;
  const groups = groupRows(calls.data?.items ?? []);
  return (
    <div className="divide-y divide-border rounded-md bg-muted/30 px-3 py-1">
      {groups.map((group) => (
        <CallGroup key={group.key} group={group} />
      ))}
      {calls.data?.next_cursor && (
        <p className="py-2 text-xs text-muted-foreground">
          Showing the first 500 rows of this session.
        </p>
      )}
    </div>
  );
}

/** Calls where an agent asked, with `include`, for fields your privacy
 * settings withhold by default (#437): what your AI chose to see. */
function Disclosures({ days }: { days: string }) {
  const rows = useQuery({
    queryKey: ["audit-disclosed", days],
    queryFn: async () => {
      const since = new Date(Date.now() - Number(days) * 86_400_000).toISOString();
      const { data, error } = await api.GET("/audit", {
        params: { query: { disclosed: true, since, limit: 500 } },
      });
      if (error) throw error;
      return data;
    },
  });
  if (rows.error) return <ErrorText>{errorDetail(rows.error)}</ErrorText>;
  const items = rows.data?.items ?? [];
  if (rows.isSuccess && items.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">
        No agent asked for more than your privacy defaults in this window.
      </p>
    );
  }
  return (
    <Table>
      <thead>
        <tr>
          <Th>When</Th>
          <Th>Tool</Th>
          <Th>Fields disclosed</Th>
        </tr>
      </thead>
      <tbody>
        {items.map((row) => (
          <tr key={row.id}>
            <Td>{fmt(row.at)}</Td>
            <Td>
              <code className="text-xs">
                {String((row.detail as { tool?: string } | null)?.tool ?? row.action)}
              </code>
            </Td>
            <Td className="space-x-1">
              {(row.disclosed ?? []).map((f) => (
                <Badge key={f} variant="amber">
                  {f}
                </Badge>
              ))}
            </Td>
          </tr>
        ))}
      </tbody>
    </Table>
  );
}

export default function AuditLog() {
  const [days, setDays] = useState("7");
  const [view, setView] = useState<"sessions" | "disclosures">("sessions");
  const [actor, setActor] = useState("all");
  const [expanded, setExpanded] = useState<string | null>(null);

  const sessions = useInfiniteQuery({
    queryKey: ["audit-sessions", days],
    initialPageParam: null as string | null,
    queryFn: async ({ pageParam }) => {
      const since = new Date(Date.now() - Number(days) * 86_400_000).toISOString();
      const { data, error } = await api.GET("/audit/sessions", {
        params: { query: { since, ...(pageParam ? { cursor: pageParam } : {}) } },
      });
      if (error) throw error;
      return data;
    },
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });

  const all: SessionRow[] = sessions.data?.pages.flatMap((p) => p.items) ?? [];
  const actors = [...new Map(all.map((s) => [s.token_name ?? "System", true])).keys()];
  const visible = actor === "all" ? all : all.filter((s) => (s.token_name ?? "System") === actor);
  const sessionKey = (s: SessionRow) => `${s.actor_token_id ?? "system"}:${s.started_at}`;

  return (
    <Card>
      <CardTitle>Audit Log</CardTitle>
      <p className="mb-4 text-sm text-muted-foreground">
        Every authenticated call — web sessions, API tokens, and MCP agents alike — grouped
        into sessions (a new session starts after 30 minutes of silence). Expand a session
        for its calls, and a write call for the exact before/after it produced. Entries are
        kept for the retention window configured in Settings.
      </p>
      <div className="mb-4 flex gap-2">
        <select value={days} onChange={(e) => setDays(e.target.value)} className={selectClass}>
          {RANGES.map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </select>
        <select
          aria-label="View"
          value={view}
          onChange={(e) => setView(e.target.value as "sessions" | "disclosures")}
          className={selectClass}
        >
          <option value="sessions">All calls by session</option>
          <option value="disclosures">Agent disclosures beyond privacy defaults</option>
        </select>
        <select value={actor} onChange={(e) => setActor(e.target.value)} className={selectClass}>
          <option value="all">All actors</option>
          {actors.map((name) => (
            <option key={name} value={name}>
              {name}
            </option>
          ))}
        </select>
      </div>
      {view === "disclosures" ? (
        <Disclosures days={days} />
      ) : (
      <>
      {sessions.error && <ErrorText>{errorDetail(sessions.error)}</ErrorText>}
      <Table>
        <thead>
          <tr>
            <Th />
            <Th>Actor</Th>
            <Th>Session</Th>
            <Th>Reads</Th>
            <Th>Writes</Th>
            <Th>Changes</Th>
          </tr>
        </thead>
        <tbody>
          {visible.map((s) => (
            <Fragment key={sessionKey(s)}>
              <tr
                className="cursor-pointer hover:bg-muted/50"
                onClick={() => setExpanded(expanded === sessionKey(s) ? null : sessionKey(s))}
              >
                <Td className="w-6 text-muted-foreground">
                  {expanded === sessionKey(s) ? "▾" : "▸"}
                </Td>
                <Td className="space-x-2">
                  <span>{s.token_name ?? "System"}</span>
                  <Badge variant={s.token_kind === "api" ? "default" : s.token_kind === "session" ? "green" : "amber"}>
                    {s.token_kind ?? "system"}
                  </Badge>
                  {s.user_email && (
                    <span className="text-xs text-muted-foreground">{s.user_email}</span>
                  )}
                </Td>
                <Td className="text-xs text-muted-foreground">
                  {fmt(s.started_at)} → {fmtTime(s.ended_at)}
                </Td>
                <Td>{s.reads}</Td>
                <Td>{s.writes}</Td>
                <Td>{s.changes}</Td>
              </tr>
              {expanded === sessionKey(s) && (
                <tr>
                  <td colSpan={6} className="p-2">
                    <SessionCalls session={s} />
                  </td>
                </tr>
              )}
            </Fragment>
          ))}
        </tbody>
      </Table>
      {visible.length === 0 && !sessions.isLoading && (
        <p className="text-sm text-muted-foreground">No activity in this window.</p>
      )}
      {sessions.hasNextPage && (
        <Button
          variant="outline"
          size="sm"
          className="mt-3"
          disabled={sessions.isFetchingNextPage}
          onClick={() => sessions.fetchNextPage()}
        >
          Load older sessions
        </Button>
      )}
      </>
      )}
    </Card>
  );
}
