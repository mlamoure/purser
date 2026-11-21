import { useId, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { configQuery } from "../../lib/config";
import { QueryState } from "../../components/QueryState";
import { Button, Card, CardTitle, ErrorText, Input, Label } from "../../components/ui";
import { Saved, selectClass, useSaveConfig, type ConfigOut } from "./saveConfig";

/** IANA zones the browser knows, for the time zone suggestions. UTC first:
 * some engines leave it out of supportedValuesOf. */
function knownTimeZones(): string[] {
  const zones = typeof Intl.supportedValuesOf === "function" ? Intl.supportedValuesOf("timeZone") : [];
  return ["UTC", ...zones.filter((z) => z !== "UTC")];
}

function browserTimeZone(): string | null {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || null;
  } catch {
    return null;
  }
}

function TimeZoneCard({ initial }: { initial: ConfigOut }) {
  const [zone, setZone] = useState(initial.values.timezone);
  const [error, setError] = useState<string | null>(null);
  const save = useSaveConfig(setError);
  const browserZone = browserTimeZone();
  const trimmed = zone.trim();

  return (
    <Card>
      <CardTitle>Time zone</CardTitle>
      <p className="mb-4 text-sm text-muted-foreground">
        The household's time zone decides what "today" is: when a bill is due or late, the
        week of upcoming bills, net worth for today, and how old an unreviewed or stale item
        is. This admin app also shows times (sign-ins, fetches, the audit log) in it. Scheduled
        jobs keep running at their UTC times, and timestamps are stored in UTC.
      </p>
      <div className="flex flex-wrap items-end gap-2">
        <div className="min-w-48 flex-1 space-y-1">
          <Label htmlFor="household-timezone">Household time zone</Label>
          <Input
            id="household-timezone"
            list="household-timezone-options"
            value={zone}
            onChange={(e) => setZone(e.target.value)}
            placeholder="e.g. America/New_York"
          />
          <datalist id="household-timezone-options">
            {knownTimeZones().map((z) => (
              <option key={z} value={z} />
            ))}
          </datalist>
        </div>
        {browserZone && browserZone !== trimmed && (
          <Button variant="outline" onClick={() => setZone(browserZone)}>
            Use {browserZone}
          </Button>
        )}
        <Button
          disabled={save.isPending || !trimmed}
          onClick={() => {
            setError(null);
            save.mutate({ timezone: trimmed });
          }}
        >
          Save
        </Button>
      </div>
      <Saved visible={save.isSuccess} />
      <ErrorText>{error}</ErrorText>
    </Card>
  );
}

function TransfersCard({ initial }: { initial: ConfigOut }) {
  const [autoLink, setAutoLink] = useState(initial.values.transfer_auto_link);
  const [error, setError] = useState<string | null>(null);
  const save = useSaveConfig(setError);
  const autoLinkId = useId();

  return (
    <Card>
      <CardTitle>Transfers</CardTitle>
      <p className="mb-4 text-sm text-muted-foreground">
        Deterministic auto-linking: exact opposite amounts within the date window whose text
        names the transfer (keyword or counterparty account name) merge automatically — evented,
        badged in the register, one click to unlink. Amount-only pairs always stay confirm-first.
      </p>
      <div className="flex flex-wrap items-end gap-2">
        <div className="space-y-1">
          <Label htmlFor={autoLinkId}>Auto-link strong pairs</Label>
          <select
            id={autoLinkId}
            className={selectClass}
            value={autoLink ? "on" : "off"}
            onChange={(e) => setAutoLink(e.target.value === "on")}
          >
            <option value="on">On (recommended)</option>
            <option value="off">Off — everything stays a candidate</option>
          </select>
        </div>
        <Button
          disabled={save.isPending}
          onClick={() => {
            setError(null);
            save.mutate({ transfer_auto_link: autoLink });
          }}
        >
          Save
        </Button>
      </div>
      <Saved visible={save.isSuccess} />
      <ErrorText>{error}</ErrorText>
    </Card>
  );
}

function LinksCard({ initial }: { initial: ConfigOut }) {
  const [grafanaUrl, setGrafanaUrl] = useState(initial.values.grafana_url ?? "");
  const [error, setError] = useState<string | null>(null);
  const save = useSaveConfig(setError);
  const grafanaId = useId();

  return (
    <Card>
      <CardTitle>Links</CardTitle>
      <p className="mb-4 text-sm text-muted-foreground">
        Grafana base URL for dashboard deep links on tool responses. Leave blank to omit links.
      </p>
      <div className="flex flex-wrap items-end gap-2">
        <div className="min-w-48 flex-1 space-y-1">
          <Label htmlFor={grafanaId}>Grafana URL</Label>
          <Input
            id={grafanaId}
            type="url"
            value={grafanaUrl}
            onChange={(e) => setGrafanaUrl(e.target.value)}
            placeholder="https://grafana.example.com"
          />
        </div>
        <Button
          disabled={save.isPending}
          onClick={() => {
            setError(null);
            save.mutate({ grafana_url: grafanaUrl.trim() || null });
          }}
        >
          Save
        </Button>
      </div>
      <Saved visible={save.isSuccess} />
      <ErrorText>{error}</ErrorText>
    </Card>
  );
}

/** Settings -> General (#542): how the household's ledger behaves and how
 * it is shown. Admin-only, like every GET/PUT /config section. */
export default function General() {
  const config = useQuery(configQuery);
  if (!config.data) return <QueryState query={config} what="settings" />;

  return (
    <div className="space-y-6">
      <TimeZoneCard initial={config.data} />
      <TransfersCard initial={config.data} />
      <LinksCard initial={config.data} />
    </div>
  );
}
