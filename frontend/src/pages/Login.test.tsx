import { screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { apiMock, fail, mockApi, ok } from "../test/api";
import { renderWithProviders } from "../test/render";
import Login from "./Login";

const signedInUser = {
  id: "u-1",
  email: "admin@example.test",
  display_name: null,
  role: "admin",
  has_password: true,
  has_oidc: false,
  created_at: "2026-01-05T12:00:00Z",
};

describe("Login", () => {
  it("offers SSO when the instance has OIDC enabled", async () => {
    mockApi({ "GET /auth/methods": ok({ password: true, oidc: true }) });
    renderWithProviders(<Login onLogin={vi.fn()} />);

    expect(screen.getByRole("heading", { name: "Sign in to Purser" })).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Sign in with SSO" })).toBeInTheDocument();
  });

  it("posts the credentials and hands over on success", async () => {
    mockApi({
      "GET /auth/methods": ok({ password: true, oidc: false }),
      "POST /auth/login": ok(signedInUser),
    });
    const onLogin = vi.fn();
    const { user } = renderWithProviders(<Login onLogin={onLogin} />);

    await user.type(screen.getByLabelText("Email"), "admin@example.test");
    await user.type(screen.getByLabelText("Password"), "correct-horse");
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() => expect(onLogin).toHaveBeenCalledOnce());
    expect(apiMock.POST).toHaveBeenCalledWith("/auth/login", {
      body: { email: "admin@example.test", password: "correct-horse" },
    });
    expect(screen.queryByRole("button", { name: "Sign in with SSO" })).not.toBeInTheDocument();
  });

  it("shows the server's reason when sign-in is refused", async () => {
    mockApi({
      "GET /auth/methods": ok({ password: true, oidc: false }),
      "POST /auth/login": fail(401, "invalid email or password"),
    });
    const onLogin = vi.fn();
    const { user } = renderWithProviders(<Login onLogin={onLogin} />);

    await user.type(screen.getByLabelText("Email"), "admin@example.test");
    await user.type(screen.getByLabelText("Password"), "wrong-password");
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByText("invalid email or password")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign in" })).toBeEnabled();
    expect(onLogin).not.toHaveBeenCalled();
  });
});
