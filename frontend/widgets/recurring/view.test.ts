/* Recurring radar view (#347): every row is an active schedule, so a
 * schedule accepted from a suggestion (source "detected") renders like any
 * other — no confirm affordance, no confirm_schedule call — and the open
 * suggestions show up only as a footer count. */

import { fireEvent, queryByText, getByText } from "@testing-library/dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { RecurringItem, RecurringResult } from "../lib/contracts";
import type { WidgetHost } from "../lib/host";
import { render } from "./view";

const GEICO: RecurringItem = {
  schedule_id: "88888888-8888-4888-8888-888888888888",
  payee: "GEICO auto",
  cadence: "every 6 months",
  amount: "920.00",
  monthly_equiv: "153.33",
  next_due: "2026-07-19",
  source: "detected",
  last_change: null,
};

function result(overrides: Partial<RecurringResult> = {}): RecurringResult {
  return {
    monthly_total: "243.33",
    annualized_total: "2919.96",
    count: 2,
    changes_12mo: { count: 0, delta: "0.00" },
    items: [
      GEICO,
      { ...GEICO, schedule_id: "s-manual", payee: "Verizon Wireless", source: "manual" },
    ],
    truncated: false,
    suggestions_open: 0,
    ...overrides,
  };
}

function fakeHost() {
  const callTool = vi.fn(async (_name: string, _args: Record<string, unknown>) => null);
  const tellModel = vi.fn();
  const host: WidgetHost = {
    callTool: callTool as WidgetHost["callTool"],
    readResource: async () => null,
    tellModel,
    demo: false,
  };
  return { host, callTool, tellModel };
}

let root: HTMLElement;

beforeEach(() => {
  root = document.createElement("div");
  root.id = "root";
  document.body.append(root);
});

afterEach(() => {
  root.remove();
});

describe("recurring view", () => {
  it("renders a schedule accepted from a suggestion as active, with no confirm", async () => {
    const { host, callTool } = fakeHost();
    render(result(), host);

    const payee = getByText(root, "GEICO auto");
    expect(root.querySelector("button")).toBeNull();
    expect(root.textContent).not.toMatch(/confirm/i);
    expect(getByText(root, "Active schedules").nextElementSibling?.textContent).toBe("2");

    // Clicking the row only fetches merchant history — never a status change.
    fireEvent.click(payee);
    await vi.waitFor(() => expect(callTool).toHaveBeenCalled());
    expect(callTool.mock.calls.map(([name]) => name)).toEqual(["search_transactions"]);
    expect(callTool).not.toHaveBeenCalledWith("confirm_schedule", expect.anything());
  });

  it("shows the open suggestions only as a footer count", () => {
    const { host } = fakeHost();

    render(result({ suggestions_open: 0 }), host);
    expect(queryByText(root, /await(s)? a decision/)).toBeNull();

    render(result({ suggestions_open: 1 }), host);
    expect(getByText(root, "1 suggestion awaits a decision — ask to list them.")).toBeTruthy();

    render(result({ suggestions_open: 3 }), host);
    expect(getByText(root, "3 suggestions await a decision — ask to list them.")).toBeTruthy();
    // A pointer, not an action: deciding needs the suggestion ids.
    expect(root.querySelector("button")).toBeNull();
  });
});
