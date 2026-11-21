import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { apiMock, fail, mockApi, ok } from "../../test/api";
import { configOut } from "../../test/fixtures";
import { renderWithProviders } from "../../test/render";
import Security from "./Security";

const CONFIG = configOut();

describe("Settings, Security: labelled fields (#538)", () => {
  it("associates every visible field with its label", async () => {
    mockApi({ "GET /config": ok(CONFIG) });
    renderWithProviders(<Security />);

    expect(await screen.findByLabelText("Session lifetime (days)")).toHaveValue(7);
    expect(screen.getByLabelText("Idle timeout (hours)")).toHaveValue(8);
    expect(screen.getByLabelText("Issuer URL")).toHaveValue("");
    expect(screen.getByLabelText("Client ID")).toHaveValue("");
    expect(screen.getByLabelText("Client secret")).toHaveValue("");
    expect(screen.getByLabelText("Unknown users")).toHaveValue("deny");
    // Session lifetime sits here; each person's sign-ins list is under Agents & API.
    expect(screen.getByRole("link", { name: "Agents & API" })).toHaveAttribute("href", "/settings/agents");
  });

  it("saves session lifetimes with their own Save", async () => {
    mockApi({ "GET /config": ok(CONFIG), "PUT /config": ok(CONFIG) });
    const { user } = renderWithProviders(<Security />);
    const ttl = await screen.findByLabelText("Session lifetime (days)");
    await user.clear(ttl);
    await user.type(ttl, "14");
    const card = screen.getByRole("heading", { name: "Session lifetime" }).closest("div")!;
    await user.click(within(card).getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(apiMock.PUT).toHaveBeenCalledWith("/config", {
        body: { session_ttl_days: 14, session_idle_hours: 8 },
      }),
    );
  });
});

describe("Settings, Security: record the encryption key after setup (#539)", () => {
  const KEY = { key: "synthetic-encryption-key", fingerprint: "abcd1234", acknowledged: false };
  const ACKED = {
    ...CONFIG,
    encryption: { ...CONFIG.encryption, key_acknowledged_at: "2026-11-17T12:00:00Z" },
  };

  it("shows the key, acknowledges it and drops it", async () => {
    let acked = false;
    mockApi({
      "GET /config": () => ok(acked ? ACKED : CONFIG),
      "GET /auth/encryption-key": ok(KEY),
      "POST /auth/encryption-key/ack": () => {
        acked = true;
        return ok({ status: "acknowledged" });
      },
    });
    const { user } = renderWithProviders(<Security />);

    await user.click(await screen.findByRole("button", { name: "Record your key" }));
    expect(await screen.findByText(KEY.key)).toBeInTheDocument();
    const confirm = screen.getByRole("button", { name: "Confirm key recorded" });
    expect(confirm).toBeDisabled();
    await user.click(screen.getByRole("checkbox", { name: "I have stored this key somewhere safe" }));
    await user.click(confirm);

    expect(apiMock.POST).toHaveBeenCalledWith("/auth/encryption-key/ack");
    await waitFor(() => expect(screen.queryByText(KEY.key)).not.toBeInTheDocument());
    expect(await screen.findByText(/Key saved by an administrator: (?!not yet)/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Record your key" })).not.toBeInTheDocument();
  });

  it("keeps the key on screen with the reason when the acknowledgement fails", async () => {
    mockApi({
      "GET /config": ok(CONFIG),
      "GET /auth/encryption-key": ok(KEY),
      "POST /auth/encryption-key/ack": fail(403, "admin scope required"),
    });
    const { user } = renderWithProviders(<Security />);

    await user.click(await screen.findByRole("button", { name: "Record your key" }));
    await user.click(await screen.findByRole("checkbox", { name: "I have stored this key somewhere safe" }));
    await user.click(screen.getByRole("button", { name: "Confirm key recorded" }));

    expect(await screen.findByText("admin scope required")).toBeInTheDocument();
    expect(screen.getByText(KEY.key)).toBeInTheDocument();
  });

  it("shows why the key could not be revealed", async () => {
    mockApi({
      "GET /config": ok(CONFIG),
      "GET /auth/encryption-key": fail(403, "admin scope required"),
    });
    const { user } = renderWithProviders(<Security />);

    await user.click(await screen.findByRole("button", { name: "Record your key" }));

    expect(await screen.findByText("admin scope required")).toBeInTheDocument();
  });

  it("refreshes instead of erroring when the key was acknowledged elsewhere (410)", async () => {
    let gone = false;
    mockApi({
      "GET /config": () => ok(gone ? ACKED : CONFIG),
      "GET /auth/encryption-key": () => {
        gone = true;
        return fail(410, "encryption key already acknowledged");
      },
    });
    const { user } = renderWithProviders(<Security />);

    await user.click(await screen.findByRole("button", { name: "Record your key" }));

    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Record your key" })).not.toBeInTheDocument(),
    );
    expect(screen.queryByText("encryption key already acknowledged")).not.toBeInTheDocument();
  });

  it("offers nothing to record once the key is acknowledged", async () => {
    mockApi({ "GET /config": ok(ACKED) });
    renderWithProviders(<Security />);
    await screen.findByText(/Key saved by an administrator/);
    expect(screen.queryByRole("button", { name: "Record your key" })).not.toBeInTheDocument();
  });
});

describe("Settings, Security: encryption in plain words (#541)", () => {
  it("says what is encrypted in words and keeps the storage terms in Technical detail", async () => {
    mockApi({ "GET /config": ok({ ...CONFIG, encryption: { ...CONFIG.encryption, key_acknowledged_at: "2026-11-17T12:00:00Z" } }) });
    renderWithProviders(<Security />);
    const heading = await screen.findByRole("heading", { name: /Encryption/ });
    expect(within(heading).getByText("on")).toBeInTheDocument();
    expect(screen.getByText(/Payee names, descriptions, memos/)).toBeInTheDocument();
    const detail = screen.getByText("Technical detail").closest("details")!;
    expect(detail).toHaveTextContent("abcd1234");
    expect(detail).toHaveTextContent("readmodel v2 views");
    expect(detail).toHaveTextContent("self-check (canary) passed");
    expect(screen.queryByText(/canary check failed/)).not.toBeInTheDocument();
  });

  it("names the failed check without storage jargon", async () => {
    mockApi({
      "GET /config": ok({
        ...CONFIG,
        encryption: { ...CONFIG.encryption, field_encryption_active: false, canary_ok: false },
      }),
    });
    renderWithProviders(<Security />);
    const heading = await screen.findByRole("heading", { name: /Encryption/ });
    expect(within(heading).getByText("check failed")).toBeInTheDocument();
    expect(screen.getByText(/could not read back its test value/)).toBeInTheDocument();
  });
});
