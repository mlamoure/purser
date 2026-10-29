import { Badge, Button, Card, CardTitle, ErrorText, Table, Td, Th } from "../../components/ui";
import type { Discovered } from "./useFeedConnection";
import { useStepRunner } from "./useStepRunner";

/** Step 4: map each discovered bank account to a new Purser account
 * (`mapAccount`, from useFeedConnection). */
export function MapAccountsStep({
  discovered,
  mapAccount,
  onContinue,
}: {
  discovered: Discovered[];
  mapAccount: (d: Discovered) => Promise<void>;
  onContinue: () => void;
}) {
  const { busy, error, run } = useStepRunner();

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
            <tr key={d.external_id}>
              <Td>{d.name ?? d.external_id}{d.org ? ` · ${d.org}` : ""}</Td>
              <Td>{d.balance ?? "—"}</Td>
              <Td>
                {d.mapped_account_path
                  ? <Badge variant="green">{d.mapped_account_path}</Badge>
                  : <Badge variant="amber">unmapped</Badge>}
              </Td>
              <Td>
                {!d.mapped_account_path && (
                  <Button size="sm" variant="outline" disabled={busy} onClick={() => run(() => mapAccount(d))}>
                    Map
                  </Button>
                )}
              </Td>
            </tr>
          ))}
        </tbody>
      </Table>
      <div className="mt-4 flex gap-2">
        <Button onClick={() => onContinue()}>Continue</Button>
      </div>
      <ErrorText>{error}</ErrorText>
    </Card>
  );
}
