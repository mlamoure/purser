import { queryOptions, useQuery } from "@tanstack/react-query";
import type { components } from "../../api/schema";
import { api, errorDetail } from "../../lib/api";
import { QueryState } from "../../components/QueryState";
import { Card, CardTitle } from "../../components/ui";
import { CopyButton } from "../wizard/CopyButton";

type Network = components["schemas"]["NetworkOut"];

/** GET /config/network (#632): how this Purser is reached, read from the
 * deployment's environment. Admin only, like the rest of /config. */
export const networkQuery = queryOptions({
  queryKey: ["config", "network"],
  queryFn: async () => {
    const { data, error } = await api.GET("/config/network");
    if (error) throw new Error(errorDetail(error));
    return data!;
  },
});

/** The port agents use, for the switch steps: the public URL's own. */
function portOf(url: string): string {
  try {
    const u = new URL(url);
    return u.port || (u.protocol === "https:" ? "443" : "80");
  } catch {
    return "8000";
  }
}

function hostOf(requestHost: string): string {
  return requestHost.startsWith("[")
    ? requestHost.slice(0, requestHost.indexOf("]") + 1)
    : requestHost.split(":")[0];
}

const Code = ({ children }: { children: string }) => (
  <code className="rounded bg-muted px-1 py-0.5 font-mono text-xs">{children}</code>
);

const PLAIN_HTTP =
  "On the home network the admin password and agent tokens travel unencrypted (plain HTTP), so do this only on a network you trust.";

function Mode({ net }: { net: Network }) {
  switch (net.reach) {
    case "this_machine":
      return (
        <p>
          Reachable from: <strong>this machine only</strong>.
        </p>
      );
    case "home_network":
      return (
        <p>
          Reachable from: <strong>your home network</strong> at <Code>{net.public_url}</Code>.
        </p>
      );
    case "reverse_proxy":
      return (
        <p>
          Reachable through: <strong>a reverse proxy</strong> at <Code>{net.public_url}</Code>.
          Purser serves plain HTTP behind it; the proxy decides who can reach that address.
        </p>
      );
    default:
      return (
        <p>
          Reachable from: <strong>not reported</strong>. This deployment does not tell Purser how
          its port is published (Docker Compose passes <Code>PURSER_BIND_ADDRESS</Code>), so this
          page does not guess.
        </p>
      );
  }
}

/** What the person can do from here: open Purser to the home network, or
 * close it again. Only Compose installs report a mode these steps fit. */
function Switch({ net }: { net: Network }) {
  const guide = (
    <a className="underline" href={net.guide_url} target="_blank" rel="noreferrer">
      the setup guide
    </a>
  );
  if (net.reach === "this_machine") {
    const lines = [
      "PURSER_BIND_ADDRESS=0.0.0.0",
      `PURSER_PUBLIC_URL=http://<this machine's address>:${portOf(net.public_url)}`,
      "PURSER_ALLOWED_HOSTS=<this machine's address>,<its name>,<its name>.local",
    ].join("\n");
    return (
      <div className="space-y-2">
        <p>
          To use Purser from other machines on your home network, add these lines to the{" "}
          <Code>.env</Code> file next to <Code>docker-compose.yml</Code> on this machine, then run{" "}
          <Code>docker compose up -d</Code>. The command in {guide} fills them in for you.
        </p>
        {/* Focusable: at phone width the lines scroll sideways, and a keyboard
            user must be able to scroll them (WCAG 2.1.1). */}
        <pre tabIndex={0} className="overflow-auto rounded bg-muted p-2 font-mono text-[11px]">
          {lines}
        </pre>
        <p>{PLAIN_HTTP}</p>
      </div>
    );
  }
  if (net.reach === "home_network") {
    return (
      <div className="space-y-2">
        <p>
          To limit Purser to this machine again, remove the <Code>PURSER_BIND_ADDRESS</Code>,{" "}
          <Code>PURSER_PUBLIC_URL</Code> and <Code>PURSER_ALLOWED_HOSTS</Code> lines from{" "}
          <Code>.env</Code> and run <Code>docker compose up -d</Code>. See {guide}.
        </p>
        <p>{PLAIN_HTTP}</p>
      </div>
    );
  }
  return null;
}

/** Warnings about the configuration itself, from what the server reports. */
function Problems({ net }: { net: Network }) {
  if (net.reach === "home_network" && net.public_url_is_local) {
    return (
      <p role="alert" className="text-warning">
        <Code>PURSER_PUBLIC_URL</Code> still says <Code>{net.public_url}</Code>: agents on other
        machines are refused until it is this machine's address (or they use a name in{" "}
        <Code>PURSER_ALLOWED_HOSTS</Code>).
      </p>
    );
  }
  if (net.reach === "this_machine" && !net.public_url_is_local) {
    return (
      <p role="alert" className="text-warning">
        <Code>PURSER_PUBLIC_URL</Code> is <Code>{net.public_url}</Code>, but Purser's port is
        published on this machine only: other machines reach that address only through a proxy or
        tunnel you run on this machine.
      </p>
    );
  }
  return null;
}

/** The admin UI is open at an address the MCP endpoint refuses (421), so
 * connection steps copied from this page would not work for an agent. */
function Mismatch({ net }: { net: Network }) {
  if (net.request_host_accepted) return null;
  const names = net.allowed_hosts.length ? `, ${net.allowed_hosts.join(", ")}` : "";
  return (
    <p role="alert" className="text-warning">
      You opened Purser at <Code>{net.request_host}</Code>, an address agents cannot use: Purser
      answers agents only at <Code>{net.public_url}</Code>
      {names}
      {" "}and <Code>localhost</Code> on its own machine. Give agents <Code>{net.mcp_url}</Code>, or
      add <Code>{hostOf(net.request_host)}</Code> to <Code>PURSER_ALLOWED_HOSTS</Code> in{" "}
      <Code>.env</Code> and run <Code>docker compose up -d</Code>.
    </p>
  );
}

/** Settings -> Agents & API (#632): whether Purser can be reached from this
 * machine only or from the home network, the address agents connect to, how
 * to switch, and a hint when this page is open at an address agents cannot
 * use. Admin only: the route is. */
export default function Reachability() {
  const query = useQuery(networkQuery);
  return (
    <Card>
      <CardTitle>How Purser is reached</CardTitle>
      <QueryState query={query} what="how Purser is reached">
        {(net) => (
          <div className="space-y-3 text-sm">
            <Mode net={net} />
            <div className="flex flex-wrap items-center gap-2">
              <span>Agents connect at</span>
              <Code>{net.mcp_url}</Code>
              <CopyButton text={net.mcp_url} />
            </div>
            {net.allowed_hosts.length > 0 && (
              <p className="text-muted-foreground">
                Also accepted by name: {net.allowed_hosts.join(", ")} (
                <Code>PURSER_ALLOWED_HOSTS</Code>).
              </p>
            )}
            <Problems net={net} />
            <Mismatch net={net} />
            <div className="text-muted-foreground">
              <Switch net={net} />
            </div>
          </div>
        )}
      </QueryState>
    </Card>
  );
}
