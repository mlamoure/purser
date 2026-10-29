import { screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { apiMock, fail, mockApi, ok } from "../../test/api";
import { renderWithProviders } from "../../test/render";
import { CreateAdminStep } from "./CreateAdminStep";

const KEY = { key: "synthetic-encryption-key", fingerprint: "fp-0011", acknowledged: false };

async function submit(view: ReturnType<typeof renderWithProviders>) {
  // The step's labels are not bound to their inputs; reach them by role/type.
  await view.user.type(screen.getByRole("textbox"), "admin@example.test");
  await view.user.type(view.container.querySelector<HTMLInputElement>('input[type="password"]')!, "long-enough");
  await view.user.click(screen.getByRole("button", { name: "Create admin" }));
}

describe("CreateAdminStep", () => {
  it("creates the admin, then hands over the encryption key to record", async () => {
    mockApi({
      "POST /auth/setup": ok({ status: "created" }, 201),
      "GET /auth/encryption-key": ok(KEY),
    });
    const onCreated = vi.fn();
    const view = renderWithProviders(<CreateAdminStep onCreated={onCreated} />);

    await submit(view);

    await waitFor(() => expect(onCreated).toHaveBeenCalledWith(KEY));
    expect(apiMock.POST).toHaveBeenCalledWith("/auth/setup", {
      body: { email: "admin@example.test", password: "long-enough" },
    });
  });

  it("hands over no key when it was already acknowledged", async () => {
    mockApi({
      "POST /auth/setup": ok({ status: "created" }, 201),
      "GET /auth/encryption-key": fail(410, "encryption key already acknowledged"),
    });
    const onCreated = vi.fn();
    const view = renderWithProviders(<CreateAdminStep onCreated={onCreated} />);

    await submit(view);

    await waitFor(() => expect(onCreated).toHaveBeenCalledWith(null));
    expect(screen.queryByText("encryption key already acknowledged")).not.toBeInTheDocument();
  });

  it("stays put with the server's reason when setup is refused", async () => {
    mockApi({ "POST /auth/setup": fail(409, "setup already completed") });
    const onCreated = vi.fn();
    const view = renderWithProviders(<CreateAdminStep onCreated={onCreated} />);

    await submit(view);

    expect(await screen.findByText("setup already completed")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create admin" })).toBeEnabled();
    expect(onCreated).not.toHaveBeenCalled();
    expect(apiMock.GET).not.toHaveBeenCalledWith("/auth/encryption-key");
  });
});
