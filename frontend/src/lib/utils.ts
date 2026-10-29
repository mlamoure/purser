import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export function fmtAmount(value: string | null | undefined): string {
  if (value == null) return "—";
  const n = Number(value);
  return n.toLocaleString("en-US", { style: "currency", currency: "USD" });
}

/** The body of a schedule-instance override: only the fields the user
 * changed (#350). Amounts stay strings — they are compared as typed, never
 * parsed. Sending the prefilled amount with every date edit made a date-only
 * override of a transfer instance an amount edit too. */
export function overrideBody(
  original: { amount?: string | null; date: string },
  draft: { amount: string; date: string },
): { amount?: string; date?: string } {
  const body: { amount?: string; date?: string } = {};
  const amount = draft.amount.trim();
  if (amount && amount !== (original.amount ?? "")) body.amount = amount;
  if (draft.date && draft.date !== original.date) body.date = draft.date;
  return body;
}
