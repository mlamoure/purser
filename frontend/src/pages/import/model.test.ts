import { describe, expect, it } from "vitest";
import { counts, previewAccount } from "../../test/fixtures";
import {
  allResolved,
  buildAccountMap,
  buildOptions,
  CREATE,
  layoutColumns,
  mergePreview,
  SKIP,
  sniffFormat,
  suggestPath,
  type Mapping,
} from "./model";

const mapping = (over: Partial<Mapping> = {}): Mapping => ({
  target: "",
  cutoff: "",
  invert: false,
  cutoffTouched: false,
  invertTouched: false,
  newName: "Everyday Checking",
  newType: "checking",
  newPath: "assets.everyday_checking",
  ...over,
});

describe("sniffFormat", () => {
  it("reads a leading ! header as QIF", () => {
    expect(sniffFormat("\n  !Type:Bank\nD01/02/2026\n")).toBe("qif");
  });

  it("reads a comma in the first line as CSV", () => {
    expect(sniffFormat("Date,Payee,Amount\r\n01/02/2026,Coffee Shop,-12.34\r\n")).toBe("csv");
  });

  it("falls back to QIF", () => {
    expect(sniffFormat("")).toBe("qif");
    expect(sniffFormat("D01/02/2026\nT-12.34, with a comma later\n")).toBe("qif");
  });
});

describe("layoutColumns", () => {
  it("returns the header columns of an unrecognised CSV layout", () => {
    const err = { detail: { code: "csv_layout_unrecognized", columns: ["Day", "Value"] } };
    expect(layoutColumns(err)).toEqual(["Day", "Value"]);
  });

  it("ignores every other error", () => {
    expect(layoutColumns({ detail: "line 3: unparseable date" })).toBeNull();
    expect(layoutColumns({ detail: { code: "something_else", columns: ["Day"] } })).toBeNull();
    expect(layoutColumns({ detail: null })).toBeNull();
    expect(layoutColumns(undefined)).toBeNull();
  });
});

describe("suggestPath", () => {
  it("puts credit cards and loans under liabilities, the rest under assets", () => {
    expect(suggestPath("Travel Card", "credit_card")).toBe("liabilities.travel_card");
    expect(suggestPath("Car Loan", "loan")).toBe("liabilities.car_loan");
    expect(suggestPath("Rainy-Day Fund", "savings")).toBe("assets.rainy_day_fund");
  });

  it("falls back to `account` for a name with no usable characters", () => {
    expect(suggestPath("???", "cash")).toBe("assets.account");
  });
});

describe("mergePreview", () => {
  it("seeds a new file account from the preview", () => {
    const a = previewAccount({
      name: "Travel Card",
      type: "credit_card",
      mapped: { id: "a-9", path: "liabilities.travel_card", type: "credit_card" },
      default_cutoff: "2026-03-01",
      sign: { positive: "150.00", negative: "0.00", net: "150.00", suspect: true },
    });

    expect(mergePreview({}, [a])).toEqual({
      "Travel Card": {
        target: "a-9",
        cutoff: "2026-03-01",
        invert: true,
        cutoffTouched: false,
        invertTouched: false,
        newName: "Travel Card",
        newType: "credit_card",
        newPath: "liabilities.travel_card",
      },
    });
  });

  it("names the single account of a headerless file", () => {
    const merged = mergePreview({}, [previewAccount({ name: "__default__", type: "checking" })]);

    expect(merged.__default__).toMatchObject({
      target: "",
      newName: "Imported account",
      newPath: "assets.imported_account",
    });
  });

  it("adopts new cutoff/invert suggestions until the user edits them", () => {
    const suggestion = previewAccount({
      default_cutoff: "2026-03-01",
      sign: { positive: "90.00", negative: "0.00", net: "90.00", suspect: true },
    });

    const untouched = mergePreview({ "Everyday Checking": mapping({ target: "a-1" }) }, [suggestion]);
    expect(untouched["Everyday Checking"]).toMatchObject({
      target: "a-1",
      cutoff: "2026-03-01",
      invert: true,
    });

    const edited = mapping({
      target: "a-1",
      cutoff: "2026-01-15",
      cutoffTouched: true,
      invert: false,
      invertTouched: true,
    });
    const kept = mergePreview({ "Everyday Checking": edited }, [suggestion]);
    expect(kept["Everyday Checking"]).toEqual(edited);
  });

  it("keeps the current cutoff when the preview offers none", () => {
    const merged = mergePreview({ "Everyday Checking": mapping({ cutoff: "2026-02-01" }) }, [
      previewAccount({ default_cutoff: null }),
    ]);

    expect(merged["Everyday Checking"].cutoff).toBe("2026-02-01");
  });
});

describe("buildAccountMap", () => {
  const accounts = [
    previewAccount({ name: "Mapped" }),
    previewAccount({ name: "Skipped" }),
    previewAccount({ name: "Creating" }),
    previewAccount({ name: "Unmapped" }),
    previewAccount({ name: "No row" }),
  ];
  const mappings = {
    Mapped: mapping({ target: "a-1" }),
    Skipped: mapping({ target: SKIP }),
    Creating: mapping({ target: CREATE }),
    Unmapped: mapping({ target: "" }),
  };

  it("sends chosen targets, and a CSV skip as null", () => {
    expect(buildAccountMap("csv", mappings, accounts)).toEqual({ Mapped: "a-1", Skipped: null });
  });

  it("has no skip for QIF", () => {
    expect(buildAccountMap("qif", mappings, accounts)).toEqual({ Mapped: "a-1" });
  });
});

describe("buildOptions", () => {
  it("sends each mapped row's cutoff (blank as null) and invert", () => {
    const accounts = [previewAccount({ name: "A" }), previewAccount({ name: "B" }), previewAccount({ name: "C" })];
    const mappings = {
      A: mapping({ cutoff: "2026-03-01" }),
      B: mapping({ cutoff: "", invert: true }),
    };

    expect(buildOptions(mappings, accounts)).toEqual({
      A: { cutoff: "2026-03-01", invert: false },
      B: { cutoff: null, invert: true },
    });
  });
});

describe("allResolved", () => {
  const accounts = [previewAccount({ name: "A" }), previewAccount({ name: "B" })];

  it("is true once every account has a target or a skip", () => {
    expect(allResolved(accounts, { A: mapping({ target: "a-1" }), B: mapping({ target: SKIP }) })).toBe(true);
  });

  it("needs no target for an investment-only account (#274)", () => {
    const brokerage = previewAccount({
      name: "B",
      counts: counts({ in_file: 7, skipped_investment: 7 }),
    });
    const both = [previewAccount({ name: "A" }), brokerage];
    expect(allResolved(both, { A: mapping({ target: "a-1" }), B: mapping({ target: "" }) })).toBe(true);
    expect(allResolved(both, { A: mapping({ target: "a-1" }), B: mapping({ target: SKIP }) })).toBe(true);
    expect(allResolved(both, { A: mapping({ target: "a-1" }), B: mapping({ target: CREATE }) })).toBe(false);
    // A QIF skip simply leaves it out of account_map.
    expect(
      buildAccountMap("qif", { A: mapping({ target: "a-1" }), B: mapping({ target: SKIP }) }, both),
    ).toEqual({ A: "a-1" });
  });

  it("is false while any account is unmapped or mid-create, or there are none", () => {
    expect(allResolved(accounts, { A: mapping({ target: "a-1" }), B: mapping({ target: "" }) })).toBe(false);
    expect(allResolved(accounts, { A: mapping({ target: "a-1" }), B: mapping({ target: CREATE }) })).toBe(false);
    expect(allResolved(accounts, { A: mapping({ target: "a-1" }) })).toBe(false);
    expect(allResolved([], {})).toBe(false);
  });
});
