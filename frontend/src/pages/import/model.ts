/* The import flow's model: types, constants and the pure helpers that turn
 * the mapping table's state into preview/import request fields. No React and
 * no I/O, so all of it is unit-tested directly (model.test.ts). */

import type { components } from "../../api/schema";

export type AccountOut = components["schemas"]["AccountOut"];
// Preview/import responses are typed by the shared backend models
// (import_models.py); the DESIGN §7.2 counts contract lives there.
export type Counts = components["schemas"]["ImportCounts"];
export type PreviewAccount = components["schemas"]["ImportPreviewAccount"];
export type PreviewResponse = components["schemas"]["ImportPreviewResponse"];
export type ImportResult = components["schemas"]["ImportResult"];

export type Format = "qif" | "csv";

/** What the user chose for one file account in the mapping table. */
export type Mapping = {
  target: string; // account id, "" (unmapped), "__create__", or "__skip__"
  cutoff: string; // "" = clear (import everything)
  invert: boolean;
  cutoffTouched: boolean;
  invertTouched: boolean;
  newName: string;
  newType: string;
  newPath: string;
};

export const CREATE = "__create__";
// Map a file account to null (don't import): CSV, and QIF investment-only
// accounts, which a QIF request then simply leaves out of account_map.
export const SKIP = "__skip__";
export const ASSET_TYPES = ["checking", "savings", "cash", "brokerage", "property"];
export const LIABILITY_TYPES = ["credit_card", "loan"];
export const ALL_TYPES = [...ASSET_TYPES, ...LIABILITY_TYPES];

// Logical CSV columns the mapping sub-form collects when auto-detect fails.
// `cleared` (blank = uncleared) and `scheduled` (Quicken reminders) per #249.
export const REQUIRED_COLUMNS = ["date", "amount", "payee"] as const;
export const OPTIONAL_COLUMNS = ["memo", "category", "account", "cleared", "scheduled"] as const;
export type ColumnField = (typeof REQUIRED_COLUMNS)[number] | (typeof OPTIONAL_COLUMNS)[number];

/** Class list for the flow's native selects (matches the Input primitive). */
export const selectClass =
  "flex h-9 rounded-md border border-border bg-card px-3 py-1 text-sm shadow-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";

const slug = (s: string) => s.toLowerCase().replace(/[^a-z0-9_]+/g, "_").replace(/^_+|_+$/g, "");

/** Suggested ledger path for a new account: under `liabilities.` for credit
 * cards and loans, `assets.` otherwise. */
export const suggestPath = (name: string, type: string) =>
  `${LIABILITY_TYPES.includes(type) ? "liabilities" : "assets"}.${slug(name) || "account"}`;

/** Infer the import format from pasted text: QIF starts with a `!` header line;
 * a comma in the first line signals CSV. Extension wins when a file is chosen. */
export function sniffFormat(text: string): Format {
  const trimmed = text.trimStart();
  if (trimmed.startsWith("!")) return "qif";
  const firstLine = trimmed.split(/\r?\n/, 1)[0] ?? "";
  return firstLine.includes(",") ? "csv" : "qif";
}

/** A CSV layout-detection failure is signalled by the backend as
 * `detail = {code: "csv_layout_unrecognized", columns: [...]}`; pull the header
 * columns out so the column-mapping sub-form can seed its selectors. */
export function layoutColumns(err: unknown): string[] | null {
  if (!err || typeof err !== "object" || !("detail" in err)) return null;
  const detail = (err as { detail: unknown }).detail as
    | { code?: unknown; columns?: unknown }
    | null
    | undefined;
  if (
    detail &&
    typeof detail === "object" &&
    detail.code === "csv_layout_unrecognized" &&
    Array.isArray(detail.columns)
  ) {
    return detail.columns as string[];
  }
  return null;
}

/** Fold a preview's accounts into the mapping table: a new file account gets
 * defaults from the preview; a known one keeps the user's choices but adopts
 * the server's cutoff/invert suggestions until the user edits that field. */
export function mergePreview(prev: Record<string, Mapping>, accounts: PreviewAccount[]) {
  const next: Record<string, Mapping> = { ...prev };
  for (const a of accounts) {
    const cur = prev[a.name];
    if (!cur) {
      next[a.name] = {
        target: a.mapped?.id ?? "",
        cutoff: a.default_cutoff ?? "",
        invert: a.sign.suspect,
        cutoffTouched: false,
        invertTouched: false,
        newName: a.name === "__default__" ? "Imported account" : a.name,
        newType: a.type,
        newPath: suggestPath(a.name === "__default__" ? "Imported account" : a.name, a.type),
      };
    } else {
      next[a.name] = {
        ...cur,
        // Mapping an account makes a default_cutoff available — adopt it until
        // the user edits the field; likewise re-suggest invert until touched.
        cutoff: cur.cutoffTouched ? cur.cutoff : (a.default_cutoff ?? cur.cutoff),
        invert: cur.invertTouched ? cur.invert : a.sign.suspect,
      };
    }
  }
  return next;
}

/** The request's `account_map`: chosen targets by file account; a CSV skip
 * maps to null (don't import); unmapped and "create new" are left out. */
export function buildAccountMap(
  format: Format,
  mappings: Record<string, Mapping>,
  accounts: PreviewAccount[],
): Record<string, string | null> {
  const account_map: Record<string, string | null> = {};
  for (const a of accounts) {
    const m = mappings[a.name];
    if (!m) continue;
    if (m.target === SKIP) {
      if (format === "csv") account_map[a.name] = null; // don't import
    } else if (m.target && m.target !== CREATE) {
      account_map[a.name] = m.target;
    }
  }
  return account_map;
}

/** The request's `account_options`: per-file-account cutoff ("" → none) and
 * sign inversion. */
export function buildOptions(mappings: Record<string, Mapping>, accounts: PreviewAccount[]) {
  const account_options: Record<string, { cutoff: string | null; invert: boolean }> = {};
  for (const a of accounts) {
    const m = mappings[a.name];
    if (!m) continue;
    account_options[a.name] = { cutoff: m.cutoff === "" ? null : m.cutoff, invert: m.invert };
  }
  return account_options;
}

/** A file account whose every row is a QIF investment record (issue #274):
 * none of it imports, so it needs no target — the server leaves an unmapped
 * investment account out, and bank transfers into it import as one-leg rows. */
export const investmentOnly = (a: PreviewAccount): boolean =>
  a.counts.in_file > 0 && a.counts.skipped_investment === a.counts.in_file;

/** Import is allowed once every file account is resolved: mapped to a real
 * target, marked skip (CSV, or an investment-only account), or investment-only
 * and left unmapped — not unmapped otherwise, not mid-"create new". */
export function allResolved(accounts: PreviewAccount[], mappings: Record<string, Mapping>): boolean {
  return (
    accounts.length > 0 &&
    accounts.every((a) => {
      const t = mappings[a.name]?.target;
      return (!!t || investmentOnly(a)) && t !== CREATE;
    })
  );
}
