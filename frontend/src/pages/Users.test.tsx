import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { components } from "../api/schema";
import { apiMock, fail, mockApi, ok } from "../test/api";
import { renderWithProviders } from "../test/render";
import Users from "./Users";

type UserOut = components["schemas"]["UserOut"];

const userRow = (over: Partial<UserOut> = {}): UserOut => ({
  id: "u-1",
  email: "admin@example.test",
  display_name: null,
  role: "admin",
  has_password: true,
  has_oidc: false,
  oidc_issuer: null,
  created_at: "2026-01-05T12:00:00Z",
  ...over,
});

// The add-user inputs carry no label association, so reach them by role/type.
function addUserForm(container: HTMLElement) {
  return {
    email: screen.getByRole("textbox"),
    password: container.querySelector<HTMLInputElement>('input[type="password"]')!,
    submit: screen.getByRole("button", { name: "Add user" }),
  };
}

describe("Users", () => {
  it("lists users with their role and sign-in methods", async () => {
    mockApi({ "GET /users": ok([userRow({ has_oidc: true })]) });
    renderWithProviders(<Users />);

    const row = (await screen.findByText("admin@example.test")).closest("tr")!;
    expect(within(row).getByText("admin")).toBeInTheDocument();
    expect(within(row).getByText("password")).toBeInTheDocument();
    expect(within(row).getByText("oidc")).toBeInTheDocument();
  });

  it("adds a user, then refreshes the list and clears the form", async () => {
    const users = [userRow()];
    mockApi({
      "GET /users": () => ok([...users]),
      "POST /users": ({ body }) => {
        const { email, role } = body as { email: string; role: string };
        const created = userRow({ id: "u-2", email, role });
        users.push(created);
        return ok(created, 201);
      },
    });
    const { user, container } = renderWithProviders(<Users />);
    await screen.findByText("admin@example.test");
    const form = addUserForm(container);

    await user.type(form.email, "member@example.test");
    await user.type(form.password, "short");
    expect(form.submit).toBeDisabled(); // passwords need 8+ characters
    await user.type(form.password, "-enough");
    await user.click(form.submit);

    expect(await screen.findByText("member@example.test")).toBeInTheDocument();
    expect(apiMock.POST).toHaveBeenCalledWith("/users", {
      body: { email: "member@example.test", password: "short-enough", role: "member" },
    });
    expect(form.email).toHaveValue("");
    expect(form.password).toHaveValue("");
  });

  it("shows why the server refused the new user", async () => {
    mockApi({
      "GET /users": ok([userRow()]),
      "POST /users": fail(409, "email already exists"),
    });
    const { user, container } = renderWithProviders(<Users />);
    await screen.findByText("admin@example.test");
    const form = addUserForm(container);

    await user.type(form.email, "admin@example.test");
    await user.type(form.password, "long-enough");
    await user.click(form.submit);

    expect(await screen.findByText("email already exists")).toBeInTheDocument();
    expect(form.email).toHaveValue("admin@example.test"); // kept for correction
  });

  it("unlinks SSO for a linked user behind a confirm, then refreshes the list", async () => {
    let linked = true;
    mockApi({
      "GET /users": () =>
        ok([
          userRow(),
          userRow({
            id: "u-2",
            email: "sso@example.test",
            role: "member",
            has_password: false,
            has_oidc: linked,
            oidc_issuer: linked ? "https://sso.example.test" : null,
          }),
        ]),
      "DELETE /users/{user_id}/oidc": () => {
        linked = false;
        return ok(userRow({ id: "u-2", email: "sso@example.test", has_oidc: false }));
      },
    });
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    const { user } = renderWithProviders(<Users />);

    const row = (await screen.findByText("sso@example.test")).closest("tr")!;
    // Only the linked user offers the action.
    const adminRow = screen.getByText("admin@example.test").closest("tr")!;
    expect(within(adminRow).queryByRole("button", { name: "Unlink SSO" })).toBeNull();

    await user.click(within(row).getByRole("button", { name: "Unlink SSO" }));
    expect(confirm).toHaveBeenCalled();
    expect(apiMock.DELETE).toHaveBeenCalledWith("/users/{user_id}/oidc", {
      params: { path: { user_id: "u-2" } },
    });
    await waitFor(() => expect(within(row).queryByText("oidc")).toBeNull());
    expect(within(row).queryByRole("button", { name: "Unlink SSO" })).toBeNull();
    confirm.mockRestore();
  });

  it("does nothing when the unlink is not confirmed", async () => {
    mockApi({ "GET /users": ok([userRow({ has_oidc: true, oidc_issuer: "https://sso.example.test" })]) });
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    const { user } = renderWithProviders(<Users />);

    await user.click(await screen.findByRole("button", { name: "Unlink SSO" }));
    expect(apiMock.DELETE).not.toHaveBeenCalled();
    confirm.mockRestore();
  });

  it("shows why the server refused the unlink", async () => {
    mockApi({
      "GET /users": ok([userRow({ has_oidc: true })]),
      "DELETE /users/{user_id}/oidc": fail(409, "user has no SSO link"),
    });
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    const { user } = renderWithProviders(<Users />);

    await user.click(await screen.findByRole("button", { name: "Unlink SSO" }));
    expect(await screen.findByText("user has no SSO link")).toBeInTheDocument();
    confirm.mockRestore();
  });
});
