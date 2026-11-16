import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { components } from "../api/schema";
import { apiMock, mockApi, ok } from "../test/api";
import { renderWithProviders } from "../test/render";
import { OccurrenceCertainty, OccurrenceEvidence } from "./OccurrenceCertainty";

type Certainty = components["schemas"]["CertaintyOut"];
const base: Certainty = {
  status: "estimated", amount_estimated: true, date_estimated: false,
  amount: { state: "estimated", value: "1000", source: "history", as_of: "2026-11-01" },
  date: { state: "confirmed", value: "2026-11-20", source: "payroll", as_of: "2026-11-01" },
  original: { amount: "1000", date: "2026-11-20", amount_basis: "last", date_basis: "fixed", as_of: "2026-11-01", backfilled: false },
};

describe("Occurrence certainty", () => {
  it("explains independent uncertainty and original provenance in keyboard-accessible details", async () => {
    const { user } = renderWithProviders(<OccurrenceCertainty value={base} />);
    expect(screen.getByText("Estimate")).toBeInTheDocument();
    await user.click(screen.getByText("Estimate"));
    expect(screen.getByText(/Amount: estimated/)).toBeVisible();
    expect(screen.getByText(/Date: confirmed/)).toBeVisible();
    expect(screen.getByText(/Original estimate/)).toBeVisible();
  });
  it.each(["confirmed", "pending_review", "matched", "matched_pending"] as const)("distinguishes %s", status => {
    renderWithProviders(<OccurrenceCertainty value={{ ...base, status }} />);
    const label = { confirmed: "Confirmed upcoming", pending_review: "Match awaiting review", matched: "Matched actual", matched_pending: "Matched pending bank entry" }[status];
    expect(screen.getByText(label)).toBeInTheDocument();
  });
  it("sends only the chosen dimension with evidence", async () => {
    mockApi({ "PATCH /schedules/instances/{transaction_id}/certainty": ok(base) });
    const saved = vi.fn();
    const { user } = renderWithProviders(<OccurrenceEvidence transactionId="tx-1" onSaved={saved} />);
    await user.click(screen.getByText("Update evidence"));
    await user.selectOptions(screen.getByLabelText("Evidence dimension"), "date");
    await user.selectOptions(screen.getByLabelText("Evidence certainty"), "confirmed");
    await user.type(screen.getByLabelText("Evidence as of"), "2026-11-15");
    await user.type(screen.getByLabelText("Evidence note"), "Paystub specifies payday");
    await user.click(screen.getByRole("button", { name: "Save evidence" }));
    expect(apiMock.PATCH).toHaveBeenCalledWith("/schedules/instances/{transaction_id}/certainty", {
      params: { path: { transaction_id: "tx-1" } },
      body: { date: { state: "confirmed", source: "user", as_of: "2026-11-15", note: "Paystub specifies payday" } },
    });
    expect(saved).toHaveBeenCalledOnce();
  });
});

it("annotates a projected slot without offering a value edit", async () => {
  mockApi({ "PATCH /schedules/{schedule_id}/occurrences/{occurrence_date}/certainty": ok(base) });
  const saved = vi.fn();
  const slot = { schedule_id: "schedule-1", occurrence_date: "2027-02-01", transaction_id: null, projected: true };
  const { user } = renderWithProviders(<OccurrenceEvidence slot={slot} onSaved={saved} />);
  await user.click(screen.getByText("Update evidence"));
  expect(screen.queryByLabelText("Evidence value")).not.toBeInTheDocument();
  await user.type(screen.getByLabelText("Evidence as of"), "2026-11-15");
  await user.type(screen.getByLabelText("Evidence note"), "Fixed planning estimate for this slot");
  await user.click(screen.getByRole("button", { name: "Save evidence" }));
  expect(apiMock.PATCH).toHaveBeenCalledWith("/schedules/{schedule_id}/occurrences/{occurrence_date}/certainty", {
    params: { path: { schedule_id: "schedule-1", occurrence_date: "2027-02-01" } },
    body: { amount: { state: "estimated", source: "user", as_of: "2026-11-15", note: "Fixed planning estimate for this slot" } },
  });
  expect(saved).toHaveBeenCalledOnce();
});

it("explains evidence retained against a previous amount", async () => {
  const { user } = renderWithProviders(<OccurrenceCertainty value={{ ...base, amount: { ...base.amount, evidence_value: "900" } }} />);
  await user.click(screen.getByText("Estimate"));
  expect(screen.getByText(/Evidence was recorded against/)).toBeVisible();
});
