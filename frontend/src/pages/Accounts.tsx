import { useState } from "react";
import { Button, Card, CardTitle } from "../components/ui";
import { AddAssetForm } from "../components/AddAssetForm";
import { QueryState } from "../components/QueryState";
import { AccountsTable, NO_ACCOUNTS, useAccounts } from "../components/AccountsTable";

/** Every account by name with its type and balance, each opening its
 * register (#542). Hidden and closed accounts show on request. */
export default function Accounts() {
  const [showHidden, setShowHidden] = useState(false);
  const [showClosed, setShowClosed] = useState(false);
  const accounts = useAccounts({ showHidden, showClosed });
  const [adding, setAdding] = useState(false);

  return (
    <div className="space-y-6">
    {adding && <AddAssetForm onCancel={() => setAdding(false)} />}
    <Card>
      <div className="mb-4 flex items-center justify-between gap-2">
        <CardTitle className="mb-0">Accounts</CardTitle>
        {!adding && (
          <Button size="sm" variant="outline" onClick={() => setAdding(true)}>
            Add asset
          </Button>
        )}
      </div>
      <QueryState query={accounts} what="accounts" empty={NO_ACCOUNTS}>
        {(rows) => <AccountsTable rows={rows} />}
      </QueryState>
      <div className="mt-3 flex flex-wrap gap-4">
        <label className="flex items-center gap-2 text-sm text-muted-foreground">
          <input
            type="checkbox"
            checked={showHidden}
            onChange={(e) => setShowHidden(e.target.checked)}
          />
          Show hidden
        </label>
        <label className="flex items-center gap-2 text-sm text-muted-foreground">
          <input
            type="checkbox"
            checked={showClosed}
            onChange={(e) => setShowClosed(e.target.checked)}
          />
          Show closed
        </label>
      </div>
    </Card>
    </div>
  );
}
