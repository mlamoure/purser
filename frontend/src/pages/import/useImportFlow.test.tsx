import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { apiMock, fail, mockApi, ok, type CallInit } from "../../test/api";
import { accountOut, importResult, previewAccount, previewResponse } from "../../test/fixtures";
import { useImportFlow } from "./useImportFlow";

const QIF = "!Type:Bank\nD01/02/2026\nT-12.34\nPCoffee Shop\n^\n";
const CSV = "Day,Description,Value\n01/02/2026,Coffee Shop,-12.34\n";
const COLUMNS = { date: "Day", amount: "Value", payee: "Description" };

function renderFlow() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  return renderHook(() => useImportFlow(), { wrapper });
}

/** CSV preview that needs a column map before it can read the file. */
const csvPreview = (init: CallInit) =>
  (init.body as { column_map?: unknown }).column_map
    ? ok(previewResponse([previewAccount()]))
    : fail(422, { code: "csv_layout_unrecognized", columns: ["Day", "Description", "Value"] });

describe("useImportFlow", () => {
  it("restarts on new source text and sniffs its format", async () => {
    mockApi({ "GET /accounts": ok([]), "POST /import/qif/preview": fail(422, "unparseable file") });
    const { result } = renderFlow();

    act(() => result.current.setSource(QIF));
    await act(() => result.current.requestPreview());
    expect(result.current.error).toBe("unparseable file");

    act(() => result.current.setSource(CSV));

    expect(result.current.format).toBe("csv");
    expect(result.current.content).toBe(CSV);
    expect(result.current.error).toBeNull();
    expect(result.current.preview).toBeNull();
  });

  it("collects a CSV column map, keeps it across a date-order change, drops it with the format", async () => {
    mockApi({ "GET /accounts": ok([]), "POST /import/csv/preview": csvPreview });
    const { result } = renderFlow();
    act(() => result.current.setSource(CSV));

    await act(() => result.current.requestPreview());
    expect(result.current.needsMapping).toEqual(["Day", "Description", "Value"]);
    expect(result.current.preview).toBeNull();

    act(() => result.current.applyColumnMap(COLUMNS));
    await waitFor(() => expect(result.current.preview).not.toBeNull());
    expect(result.current.needsMapping).toBeNull();

    act(() => result.current.chooseDateFormat("dmy"));
    expect(result.current.preview).toBeNull();
    expect(result.current.columnMap).toEqual(COLUMNS);
    await act(() => result.current.requestPreview());
    expect(apiMock.POST).toHaveBeenLastCalledWith("/import/csv/preview", {
      body: { content: CSV, date_format: "dmy", account_map: {}, account_options: {}, column_map: COLUMNS },
    });

    act(() => result.current.chooseFormat("qif"));
    expect(result.current.columnMap).toBeNull();
    expect(result.current.preview).toBeNull();
    expect(result.current.mappings).toEqual({});
  });

  it("re-previews after every mapping change", async () => {
    mockApi({
      "GET /accounts": ok([accountOut()]),
      "POST /import/qif/preview": ok(previewResponse([previewAccount()])),
    });
    const { result } = renderFlow();
    act(() => result.current.setSource(QIF));
    await act(() => result.current.requestPreview());

    act(() => result.current.patchMapping("Everyday Checking", { invert: true, invertTouched: true }));

    await waitFor(() => expect(apiMock.POST).toHaveBeenCalledTimes(2));
    expect(apiMock.POST).toHaveBeenLastCalledWith("/import/qif/preview", {
      body: {
        content: QIF,
        date_format: "mdy",
        account_map: {},
        account_options: { "Everyday Checking": { cutoff: null, invert: true } },
      },
    });
    await waitFor(() => expect(result.current.busy).toBe(false));
    expect(result.current.mappings["Everyday Checking"].invert).toBe(true);
  });

  it("reports a refused new account without re-previewing", async () => {
    mockApi({
      "GET /accounts": ok([]),
      "POST /import/qif/preview": ok(previewResponse([previewAccount()])),
      "POST /accounts": fail(409, "path already exists"),
    });
    const { result } = renderFlow();
    act(() => result.current.setSource(QIF));
    await act(() => result.current.requestPreview());

    await act(() => result.current.createAccount("Everyday Checking"));

    expect(result.current.error).toBe("path already exists");
    expect(result.current.busy).toBe(false);
    expect(apiMock.POST.mock.calls.filter(([path]) => path === "/import/qif/preview")).toHaveLength(1);
  });

  it("keeps the mapping table when the import fails, and returns the result when it succeeds", async () => {
    let refuse = true;
    mockApi({
      "GET /accounts": ok([accountOut()]),
      "POST /import/qif/preview": ok(previewResponse([previewAccount()])),
      "POST /import/qif": () => (refuse ? fail(500, "import failed") : ok(importResult({ imported: 2 }))),
    });
    const { result } = renderFlow();
    act(() => result.current.setSource(QIF));
    await act(() => result.current.requestPreview());

    await act(() => result.current.runImport());
    expect(result.current.error).toBe("import failed");
    expect(result.current.result).toBeNull();
    expect(result.current.preview).not.toBeNull();

    refuse = false;
    await act(() => result.current.runImport());
    expect(result.current.result).toEqual(importResult({ imported: 2 }));
    expect(result.current.error).toBeNull();
  });
});
