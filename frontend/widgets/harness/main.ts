/* Dev-only mock MCP Apps host (issue #72): renders a built widget bundle in
 * a sandboxed iframe and speaks the real Apps protocol via the SDK's
 * AppBridge — initialize handshake, tool-result delivery, canned
 * tools/call + resources/read replies, theme switching, and a log of
 * everything the widget asks for. Used for local development, the
 * second-host acceptance check, and screenshot capture. Never shipped.
 *
 * All fixture data is synthetic (guardrail 1: decimal strings; no PII). */

import { AppBridge, PostMessageTransport } from "@modelcontextprotocol/ext-apps/app-bridge";
import type { CallToolResult } from "@modelcontextprotocol/client";

import { FIXTURES, type WidgetFixture } from "./fixtures";

const widgetSelect = document.getElementById("widget") as HTMLSelectElement;
const themeSelect = document.getElementById("theme") as HTMLSelectElement;
const frameHolder = document.getElementById("frame-holder") as HTMLDivElement;
const logEl = document.getElementById("log") as HTMLPreElement;

let bridge: AppBridge | null = null;

function log(line: string): void {
  logEl.textContent = `${new Date().toLocaleTimeString()} ${line}\n${logEl.textContent ?? ""}`.slice(0, 8000);
}

function toolResult(structured: unknown): CallToolResult {
  return {
    content: [{ type: "text", text: JSON.stringify(structured) }],
    structuredContent: structured as Record<string, unknown>,
    isError: false,
  };
}

async function mount(): Promise<void> {
  const name = widgetSelect.value;
  const fixture: WidgetFixture | undefined = FIXTURES[name];
  if (!fixture) {
    log(`no fixture for ${name}`);
    return;
  }

  await bridge?.close().catch(() => undefined);
  frameHolder.replaceChildren();

  const resp = await fetch(`/dist-widgets/${name}.html`);
  if (!resp.ok) {
    frameHolder.textContent = `dist-widgets/${name}.html not built — run: npm run build:widgets`;
    return;
  }
  const html = await resp.text();

  const iframe = document.createElement("iframe");
  iframe.setAttribute("sandbox", "allow-scripts allow-same-origin");
  iframe.style.cssText = "width:100%;height:460px;border:1px solid #d3d1c7;border-radius:8px;background:transparent;";
  iframe.srcdoc = html;
  frameHolder.append(iframe);

  await new Promise<void>((resolve) => {
    iframe.addEventListener("load", () => resolve(), { once: true });
  });

  bridge = new AppBridge(
    null,
    { name: "purser-widget-harness", version: "0.2.0" },
    { serverTools: {}, serverResources: {} },
    { hostContext: { theme: themeSelect.value as "light" | "dark" } },
  );
  bridge.oncalltool = async (params) => {
    log(`tools/call ${params.name} ${JSON.stringify(params.arguments ?? {})}`);
    const reply = fixture.tools[params.name]?.(params.arguments ?? {});
    if (!reply) {
      return { content: [{ type: "text", text: `no fixture for tool ${params.name}` }], isError: true };
    }
    return toolResult(reply);
  };
  bridge.onreadresource = async (params) => {
    log(`resources/read ${params.uri}`);
    const body = fixture.resources[params.uri];
    if (!body) throw new Error(`no fixture resource ${params.uri}`);
    return { contents: [{ uri: params.uri, mimeType: "application/json", text: JSON.stringify(body) }] };
  };
  bridge.onupdatemodelcontext = async (params) => {
    log(`ui/update-model-context ${JSON.stringify(params.content ?? [])}`);
    return {};
  };
  bridge.oninitialized = () => {
    log("view initialized — sending tool result");
    void bridge?.sendToolInput({ arguments: fixture.toolInput });
    void bridge?.sendToolResult(toolResult(fixture.result));
  };

  const transport = new PostMessageTransport(iframe.contentWindow ?? undefined, iframe.contentWindow!);
  await bridge.connect(transport);
  log(`mounted ${name} (theme=${themeSelect.value})`);
}

widgetSelect.addEventListener("change", () => void mount());
themeSelect.addEventListener("change", () => {
  bridge?.setHostContext({ theme: themeSelect.value as "light" | "dark" });
  log(`theme -> ${themeSelect.value}`);
});

void mount();
