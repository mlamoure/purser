import { Button, Card, CardTitle } from "../../components/ui";
import { ImportPanel } from "../import/ImportPanel";

/** Step 5 (optional): the shared QIF/CSV import panel, also mounted on the
 * Import page. `onDone` fires after an import's "Done" or on skip. */
export function ImportHistoryStep({ onDone }: { onDone: () => void }) {
  return (
    <Card>
      <CardTitle>Import bank history (optional)</CardTitle>
      <p className="mb-4 text-sm text-muted-foreground">
        Paste or upload a QIF or CSV export to bring years of history in. Re-importing is always
        safe (idempotent), transfers between accounts link instead of duplicating, and a
        per-account cutoff hands the recent window to your SimpleFIN feed.
      </p>
      <ImportPanel onDone={onDone} />
      <div className="mt-4">
        <Button variant="ghost" onClick={() => onDone()}>
          Skip for now
        </Button>
      </div>
    </Card>
  );
}
