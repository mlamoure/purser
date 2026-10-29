import { screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
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
};

const rowOf = (text: string) => screen.getByText(text).closest("tr")!;

// `mapAccount` is useFeedConnection's (its API calls are covered there).
describe("MapAccountsStep", () => {
  it("offers Map only for unmapped accounts and maps the one clicked", async () => {
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

    expect(mapAccount).toHaveBeenCalledExactlyOnceWith(checking);
  });

  it("shows why a mapping failed", async () => {
    const mapAccount = vi.fn(async () => {
      throw new Error("path already exists: assets.everyday_checking");
    });
    const { user } = renderWithProviders(
      <MapAccountsStep discovered={[checking]} mapAccount={mapAccount} onContinue={vi.fn()} />,
    );

    await user.click(screen.getByRole("button", { name: "Map" }));

    expect(
      await screen.findByText("path already exists: assets.everyday_checking"),
    ).toBeInTheDocument();
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
