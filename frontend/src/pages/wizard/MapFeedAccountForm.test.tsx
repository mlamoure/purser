import { screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { accountOut } from "../../test/fixtures";
import { apiMock, fail, mockApi, ok } from "../../test/api";
import { renderWithProviders } from "../../test/render";
import {
  accountLabel,
  defaultPath,
  guessAccountType,
  mapFeedAccount,
  MapFeedAccountForm,
  type Discovered,
} from "./MapFeedAccountForm";

const card: Discovered = {
  external_id: "ext-card",
  name: "Rewards Card",
  org: "Example Bank",
  currency: "USD",
  balance: "-42.10",
  mapped_account_id: null,
  mapped_account_path: null,
  drift_basis_pinned: false,
  ambiguous: false,
    ignored: false,
};

describe("guessAccountType", () => {
  it("reads the product a bank account's name states", () => {
    expect(guessAccountType("Everyday Checking")).toBe("checking");
    expect(guessAccountType("SimpleFIN Savings")).toBe("savings");
    expect(guessAccountType("Premier Money Market")).toBe("savings");
    expect(guessAccountType("Rewards Visa Signature")).toBe("credit_card");
    expect(guessAccountType("Platinum Card")).toBe("credit_card");
    expect(guessAccountType("Home Mortgage")).toBe("loan");
    expect(guessAccountType("Auto Loan (x1111)")).toBe("loan");
    expect(guessAccountType("Rollover IRA")).toBe("brokerage");
    expect(guessAccountType("Individual Brokerage")).toBe("brokerage");
  });

  it("lets the product win over a brand name, and defaults to checking", () => {
    expect(guessAccountType("Example Card Company Online Savings")).toBe("savings");
    expect(guessAccountType("Checking with Debit Card")).toBe("checking");
    expect(guessAccountType("Joint Account")).toBe("checking");
  });
});

describe("accountLabel / defaultPath", () => {
  it("turns the bank's account name into a ledger label", () => {
    expect(accountLabel({ name: "Joint Checking (x1234)", external_id: "ext-1" })).toBe("joint_checking_x1234");
    expect(accountLabel({ name: null, external_id: "ACT-99" })).toBe("act_99");
  });

  it("files cards and loans under liabilities, the rest under assets", () => {
    expect(defaultPath("credit_card", "Rewards Card", "x")).toBe("liabilities.rewards_card");
    expect(defaultPath("loan", "Car Loan", "x")).toBe("liabilities.car_loan");
    expect(defaultPath("savings", "Rainy Day", "x")).toBe("assets.rainy_day");
  });
});

describe("mapFeedAccount", () => {
  it("posts the server's MapRequest shape and throws its reason", async () => {
    mockApi({ "POST /connections/{connection_id}/map": fail(422, "cannot map a feed onto a category") });

    await expect(mapFeedAccount("c-1", "ext-card", { account_id: "a-9" })).rejects.toThrow(
      "cannot map a feed onto a category",
    );
    expect(apiMock.POST).toHaveBeenCalledWith("/connections/{connection_id}/map", {
      params: { path: { connection_id: "c-1" } },
      body: { external_id: "ext-card", starting_balance: false, account_id: "a-9" },
    });
  });
});

describe("MapFeedAccountForm", () => {
  it("creates a new account; the path follows the type until edited", async () => {
    mockApi({ "GET /accounts": ok([]) });
    const onMap = vi.fn(async () => {});
    const { user } = renderWithProviders(
      <MapFeedAccountForm account={card} onMap={onMap} onCancel={vi.fn()} />,
    );

    const path = screen.getByLabelText("Ledger path");
    // The type is proposed from the bank's name, and the path follows it.
    expect(screen.getByLabelText("Account type")).toHaveValue("credit_card");
    expect(path).toHaveValue("liabilities.rewards_card");
    await user.selectOptions(screen.getByLabelText("Account type"), "savings");
    expect(path).toHaveValue("assets.rewards_card");
    await user.selectOptions(screen.getByLabelText("Account type"), "credit_card");
    expect(path).toHaveValue("liabilities.rewards_card");
    await user.clear(screen.getByLabelText("Account name"));
    await user.type(screen.getByLabelText("Account name"), "Travel Card");
    expect(path).toHaveValue("liabilities.travel_card");
    await user.clear(path);
    await user.type(path, "liabilities.cards.travel");
    await user.selectOptions(screen.getByLabelText("Account type"), "loan");
    expect(path).toHaveValue("liabilities.cards.travel"); // edited: left alone

    await user.click(screen.getByRole("button", { name: "Map account" }));

    expect(onMap).toHaveBeenCalledExactlyOnceWith({
      create: { name: "Travel Card", type: "loan", path: "liabilities.cards.travel", currency: "USD" },
    });
  });

  it("starts from Purser's proposal and says what the type rests on", async () => {
    mockApi({ "GET /accounts": ok([]) });
    const onMap = vi.fn(async () => {});
    const proposed: Discovered = {
      ...card,
      name: "Platinum (x3333)",
      proposal: {
        action: "map",
        name: "Platinum",
        type: "credit_card",
        type_basis: "balance",
        confident: true,
        path: "liabilities.platinum",
        starting_balance: true,
        duplicate_of: null,
      },
    };
    const { user } = renderWithProviders(
      <MapFeedAccountForm account={proposed} onMap={onMap} onCancel={vi.fn()} />,
    );

    expect(screen.getByLabelText("Account name")).toHaveValue("Platinum");
    expect(screen.getByLabelText("Account type")).toHaveValue("credit_card");
    expect(screen.getByLabelText("Ledger path")).toHaveValue("liabilities.platinum");
    expect(screen.getByText(/because the account owes money/)).toBeInTheDocument();
    expect(screen.getByText("The account type cannot be changed after mapping.")).toBeInTheDocument();
    // The reason belongs to the proposed type: another choice drops it.
    await user.selectOptions(screen.getByLabelText("Account type"), "loan");
    expect(screen.queryByText(/because the account owes money/)).not.toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("Account type"), "credit_card");

    await user.click(screen.getByRole("button", { name: "Map account" }));
    expect(onMap).toHaveBeenCalledExactlyOnceWith({
      create: { name: "Platinum", type: "credit_card", path: "liabilities.platinum", currency: "USD" },
      starting_balance: true,
    });
  });

  it("asks about a cash product that also lists investments", async () => {
    mockApi({ "GET /accounts": ok([]) });
    const mixed: Discovered = {
      ...card,
      name: "Demo Savings",
      proposal: {
        action: "map",
        name: "Demo Savings",
        type: "savings",
        type_basis: "name",
        confident: false,
        path: "assets.demo_savings",
        starting_balance: true,
        duplicate_of: null,
        note: "the feed lists holdings for this account",
      },
    };
    renderWithProviders(<MapFeedAccountForm account={mixed} onMap={vi.fn()} onCancel={vi.fn()} />);

    expect(screen.getByLabelText("Account type")).toHaveValue("savings");
    expect(screen.getByText(/also lists investments in this account/)).toBeInTheDocument();
  });

  it("points out an account a second bank login delivers again", async () => {
    mockApi({ "GET /accounts": ok([]) });
    const twin: Discovered = {
      ...card,
      proposal: {
        action: "ignore",
        name: "Joint Account",
        type: "checking",
        type_basis: "default",
        confident: false,
        path: "assets.joint_account",
        starting_balance: true,
        duplicate_of: "assets.joint_account",
      },
    };
    renderWithProviders(<MapFeedAccountForm account={twin} onMap={vi.fn()} onCancel={vi.fn()} />);

    expect(
      screen.getByText(/another bank login already delivers \(assets\.joint_account\)/),
    ).toBeInTheDocument();
    expect(screen.getByText(/does not say what kind of account this is/)).toBeInTheDocument();
  });

  it("maps onto an existing open account in the feed's currency", async () => {
    mockApi({
      "GET /accounts": ok([
        accountOut({ id: "a-card", name: "Old Card", path: "liabilities.old_card", type: "credit_card" }),
        accountOut({ id: "a-eur", name: "Euro Card", path: "liabilities.euro_card", currency: "EUR" }),
        accountOut({ id: "a-closed", name: "Closed", path: "assets.closed", closed_at: "2026-02-01T00:00:00Z" }),
      ]),
    });
    const onMap = vi.fn(async () => {});
    const { user } = renderWithProviders(
      <MapFeedAccountForm account={card} onMap={onMap} onCancel={vi.fn()} />,
    );

    await user.click(await screen.findByRole("radio", { name: "An existing account" }));
    const select = screen.getByLabelText("Purser account");
    expect(screen.getByRole("button", { name: "Map account" })).toBeDisabled();
    expect(screen.queryByRole("option", { name: /euro_card/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("option", { name: /assets\.closed/ })).not.toBeInTheDocument();
    await user.selectOptions(select, "a-card");
    await user.click(screen.getByRole("button", { name: "Map account" }));

    expect(onMap).toHaveBeenCalledExactlyOnceWith({ account_id: "a-card" });
  });

  it("asks for a starting balance from the bank on a new account, unless unticked", async () => {
    mockApi({ "GET /accounts": ok([]) });
    const onMap = vi.fn(async () => {});
    const { user } = renderWithProviders(
      <MapFeedAccountForm account={card} onMap={onMap} onCancel={vi.fn()} />,
    );

    const option = screen.getByRole("checkbox", { name: "Start from the bank's balance" });
    expect(option).toBeChecked();
    await user.click(screen.getByRole("button", { name: "Map account" }));
    expect(onMap).toHaveBeenLastCalledWith({
      create: { name: "Rewards Card", type: "credit_card", path: "liabilities.rewards_card", currency: "USD" },
      starting_balance: true,
    });

    await user.click(option);
    await user.click(screen.getByRole("button", { name: "Map account" }));
    expect(onMap).toHaveBeenLastCalledWith({
      create: { name: "Rewards Card", type: "credit_card", path: "liabilities.rewards_card", currency: "USD" },
    });
  });

  it("does not offer it where the balance is not the sum of the transactions", async () => {
    mockApi({ "GET /accounts": ok([]) });
    const { user } = renderWithProviders(
      <MapFeedAccountForm account={card} onMap={vi.fn()} onCancel={vi.fn()} />,
    );

    for (const type of ["brokerage", "loan", "property"]) {
      await user.selectOptions(screen.getByLabelText("Account type"), type);
      expect(
        screen.queryByRole("checkbox", { name: "Start from the bank's balance" }),
      ).not.toBeInTheDocument();
    }
    await user.selectOptions(screen.getByLabelText("Account type"), "savings");
    expect(screen.getByRole("checkbox", { name: "Start from the bank's balance" })).toBeChecked();
  });

  it("leaves it unticked for an existing account, which the person may tick", async () => {
    mockApi({
      "GET /accounts": ok([
        accountOut({ id: "a-card", name: "Old Card", path: "liabilities.old_card", type: "credit_card" }),
      ]),
    });
    const onMap = vi.fn(async () => {});
    const { user } = renderWithProviders(
      <MapFeedAccountForm account={card} onMap={onMap} onCancel={vi.fn()} />,
    );

    await user.click(await screen.findByRole("radio", { name: "An existing account" }));
    await user.selectOptions(screen.getByLabelText("Purser account"), "a-card");
    const option = screen.getByRole("checkbox", { name: "Start from the bank's balance" });
    expect(option).not.toBeChecked();
    await user.click(option);
    await user.click(screen.getByRole("button", { name: "Map account" }));

    expect(onMap).toHaveBeenCalledExactlyOnceWith({ account_id: "a-card", starting_balance: true });
  });

  it("offers only a new account when no existing one fits", async () => {
    mockApi({ "GET /accounts": ok([]) });
    renderWithProviders(<MapFeedAccountForm account={card} onMap={vi.fn()} onCancel={vi.fn()} />);

    await waitFor(() => expect(apiMock.GET).toHaveBeenCalledWith("/accounts", expect.anything()));
    expect(screen.queryByRole("radio", { name: "An existing account" })).not.toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "A new Purser account" })).toBeChecked();
  });

  it("shows what onMap throws and cancels on request", async () => {
    mockApi({ "GET /accounts": ok([]) });
    const onCancel = vi.fn();
    const { user } = renderWithProviders(
      <MapFeedAccountForm
        account={card}
        onMap={async () => {
          throw new Error("account path 'assets.rewards_card' already exists");
        }}
        onCancel={onCancel}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Map account" }));
    expect(
      await screen.findByText("account path 'assets.rewards_card' already exists"),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onCancel).toHaveBeenCalledOnce();
  });
});
