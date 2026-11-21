import { screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { accountOut } from "../../test/fixtures";
import { apiMock, fail, mockApi, ok } from "../../test/api";
import { renderWithProviders } from "../../test/render";
import {
  accountLabel,
  defaultPath,
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
      body: { external_id: "ext-card", account_id: "a-9" },
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
