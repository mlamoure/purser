/* Host bridge (issue #72): wires a widget to an MCP Apps host via the
 * official iframe SDK. All server interaction goes through the host
 * (callServerTool / readServerResource) so it inherits the token scope
 * model — a widget never fetches Purser directly (guardrail 11).
 *
 * Standalone modes:
 *   ?demo        render the embedded synthetic fixture (screenshots, dev)
 *   (no host)    show a quiet "waiting for host" hint after a beat
 */

import { App } from "@modelcontextprotocol/ext-apps";

export interface WidgetHost {
  /** Call an MCP tool through the host; returns structuredContent or null. */
  callTool<T>(name: string, args: Record<string, unknown>): Promise<T | null>;
  /** Read an MCP resource through the host — resources/read, else the
   * read_series tool for hosts and gateways that forward tools only (#354);
   * null when neither is available or the read is denied. */
  readResource(uri: string): Promise<string | null>;
  /** Push a short note about UI state into the conversation context. */
  tellModel(text: string): void;
  readonly demo: boolean;
}

export interface WidgetSpec<T> {
  name: string;
  /** Synthetic fixture rendered in ?demo mode. Never real data. */
  demo: T;
  render(data: T, host: WidgetHost): void;
}

const WIDGET_VERSION = "0.2.0";

class DemoHost implements WidgetHost {
  readonly demo = true;

  async callTool<T>(): Promise<T | null> {
    return null;
  }

  async readResource(): Promise<string | null> {
    return null;
  }

  tellModel(): void {
    /* no conversation in demo mode */
  }
}

class AppHost implements WidgetHost {
  readonly demo = false;

  constructor(private app: App) {}

  async callTool<T>(name: string, args: Record<string, unknown>): Promise<T | null> {
    try {
      const result = await this.app.callServerTool({ name, arguments: args });
      if (result.isError) return null;
      return (result.structuredContent as T | undefined) ?? null;
    } catch {
      return null;
    }
  }

  async readResource(uri: string): Promise<string | null> {
    try {
      const result = await this.app.readServerResource({ uri });
      const first = result.contents[0];
      if (first && "text" in first && typeof first.text === "string") return first.text;
    } catch {
      /* the host (or a gateway between it and Purser) may forward tools only */
    }
    return this.readSeries(uri);
  }

  /** The same series through the read_series tool (#354): its single text
   * content is the resource body byte for byte. */
  private async readSeries(uri: string): Promise<string | null> {
    try {
      const result = await this.app.callServerTool({ name: "read_series", arguments: { uri } });
      if (result.isError) return null;
      const first = result.content?.[0];
      if (first && first.type === "text" && typeof first.text === "string") return first.text;
      return null;
    } catch {
      return null;
    }
  }

  tellModel(text: string): void {
    void this.app.updateModelContext({ content: [{ type: "text", text }] }).catch(() => undefined);
  }
}

function applyTheme(theme: string | undefined): void {
  if (theme === "dark" || theme === "light") {
    document.documentElement.dataset.theme = theme;
  }
}

/** Boot a widget: demo mode when asked, otherwise connect to the host and
 * re-render on every tool result. */
export function initWidget<T>(spec: WidgetSpec<T>): void {
  const params = new URLSearchParams(window.location.search);
  if (params.has("demo")) {
    if (params.get("demo") === "dark") document.documentElement.dataset.theme = "dark";
    spec.render(spec.demo, new DemoHost());
    return;
  }

  const app = new App({ name: `purser-${spec.name}`, version: WIDGET_VERSION }, {});
  const host = new AppHost(app);
  let rendered = false;

  app.ontoolresult = (result) => {
    const data = result.structuredContent as T | undefined;
    if (data) {
      rendered = true;
      spec.render(data, host);
    }
  };
  app.onhostcontextchanged = (context) => {
    applyTheme(context.theme);
  };

  void app
    .connect()
    .then(() => {
      applyTheme(app.getHostContext()?.theme);
    })
    .catch(() => {
      /* not inside a host; the hint below covers it */
    });

  window.setTimeout(() => {
    if (!rendered && document.body.childElementCount === 0) {
      const hint = document.createElement("p");
      hint.style.cssText = "font-size:13px;color:var(--text-muted);padding:1rem;";
      hint.textContent = "Waiting for the host to deliver data… (append ?demo for a preview)";
      document.body.append(hint);
    }
  }, 2500);
}
