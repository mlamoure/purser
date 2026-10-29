import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { apiMock, mockApi, ok } from "../test/api";
import { renderWithProviders } from "../test/render";
import Tokens from "./Tokens";

describe("Tokens", () => {
  it("mints a read + write token by default and shows the secret once", async () => {
    mockApi({
      "GET /tokens": ok([]),
      "POST /tokens": ({ body }) =>
        ok(
          {
            ...(body as { name: string; scopes: string[] }),
            id: "t-1",
            kind: "api",
            expires_at: null,
            last_used_at: null,
            created_at: "2026-01-05T12:00:00Z",
            is_current: false,
            token: "synthetic-token-value",
          },
          201,
        ),
    });
    const { user } = renderWithProviders(<Tokens />);
    expect(await screen.findByText("No API tokens yet.")).toBeInTheDocument();

    await user.type(screen.getByRole("textbox"), "ci-agent");
    await user.click(screen.getByRole("button", { name: "Create token" }));

    expect(await screen.findByText("synthetic-token-value")).toBeInTheDocument();
    expect(apiMock.POST).toHaveBeenCalledWith("/tokens", {
      body: { name: "ci-agent", scopes: ["read", "write"], expires_at: undefined },
    });
    expect(screen.getByRole("textbox")).toHaveValue("");
  });
});
