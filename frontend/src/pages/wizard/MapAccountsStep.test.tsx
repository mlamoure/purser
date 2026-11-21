import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { mockApi, ok } from "../../test/api";
import { renderWithProviders } from "../../test/render";
import { MapAccountsStep } from "./MapAccountsStep";
import type { Discovered } from "./useFeedConnection";

const checking: Discovered = {
  external_id: "ext-1",
  name: "Everyday Checking",
  org: "Example Bank",
  currency: "USD",
  balance: "1250.00",
  mapped_account_id: null,
  mapped_account_path: null,
  drift_basis_pinned: false,
  ambiguous: false,
    ignored: false,
};
const savings: Discovered = {
  external_id: "ext-2",
  name: null,
  org: null,
  currency: "USD",
  balance: null,
  mapped_account_id: "a-2",
  mapped_account_path: "assets.rainy_day",
  drift_basis_pinned: false,
  ambiguous: false,
    ignored: false,
};

const rowOf = (text: string) => screen.getByText(text).closest("tr")!;

// `mapAccount` is useFeedConnection's (its API calls are covered there); the
// form's own behaviour is MapFeedAccountForm.test.tsx's.
describe("MapAccountsStep", () => {
  it("offers Map only for unmapped accounts and maps the one clicked", async () => {
    mockApi({ "GET /accounts": ok([]) });
    const mapAccount = vi.fn(async () => {});
    const { user } = renderWithProviders(
      <MapAccountsStep discovered={[checking, savings]} mapAccount={mapAccount} onContinue={vi.fn()} />,
    );

    const unmapped = rowOf("Everyday Checking · Example Bank");
    expect(within(unmapped).getByText("unmapped")).toBeInTheDocument();
    expect(within(unmapped).getByText("1250.00")).toBeInTheDocument();
    const mapped = rowOf("ext-2"); // no name: the external id stands in
    expect(within(mapped).getByText("assets.rainy_day")).toBeInTheDocument();
    expect(within(mapped).queryByRole("button", { name: "Map" })).not.toBeInTheDocument();

    await user.click(within(unmapped).getByRole("button", { name: "Map" }));
    await user.click(screen.getByRole("button", { name: "Map account" }));

    await waitFor(() =>
      expect(mapAccount).toHaveBeenCalledExactlyOnceWith(checking, {
        create: {
          name: "Everyday Checking",
          type: "checking",
          path: "assets.everyday_checking",
          currency: "USD",
        },
      }),
    );
    // The form closes once the mapping succeeds.
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Map account" })).not.toBeInTheDocument(),
    );
  });

  it("shows why a mapping failed and keeps the form open", async () => {
    mockApi({ "GET /accounts": ok([]) });
    const mapAccount = vi.fn(async () => {
      throw new Error("account path 'assets.everyday_checking' already exists");
    });
    const { user } = renderWithProviders(
      <MapAccountsStep discovered={[checking]} mapAccount={mapAccount} onContinue={vi.fn()} />,
    );

    await user.click(screen.getByRole("button", { name: "Map" }));
    await user.click(screen.getByRole("button", { name: "Map account" }));

    expect(
      await screen.findByText("account path 'assets.everyday_checking' already exists"),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Map account" })).toBeInTheDocument();
  });

  it("continues whether or not every account is mapped", async () => {
    const onContinue = vi.fn();
    const { user } = renderWithProviders(
      <MapAccountsStep discovered={[checking]} mapAccount={vi.fn()} onContinue={onContinue} />,
    );

    await user.click(screen.getByRole("button", { name: "Continue" }));

    expect(onContinue).toHaveBeenCalledOnce();
  });
});
