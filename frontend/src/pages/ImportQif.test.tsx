import { screen, waitFor, within } from "@testing-library/react";
import { Route, Routes } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { apiMock, fail, mockApi, ok, type CallInit } from "../test/api";
import { accountOut, counts, importResult, previewAccount, previewResponse } from "../test/fixtures";
import { renderWithProviders } from "../test/render";
import ImportQif from "./ImportQif";

// Synthetic exports. A QIF file opens with a `!Type` header; a CSV with a
// comma-separated header row — that is all the format sniffing looks at.
const QIF = "!Type:Bank\nD01/02/2026\nT-12.34\nPCoffee Shop\n^\n";
const CSV = "Day,Description,Value,Clr,Sched\n01/02/2026,Coffee Shop,-12.34,c,\n";

const FILE_ACCOUNT = "Everyday Checking";
const ACCOUNTS = [
  accountOut({ id: "a-1", name: "Checking", path: "assets.checking" }),
  accountOut({ id: "a-2", name: "Feed Checking", path: "assets.feed_checking", connection_id: "c-1" }),
];

const bodyOf = (init: CallInit) =>
  init.body as {
    account_map: Record<string, string | null>;
    account_options: Record<string, { cutoff: string | null; invert: boolean }>;
  };

/** A preview that answers like the server: once the file account is mapped,
 * its rows count towards the import and a default cutoff is offered. */
function previewFor(init: CallInit) {
  const target = bodyOf(init).account_map[FILE_ACCOUNT];
  if (target === undefined) {
    return ok(previewResponse([previewAccount({ name: FILE_ACCOUNT })]));
  }
  if (target === null) {
    return ok(
      previewResponse([
        previewAccount({ name: FILE_ACCOUNT, counts: counts({ in_file: 3, skipped_unmapped: 3 }) }),
      ]),
    );
  }
  return ok(
    previewResponse([
      previewAccount({
        name: FILE_ACCOUNT,
        mapped: { id: target, path: "assets.checking", type: "checking" },
        default_cutoff: "2026-03-01",
        counts: counts({ in_file: 3, would_import: 1, uncleared: 1, skipped_scheduled: 1 }),
      }),
    ]),
  );
}

const RESULT = importResult({
  imported: 1,
  uncleared: 1,
  skipped_scheduled: 1,
  per_account: {
    [FILE_ACCOUNT]: counts({ in_file: 3, would_import: 1, uncleared: 1, skipped_scheduled: 1 }),
  },
});

function renderPage() {
  return renderWithProviders(
    <Routes>
      <Route path="/import" element={<ImportQif />} />
      <Route path="/" element={<p>dashboard</p>} />
    </Routes>,
    { route: "/import" },
  );
}

async function paste(user: ReturnType<typeof renderPage>["user"], text: string) {
  await user.click(screen.getByPlaceholderText(/!Type:Bank/));
  await user.paste(text);
}

/** The target picker is the first select in a mapping row. */
const targetSelect = (row: HTMLElement) => within(row).getAllByRole("combobox")[0];
const mappingRow = () => screen.getByText(FILE_ACCOUNT).closest("tr")!;

describe("ImportQif page (paste → preview → map → import)", () => {
  it("imports a QIF file into an existing account and reports the counts", async () => {
    mockApi({
      "GET /accounts": ok(ACCOUNTS),
      "POST /import/qif/preview": previewFor,
      "POST /import/qif": ok(RESULT),
    });
    const { user } = renderPage();

    await paste(user, QIF);
    expect(screen.getByDisplayValue("QIF")).toBeInTheDocument(); // sniffed from the header
    await user.click(screen.getByRole("button", { name: "Preview" }));

    expect(await screen.findByText(FILE_ACCOUNT)).toBeInTheDocument();
    expect(apiMock.POST).toHaveBeenCalledWith("/import/qif/preview", {
      body: { content: QIF, date_format: "mdy", account_map: {}, account_options: {} },
    });
    expect(screen.getByRole("button", { name: "Import" })).toBeDisabled();
    expect(screen.getByText("Map or skip every account to enable import.")).toBeInTheDocument();

    await user.selectOptions(targetSelect(mappingRow()), "a-1");

    // The re-preview adopts the offered cutoff; the counts and #249 notes update.
    await waitFor(() => expect(within(mappingRow()).getByDisplayValue("2026-03-01")).toBeInTheDocument());
    expect(within(mappingRow()).getByText("uncleared 1")).toBeInTheDocument();
    expect(within(mappingRow()).getByText("reminder 1")).toBeInTheDocument();
    expect(screen.getByText(/1 Quicken reminder row \(Scheduled\) is not history/)).toBeInTheDocument();
    expect(screen.getByText(/1 uncleared row will land as/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Import" }));

    expect(await screen.findByText("Uncleared (expected)")).toBeInTheDocument();
    expect(apiMock.POST).toHaveBeenCalledWith("/import/qif", {
      body: {
        content: QIF,
        date_format: "mdy",
        account_map: { [FILE_ACCOUNT]: "a-1" },
        category_map: {},
        account_options: { [FILE_ACCOUNT]: { cutoff: "2026-03-01", invert: false } },
      },
    });
    expect(screen.getByText("Skipped (reminders)")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Done" }));
    expect(await screen.findByText("dashboard")).toBeInTheDocument();
  });

  it("asks for the columns of an unrecognised CSV layout, and can skip an account", async () => {
    mockApi({
      "GET /accounts": ok(ACCOUNTS),
      "POST /import/csv/preview": (init) =>
        (init.body as { column_map?: unknown }).column_map
          ? previewFor(init)
          : fail(422, {
              code: "csv_layout_unrecognized",
              columns: ["Day", "Description", "Value", "Clr", "Sched"],
            }),
      "POST /import/csv": ok(importResult()),
    });
    const { user } = renderPage();

    await paste(user, CSV);
    expect(screen.getByDisplayValue("CSV")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Preview" }));

    expect(await screen.findByText("Unrecognized CSV layout.")).toBeInTheDocument();
    const apply = screen.getByRole("button", { name: "Use these columns" });
    expect(apply).toBeDisabled();
    const [date, amount, payee, , , , cleared, scheduled] = screen.getAllByRole("combobox").slice(2);
    await user.selectOptions(date, "Day");
    await user.selectOptions(amount, "Value");
    await user.selectOptions(payee, "Description");
    await user.selectOptions(cleared, "Clr");
    await user.selectOptions(scheduled, "Sched");
    await user.click(apply);

    expect(await screen.findByText(FILE_ACCOUNT)).toBeInTheDocument();
    const columnMap = { date: "Day", amount: "Value", payee: "Description", cleared: "Clr", scheduled: "Sched" };
    expect(apiMock.POST).toHaveBeenLastCalledWith("/import/csv/preview", {
      body: {
        content: CSV,
        date_format: "mdy",
        account_map: {},
        account_options: {},
        column_map: columnMap,
      },
    });
    expect(screen.queryByText("Unrecognized CSV layout.")).not.toBeInTheDocument();

    await user.selectOptions(targetSelect(mappingRow()), "__skip__"); // "Don't import" — CSV only
    await waitFor(() => expect(screen.getByRole("button", { name: "Import" })).toBeEnabled());
    expect(within(mappingRow()).getByText("not imported")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Import" }));

    expect(await screen.findByText("Uncleared (expected)")).toBeInTheDocument();
    expect(apiMock.POST).toHaveBeenCalledWith("/import/csv", {
      body: {
        content: CSV,
        date_format: "mdy",
        account_map: { [FILE_ACCOUNT]: null },
        category_map: {},
        account_options: { [FILE_ACCOUNT]: { cutoff: null, invert: false } },
        column_map: columnMap,
      },
    });
  });

  it("creates a new account for a file account and selects it", async () => {
    const accounts = [...ACCOUNTS];
    mockApi({
      "GET /accounts": () => ok([...accounts]),
      "POST /import/qif/preview": previewFor,
      "POST /accounts": (init) => {
        const body = init.body as { name: string; path: string; type: string };
        const created = accountOut({ id: "a-3", ...body });
        accounts.push(created);
        return ok(created, 201);
      },
    });
    const { user } = renderPage();
    await paste(user, QIF);
    await user.click(screen.getByRole("button", { name: "Preview" }));
    await screen.findByText(FILE_ACCOUNT);

    await user.selectOptions(targetSelect(mappingRow()), "__create__");
    const row = mappingRow();
    expect(within(row).getByDisplayValue(FILE_ACCOUNT)).toBeInTheDocument(); // suggested name
    expect(within(row).getByDisplayValue("assets.everyday_checking")).toBeInTheDocument();
    await user.selectOptions(within(row).getAllByRole("combobox")[1], "credit_card");
    expect(within(row).getByDisplayValue("liabilities.everyday_checking")).toBeInTheDocument();

    await user.click(within(row).getByRole("button", { name: "Create & select" }));

    await waitFor(() => expect(targetSelect(mappingRow())).toHaveValue("a-3"));
    expect(apiMock.POST).toHaveBeenCalledWith("/accounts", {
      body: {
        name: FILE_ACCOUNT,
        type: "credit_card",
        path: "liabilities.everyday_checking",
        currency: "USD",
      },
    });
    expect(apiMock.POST).toHaveBeenLastCalledWith("/import/qif/preview", {
      body: expect.objectContaining({ account_map: { [FILE_ACCOUNT]: "a-3" } }),
    });
    expect(screen.getByRole("button", { name: "Import" })).toBeEnabled();
  });

  it("shows the server's reason when the preview fails", async () => {
    mockApi({
      "GET /accounts": ok(ACCOUNTS),
      "POST /import/qif/preview": fail(422, "line 3: unparseable date"),
    });
    const { user } = renderPage();

    await paste(user, QIF);
    await user.click(screen.getByRole("button", { name: "Preview" }));

    expect(await screen.findByText("line 3: unparseable date")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Import" })).not.toBeInTheDocument();
  });
});
