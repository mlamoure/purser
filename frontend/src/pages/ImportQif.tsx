import { Card, CardTitle } from "../components/ui";
import { ImportPanel } from "./import/ImportPanel";

// The /import page. The flow itself (ImportPanel and its parts under
// ./import/) is shared with the first-run wizard's import step.
export default function ImportQif() {
  return (
    <Card>
      <CardTitle>Import bank history</CardTitle>
      <p className="mb-4 text-sm text-muted-foreground">
        Bring years of history in from a Quicken (QIF) or CSV bank export (Quicken CSV or Amex
        activity). Re-importing is always safe (idempotent), transfers between accounts link instead
        of duplicating, and a per-account cutoff hands the recent window to your SimpleFIN feed.
      </p>
      <ImportPanel />
    </Card>
  );
}
