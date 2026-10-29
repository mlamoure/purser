import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { apiMock, mockApi, ok } from "../../test/api";
import { renderWithProviders } from "../../test/render";
import Settings from "./Settings";

const CONFIG = {
  values: {
    sync_schedule: { preset: "daily", hour: 6, minute: 30 },
    transfer_auto_link: true,
    privacy_purchase: false,
    privacy_amount: false,
    ai_provider: "none",
    ai_model: null,
    ai_base_url: "https://api.example.com/v1",
    grafana_url: null,
    session_ttl_days: 7,
    session_idle_hours: 8,
    audit_retention_days: 90,
    oidc_issuer: null,
    oidc_client_id: null,
    oidc_enabled: false,
    oidc_auto_provision: false,
    price_provider: "none",
    price_stale_days: 7,
  },
  overridden: [],
  ai_api_key_set: false,
  oidc_client_secret_set: false,
  price_api_key_set: false,
  sync_reschedule_live: true,
  encryption: {
    field_encryption_active: true,
    canary_ok: true,
    key_fingerprint: "abcd1234",
    key_acknowledged_at: null,
    encrypted_scopes: [],
  },
};

describe("Config, Settings: privacy with agents (#437)", () => {
  it("saves both privacy defaults", async () => {
    mockApi({ "GET /config": ok(CONFIG), "PUT /config": ok(CONFIG) });
    const { user } = renderWithProviders(<Settings />);
    await user.click(await screen.findByLabelText(/Privatize the purchase/));
    await user.click(screen.getByLabelText(/Privatize the amount/));
    const card = screen.getByRole("heading", { name: "Privacy with agents" }).closest("div")!;
    await user.click(within(card).getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(apiMock.PUT).toHaveBeenCalledWith("/config", {
        body: { privacy_purchase: true, privacy_amount: true },
      }),
    );
  });
});
