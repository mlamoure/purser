import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { apiMock, mockApi, ok } from "../test/api";
import { accountOut } from "../test/fixtures";
import { renderWithProviders } from "../test/render";
import { AssetPanel } from "./AssetPanel";
import { AddAssetForm, assetPathFor } from "./AddAssetForm";
import { accountIconFor, assetKindLabel } from "./AccountIcon";
import { Car, CreditCard, House, Landmark, Package, PiggyBank } from "lucide-react";

const CLOCK = { "GET /clock": ok({ ledger_today: "2026-11-21", timezone: "America/New_York" }) };

const house = accountOut({
  id: "a-house",
  name: "Main house",
  path: "assets.main_house",
  type: "property",
  balance: "650000.0000",
  asset: {
    kind: "house",
    acquired_on: "2016-12-15",
    description: "Synthetic address 1",
    revalue_after_days: 35,
    last_valued: "2026-11-01",
    stale: false,
  },
  equity: "400000.0000",
  secured_loan_ids: ["a-loan"],
});
const loan = accountOut({
  id: "a-loan",
  name: "Mortgage",
  path: "liabilities.mortgage",
  type: "loan",
  balance: "-250000.0000",
  secured_by_id: "a-house",
});
const carLoan = accountOut({
  id: "a-carloan",
  name: "Car loan",
  path: "liabilities.car",
  type: "loan",
  balance: "-9000.0000",
});

describe("account icons (#567)", () => {
  it("maps each account type, and each asset kind, to its icon", () => {
    expect(accountIconFor("checking")).toBe(Landmark);
    expect(accountIconFor("savings")).toBe(PiggyBank);
    expect(accountIconFor("credit_card")).toBe(CreditCard);
    expect(accountIconFor("property", "house")).toBe(House);
    expect(accountIconFor("property", "car")).toBe(Car);
    expect(accountIconFor("property", null)).toBe(Package);
    expect(assetKindLabel("art")).toBe("Art & collectibles");
    expect(assetKindLabel(undefined)).toBe("Other");
  });
});

describe("AssetPanel (#566, #567)", () => {
  it("shows what the asset is and its equity against the loans it secures", async () => {
    mockApi({ ...CLOCK, "GET /accounts": ok([house, loan, carLoan]) });
    renderWithProviders(<AssetPanel account={house} />);
    const panel = screen.getByRole("region", { name: "Asset" });
    expect(within(panel).getByText("House")).toBeInTheDocument();
    expect(within(panel).getByText("Synthetic address 1")).toBeInTheDocument();
    expect(within(panel).getByText("$400,000.00")).toBeInTheDocument();
    expect(await within(panel).findByText("Mortgage")).toBeInTheDocument();
    expect(within(panel).getByRole("option", { name: "Car loan" })).toBeInTheDocument();
  });

  it("records a value on a date through reconcile, with its source", async () => {
    mockApi({
      ...CLOCK,
      "GET /accounts": ok([house, loan]),
      "POST /accounts/{account_id}/reconcile": ok({
        as_of: "2026-11-01",
        adjustment: "12500.0000",
      }),
    });
    const { user } = renderWithProviders(<AssetPanel account={house} />);
    await user.click(screen.getByRole("button", { name: "Record value" }));
    await user.type(screen.getByLabelText("Value"), "662500.00");
    await user.clear(screen.getByLabelText("Value date"));
    await user.type(screen.getByLabelText("Value date"), "2026-11-01");
    await user.type(screen.getByLabelText("Value source"), "Estimate midpoint");
    await user.click(screen.getByRole("button", { name: "Save value" }));
    await waitFor(() =>
      expect(apiMock.POST).toHaveBeenCalledWith("/accounts/{account_id}/reconcile", {
        params: { path: { account_id: "a-house" } },
        body: {
          as_of: "2026-11-01",
          statement_balance: "662500.00",
          basis: "posted",
          component: "balance",
          dry_run: false,
          note: "Estimate midpoint",
        },
      }),
    );
    expect(await screen.findByRole("status")).toHaveTextContent("+$12,500.00");
  });

  it("records a change, or money put in", async () => {
    mockApi({
      ...CLOCK,
      "GET /accounts": ok([house]),
      "POST /accounts/{account_id}/value-changes": ok({ balance: "640000.0000" }, 201),
    });
    const { user } = renderWithProviders(<AssetPanel account={house} />);
    await user.click(screen.getByRole("button", { name: "Change value" }));
    await user.type(screen.getByLabelText("Change amount"), "-10000.00");
    await user.selectOptions(screen.getByLabelText("Change kind"), "capital");
    await user.click(screen.getByRole("button", { name: "Save change" }));
    await waitFor(() =>
      expect(apiMock.POST).toHaveBeenCalledWith("/accounts/{account_id}/value-changes", {
        params: { path: { account_id: "a-house" } },
        body: { as_of: "2026-11-21", amount: "-10000.00", kind: "capital" },
      }),
    );
    expect(await screen.findByRole("status")).toHaveTextContent("$640,000.00");
  });

  it("says when the value is stale, and saves the reminder", async () => {
    const stale = { ...house, asset: { ...house.asset!, last_valued: "2026-09-01", stale: true } };
    mockApi({
      ...CLOCK,
      "GET /accounts": ok([stale]),
      "PATCH /accounts/{account_id}": ok(stale),
    });
    const { user } = renderWithProviders(<AssetPanel account={stale} />);
    expect(screen.getByRole("note")).toHaveTextContent("older than 35 days");
    expect(screen.getByText(/Value last stated/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Edit details" }));
    const field = screen.getByLabelText("Remind after days");
    await user.clear(field);
    await user.type(field, "60");
    await user.click(screen.getByRole("button", { name: "Save details" }));
    await waitFor(() =>
      expect(apiMock.PATCH).toHaveBeenCalledWith("/accounts/{account_id}", {
        params: { path: { account_id: "a-house" } },
        body: {
          asset_kind: "house",
          acquired_on: "2016-12-15",
          asset_description: "Synthetic address 1",
          revalue_after_days: 60,
        },
      }),
    );
  });

  it("previews a disposal, then closes the asset", async () => {
    const preview = {
      account_id: "a-house",
      as_of: "2026-11-21",
      value_before: "650000.0000",
      closing_change: "-650000.0000",
      gain: "-550000.0000",
      money_put_in: "1200000.0000",
      dry_run: true,
    };
    mockApi({
      ...CLOCK,
      "GET /accounts": ok([house]),
      "POST /accounts/{account_id}/dispose": (init) =>
        ok({ ...preview, dry_run: (init.body as { dry_run: boolean }).dry_run }),
    });
    const { user } = renderWithProviders(<AssetPanel account={house} />);
    await user.click(screen.getByRole("button", { name: "Sell or dispose…" }));
    await user.click(screen.getByRole("button", { name: "Preview" }));
    const status = await screen.findByText(/Left on the asset/);
    expect(status).toHaveTextContent("$650,000.00");
    expect(screen.getByText(/whole value is written off/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Confirm and close" }));
    await waitFor(() =>
      expect(apiMock.POST).toHaveBeenLastCalledWith("/accounts/{account_id}/dispose", {
        params: { path: { account_id: "a-house" } },
        body: { as_of: "2026-11-21", dry_run: false },
      }),
    );
  });

  it("links and unlinks a loan", async () => {
    mockApi({
      ...CLOCK,
      "GET /accounts": ok([house, loan, carLoan]),
      "PATCH /accounts/{account_id}": ok(carLoan),
    });
    const { user } = renderWithProviders(<AssetPanel account={house} />);
    await user.selectOptions(await screen.findByLabelText("Loan to link"), "a-carloan");
    await user.click(screen.getByRole("button", { name: "Link" }));
    await waitFor(() =>
      expect(apiMock.PATCH).toHaveBeenCalledWith("/accounts/{account_id}", {
        params: { path: { account_id: "a-carloan" } },
        body: { secured_by_id: "a-house" },
      }),
    );
    await user.click(screen.getByRole("button", { name: "Unlink Mortgage" }));
    await waitFor(() =>
      expect(apiMock.PATCH).toHaveBeenCalledWith("/accounts/{account_id}", {
        params: { path: { account_id: "a-loan" } },
        body: { secured_by_id: null },
      }),
    );
  });
});

describe("AddAssetForm (#567)", () => {
  it("derives a path from the name", () => {
    expect(assetPathFor("Main Street House!")).toBe("assets.main_street_house");
    expect(assetPathFor("  ")).toBe("assets.asset");
  });

  it("creates the asset, then records its purchase price as money put in", async () => {
    mockApi({
      ...CLOCK,
      "POST /accounts": ok(accountOut({ id: "a-new", type: "property" }), 201),
      "POST /accounts/{account_id}/value-changes": ok({ balance: "30000.0000" }, 201),
      "GET /accounts": ok([]),
    });
    const { user } = renderWithProviders(<AddAssetForm onCancel={() => {}} />);
    await user.type(screen.getByLabelText("Asset name"), "Blue Car");
    await user.selectOptions(screen.getByLabelText("Asset kind"), "car");
    await user.type(screen.getByLabelText("Purchased on"), "2024-03-01");
    await user.type(screen.getByLabelText("Purchase price"), "30000.00");
    await user.click(screen.getByRole("button", { name: "Add asset" }));
    await waitFor(() =>
      expect(apiMock.POST).toHaveBeenCalledWith("/accounts", {
        body: {
          name: "Blue Car",
          type: "property",
          path: "assets.blue_car",
          currency: "USD",
          asset_kind: "car",
          acquired_on: "2024-03-01",
        },
      }),
    );
    await waitFor(() =>
      expect(apiMock.POST).toHaveBeenCalledWith("/accounts/{account_id}/value-changes", {
        params: { path: { account_id: "a-new" } },
        body: { as_of: "2024-03-01", amount: "30000.00", kind: "capital", note: "Purchase price" },
      }),
    );
  });

  it("refuses a price that is not an amount", async () => {
    mockApi({ ...CLOCK });
    const { user } = renderWithProviders(<AddAssetForm onCancel={() => {}} />);
    await user.type(screen.getByLabelText("Asset name"), "Ring");
    await user.type(screen.getByLabelText("Purchase price"), "a lot");
    expect(screen.getByRole("button", { name: "Add asset" })).toBeDisabled();
    expect(screen.getByText(/Enter a price/)).toBeInTheDocument();
  });
});
