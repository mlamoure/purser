/* A rule as a sentence (issue #541).
 *
 * GET /rules renders each rule's predicate and action as one stored-terms
 * line each (purser/api/rules.py _render_predicate / _render_action):
 *
 *   account in assets.checking AND payee contains "acme" AND debit
 *   -> expenses.fitness
 *
 * The Rules page reads them back as words — "Payee contains "acme" on
 * Checking, money out → category Fitness" — with paths turned into names. A
 * clause this parser does not recognize is shown as the server wrote it, so
 * a new clause shape degrades to the raw text, never to nothing. The raw
 * form stays on the page for whoever needs it. */

import { formatDate } from "../../lib/dates";
import { formatMoney } from "../../lib/money";
import { capitalize } from "../../lib/labels";
import type { PathName } from "../../lib/names";

const FIELD: Record<string, string> = {
  payee: "payee",
  description: "description",
  payee_or_description: "payee or description",
};
const MODE: Record<string, string> = {
  equals: "is",
  prefix: "starts with",
  contains: "contains",
};

/** Split on " AND " outside double quotes: a match text may contain it. */
export function splitClauses(predicate: string): string[] {
  const out: string[] = [];
  let quoted = false;
  let start = 0;
  for (let i = 0; i < predicate.length; i++) {
    if (predicate[i] === '"') quoted = !quoted;
    else if (!quoted && predicate.startsWith(" AND ", i)) {
      out.push(predicate.slice(start, i));
      start = i + 5;
      i += 4;
    }
  }
  out.push(predicate.slice(start));
  return out.map((c) => c.trim()).filter(Boolean);
}

const joinOr = (names: string[]) =>
  names.length <= 1 ? (names[0] ?? "") : `${names.slice(0, -1).join(", ")} or ${names[names.length - 1]}`;

function clauseWords(clause: string, name: PathName): string {
  let m: RegExpExecArray | null;
  if (clause === "every row") return "every transaction";
  if ((m = /^account in (.+)$/.exec(clause)))
    return `on ${joinOr(m[1].split(", ").map((p) => name(p.trim())))}`;
  if ((m = /^(payee_or_description|payee|description) (equals|prefix|contains) "(.*)"$/.exec(clause)))
    return `${FIELD[m[1]]} ${MODE[m[2]]} "${m[3]}"`;
  if ((m = /^amount (=|>=|<=) (\S+)$/.exec(clause))) {
    const verb = m[1] === "=" ? "is" : m[1] === ">=" ? "is at least" : "is at most";
    return `amount ${verb} ${formatMoney(m[2])}`;
  }
  if (clause === "debit") return "money out";
  if (clause === "credit") return "money in";
  if ((m = /^on or after (\d{4}-\d{2}-\d{2})$/.exec(clause))) return `from ${formatDate(m[1])}`;
  if ((m = /^a leg's payee is "(.*)" or an alias$/.exec(clause)))
    return `one side's payee is "${m[1]}" (or an alias)`;
  if ((m = /^legs <= (\d+) days apart$/.exec(clause))) {
    const days = Number(m[1]);
    return days === 0 ? "both sides on the same day" : `sides within ${days} day${days === 1 ? "" : "s"}`;
  }
  return clause;
}

/** The predicate as words: "Payee contains "acme" on Checking, money out". */
export function predicateWords(predicate: string, name: PathName): string {
  const parts = splitClauses(predicate).map((c) => clauseWords(c, name));
  let text = "";
  for (const part of parts) {
    if (!text) text = part;
    // "on Checking" reads as part of the clause before it.
    else text += part.startsWith("on ") ? ` ${part}` : `, ${part}`;
  }
  return text ? capitalize(text) : predicate;
}

/** The action as words: "category Fitness", "transfer to Savings". */
export function actionWords(action: string, name: PathName): string {
  let m: RegExpExecArray | null;
  if ((m = /^-> payee "(.*)"$/.exec(action))) return `rename the payee to "${m[1]}"`;
  if ((m = /^-> \[(.+)\]$/.exec(action))) return `transfer to ${name(m[1])}`;
  if ((m = /^-> (.+)$/.exec(action))) return `category ${name(m[1])}`;
  if ((m = /^link (.+) <-> (.+)$/.exec(action)))
    return `link transfers between ${name(m[1])} and ${name(m[2])}`;
  return action;
}

/** The whole rule: "Payee contains "acme" on Checking → category Fitness". */
export function ruleSentence(rule: { predicate: string; action: string }, name: PathName): string {
  return `${predicateWords(rule.predicate, name)} → ${actionWords(rule.action, name)}`;
}
