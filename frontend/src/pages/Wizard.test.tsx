import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { apiMock, fail, mockApi, ok } from "../test/api";
import { renderWithProviders } from "../test/render";
import Wizard from "./Wizard";

// Synthetic values only — nothing here is a real key, token or account.
const KEY = { key: "synthetic-encryption-key", fingerprint: "fp-0011", acknowledged: false };
const CONNECTION = {
  id: "c-1",
  provider: "simplefin",
  status: "active",
  created_at: "2026-01-05T12:00:00Z",
  last_fetch_at: null,
};
const discovered = (mappedPath: string | null) => ({
  external_id: "ext-1",
  name: "Everyday Checking",
  org: "Example Bank",
  currency: "USD",
  balance: "1250.00",
  mapped_account_id: mappedPath ? "a-1" : null,
  mapped_account_path: mappedPath,
});

async function createAdmin(
  user: ReturnType<typeof renderWithProviders>["user"],
  container: HTMLElement,
) {
  await user.type(screen.getByRole("textbox"), "admin@example.test");
  await user.type(container.querySelector<HTMLInputElement>('input[type="password"]')!, "long-enough");
  await user.click(screen.getByRole("button", { name: "Create admin" }));
}

describe("Wizard (first-run flow across the steps)", () => {
  it("walks from the admin account to a connected agent", async () => {
    let mapped = false;
    mockApi({
      "POST /auth/setup": ok({ status: "created" }, 201),
      "GET /auth/encryption-key": ok(KEY),
      "POST /auth/encryption-key/ack": ok({ status: "acknowledged" }),
      "POST /connections": ok(CONNECTION, 201),
      "POST /connections/{connection_id}/sync": ok({}),
      "GET /connections/{connection_id}/discovered": () =>
        ok([discovered(mapped ? "assets.everyday_checking" : null)]),
      "POST /connections/{connection_id}/map": () => {
        mapped = true;
        return ok({});
      },
      "GET /accounts": ok([]), // the import step's account picker
      "POST /tokens": ok({ token: "synthetic-agent-token" }, 201),
    });
    const { user, container } = renderWithProviders(<Wizard />);

    // 1. Create admin
    await createAdmin(user, container);
    expect(apiMock.POST).toHaveBeenCalledWith("/auth/setup", {
      body: { email: "admin@example.test", password: "long-enough" },
    });

    // 2. Record the encryption key
    expect(await screen.findByText(KEY.key)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Continue" })).toBeDisabled();
    await user.click(screen.getByRole("checkbox"));
    await user.click(screen.getByRole("button", { name: "Continue" }));

    // 3. Connect SimpleFIN — the key is gone from the page
    expect(
      await screen.findByRole("heading", { name: "Connect your bank feed (SimpleFIN)" }),
    ).toBeInTheDocument();
    expect(screen.queryByText(KEY.key)).not.toBeInTheDocument();
    await user.type(screen.getByRole("textbox"), "  synthetic-setup-token  ");
    await user.click(screen.getByRole("button", { name: "Connect & fetch" }));
    expect(apiMock.POST).toHaveBeenCalledWith("/connections", {
      body: { setup_token: "synthetic-setup-token" },
    });
    expect(apiMock.POST).toHaveBeenCalledWith("/connections/{connection_id}/sync", {
      params: { path: { connection_id: "c-1" } },
    });

    // 4. Map the discovered account
    const row = (await screen.findByText("Everyday Checking · Example Bank")).closest("tr")!;
    expect(within(row).getByText("unmapped")).toBeInTheDocument();
    await user.click(within(row).getByRole("button", { name: "Map" }));
    expect(await within(row).findByText("assets.everyday_checking")).toBeInTheDocument();
    expect(within(row).queryByRole("button", { name: "Map" })).not.toBeInTheDocument();
    expect(apiMock.POST).toHaveBeenCalledWith("/connections/{connection_id}/map", {
      params: { path: { connection_id: "c-1" } },
      body: {
        external_id: "ext-1",
        create: { name: "Everyday Checking", type: "checking", path: "assets.everyday_checking" },
      },
    });
    await user.click(screen.getByRole("button", { name: "Continue" }));

    // 5. Import history — skipped
    expect(
      await screen.findByRole("heading", { name: "Import bank history (optional)" }),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Skip for now" }));

    // 6. Connect the agent: a read + write token, shown with setup snippets
    expect(await screen.findByRole("heading", { name: "Connect your AI agent" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Create agent token" }));
    expect(await screen.findByText("synthetic-agent-token")).toBeInTheDocument();
    expect(apiMock.POST).toHaveBeenCalledWith("/tokens", {
      body: { name: "agent", scopes: ["read", "write"] },
    });
    expect(screen.getByRole("button", { name: "Finish" })).toBeInTheDocument();
  });

  it("goes straight to the bank feed when the key was already acknowledged", async () => {
    mockApi({
      "POST /auth/setup": ok({ status: "created" }, 201),
      "GET /auth/encryption-key": fail(410, "encryption key already acknowledged"),
    });
    const { user, container } = renderWithProviders(<Wizard />);

    await createAdmin(user, container);

    expect(
      await screen.findByRole("heading", { name: "Connect your bank feed (SimpleFIN)" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Record your encryption key" })).not.toBeInTheDocument();
  });

  it("skipping the bank feed leads to the import step", async () => {
    mockApi({
      "POST /auth/setup": ok({ status: "created" }, 201),
      "GET /auth/encryption-key": fail(410, "encryption key already acknowledged"),
      "GET /accounts": ok([]),
    });
    const { user, container } = renderWithProviders(<Wizard />);
    await createAdmin(user, container);
    await screen.findByRole("heading", { name: "Connect your bank feed (SimpleFIN)" });

    await user.click(screen.getByRole("button", { name: "Skip for now" }));

    expect(
      await screen.findByRole("heading", { name: "Import bank history (optional)" }),
    ).toBeInTheDocument();
    expect(apiMock.POST).not.toHaveBeenCalledWith("/connections", expect.anything());
  });

  it("does not carry a failed step's error into later steps", async () => {
    mockApi({
      "POST /auth/setup": ok({ status: "created" }, 201),
      "GET /auth/encryption-key": fail(410, "encryption key already acknowledged"),
      "POST /connections": fail(400, "setup token already claimed"),
      "GET /accounts": ok([]),
    });
    const { user, container } = renderWithProviders(<Wizard />);
    await createAdmin(user, container);
    await screen.findByRole("heading", { name: "Connect your bank feed (SimpleFIN)" });
    await user.type(screen.getByRole("textbox"), "synthetic-setup-token");
    await user.click(screen.getByRole("button", { name: "Connect & fetch" }));
    expect(await screen.findByText("setup token already claimed")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Skip for now" })); // the feed
    await screen.findByRole("heading", { name: "Import bank history (optional)" });
    await user.click(screen.getByRole("button", { name: "Skip for now" })); // the import

    expect(await screen.findByRole("heading", { name: "Connect your AI agent" })).toBeInTheDocument();
    expect(screen.queryByText("setup token already claimed")).not.toBeInTheDocument();
  });
});
