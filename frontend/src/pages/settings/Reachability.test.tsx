import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { fail, mockApi, ok } from "../../test/api";
import { renderWithProviders } from "../../test/render";
import Reachability from "./Reachability";

const GUIDE =
  "https://docs.example.com/tutorials/ai-setup/#use-purser-from-another-machine-on-the-home-network";

/** A GET /config/network body (#632); RFC 5737 addresses only. */
function network(overrides: Record<string, unknown> = {}) {
  return {
    reach: "this_machine",
    public_url: "http://localhost:8000",
    mcp_url: "http://localhost:8000/mcp",
    allowed_hosts: [],
    bind_address: "127.0.0.1",
    public_url_is_local: true,
    request_host: "localhost:8000",
    request_host_accepted: true,
    guide_url: GUIDE,
    ...overrides,
  };
}

function renderWith(body: Record<string, unknown>) {
  mockApi({ "GET /config/network": ok(body) });
  return renderWithProviders(<Reachability />);
}

describe("Settings: how Purser is reached (#632)", () => {
  it("says this machine only, the agent address, and how to open it to the home network", async () => {
    renderWith(network());

    expect(await screen.findByText("this machine only")).toBeInTheDocument();
    expect(screen.getByText("http://localhost:8000/mcp")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Copy" })).toBeInTheDocument();
    // How to switch: the three .env lines, the guide, the plain-HTTP trade-off.
    expect(screen.getByText(/PURSER_BIND_ADDRESS=0\.0\.0\.0/)).toBeInTheDocument();
    expect(screen.getByText(/PURSER_PUBLIC_URL=http:\/\/<this machine's address>:8000/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "the setup guide" })).toHaveAttribute("href", GUIDE);
    expect(screen.getByText(/travel unencrypted \(plain HTTP\)/)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    // The lines scroll sideways at phone width: the keyboard must reach them.
    expect(screen.getByText(/PURSER_BIND_ADDRESS=0\.0\.0\.0/)).toHaveAttribute("tabindex", "0");
  });

  it("says home network at the public address, the other names, and how to go back", async () => {
    renderWith(
      network({
        reach: "home_network",
        public_url: "http://192.0.2.10:8000",
        mcp_url: "http://192.0.2.10:8000/mcp",
        allowed_hosts: ["192.0.2.10", "homeserver", "homeserver.local"],
        bind_address: "0.0.0.0",
        public_url_is_local: false,
        request_host: "homeserver.local:8000",
      }),
    );

    expect(await screen.findByText("your home network")).toBeInTheDocument();
    expect(screen.getByText("http://192.0.2.10:8000")).toBeInTheDocument();
    expect(screen.getByText("http://192.0.2.10:8000/mcp")).toBeInTheDocument();
    expect(screen.getByText(/Also accepted by name: 192\.0\.2\.10, homeserver, homeserver\.local/)).toBeInTheDocument();
    expect(screen.getByText(/To limit Purser to this machine again/)).toBeInTheDocument();
    expect(screen.getByText(/travel unencrypted/)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("says when the mode is not reported, shows only the agent address, and guesses nothing", async () => {
    renderWith(
      network({
        reach: "unknown",
        public_url: "http://192.0.2.10:8000",
        mcp_url: "http://192.0.2.10:8000/mcp",
        bind_address: null,
        public_url_is_local: false,
        request_host: "192.0.2.10:8000",
      }),
    );

    expect(await screen.findByText("not reported")).toBeInTheDocument();
    expect(screen.getByText("http://192.0.2.10:8000/mcp")).toBeInTheDocument();
    expect(screen.queryByText("this machine only")).not.toBeInTheDocument();
    expect(screen.queryByText("your home network")).not.toBeInTheDocument();
    expect(screen.queryByText(/To limit Purser/)).not.toBeInTheDocument();
    expect(screen.queryByText(/PURSER_BIND_ADDRESS=0\.0\.0\.0/)).not.toBeInTheDocument();
  });

  it("says a reverse proxy serves an https public address", async () => {
    renderWith(
      network({
        reach: "reverse_proxy",
        public_url: "https://purser.example.com",
        mcp_url: "https://purser.example.com/mcp",
        public_url_is_local: false,
        request_host: "purser.example.com",
      }),
    );

    expect(await screen.findByText("a reverse proxy")).toBeInTheDocument();
    expect(screen.getByText("https://purser.example.com/mcp")).toBeInTheDocument();
    expect(screen.queryByText(/PURSER_BIND_ADDRESS=0\.0\.0\.0/)).not.toBeInTheDocument();
  });

  it("warns when the page is open at an address agents cannot use, and says how to fix it", async () => {
    renderWith(
      network({
        reach: "home_network",
        public_url: "http://192.0.2.10:8000",
        mcp_url: "http://192.0.2.10:8000/mcp",
        allowed_hosts: ["homeserver"],
        bind_address: "0.0.0.0",
        public_url_is_local: false,
        request_host: "otherbox:8000",
        request_host_accepted: false,
      }),
    );

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("You opened Purser at otherbox:8000, an address agents cannot use");
    expect(alert).toHaveTextContent("only at http://192.0.2.10:8000, homeserver and localhost");
    expect(alert).toHaveTextContent("Give agents http://192.0.2.10:8000/mcp");
    expect(alert).toHaveTextContent("add otherbox to PURSER_ALLOWED_HOSTS");
  });

  it("warns when the home network is open but the public address still says localhost", async () => {
    renderWith(network({ reach: "home_network", bind_address: "0.0.0.0" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("PURSER_PUBLIC_URL still says http://localhost:8000");
  });

  it("says why a failed read failed", async () => {
    mockApi({ "GET /config/network": fail(403, "requires scope: admin") });
    renderWithProviders(<Reachability />);

    expect(
      await screen.findByText("Could not load how Purser is reached: requires scope: admin"),
    ).toBeInTheDocument();
  });
});
