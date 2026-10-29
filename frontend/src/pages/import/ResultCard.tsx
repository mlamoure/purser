import { useNavigate } from "react-router-dom";
import { Button, Table, Td, Th } from "../../components/ui";
import type { ImportResult } from "./model";

/** What an import did, in total and per file account. "Done" hands back to
 * the wizard (`onDone`) or goes to the dashboard. */
export function ResultCard({ result, onDone }: { result: ImportResult; onDone?: () => void }) {
  const navigate = useNavigate();
  const stat = (label: string, n: number) => (
    <div className="rounded-md border border-border bg-card p-3">
      <div className="text-2xl font-bold">{n}</div>
      <div className="text-xs text-muted-foreground">{label}</div>
    </div>
  );
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        {stat("Imported", result.imported)}
        {stat("Transfers", result.transfers_created)}
        {stat("One-leg", result.one_leg_transfers)}
        {stat("Uncleared (expected)", result.uncleared)}
        {stat("Promoted (posted)", result.promoted)}
        {stat("Skipped (dedup)", result.skipped)}
        {stat("Skipped (cutoff)", result.skipped_cutoff)}
        {stat("Skipped (unmapped)", result.skipped_unmapped)}
        {stat("Skipped (reminders)", result.skipped_scheduled)}
        {stat("Skipped (investment)", result.skipped_investment)}
      </div>
      {Object.keys(result.per_account).length > 0 && (
        <Table>
          <thead>
            <tr>
              <Th>Account</Th>
              <Th className="text-right">Imported</Th>
              <Th className="text-right">Transfers</Th>
              <Th className="text-right">One-leg</Th>
              <Th className="text-right">Uncleared</Th>
              <Th className="text-right">Promoted</Th>
              <Th className="text-right">Dedup</Th>
              <Th className="text-right">Cutoff</Th>
              <Th className="text-right">Unmapped</Th>
              <Th className="text-right">Reminders</Th>
              <Th className="text-right">Investment</Th>
            </tr>
          </thead>
          <tbody>
            {Object.entries(result.per_account).map(([name, c]) => (
              <tr key={name}>
                <Td>{name === "__default__" ? "(single account)" : name}</Td>
                <Td className="text-right font-mono">{c.would_import}</Td>
                <Td className="text-right font-mono">{c.transfers}</Td>
                <Td className="text-right font-mono">{c.one_leg_transfers}</Td>
                <Td className="text-right font-mono">{c.uncleared}</Td>
                <Td className="text-right font-mono">{c.promoted}</Td>
                <Td className="text-right font-mono">{c.skipped_dedup}</Td>
                <Td className="text-right font-mono">{c.skipped_cutoff}</Td>
                <Td className="text-right font-mono">{c.skipped_unmapped}</Td>
                <Td className="text-right font-mono">{c.skipped_scheduled}</Td>
                <Td className="text-right font-mono">{c.skipped_investment}</Td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
      <Button onClick={() => (onDone ? onDone() : navigate("/"))}>Done</Button>
    </div>
  );
}
