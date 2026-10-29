import { screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { apiMock, fail, mockApi, ok } from "../../test/api";
import { renderWithProviders } from "../../test/render";
import { EncryptionKeyStep } from "./EncryptionKeyStep";

const KEY = { key: "synthetic-encryption-key", fingerprint: "fp-0011", acknowledged: false };

describe("EncryptionKeyStep", () => {
  it("shows the key and continues only once the user says it is stored", async () => {
    mockApi({ "POST /auth/encryption-key/ack": ok({ status: "acknowledged" }) });
    const onAcknowledged = vi.fn();
    const { user } = renderWithProviders(
      <EncryptionKeyStep keyInfo={KEY} onAcknowledged={onAcknowledged} />,
    );

    expect(screen.getByText(KEY.key)).toBeInTheDocument();
    expect(screen.getByText(KEY.fingerprint)).toBeInTheDocument();
    const next = screen.getByRole("button", { name: "Continue" });
    expect(next).toBeDisabled();

    await user.click(screen.getByRole("checkbox", { name: "I have stored this key somewhere safe" }));
    await user.click(next);

    await waitFor(() => expect(onAcknowledged).toHaveBeenCalledOnce());
    expect(apiMock.POST).toHaveBeenCalledWith("/auth/encryption-key/ack");
  });

  it("copies the key to the clipboard", async () => {
    const { user } = renderWithProviders(<EncryptionKeyStep keyInfo={KEY} onAcknowledged={vi.fn()} />);

    await user.click(screen.getByRole("button", { name: "Copy" }));

    expect(await navigator.clipboard.readText()).toBe(KEY.key);
  });

  it("stays on the key with the reason when the acknowledgement fails", async () => {
    mockApi({ "POST /auth/encryption-key/ack": fail(403, "admin scope required") });
    const onAcknowledged = vi.fn();
    const { user } = renderWithProviders(
      <EncryptionKeyStep keyInfo={KEY} onAcknowledged={onAcknowledged} />,
    );

    await user.click(screen.getByRole("checkbox"));
    await user.click(screen.getByRole("button", { name: "Continue" }));

    expect(await screen.findByText("admin scope required")).toBeInTheDocument();
    expect(screen.getByText(KEY.key)).toBeInTheDocument();
    expect(onAcknowledged).not.toHaveBeenCalled();
  });
});
