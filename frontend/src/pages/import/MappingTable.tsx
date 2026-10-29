import { Button, ErrorText, Table, Th } from "../../components/ui";
import { AccountRow } from "./AccountRow";
import {
  allResolved,
  CREATE,
  type AccountOut,
  type Mapping,
  type PreviewAccount,
  type PreviewResponse,
} from "./model";

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

/** Quicken reminders and investment records are skipped, uncleared rows land
 * `expected`, and rows an earlier import left `expected` post once the file
 * marks them cleared (DESIGN decisions 60 and 84) — say so before the commit,
 * not after. */
function StatusNote({ accounts }: { accounts: PreviewAccount[] }) {
  const reminders = accounts.reduce((n, a) => n + a.counts.skipped_scheduled, 0);
  const investment = accounts.reduce((n, a) => n + a.counts.skipped_investment, 0);
  const uncleared = accounts.reduce((n, a) => n + a.counts.uncleared, 0);
  const promoted = accounts.reduce((n, a) => n + a.counts.promoted, 0);
  if (reminders === 0 && investment === 0 && uncleared === 0 && promoted === 0) return null;
  return (
    <div className="space-y-1 rounded-md border border-border bg-muted/30 p-3 text-sm">
      {reminders > 0 && (
        <div>
          {plural(reminders, "Quicken reminder row")} (Scheduled){" "}
          {reminders === 1 ? "is" : "are"} not history and will be skipped.
        </div>
      )}
      {investment > 0 && (
        <div>
          {plural(investment, "investment record")} (buys, sells, dividends){" "}
          {investment === 1 ? "is" : "are"} not bank activity and will be skipped. Leave an
          investment account unmapped to keep it out entirely; bank transfers into it then import
          as one-leg rows.
        </div>
      )}
      {uncleared > 0 && (
        <div>
          {plural(uncleared, "uncleared row")} will land as{" "}
          <span className="font-medium">expected</span>: out of the balance until the matcher pairs
          each with the bank's posted row, and in the forecast meanwhile.
        </div>
      )}
      {promoted > 0 && (
        <div>
          {plural(promoted, "row")} an earlier import left expected{" "}
          {promoted === 1 ? "is" : "are"} now marked cleared and will be{" "}
          <span className="font-medium">posted</span>.
        </div>
      )}
    </div>
  );
}

/** The preview: one mapping row per file account with live counts, and the
 * Import button once every account is resolved. */
export function MappingTable({
  preview,
  mappings,
  accounts,
  allowSkip,
  busy,
  error,
  onPatch,
  onCreate,
  onImport,
}: {
  preview: PreviewResponse;
  mappings: Record<string, Mapping>;
  accounts: AccountOut[];
  allowSkip: boolean;
  busy: boolean;
  error: string | null;
  onPatch: (name: string, change: Partial<Mapping>) => void;
  onCreate: (name: string) => void;
  onImport: () => void;
}) {
  const accountById = new Map(accounts.map((a) => [a.id, a]));
  const allMapped = allResolved(preview.accounts, mappings);
  return (
    <div className="space-y-3">
      <p className="text-sm text-muted-foreground">
        {preview.total_transactions} transaction{preview.total_transactions === 1 ? "" : "s"}{" "}
        across {preview.accounts.length} account{preview.accounts.length === 1 ? "" : "s"}. Map
        each to a Purser account; rows on or after a cutoff are left to the feed.
      </p>
      <StatusNote accounts={preview.accounts} />
      <Table>
        <thead>
          <tr>
            <Th>File account</Th>
            <Th>Purser account</Th>
            <Th>Cutoff (import before)</Th>
            <Th>Invert</Th>
            <Th>Counts</Th>
          </tr>
        </thead>
        <tbody>
          {preview.accounts.map((a) => {
            const m = mappings[a.name];
            if (!m) return null;
            const selected = m.target && m.target !== CREATE ? accountById.get(m.target) : null;
            return (
              <AccountRow
                key={a.name}
                account={a}
                mapping={m}
                accounts={accounts}
                selected={selected ?? null}
                allowSkip={allowSkip}
                busy={busy}
                onPatch={(change) => onPatch(a.name, change)}
                onCreate={() => onCreate(a.name)}
              />
            );
          })}
        </tbody>
      </Table>
      <div className="flex items-center gap-3">
        <Button disabled={busy || !allMapped} onClick={onImport}>
          {busy ? "Working…" : "Import"}
        </Button>
        {!allMapped && (
          <span className="text-xs text-muted-foreground">
            Map or skip every account to enable import.
          </span>
        )}
      </div>
      <ErrorText>{error}</ErrorText>
    </div>
  );
}
