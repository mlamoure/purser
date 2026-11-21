import { screen } from "@testing-library/react";
import { useQuery } from "@tanstack/react-query";
import { describe, expect, it } from "vitest";
import { api, errorDetail } from "../lib/api";
import { fail, mockApi, ok } from "../test/api";
import { renderWithProviders } from "../test/render";
import { QueryState, type QueryLike } from "./QueryState";

/** A list page in miniature: GET /users through QueryState. */
function UserList({ enabled = true }: { enabled?: boolean }) {
  const users = useQuery({
    queryKey: ["users"],
    enabled,
    queryFn: async () => {
      const { data, error } = await api.GET("/users");
      if (error) throw new Error(errorDetail(error));
      return data ?? [];
    },
  });
  return (
    <QueryState query={users} what="users" empty="No users yet.">
      {(rows) => (
        <ul>
          {rows.map((u) => (
            <li key={u.id}>{u.email}</li>
          ))}
        </ul>
      )}
    </QueryState>
  );
}

const USER = {
  id: "u-1",
  email: "someone@example.com",
  role: "member",
  has_password: true,
  has_oidc: false,
  oidc_issuer: null,
  created_at: "2026-11-01T12:00:00Z",
};

describe("QueryState (#540)", () => {
  it("shows loading while the first read is in flight", () => {
    mockApi({ "GET /users": () => new Promise(() => {}) });
    renderWithProviders(<UserList />);
    expect(screen.getByRole("status")).toHaveTextContent("Loading users…");
  });

  it("shows a failed read with the server's detail, not an empty list", async () => {
    mockApi({ "GET /users": fail(403, "admin scope required") });
    renderWithProviders(<UserList />);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not load users: admin scope required",
    );
    expect(screen.queryByText("No users yet.")).not.toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("shows the empty message for an empty list", async () => {
    mockApi({ "GET /users": ok([]) });
    renderWithProviders(<UserList />);
    expect(await screen.findByText("No users yet.")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("renders the data", async () => {
    mockApi({ "GET /users": ok([USER]) });
    renderWithProviders(<UserList />);
    expect(await screen.findByText("someone@example.com")).toBeInTheDocument();
  });

  it("renders nothing for a disabled query", () => {
    mockApi({});
    const { container } = renderWithProviders(<UserList enabled={false} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("keeps showing data under a failed refetch, and takes a custom emptiness test", () => {
    const query: QueryLike<{ items: string[] }> = {
      data: { items: [] },
      error: new Error("timed out"),
      isLoading: false,
    };
    renderWithProviders(
      <QueryState
        query={query}
        what="entries"
        empty={<p>Nothing matches.</p>}
        isEmpty={(d) => d.items.length === 0}
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("Could not load entries: timed out");
    expect(screen.getByText("Nothing matches.")).toBeInTheDocument();
  });
});
