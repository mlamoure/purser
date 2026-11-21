import { Fragment, useState } from "react";
import { Badge, Button, Card, CardTitle, Table, Td, Th } from "../../components/ui";
import { MapFeedAccountForm, type Discovered, type MapTarget } from "./MapFeedAccountForm";

/** Step 4: map each discovered bank account to a Purser account, new or
 * existing (`mapAccount`, from useFeedConnection). Map opens the same form
 * Settings -> Connections uses for accounts that appear after setup. */
export function MapAccountsStep({
  discovered,
  mapAccount,
  onContinue,
}: {
  discovered: Discovered[];
  mapAccount: (d: Discovered, target: MapTarget) => Promise<void>;
  onContinue: () => void;
}) {
  const [open, setOpen] = useState<string | null>(null); // external_id being mapped

  return (
    <Card>
      <CardTitle>Map discovered accounts</CardTitle>
      <p className="mb-4 text-sm text-muted-foreground">
        Each bank account becomes a Purser account; transactions import from the
        already-archived fetch the moment you map it.
      </p>
      <Table>
        <thead>
          <tr><Th>Bank account</Th><Th>Balance</Th><Th>Status</Th><Th /></tr>
        </thead>
        <tbody>
          {discovered.map((d) => (
            <Fragment key={d.external_id}>
              <tr>
                <Td>{d.name ?? d.external_id}{d.org ? ` · ${d.org}` : ""}</Td>
                <Td>{d.balance ?? "—"}</Td>
                <Td>
                  {d.mapped_account_path
                    ? <Badge variant="green">{d.mapped_account_path}</Badge>
                    : <Badge variant="amber">unmapped</Badge>}
                </Td>
                <Td>
                  {!d.mapped_account_path && open !== d.external_id && (
                    <Button size="sm" variant="outline" onClick={() => setOpen(d.external_id)}>
                      Map
                    </Button>
                  )}
                </Td>
              </tr>
              {!d.mapped_account_path && open === d.external_id && (
                <tr>
                  <Td colSpan={4}>
                    <MapFeedAccountForm
                      account={d}
                      onMap={async (target) => {
                        await mapAccount(d, target);
                        setOpen(null);
                      }}
                      onCancel={() => setOpen(null)}
                    />
                  </Td>
                </tr>
              )}
            </Fragment>
          ))}
        </tbody>
      </Table>
      <div className="mt-4 flex gap-2">
        <Button onClick={() => onContinue()}>Continue</Button>
      </div>
    </Card>
  );
}
