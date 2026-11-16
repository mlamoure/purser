import { screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { apiMock, fail, mockApi, ok } from "../test/api";
import { renderWithProviders } from "../test/render";
import AuditLog from "./AuditLog";

const options = {
  integrations: [
    { id: "integration-one", name: "Budget Assistant" },
    { id: "integration-two", name: "Planner" },
  ],
  accounts: [{ id: "checking", name: "Checking" }],
  data_types: ["Individual amounts"],
  retention_days: 90,
  retained_since: "2026-10-01T10:00:00Z",
  capture_health: { failures: 0 },
};
const entry = {
  id: "event-one",
  at: "2026-11-10T12:00:00Z",
  kind: "read",
  action: "mcp.search_transactions",
  entity_type: "transaction",
  entity_id: null,
  request_id: "request-one",
  channel: "mcp",
  outcome: "shared",
  identity: {
    name: "Budget Assistant",
    kind: "api",
    historical: true,
    scopes: ["read"],
    authorization: null,
  },
  sharing: {
    data_types: ["Individual amounts"],
    fields: ["[].amount"],
    withheld: ["payee"],
    accounts: ["Checking"],
    record_count: 24,
    count_unit: "records",
    requested_coverage: {},
    partial: false,
    response_has_more: false,
  },
  extra_fields: [],
  requested_extra: [],
  before_after: null,
  technical: {},
  completion_point: "tool_return",
};
const page = (items: unknown[], next_cursor: string | null = null) => ({
  items,
  next_cursor,
  until: "2026-11-15T18:00:00Z",
});

describe("Audit Log", () => {
  it("defaults to sharing and explains actual contents and withheld fields", async () => {
    mockApi({
      "GET /audit/options": ok(options),
      "GET /audit/activity": ok(page([entry])),
    });
    const { user } = renderWithProviders(<AuditLog />);
    expect(
      await screen.findByText("24 records · Individual amounts"),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Data shared" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await user.click(
      screen.getByText("Budget Assistant", { selector: "span" }),
    );
    expect(screen.getByText("Withheld: Merchant names")).toBeVisible();
    expect(
      screen.getByText("Original access grant was not recorded."),
    ).toBeVisible();
    expect(
      screen.getByText(/Downstream receipt or forwarding is not verified/),
    ).toBeVisible();
    expect(apiMock.GET).toHaveBeenCalledWith(
      "/audit/activity",
      expect.objectContaining({
        params: { query: expect.objectContaining({ view: "sharing" }) },
      }),
    );
  });

  it.each([null, 0, 24])(
    "shows recorded facts without empty summaries when count is %s",
    async (recordCount) => {
      mockApi({
        "GET /audit/options": ok(options),
        "GET /audit/activity": ok(
          page([
            {
              ...entry,
              sharing: {
                ...entry.sharing,
                data_types: [],
                fields: ["[].custom_field"],
                accounts: [],
                withheld: [],
                record_count: recordCount,
              },
            },
          ]),
        ),
      });
      const { user } = renderWithProviders(<AuditLog />);
      await user.click(
        await screen.findByText("Budget Assistant", { selector: "span" }),
      );
      expect(screen.getByText("Outcome and evidence")).toBeVisible();
      for (const text of [
        "No recognized financial fields in this response.",
        "Response metadata",
        "Coverage in details",
        "No field omissions recorded for this response.",
        "Coverage",
        "Privacy",
      ]) {
        expect(screen.queryByText(text)).not.toBeInTheDocument();
      }
      if (recordCount === null) {
        expect(screen.queryByText("Response contents")).not.toBeInTheDocument();
      } else {
        expect(screen.getByText(`${recordCount} records`)).toBeVisible();
        expect(screen.getByText(`${recordCount} records returned`)).toBeVisible();
      }
      await user.click(
        screen.getByText("Technical details", { selector: "summary" }),
      );
      expect(
        screen.getByText("Observed field paths: [].custom_field"),
      ).toBeVisible();
    },
  );

  it("applies integration and extra filters server-side in every view", async () => {
    mockApi({
      "GET /audit/options": ok(options),
      "GET /audit/activity": ok(page([])),
    });
    const { user } = renderWithProviders(<AuditLog />);
    await screen.findByText("No recorded activity matches these filters.");
    await user.selectOptions(
      screen.getByLabelText("Integrator"),
      "integration-two",
    );
    await user.click(
      screen.getByLabelText("Only fields returned beyond privacy defaults"),
    );
    await user.click(
      screen.getByRole("button", { name: "Access & permissions" }),
    );
    await waitFor(() =>
      expect(apiMock.GET).toHaveBeenCalledWith("/audit/activity", {
        params: {
          query: expect.objectContaining({
            view: "access",
            integration_id: "integration-two",
            extra: true,
          }),
        },
      }),
    );
  });

  it("loads additional pages and keeps a fixed review window", async () => {
    mockApi({
      "GET /audit/options": ok(options),
      "GET /audit/activity": ({ params }) =>
        ok(
          params?.query?.cursor
            ? page([{ ...entry, id: "event-two" }])
            : page([entry], "next-page"),
        ),
    });
    const { user } = renderWithProviders(<AuditLog />, {
      route: "/audit?since=2026-11-01T00:00:00Z&until=2026-11-15T00:00:00Z",
    });
    await user.click(
      await screen.findByRole("button", { name: "Load older entries" }),
    );
    expect(
      await screen.findByText("2 entries loaded · all matching entries loaded"),
    ).toBeInTheDocument();
    expect(apiMock.GET).toHaveBeenCalledWith("/audit/activity", {
      params: {
        query: expect.objectContaining({
          cursor: "next-page",
          until: "2026-11-15T00:00:00Z",
        }),
      },
    });
  });

  it("restores filters from a URL and lets custom dates change", async () => {
    mockApi({
      "GET /audit/options": ok(options),
      "GET /audit/activity": ok(page([])),
    });
    const { user } = renderWithProviders(<AuditLog />, {
      route:
        "/audit?view=technical&integration_id=integration-one&outcome=legacy",
    });
    await screen.findByText("No recorded activity matches these filters.");
    expect(screen.getByLabelText("Integrator")).toHaveValue("integration-one");
    expect(screen.getByLabelText("Outcome")).toHaveValue("legacy");
    await user.click(screen.getByRole("button", { name: "Reset filters" }));
    expect(screen.getByLabelText("Integrator")).toHaveValue("");
  });

  it("does not present an API failure as an empty log", async () => {
    mockApi({
      "GET /audit/options": ok(options),
      "GET /audit/activity": fail(503, "Audit unavailable"),
    });
    renderWithProviders(<AuditLog />);
    expect(await screen.findByText("Audit unavailable")).toBeInTheDocument();
    expect(
      screen.queryByText("No recorded activity matches these filters."),
    ).not.toBeInTheDocument();
  });

  it("marks legacy entries and capture failures without inventing evidence", async () => {
    mockApi({
      "GET /audit/options": ok({
        ...options,
        capture_health: { failures: 1, since: "2026-11-15" },
      }),
      "GET /audit/activity": ok(
        page([
          {
            ...entry,
            outcome: "legacy",
            sharing: null,
            identity: { ...entry.identity, historical: false },
          },
        ]),
      ),
    });
    renderWithProviders(<AuditLog />);
    expect(
      (
        await screen.findAllByText(
          "Detailed sharing information was not recorded.",
        )
      ).length,
    ).toBeGreaterThan(0);
    expect(
      screen.getByText(/Audit capture or completion failed 1 time/),
    ).toBeInTheDocument();
  });
});
