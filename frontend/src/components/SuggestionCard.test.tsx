import { screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { apiMock, fail, mockApi, ok } from "../test/api";
import {
  accountOut,
  matchSuggestion,
  ruleOut,
  splitSuggestion,
  transferLinkSuggestion,
  transferPairSuggestion,
} from "../test/fixtures";
import { shownText, spokenText } from "../test/a11y";
import { renderWithProviders } from "../test/render";
import { SuggestionCard, type SuggestionOut } from "./SuggestionCard";

const DECIDE = "POST /suggestions/{suggestion_id}/decide";
const SET_STATE = "POST /rules/{rule_id}/state";

/** The accounts the cards' drafts name by path: each card reads them by name
 * (#552, the #541 helpers over GET /accounts). */
const ACCOUNTS = [
  accountOut({ id: "a-1", name: "Checking", path: "assets.checking" }),
  accountOut({ id: "a-2", name: "Rainy Day Savings", path: "assets.savings" }),
  accountOut({ id: "a-3", name: "Example Card", path: "liabilities.card", type: "credit_card" }),
  accountOut({ id: "a-4", name: "Example Brokerage", path: "assets.broker", type: "brokerage" }),
];
beforeEach(() => mockApi({ "GET /accounts": ok(ACCOUNTS) }));

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
  it("shows the pair, the payee scope and the window the draft rule would declare", async () => {
    renderWithProviders(<SuggestionCard suggestion={transferLinkSuggestion()} />);
    expect(screen.getByText(/^Link transfers between Checking and Rainy Day Savings/)).toBeInTheDocument();
    // The detail line names the accounts too, never by path (#552).
    expect(await screen.findByText(/Checking and Rainy Day Savings · payee/)).toBeInTheDocument();
    expect(screen.queryByText(/assets\./)).not.toBeInTheDocument();
    expect(screen.getByText("your confirm")).toBeInTheDocument();
    expect(screen.getByText(/or an alias · legs within 3 days \(standard\) · any amount/)).toBeInTheDocument();
    expect(screen.getByText(/1 confirmed pair\(s\) on May 4, 2026/)).toBeInTheDocument();
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
      headline: "Gym: monthly -15.99 on Checking with Gym",
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
    expect(screen.getByText(/3 occurrences from Mar 15, 2026 to May 15, 2026/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Create scheduled transaction" }));
    expect(apiMock.POST).toHaveBeenCalledWith("/suggestions/{suggestion_id}/decide", {
      params: { path: { suggestion_id: "s-2" } },
      body: { decision: "accept", reject_scope: "exact" },
    });
    expect(screen.queryByRole("button", { name: /Activate/ })).not.toBeInTheDocument();
  });

  /** A detected gym bill on Checking, filed under Fitness. */
  const gym = transferLinkSuggestion({
    id: "s-gym",
    kind: "schedule",
    source: "detected",
    confidence: "0.750",
    headline: "Gym: monthly -15.99 on Checking with Gym",
    draft: {
      version: 1,
      name: "Gym",
      account: "assets.checking",
      counterparty_account: null,
      rrule: "FREQ=MONTHLY;BYMONTHDAY=15",
      dtstart: "2026-05-15",
      amount: "-15.99",
      payee: "Gym",
      category: "expenses.fitness",
    },
  });
  const categories = () =>
    mockApi({
      "GET /categories": ok([
        accountOut({ id: "c-1", name: "Fitness", path: "expenses.fitness", type: "category_expense", is_virtual: true }),
        accountOut({ id: "c-2", name: "Health", path: "expenses.health", type: "category_expense", is_virtual: true }),
      ]),
      [DECIDE]: ok({ suggestion: { ...gym, status: "accepted" }, schedule: null, rule: null, projected_links: null }),
    });

  it("names the account and picks the category by name, sending its path (#552)", async () => {
    categories();
    const { user } = renderWithProviders(<SuggestionCard suggestion={gym} />);
    expect(await screen.findByText(/scheduled transaction on Checking;/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Edit first" }));
    const picker = screen.getByLabelText("Category, or transfer to");
    await screen.findByRole("option", { name: "Health" });
    expect(picker).toHaveDisplayValue("Fitness"); // the draft's path, read as its name
    await user.selectOptions(picker, "Health");
    await user.click(screen.getByRole("button", { name: "Create with edits" }));
    expect(apiMock.POST).toHaveBeenCalledWith("/suggestions/{suggestion_id}/decide", {
      params: { path: { suggestion_id: "s-gym" } },
      body: { decision: "edit_then_accept", edits: { category: "expenses.health" }, reject_scope: "exact" },
    });
  });

  it("shows an estimated amount as ≈ −95.00, heard as estimated (#556)", async () => {
    categories();
    const power = {
      ...gym,
      id: "s-power",
      headline: "Example Power: monthly \u2248 \u221295.00 on Checking with Example Power",
      draft: { ...gym.draft, name: "Example Power", amount_type: "estimated", estimate_basis: "rolling_avg_3", amount: "-95.00" },
    };
    const { container } = renderWithProviders(<SuggestionCard suggestion={power} />);
    const headline = await screen.findByText(/^Example Power: monthly/, { selector: "span" });
    expect(shownText(headline)).toBe("Example Power: monthly \u2248 \u221295.00 on Checking with Example Power");
    expect(spokenText(headline)).toBe("Example Power: monthly estimated \u221295.00 on Checking with Example Power");
    const note = screen.getByText(/scheduled transaction on Checking;/);
    expect(shownText(note)).toContain("Amount: \u2248 \u2212$95.00.");
    expect(spokenText(note)).toContain("Amount: estimated \u2212$95.00.");
    expect(container).not.toHaveTextContent("~"); // the old mark is gone
  });

  it("keeps a fixed amount plain (#556)", async () => {
    categories();
    renderWithProviders(<SuggestionCard suggestion={gym} />);
    const note = await screen.findByText(/scheduled transaction on Checking;/);
    expect(note).toHaveTextContent("Amount: -$15.99.");
    expect(note.textContent).not.toContain("\u2248");
  });

  it("turns a transfer target into the draft's counterparty account, clearing the category", async () => {
    categories();
    const { user } = renderWithProviders(<SuggestionCard suggestion={gym} />);
    await user.click(screen.getByRole("button", { name: "Edit first" }));
    await screen.findByRole("option", { name: "Transfer to Rainy Day Savings" });
    await user.selectOptions(screen.getByLabelText("Category, or transfer to"), "Transfer to Rainy Day Savings");
    await user.click(screen.getByRole("button", { name: "Create with edits" }));
    expect(apiMock.POST).toHaveBeenCalledWith("/suggestions/{suggestion_id}/decide", {
      params: { path: { suggestion_id: "s-gym" } },
      body: {
        decision: "edit_then_accept",
        edits: { category: null, counterparty_account: "assets.savings" },
        reject_scope: "exact",
      },
    });
  });

  it("edits an every-4-weeks cadence and switches it to monthly before accepting (#556)", async () => {
    const schedule = transferLinkSuggestion({
      id: "s-3",
      kind: "schedule",
      source: "detected",
      confidence: "0.825",
      headline: "Example subscription: every 4 weeks -24.00 on assets.checking",
      draft: {
        version: 1,
        name: "Example subscription",
        account: "assets.checking",
        rrule: "FREQ=WEEKLY;INTERVAL=4;BYDAY=MO",
        dtstart: "2026-11-09",
        amount: "-24.00",
        category: null,
      },
    });
    mockApi({
      "GET /categories": ok([]),
      [DECIDE]: ok({
        suggestion: { ...schedule, status: "accepted" },
        schedule: null,
        rule: null,
        projected_links: null,
      }),
    });
    const { user } = renderWithProviders(<SuggestionCard suggestion={schedule} />);

    await user.click(screen.getByRole("button", { name: "Edit first" }));
    const repeats = screen.getByLabelText("Repeats");
    expect(repeats).toHaveValue("weeks-4");
    expect(screen.getByLabelText("Day of week")).toHaveValue("MO");

    await user.selectOptions(repeats, "monthly");
    expect(screen.getByLabelText("Day of month")).toHaveValue(1);
    expect(screen.queryByLabelText("Day of week")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Create with edits" }));
    expect(apiMock.POST).toHaveBeenCalledWith("/suggestions/{suggestion_id}/decide", {
      params: { path: { suggestion_id: "s-3" } },
      body: {
        decision: "edit_then_accept",
        edits: { rrule: "FREQ=MONTHLY;BYMONTHDAY=1" },
        reject_scope: "exact",
      },
    });
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

  it("shows the expected row against the bank row and the variance, with no edit", async () => {
    renderWithProviders(<SuggestionCard suggestion={matchSuggestion()} />);
    expect(screen.getByText("Expected · City Utilities")).toBeInTheDocument();
    expect(screen.getByText("Bank row")).toBeInTheDocument();
    expect(screen.getByText("Jul 18, 2026")).toBeInTheDocument();
    expect(
      await screen.findByText(
        (_, el) => el?.tagName === "P" && /^on Checking · variance/.test(el.textContent ?? ""),
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
      headline: "Internet: monthly -80.00 on Checking with Example Fiber",
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

  it("shows where the money left and arrived, with no edit and no confidence", async () => {
    renderWithProviders(<SuggestionCard suggestion={transferPairSuggestion()} />);
    expect(screen.getByText(/Link 250\.00 from Checking/)).toBeInTheDocument();
    expect(screen.getByText("Leaves")).toBeInTheDocument();
    expect(screen.getByText("Arrives")).toBeInTheDocument();
    // Each side's account by name (#552), never by path.
    expect(await screen.findByText("Example Card")).toBeInTheDocument();
    expect(within(screen.getByText("Leaves").closest("tr")!).getByText("Checking")).toBeInTheDocument();
    expect(screen.queryByText("liabilities.card")).not.toBeInTheDocument();
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

describe("SuggestionCard — split (#590)", () => {
  const names = () =>
    mockApi({
      "GET /categories": ok([
        accountOut({ id: "c-1", name: "Salary", path: "income.salary", type: "category_income", is_virtual: true }),
        accountOut({ id: "c-2", name: "Taxes", path: "expenses.taxes", type: "category_expense", is_virtual: true }),
      ]),
      "GET /accounts": ok([
        accountOut({ id: "a-1", name: "Checking", path: "assets.checking" }),
        accountOut({ id: "a-2", name: "Example 401(k)", path: "assets.retirement" }),
      ]),
    });
  const decided = (status: string) =>
    ok({ suggestion: splitSuggestion({ status }), split_transaction_id: "t-split" });

  it("shows the row and the proposed lines, and accept applies them as shown", async () => {
    names();
    mockApi({ [DECIDE]: decided("accepted") });
    const { user } = renderWithProviders(<SuggestionCard suggestion={splitSuggestion()} />);
    expect(screen.getByText(/like last time/)).toBeInTheDocument();
    expect(await screen.findByText("To Example 401(k)")).toBeInTheDocument();
    expect(screen.getByText("1 bank row")).toBeInTheDocument();
    expect(screen.getByText("$4,050.00")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Split like this" }));
    await waitFor(() =>
      expect(apiMock.POST).toHaveBeenCalledWith("/suggestions/{suggestion_id}/decide", {
        params: { path: { suggestion_id: "s-5" } },
        body: { decision: "accept", reject_scope: "exact" },
      }),
    );
  });

  it("Edit… opens the prefilled editor and accepts the edited lines", async () => {
    names();
    mockApi({
      [DECIDE]: decided("accepted"),
      "GET /transactions/{tx_id}/split/candidates": ok({
        account: "assets.retirement",
        window_days: 7,
        amount: "600.00",
        candidates: [],
      }),
    });
    const { user } = renderWithProviders(<SuggestionCard suggestion={splitSuggestion()} />);
    await user.click(screen.getByRole("button", { name: "Edit…" }));
    const salary = screen.getByRole("textbox", { name: "Line 1 amount" });
    await user.clear(salary);
    await user.type(salary, "4000");
    const taxes = screen.getByRole("textbox", { name: "Line 2 amount" });
    await user.clear(taxes);
    await user.type(taxes, "-850");
    await user.click(screen.getByRole("button", { name: "Save split" }));
    await waitFor(() =>
      expect(apiMock.POST).toHaveBeenCalledWith("/suggestions/{suggestion_id}/decide", {
        params: { path: { suggestion_id: "s-5" } },
        body: {
          decision: "edit_then_accept",
          reject_scope: "exact",
          edits: {
            lines: [
              { target: "income.salary", amount: "4000.00", memo: null, backing_ids: [] },
              { target: "expenses.taxes", amount: "-850.00", memo: "withholding", backing_ids: [] },
              {
                target: "[assets.retirement]",
                amount: "-600.00",
                memo: null,
                backing_ids: ["t-401k"],
              },
            ],
          },
        },
      }),
    );
    expect(apiMock.PUT).not.toHaveBeenCalled();
  });

  it("rejects for good for this payee", async () => {
    names();
    mockApi({ [DECIDE]: decided("rejected") });
    const { user } = renderWithProviders(<SuggestionCard suggestion={splitSuggestion()} />);
    await user.click(screen.getByRole("button", { name: "Reject" }));
    await user.click(screen.getByRole("button", { name: "Never for this payee" }));
    await waitFor(() =>
      expect(apiMock.POST).toHaveBeenCalledWith("/suggestions/{suggestion_id}/decide", {
        params: { path: { suggestion_id: "s-5" } },
        body: { decision: "reject", reject_scope: "counterparty" },
      }),
    );
  });
});

const tradeRuleSuggestion = (over: Partial<SuggestionOut> = {}): SuggestionOut =>
  transferLinkSuggestion({
    id: "s-trade",
    kind: "trade_rule",
    source: "mark",
    headline:
      "Mark buy (money out) rows like the one you marked on Example Brokerage as a securities trade, with a rule",
    draft: {
      version: 1,
      account: "assets.broker",
      account_id: "acct-b",
      match_field: "payee_or_description",
      match_mode: "prefix",
      match_text: "Example Buy",
      direction: "debit",
    },
    ...over,
  });

describe("SuggestionCard — trade_rule (#591)", () => {
  it("shows the rule a mark proposes", async () => {
    renderWithProviders(<SuggestionCard suggestion={tradeRuleSuggestion()} />);
    expect(screen.getByText("your mark")).toBeInTheDocument();
    expect(await screen.findByText(/^On Example Brokerage:/)).toBeInTheDocument();
    expect(screen.getByText(/payee or description starts with/)).toBeInTheDocument();
    expect(screen.getByText(/money out \(buys\) → Securities trade/)).toBeInTheDocument();
  });

  it("accepts with edits into a draft rule, then activates it on request", async () => {
    mockApi({
      [DECIDE]: ok({
        suggestion: tradeRuleSuggestion({ status: "accepted" }),
        schedule: null,
        rule: ruleOut({ id: "r-9", type: "categorize", securities_trade: true }),
        projected_matches: 4,
      }),
      [SET_STATE]: ok(ruleOut({ id: "r-9", state: "active" })),
    });
    const { user } = renderWithProviders(<SuggestionCard suggestion={tradeRuleSuggestion()} />);
    await user.click(screen.getByRole("button", { name: "Edit & accept" }));
    const text = screen.getByLabelText("Text");
    await user.clear(text);
    await user.type(text, "Example");
    await user.click(screen.getByRole("button", { name: "Create with edits" }));
    expect(apiMock.POST).toHaveBeenCalledWith("/suggestions/{suggestion_id}/decide", {
      params: { path: { suggestion_id: "s-trade" } },
      body: { decision: "edit_then_accept", edits: { match_text: "Example" }, reject_scope: "exact" },
    });
    expect(await screen.findByText(/It matches/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Activate" }));
    expect(apiMock.POST).toHaveBeenLastCalledWith("/rules/{rule_id}/state", {
      params: { path: { rule_id: "r-9" } },
      body: { state: "active" },
    });
  });
});
