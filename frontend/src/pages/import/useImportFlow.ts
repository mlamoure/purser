import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, errorDetail } from "../../lib/api";
import {
  buildAccountMap,
  buildOptions,
  layoutColumns,
  mergePreview,
  sniffFormat,
  type AccountOut,
  type Format,
  type ImportResult,
  type Mapping,
  type PreviewResponse,
} from "./model";

/** State and API calls of the mapping-first import flow: source text and
 * options → preview → mapping table (every change re-previews, so counts and
 * suggestions stay live) → commit. Failures land in `error`; nothing throws. */
export function useImportFlow() {
  const qc = useQueryClient();
  const [content, setContent] = useState("");
  const [format, setFormat] = useState<Format>("qif");
  const [dateFormat, setDateFormat] = useState("mdy");
  const [preview, setPreview] = useState<PreviewResponse | null>(null);
  const [mappings, setMappings] = useState<Record<string, Mapping>>({});
  const [columnMap, setColumnMap] = useState<Record<string, string> | null>(null);
  const [needsMapping, setNeedsMapping] = useState<string[] | null>(null);
  const [result, setResult] = useState<ImportResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const accounts = useQuery({
    queryKey: ["accounts"],
    queryFn: async () =>
      (await api.GET("/accounts", { params: { query: {} } })).data ?? ([] as AccountOut[]),
  });

  /** New source text (typed, pasted, or read from a file) starts the flow
   * over; the format is sniffed unless the file's extension decided it. */
  const setSource = (value: string, explicitFormat?: Format) => {
    setContent(value);
    setPreview(null);
    setResult(null);
    setMappings({});
    setColumnMap(null);
    setNeedsMapping(null);
    setError(null);
    setFormat(explicitFormat ?? sniffFormat(value));
  };

  const chooseFormat = (next: Format) => {
    setFormat(next);
    setPreview(null);
    setResult(null);
    setMappings({});
    setColumnMap(null);
    setNeedsMapping(null);
  };

  const chooseDateFormat = (next: string) => {
    setDateFormat(next);
    setPreview(null);
    setResult(null);
    setMappings({});
  };

  const runPreview = async (
    nextMappings: Record<string, Mapping>,
    columnMapOverride?: Record<string, string> | null,
  ) => {
    setBusy(true);
    setError(null);
    const acctList = preview?.accounts ?? [];
    const account_map = buildAccountMap(format, nextMappings, acctList);
    const account_options = buildOptions(nextMappings, acctList);
    const effColumnMap = columnMapOverride === undefined ? columnMap : columnMapOverride;
    try {
      let data: unknown;
      let err: unknown;
      if (format === "csv") {
        const body = {
          content,
          date_format: dateFormat,
          account_map,
          account_options,
          ...(effColumnMap ? { column_map: effColumnMap } : {}),
        };
        ({ data, error: err } = await api.POST("/import/csv/preview", { body }));
      } else {
        const body = {
          content,
          date_format: dateFormat,
          account_map: account_map as Record<string, string>,
          account_options,
        };
        ({ data, error: err } = await api.POST("/import/qif/preview", { body }));
      }
      if (err) {
        const cols = layoutColumns(err);
        if (cols) {
          setNeedsMapping(cols);
          setPreview(null);
          return;
        }
        throw new Error(errorDetail(err));
      }
      setNeedsMapping(null);
      const resp = data as PreviewResponse;
      setPreview(resp);
      setMappings((prev) => mergePreview(prev, resp.accounts));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  /** The Preview button: preview with the mappings as they stand. */
  const requestPreview = () => runPreview(mappings);

  /** The CSV column-mapping sub-form's answer; previews again with it. */
  const applyColumnMap = (map: Record<string, string>) => {
    setColumnMap(map);
    setNeedsMapping(null);
    void runPreview(mappings, map);
  };

  /** Change one file account's mapping, then re-preview so counts and
   * suggestions reflect it. */
  const patchMapping = (name: string, change: Partial<Mapping>) => {
    const next = { ...mappings, [name]: { ...mappings[name], ...change } };
    setMappings(next);
    void runPreview(next);
  };

  /** Create the Purser account described by a "Create new…" row and select it. */
  const createAccount = async (name: string) => {
    const m = mappings[name];
    setBusy(true);
    setError(null);
    try {
      const { data, error: err } = await api.POST("/accounts", {
        body: { name: m.newName, type: m.newType, path: m.newPath, currency: "USD" },
      });
      if (err) throw new Error(errorDetail(err));
      await qc.invalidateQueries({ queryKey: ["accounts"] });
      await accounts.refetch();
      patchMapping(name, { target: data!.id });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setBusy(false);
    }
  };

  /** Commit the import with the current mappings. */
  const runImport = async () => {
    setBusy(true);
    setError(null);
    const acctList = preview?.accounts ?? [];
    const account_map = buildAccountMap(format, mappings, acctList);
    const account_options = buildOptions(mappings, acctList);
    try {
      let data: unknown;
      let err: unknown;
      if (format === "csv") {
        const body = {
          content,
          date_format: dateFormat,
          account_map,
          category_map: {},
          account_options,
          ...(columnMap ? { column_map: columnMap } : {}),
        };
        ({ data, error: err } = await api.POST("/import/csv", { body }));
      } else {
        const body = {
          content,
          date_format: dateFormat,
          account_map: account_map as Record<string, string>,
          category_map: {},
          account_options,
        };
        ({ data, error: err } = await api.POST("/import/qif", { body }));
      }
      if (err) throw new Error(errorDetail(err));
      setResult(data as ImportResult);
      await qc.invalidateQueries({ queryKey: ["accounts"] });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return {
    content,
    format,
    dateFormat,
    preview,
    mappings,
    columnMap,
    needsMapping,
    result,
    busy,
    error,
    accounts: accounts.data ?? [],
    setSource,
    chooseFormat,
    chooseDateFormat,
    requestPreview,
    applyColumnMap,
    patchMapping,
    createAccount,
    runImport,
  };
}
