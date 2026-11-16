import { screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { apiMock, fail, mockApi, ok } from "../test/api";
import {
  matchSuggestion,
  ruleOut,
  transferLinkSuggestion,
  transferPairSuggestion,
} from "../test/fixtures";
import { renderWithProviders } from "../test/render";
import { SuggestionCard } from "./SuggestionCard";

const DECIDE = "POST /suggestions/{suggestion_id}/decide";
const SET_STATE = "POST /rules/{rule_id}/state";

/** What a transfer_link accept returns: the suggestion, now accepted, and
 * the DRAFT rule it created with the count its activation will ask for. */
const accepted = (projected: number, rule = ruleOut()) =>
  ok({
    suggestion: transferLinkSuggestion({
      status: "accepted",
      result_entity_type: "rule",
      result_entity_id: rule.id,
    }),
    schedule: null,
    rule,
    projected_links: projected,
  });

describe("SuggestionCard — transfer_link (#271)", () => {
  it("shows the pair, the payee scope and the window the draft rule would declare", () => {
    renderWithProviders(<SuggestionCard suggestion={transferLinkSuggestion()} />);
    expect(screen.getByText(/Link transfers between assets\.checking/)).toBeInTheDocument();
    expect(screen.getByText("your confirm")).toBeInTheDocument();
    expect(screen.getByText(/or an alias · legs within 3 days \(standard\) · any amount/)).toBeInTheDocument();
    expect(screen.getByText(/1 confirmed pair\(s\) on 2026-05-04/)).toBeInTheDocument();
    expect(screen.queryByText(/% sure/)).not.toBeInTheDocument();
  });

  it("accepts into a draft rule and activates it only on an explicit confirm of the count", async () => {
    mockApi({ [DECIDE]: accepted(2), [SET_STATE]: ok(ruleOut({ state: "active" })) });
    const { user } = renderWithProviders(<SuggestionCard suggestion={transferLinkSuggestion()} />);

    await user.click(screen.getByRole("button", { name: "Create draft rule" }));
    const activate = await screen.findByRole("button", { name: "Activate (links 2)" });
    expect(apiMock.POST).toHaveBeenCalledWith("/suggestions/{suggestion_id}/decide", {
      params: { path: { suggestion_id: "s-1" } },
      body: { decision: "accept", reject_scope: "exact" },
    });
    // Accepting created a draft and nothing more.
    expect(screen.getByText(/Rule created as a/)).toBeInTheDocument();
    expect(apiMock.POST).toHaveBeenCalledTimes(1);

    await user.click(activate);
    expect(apiMock.POST).toHaveBeenLastCalledWith("/rules/{rule_id}/state", {
      params: { path: { rule_id: "r-1" } },
      body: { state: "active", confirm_links: 2 },
    });
  });

  it("re-asks with the server's count when history moved before activation", async () => {
    let attempts = 0;
    mockApi({
      [DECIDE]: accepted(1),
      [SET_STATE]: () =>
        ++attempts === 1
          ? fail(
              422,
              "activating this transfer_link rule would link 3 historical pair(s) with legs " +
                "up to 3 days apart; re-send with confirm_links=3 to proceed",
            )
          : ok(ruleOut({ state: "active" })),
    });
    const { user } = renderWithProviders(<SuggestionCard suggestion={transferLinkSuggestion()} />);

    await user.click(screen.getByRole("button", { name: "Create draft rule" }));
    await user.click(await screen.findByRole("button", { name: "Activate (links 1)" }));
    expect(await screen.findByText(/would link 3 historical pair/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Activate (links 3)" }));
    expect(apiMock.POST).toHaveBeenLastCalledWith("/rules/{rule_id}/state", {
      params: { path: { rule_id: "r-1" } },
      body: { state: "active", confirm_links: 3 },
    });
  });

  it("leaves the rule as a draft when asked", async () => {
    mockApi({ [DECIDE]: accepted(4) });
    const { user } = renderWithProviders(<SuggestionCard suggestion={transferLinkSuggestion()} />);
    await user.click(screen.getByRole("button", { name: "Create draft rule" }));
    await user.click(await screen.findByRole("button", { name: "Leave as draft" }));
    expect(apiMock.POST).toHaveBeenCalledTimes(1); // the decide; no activation
  });

  it("edits only the narrowing fields before accepting", async () => {
    mockApi({ [DECIDE]: accepted(0, ruleOut({ window_days: 5 })) });
    const { user } = renderWithProviders(<SuggestionCard suggestion={transferLinkSuggestion()} />);

    await user.click(screen.getByRole("button", { name: "Edit & accept" }));
    expect(screen.queryByDisplayValue("ZEPHYR BKG SVC SENT")).not.toBeInTheDocument();
    await user.type(screen.getByPlaceholderText("3"), "5");
    await user.type(screen.getByPlaceholderText("any amount"), "250.00");
    await user.click(screen.getByRole("button", { name: "Create with edits" }));

    expect(apiMock.POST).toHaveBeenCalledWith("/suggestions/{suggestion_id}/decide", {
      params: { path: { suggestion_id: "s-1" } },
      body: {
        decision: "edit_then_accept",
        edits: { window_days: 5, amount_exact: "250.00" },
        reject_scope: "exact",
      },
    });
    expect(await screen.findByRole("button", { name: "Activate (links 0)" })).toBeInTheDocument();
  });

  it("rejects for the whole account pair", async () => {
    mockApi({
      [DECIDE]: ok({
        suggestion: transferLinkSuggestion({ status: "rejected", reject_scope: "counterparty" }),
        schedule: null,
        rule: null,
        projected_links: null,
      }),
    });
    const { user } = renderWithProviders(<SuggestionCard suggestion={transferLinkSuggestion()} />);
    await user.click(screen.getByRole("button", { name: "Reject" }));
    await user.click(screen.getByRole("button", { name: "Never for this pair" }));
    expect(apiMock.POST).toHaveBeenCalledWith("/suggestions/{suggestion_id}/decide", {
      params: { path: { suggestion_id: "s-1" } },
      body: { decision: "reject", reject_scope: "counterparty" },
    });
  });
});

describe("SuggestionCard — schedule", () => {
  it("accepts a schedule suggestion straight into a schedule, with no activation step", async () => {
    const schedule = transferLinkSuggestion({
      id: "s-2",
      kind: "schedule",
      source: "detected",
      confidence: "0.750",
      headline: "Gym: monthly ~-15.99 on assets.checking with Gym",
      draft: {
        version: 1,
        name: "Gym",
        account: "assets.checking",
        rrule: "FREQ=MONTHLY;BYMONTHDAY=15",
        dtstart: "2026-05-15",
        amount: "-15.99",
        category: null,
      },
      evidence_summary: {
        count: 3,
        first: "2026-03-15",
        last: "2026-05-15",
        amount_range: { low: "-15.99", high: "-15.99" },
        cadence: "monthly",
      },
    });
    mockApi({
      [DECIDE]: ok({
        suggestion: { ...schedule, status: "accepted" },
        schedule: null,
        rule: null,
        projected_links: null,
      }),
    });
    const { user } = renderWithProviders(<SuggestionCard suggestion={schedule} />);
    expect(screen.getByText("Pattern strength 75/100")).toBeInTheDocument();
    expect(screen.getByText(/3 occurrences from 2026-03-15 to 2026-05-15/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Create schedule" }));
    expect(apiMock.POST).toHaveBeenCalledWith("/suggestions/{suggestion_id}/decide", {
      params: { path: { suggestion_id: "s-2" } },
      body: { decision: "accept", reject_scope: "exact" },
    });
    expect(screen.queryByRole("button", { name: /Activate/ })).not.toBeInTheDocument();
  });
});

describe("SuggestionCard — match (#246)", () => {
  const decided = (status: string) =>
    ok({
      suggestion: matchSuggestion({ status }),
      schedule: null,
      rule: null,
      projected_links: null,
      match: null,
    });

  it("shows the expected row against the bank row and the variance, with no edit", () => {
    renderWithProviders(<SuggestionCard suggestion={matchSuggestion()} />);
    expect(screen.getByText("Expected · City Utilities")).toBeInTheDocument();
    expect(screen.getByText("Bank row")).toBeInTheDocument();
    expect(screen.getByText("2026-07-18")).toBeInTheDocument();
    expect(
      screen.getByText(
        (_, el) => el?.tagName === "P" && /on assets\.checking · variance/.test(el.textContent ?? ""),
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("Pattern strength 61/100")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Edit/ })).not.toBeInTheDocument();
  });

  it("accepts by confirming the proposal", async () => {
    mockApi({ [DECIDE]: decided("accepted") });
    const { user } = renderWithProviders(<SuggestionCard suggestion={matchSuggestion()} />);
    await user.click(screen.getByRole("button", { name: "Confirm payment match" }));
    expect(apiMock.POST).toHaveBeenCalledWith("/suggestions/{suggestion_id}/decide", {
      params: { path: { suggestion_id: "s-3" } },
      body: { decision: "accept", reject_scope: "exact" },
    });
  });

  it("rejects exactly this pair", async () => {
    mockApi({ [DECIDE]: decided("rejected") });
    const { user } = renderWithProviders(<SuggestionCard suggestion={matchSuggestion()} />);
    await user.click(screen.getByRole("button", { name: "Reject" }));
    expect(screen.queryByRole("button", { name: /Never/ })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Not this row" }));
    expect(apiMock.POST).toHaveBeenCalledWith("/suggestions/{suggestion_id}/decide", {
      params: { path: { suggestion_id: "s-3" } },
      body: { decision: "reject", reject_scope: "exact" },
    });
  });

  it("shows the server's refusal when the proposal is gone", async () => {
    mockApi({
      [DECIDE]: fail(
        422,
        "this match is no longer proposed — it was decided, or its rows moved on; " +
          "the suggestion is withdrawn",
      ),
    });
    const { user } = renderWithProviders(<SuggestionCard suggestion={matchSuggestion()} />);
    await user.click(screen.getByRole("button", { name: "Confirm payment match" }));
    expect(await screen.findByText(/no longer proposed/)).toBeInTheDocument();
  });
});

describe("SuggestionCard — agent proposal (#250)", () => {
  it("shows the agent's note and claims no confidence", () => {
    const proposal = transferLinkSuggestion({
      id: "s-3",
      kind: "schedule",
      source: "agent",
      confidence: null,
      headline: "Internet: monthly ~-80.00 on assets.checking with Example Fiber",
      draft: {
        version: 1,
        name: "Internet",
        account: "assets.checking",
        rrule: "FREQ=MONTHLY;BYMONTHDAY=3",
        dtstart: "2026-10-03",
        amount: "-80.00",
        category: null,
      },
      evidence_summary: {
        count: 0,
        first: null,
        last: null,
        amount_range: null,
        cadence: null,
        note: "Statement 2026-09: 80.00 due on the 3rd",
      },
    });
    renderWithProviders(<SuggestionCard suggestion={proposal} />);
    expect(screen.getByText("agent")).toBeInTheDocument();
    expect(screen.getByText("Statement 2026-09: 80.00 due on the 3rd")).toBeInTheDocument();
    expect(screen.getByText(/Agent's note:/)).toBeInTheDocument();
    expect(screen.queryByText(/% sure/)).not.toBeInTheDocument();
  });
});

describe("SuggestionCard — transfer pair (#386)", () => {
  const decided = (status: string, transfer: unknown = null) =>
    ok({
      suggestion: transferPairSuggestion({ status }),
      schedule: null,
      rule: null,
      projected_links: null,
      match: null,
      transfer,
    });

  it("shows where the money left and arrived, with no edit and no confidence", () => {
    renderWithProviders(<SuggestionCard suggestion={transferPairSuggestion()} />);
    expect(screen.getByText(/Link 250\.00 from assets\.checking/)).toBeInTheDocument();
    expect(screen.getByText("Leaves")).toBeInTheDocument();
    expect(screen.getByText("Arrives")).toBeInTheDocument();
    expect(screen.getByText("liabilities.card")).toBeInTheDocument();
    expect(screen.getByText("EXAMPLE BANK WEB DEBIT")).toBeInTheDocument();
    expect(screen.getByText("THANK YOU FOR YOUR REMITTANCE")).toBeInTheDocument();
    expect(screen.getByText("-$250.00")).toBeInTheDocument();
    expect(screen.getByText("$250.00")).toBeInTheDocument();
    expect(screen.getByText(/1 day apart/)).toBeInTheDocument();
    expect(screen.queryByText(/% sure/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Edit/ })).not.toBeInTheDocument();
  });

  it("accepts by linking the pair as one transfer", async () => {
    mockApi({
      [DECIDE]: decided("accepted", { merged_transaction_id: "t-merged", suggestion_id: "s-1" }),
    });
    const onDecided = vi.fn();
    const { user } = renderWithProviders(
      <SuggestionCard suggestion={transferPairSuggestion()} onDecided={onDecided} />,
    );
    await user.click(screen.getByRole("button", { name: "Link as transfer" }));
    expect(apiMock.POST).toHaveBeenCalledWith("/suggestions/{suggestion_id}/decide", {
      params: { path: { suggestion_id: "s-4" } },
      body: { decision: "accept", reject_scope: "exact" },
    });
    await waitFor(() => expect(onDecided).toHaveBeenCalledTimes(1));
  });

  it("rejects exactly this pair as not a transfer", async () => {
    mockApi({ [DECIDE]: decided("rejected") });
    const { user } = renderWithProviders(<SuggestionCard suggestion={transferPairSuggestion()} />);
    await user.click(screen.getByRole("button", { name: "Reject" }));
    expect(screen.queryByRole("button", { name: /Never/ })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Not a transfer" }));
    expect(apiMock.POST).toHaveBeenCalledWith("/suggestions/{suggestion_id}/decide", {
      params: { path: { suggestion_id: "s-4" } },
      body: { decision: "reject", reject_scope: "exact" },
    });
  });

  it("snoozes, and shows the server's refusal when the pair moved on", async () => {
    mockApi({
      [DECIDE]: fail(
        422,
        "this pair is no longer a transfer candidate — a row was linked, matched or voided; " +
          "the suggestion is withdrawn",
      ),
    });
    const { user } = renderWithProviders(<SuggestionCard suggestion={transferPairSuggestion()} />);
    await user.click(screen.getByRole("button", { name: "Snooze 30d" }));
    expect(apiMock.POST).toHaveBeenCalledWith("/suggestions/{suggestion_id}/decide", {
      params: { path: { suggestion_id: "s-4" } },
      body: { decision: "snooze", reject_scope: "exact" },
    });
    expect(await screen.findByText(/no longer a transfer candidate/)).toBeInTheDocument();
  });
});
