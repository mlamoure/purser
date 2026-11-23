import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { api, errorDetail } from "../../lib/api";
import { useFormatTimestamp } from "../../lib/clock";
import {
  Badge,
  Button,
  Card,
  CardTitle,
  ErrorText,
  Input,
} from "../../components/ui";
import type { components, paths } from "../../api/schema";

type Event = components["schemas"]["ActivityOut"];
type Filters = NonNullable<
  paths["/audit/activity"]["get"]["parameters"]["query"]
>;
const VIEWS = [
  ["sharing", "Data shared"],
  ["changes", "Changes made"],
  ["access", "Access & permissions"],
  ["technical", "Technical activity"],
] as const;
const OUTCOMES: Record<string, string> = {
  shared: "Returned by Purser",
  empty: "No matching records",
  denied: "Denied",
  failed: "Failed",
  uncertain: "Delivery uncertain",
  legacy: "Historical entry",
  changed: "Changed",
};
const FIELDS: Record<string, string> = {
  payee: "Merchant names",
  description: "Descriptions",
  amount: "Individual amounts",
  external_ids: "External identifiers",
};
const ENTITIES: Record<string, string> = {
  account: "Account information",
  transaction: "Transactions",
  holding: "Holdings",
  forecast: "Cash-flow forecast",
  networth: "Net worth",
  series: "Financial series",
  schedule: "Scheduled transactions",
  memory: "Financial notes",
  rule: "Rules",
  suggestion: "Suggestions",
  position: "Positions",
  lot: "Tax lots",
  award: "Equity awards",
  api_token: "Access token",
  auth: "Sign-in",
  user: "User access",
  config: "Settings",
};
const selectClass =
  "h-9 w-full min-w-0 rounded-md border border-border bg-card px-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";
const words = (s: string) => s.replaceAll("_", " ").replaceAll(".", " · ");
const fieldList = (fields: string[]) =>
  fields.map((f) => FIELDS[f] ?? words(f)).join(", ");
const localInput = (iso: string) => {
  const date = new Date(iso);
  return Number.isNaN(date.getTime())
    ? ""
    : new Date(date.getTime() - date.getTimezoneOffset() * 60_000)
        .toISOString()
        .slice(0, 16);
};

function Changes({ value }: { value: Record<string, unknown> }) {
  const object = (x: unknown): Record<string, unknown> =>
    x && typeof x === "object" && !Array.isArray(x)
      ? (x as Record<string, unknown>)
      : {};
  const before = object(value.before),
    after = object(value.after);
  const keys = [
    ...new Set([...Object.keys(before), ...Object.keys(after)]),
  ].filter((key) => JSON.stringify(before[key]) !== JSON.stringify(after[key]));
  const show = (x: unknown) =>
    x === undefined
      ? "Not present"
      : x === null
        ? "Not set"
        : typeof x === "object"
          ? JSON.stringify(x)
          : String(x);
  return (
    <div>
      <p className="mb-2 font-medium">Recorded changes</p>
      <dl className="space-y-2">
        {keys.map((key) => (
          <div key={key} className="rounded bg-muted p-3">
            <dt className="font-medium capitalize">{words(key)}</dt>
            <dd className="mt-1 grid gap-2 break-words sm:grid-cols-2">
              <span>Before: {show(before[key])}</span>
              <span>After: {show(after[key])}</span>
            </dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

function EventDetails({ event }: { event: Event }) {
  const formatTimestamp = useFormatTimestamp();
  const shared = event.sharing;
  const hasContents =
    !shared ||
    !!shared.data_types?.length ||
    shared.record_count != null ||
    shared.partial ||
    shared.response_has_more;
  const hasCoverage =
    !!shared?.accounts?.length ||
    !!shared?.requested_accounts?.length ||
    !!shared?.returned_dates ||
    Object.keys(shared?.requested_coverage ?? {}).length > 0;
  const privacy = event.privacy;
  const hasPrivacy =
    !!privacy ||
    !!shared?.withheld?.length ||
    !!event.extra_fields.length ||
    !!event.requested_extra.length;
  const grant = event.identity.authorization;
  const grantBy =
    typeof grant?.granted_by === "string" ? grant.granted_by : null;
  const grantAt =
    typeof grant?.granted_at === "string" ? grant.granted_at : null;
  const related = new URLSearchParams({
    view: "technical",
    request_id: event.request_id ?? "",
    since: "1970-01-01T00:00:00.000Z",
    until: new Date().toISOString(),
  });
  return (
    <div className="space-y-4 border-t border-border p-4 text-sm">
      <dl className="grid gap-4 sm:grid-cols-2">
        <div>
          <dt className="font-medium">Recipient / actor</dt>
          <dd className="mt-1 break-words">
            {event.identity.name}
            {event.identity.user_name
              ? ` · acting for ${event.identity.user_name}`
              : ""}
          </dd>
          {!event.identity.historical && (
            <dd className="text-muted-foreground">
              Current credential label; historical identity was not captured.
            </dd>
          )}
        </div>
        <div>
          <dt className="font-medium">Authorization at the time</dt>
          <dd className="mt-1">
            {event.identity.historical
              ? (event.identity.scopes ?? []).join(", ") ||
                "No credential permissions recorded"
              : "Historical permissions were not recorded."}
          </dd>
          <dd className="text-muted-foreground">
            {grantBy && grantAt
              ? `Access granted by ${grantBy} on ${formatTimestamp(grantAt)}.`
              : "Original access grant was not recorded."}
          </dd>
          {grant && (
            <dd className="text-muted-foreground">
              This authorizes the integration; it is not approval of each
              request.
            </dd>
          )}
        </div>
        {hasContents && (
          <div>
            <dt className="font-medium">Response contents</dt>
            {!!shared?.data_types?.length && (
              <dd className="mt-1">{shared.data_types.join(", ")}</dd>
            )}
            {!shared && <dd>Detailed sharing information was not recorded.</dd>}
            {shared?.record_count != null && (
              <dd>
                {shared.record_count} {shared.count_unit} returned
              </dd>
            )}
            {shared?.partial && (
              <dd className="text-warning">
                Partial field inventory; additional text or fields may be present.
              </dd>
            )}
            {shared?.response_has_more && (
              <dd className="text-muted-foreground">
                This response was paginated or truncated. Other pages require
                separate requests.
              </dd>
            )}
          </div>
        )}
        {hasCoverage && (
          <div>
            <dt className="font-medium">Coverage</dt>
            {!!shared?.accounts?.length && (
              <dd className="mt-1 break-words">
                Accounts in response: {shared.accounts.join(", ")}
              </dd>
            )}
            {!!shared?.requested_accounts?.length && (
              <dd>Requested accounts: {shared.requested_accounts.join(", ")}</dd>
            )}
            {shared?.returned_dates && (
              <dd>
                Dates in response: {shared.returned_dates.from} to{" "}
                {shared.returned_dates.to}
              </dd>
            )}
            {shared &&
              Object.entries(shared.requested_coverage ?? {}).map(
                ([key, value]) => (
                  <dd key={key}>
                    Requested {words(key)}: {value}
                  </dd>
                ),
              )}
          </div>
        )}
        {hasPrivacy && (
          <div>
            <dt className="font-medium">Privacy</dt>
            {privacy && privacy.requested.length > 0 && (
              <dd className="mt-1">
                Privacy requested: the caller asked to leave out{" "}
                {fieldList(privacy.requested).toLowerCase()}.
              </dd>
            )}
            {(privacy?.withheld.length ?? shared?.withheld?.length ?? 0) > 0 && (
              <dd>
                Withheld from this answer:{" "}
                {fieldList(privacy?.withheld ?? shared?.withheld ?? [])}
              </dd>
            )}
            {privacy && privacy.identifiable.length > 0 && (
              <dd>
                Identifiable data returned: {fieldList(privacy.identifiable)}
              </dd>
            )}
            {privacy &&
              privacy.requested.length === 0 &&
              privacy.identifiable.length === 0 &&
              (event.outcome === "shared" || event.outcome === "empty") && (
                <dd className="text-muted-foreground">
                  No merchant names, descriptions or individual amounts were
                  returned.
                </dd>
              )}
            {!!event.extra_fields.length && (
              <dd>
                Included on request: {fieldList(event.extra_fields)}
              </dd>
            )}
            {!!event.requested_extra.length && (
              <dd>
                Additional fields requested: {fieldList(event.requested_extra)}. A
                request does not prove those fields were returned.
              </dd>
            )}
          </div>
        )}
        <div>
          <dt className="font-medium">Outcome and evidence</dt>
          <dd className="mt-1">{OUTCOMES[event.outcome] ?? event.outcome}</dd>
          <dd>{formatTimestamp(event.at)}</dd>
          <dd className="break-all text-xs text-muted-foreground">
            Event {event.id}
          </dd>
          <dd className="text-muted-foreground">
            {event.completion_point === "http_send"
              ? "HTTP response completion"
              : event.completion_point
                ? "MCP result returned to the transport"
                : "Completion point not recorded"}
            . Downstream receipt or forwarding is not verified.
          </dd>
        </div>
      </dl>
      {event.before_after && <Changes value={event.before_after} />}
      <div className="flex flex-wrap gap-4">
        {event.request_id && (
          <Link className="underline" to={`/settings/audit?${related}`}>
            View related activity
          </Link>
        )}
        {event.identity.kind === "api" && (
          <Link className="underline" to="/settings/agents">
            Manage integration access
          </Link>
        )}
      </div>
      <details>
        <summary className="cursor-pointer font-medium">
          Technical details
        </summary>
        <p className="mt-2 break-all">
          {event.action} · {event.channel ?? "channel not recorded"}
        </p>
        <p className="break-all">
          Credential: {event.identity.token_id ?? "none"}
        </p>
        <p className="break-all">
          Integration: {event.identity.integration_id ?? "not recorded"}
        </p>
        {!!shared?.fields?.length && (
          <p className="break-words">
            Observed field paths: {shared.fields.join(", ")}
          </p>
        )}
        <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-all rounded bg-muted p-3 text-xs">
          {JSON.stringify(event.technical, null, 2)}
        </pre>
      </details>
    </div>
  );
}

function EventCard({ event }: { event: Event }) {
  const formatTimestamp = useFormatTimestamp();
  const shared = event.sharing;
  const contents = shared
    ? [
        ...(shared.record_count != null
          ? [`${shared.record_count} ${shared.count_unit}`]
          : []),
        ...(shared.data_types?.length ? [shared.data_types.join(", ")] : []),
      ].join(" · ")
    : "Detailed sharing information was not recorded.";
  return (
    <details className="min-w-0 rounded-lg border border-border bg-card">
      <summary className="cursor-pointer rounded-lg p-4 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
        <span className="ml-1 font-medium">{event.identity.name}</span>
        <span className="ml-2 text-xs text-muted-foreground">
          {event.identity.kind === "api"
            ? "Integration"
            : event.identity.kind === "session"
              ? "Browser session"
              : "System"}
        </span>
        <div className="mt-2 grid min-w-0 gap-2 sm:grid-cols-[1fr_1fr_auto]">
          <div className="min-w-0">
            <p className="font-medium">
              {event.kind === "change"
                ? words(event.action)
                : (ENTITIES[event.entity_type] ?? words(event.entity_type))}
            </p>
            {contents && (
              <p className="break-words text-sm text-muted-foreground">
                {contents}
              </p>
            )}
          </div>
          <div className="min-w-0 text-sm">
            {!!shared?.accounts?.length && (
              <p className="break-words">{shared.accounts.join(", ")}</p>
            )}
            <time dateTime={event.at} className="text-xs text-muted-foreground">
              {formatTimestamp(event.at)}
            </time>
          </div>
          <div className="flex flex-wrap items-start gap-1 sm:max-w-48 sm:justify-end">
            <Badge
              variant={
                event.outcome === "denied" || event.outcome === "failed"
                  ? "red"
                  : event.outcome === "uncertain"
                    ? "amber"
                    : "default"
              }
            >
              {OUTCOMES[event.outcome] ?? event.outcome}
            </Badge>
            {!!event.privacy?.requested.length && (
              <Badge variant="green">Privacy used</Badge>
            )}
            {!!event.privacy?.identifiable.length && (
              <Badge variant="amber">Identifiable</Badge>
            )}
            {!!event.extra_fields.length && <Badge>External ids included</Badge>}
          </div>
        </div>
      </summary>
      <EventDetails event={event} />
    </details>
  );
}

export default function AuditLog() {
  const formatTimestamp = useFormatTimestamp();
  const [params, setParams] = useSearchParams();
  const [anchor, setAnchor] = useState(() => new Date().toISOString());
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);
  const view =
    VIEWS.find(([key]) => key === params.get("view"))?.[0] ?? "sharing";
  const since =
    params.get("since") ??
    new Date(new Date(anchor).getTime() - 7 * 86_400_000).toISOString();
  const until = params.get("until") ?? anchor;
  const filters: Filters = {
    view,
    since,
    until,
    extra: params.get("extra") === "true",
    ...(params.get("privacy")
      ? { privacy: params.get("privacy") as Filters["privacy"] }
      : {}),
    ...(params.get("integration_id")
      ? { integration_id: params.get("integration_id")! }
      : {}),
    ...(params.get("account_id")
      ? { account_id: params.get("account_id")! }
      : {}),
    ...(params.get("data_type") ? { data_type: params.get("data_type")! } : {}),
    ...(params.get("outcome")
      ? { outcome: params.get("outcome") as Filters["outcome"] }
      : {}),
    ...(params.get("request_id")
      ? { request_id: params.get("request_id")! }
      : {}),
  };
  const update = (key: string, value: string) => {
    const next = new URLSearchParams(params);
    next.set("since", since);
    next.set("until", until);
    value ? next.set(key, value) : next.delete(key);
    setParams(next);
  };
  const options = useQuery({
    queryKey: ["audit-options"],
    queryFn: async () => {
      const { data, error } = await api.GET("/audit/options");
      if (error) throw error;
      return data!;
    },
  });
  const events = useInfiniteQuery({
    queryKey: ["audit-activity", filters],
    initialPageParam: null as string | null,
    queryFn: async ({ pageParam }) => {
      const { data, error } = await api.GET("/audit/activity", {
        params: {
          query: {
            ...filters,
            limit: 50,
            ...(pageParam ? { cursor: pageParam } : {}),
          },
        },
      });
      if (error) throw error;
      return data!;
    },
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
  const rows = events.data?.pages.flatMap((p) => p.items) ?? [];
  const exportRows = async (format: "csv" | "json") => {
    setExporting(true);
    setExportError(null);
    try {
      const { data, error } = await api.GET("/audit/export", {
        params: { query: { ...filters, format } },
        parseAs: "blob",
      });
      if (error || !data) throw error ?? new Error("Export unavailable");
      const url = URL.createObjectURL(data);
      const link = document.createElement("a");
      link.href = url;
      link.download = `purser-audit.${format}`;
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) {
      setExportError(errorDetail(error));
    } finally {
      setExporting(false);
    }
  };
  const range = (days: number) => {
    const end = new Date().toISOString();
    setAnchor(end);
    const next = new URLSearchParams(params);
    next.set(
      "since",
      new Date(new Date(end).getTime() - days * 86_400_000).toISOString(),
    );
    next.set("until", end);
    setParams(next);
  };
  return (
    <div className="min-w-0 space-y-4">
      <Card>
        <CardTitle>Audit log</CardTitle>
        <p className="mb-4 text-sm text-muted-foreground">
          See what Purser returned to authorized integrations, what changed, and
          who granted access. Purser cannot verify what an integration
          subsequently forwarded.
        </p>
        <nav aria-label="Audit views" className="mb-5 flex flex-wrap gap-2">
          {VIEWS.map(([key, label]) => (
            <Button
              key={key}
              variant={view === key ? "default" : "outline"}
              aria-pressed={view === key}
              onClick={() => update("view", key)}
            >
              {label}
            </Button>
          ))}
        </nav>
        <div className="grid min-w-0 gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <label className="min-w-0 space-y-1 text-sm">
            <span>Integrator</span>
            <select
              aria-label="Integrator"
              className={selectClass}
              value={filters.integration_id ?? ""}
              onChange={(e) => update("integration_id", e.target.value)}
            >
              <option value="">All integrators</option>
              {options.data?.integrations.map((x) => (
                <option key={x.id} value={x.id}>
                  {x.name} · {x.id.slice(0, 8)}
                </option>
              ))}
            </select>
          </label>
          <label className="min-w-0 space-y-1 text-sm">
            <span>Account</span>
            <select
              aria-label="Account"
              className={selectClass}
              value={filters.account_id ?? ""}
              onChange={(e) => update("account_id", e.target.value)}
            >
              <option value="">All accounts</option>
              {options.data?.accounts.map((x) => (
                <option key={x.id} value={x.id}>
                  {x.name}
                </option>
              ))}
            </select>
          </label>
          <label className="min-w-0 space-y-1 text-sm">
            <span>Data type</span>
            <select
              aria-label="Data type"
              className={selectClass}
              value={filters.data_type ?? ""}
              onChange={(e) => update("data_type", e.target.value)}
            >
              <option value="">All data types</option>
              {options.data?.data_types.map((x) => (
                <option key={x} value={x}>
                  {x}
                </option>
              ))}
            </select>
          </label>
          <label className="min-w-0 space-y-1 text-sm">
            <span>Outcome</span>
            <select
              aria-label="Outcome"
              className={selectClass}
              value={filters.outcome ?? ""}
              onChange={(e) => update("outcome", e.target.value)}
            >
              <option value="">All outcomes</option>
              {Object.entries(OUTCOMES).map(([key, label]) => (
                <option key={key} value={key}>
                  {label}
                </option>
              ))}
            </select>
          </label>
          <label className="min-w-0 space-y-1 text-sm">
            <span>Privacy use</span>
            <select
              aria-label="Privacy use"
              className={selectClass}
              value={filters.privacy ?? ""}
              onChange={(e) => update("privacy", e.target.value)}
            >
              <option value="">All calls</option>
              <option value="requested">Privacy used (fields withheld on request)</option>
              <option value="identifiable">Identifiable data returned</option>
            </select>
          </label>
          <label className="min-w-0 space-y-1 text-sm">
            <span>From (local time)</span>
            <Input
              type="datetime-local"
              value={localInput(since)}
              onChange={(e) => {
                if (e.target.value)
                  update("since", new Date(e.target.value).toISOString());
              }}
            />
          </label>
          <label className="min-w-0 space-y-1 text-sm">
            <span>Through (local time)</span>
            <Input
              type="datetime-local"
              value={localInput(until)}
              onChange={(e) => {
                if (e.target.value)
                  update("until", new Date(e.target.value).toISOString());
              }}
            />
          </label>
          <label className="flex items-center gap-2 text-sm sm:col-span-2">
            <input
              type="checkbox"
              checked={filters.extra}
              onChange={(e) => update("extra", e.target.checked ? "true" : "")}
            />
            Only calls that included external identifiers
          </label>
        </div>
        <div className="mt-4 flex flex-wrap items-center gap-2">
          {[1, 7, 30].map((days) => (
            <Button
              key={days}
              variant="outline"
              size="sm"
              onClick={() => range(days)}
            >
              {days === 1 ? "Last 24 hours" : `Last ${days} days`}
            </Button>
          ))}
          <Button
            variant="ghost"
            size="sm"
            onClick={() => {
              setParams({});
              setAnchor(new Date().toISOString());
            }}
          >
            Reset filters
          </Button>
          <Button
            variant="outline"
            size="sm"
            disabled={exporting || !!events.error}
            onClick={() => exportRows("csv")}
          >
            Export CSV
          </Button>
          <Button
            variant="outline"
            size="sm"
            disabled={exporting || !!events.error}
            onClick={() => exportRows("json")}
          >
            Export JSON
          </Button>
        </div>
        <ErrorText>{exportError}</ErrorText>
        {params.get("request_id") && (
          <p className="mt-3 break-all text-sm">
            Related request: {params.get("request_id")}{" "}
            <button
              className="underline"
              onClick={() => update("request_id", "")}
            >
              Clear
            </button>
          </p>
        )}
        {options.data && (
          <p className="mt-4 text-xs text-muted-foreground">
            Retention: {options.data.retention_days} days. Earliest retained
            entry:{" "}
            {options.data.retained_since
              ? formatTimestamp(options.data.retained_since)
              : "none"}
            . Exports include all matching retained entries, not just loaded
            rows.
          </p>
        )}
        {(filters.account_id ||
          filters.data_type ||
          filters.extra ||
          filters.privacy) && (
          <p className="mt-2 text-xs text-muted-foreground">
            These filters require recorded response evidence. Historical entries
            without that evidence cannot match them.
          </p>
        )}
        {options.data && Number(options.data.capture_health.failures) > 0 && (
          <p
            role="status"
            className="mt-3 rounded border border-warning/40 p-3 text-sm"
          >
            Audit capture or completion failed{" "}
            {String(options.data.capture_health.failures)} time(s) in this
            server process since {formatTimestamp(String(options.data.capture_health.since))}.
            Integration data is withheld when the initial record cannot be
            saved. Missing completion evidence is shown as delivery uncertain.
          </p>
        )}
        <ErrorText>
          {options.error ? errorDetail(options.error) : null}
        </ErrorText>
      </Card>
      {events.isLoading && (
        <p role="status" className="p-4 text-sm">
          Loading audit entries…
        </p>
      )}
      <ErrorText>{events.error ? errorDetail(events.error) : null}</ErrorText>
      {events.isSuccess && !rows.length && (
        <Card>
          <p>No recorded activity matches these filters.</p>
          <p className="mt-1 text-sm text-muted-foreground">
            Try a wider date range or reset the filters. This does not establish
            that no sharing occurred outside the retained history.
          </p>
        </Card>
      )}
      <div className="min-w-0 space-y-3" aria-label="Audit entries">
        {rows.map((event) => (
          <EventCard key={event.id} event={event} />
        ))}
      </div>
      {!!rows.length && (
        <div className="flex flex-wrap items-center gap-3">
          <p className="text-sm text-muted-foreground">
            {rows.length} entries loaded
            {events.hasNextPage
              ? " · more available"
              : " · all matching entries loaded"}
          </p>
          {events.hasNextPage && (
            <Button
              variant="outline"
              disabled={events.isFetchingNextPage}
              onClick={() => events.fetchNextPage()}
            >
              {events.isFetchingNextPage ? "Loading…" : "Load older entries"}
            </Button>
          )}
        </div>
      )}
    </div>
  );
}
