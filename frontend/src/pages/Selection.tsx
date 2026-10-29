import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { api, errorDetail } from "../lib/api";
import { fmtAmount } from "../lib/utils";
import { Card, CardTitle, ErrorText, Table, Td, Th } from "../components/ui";

/** The rows an agent showed you with MCP `present` (#437): the link carries
 * only transaction ids and a note, and the details load here, in your
 * signed-in session, without passing through the agent's model. */
export default function Selection() {
  const [params] = useSearchParams();
  const ids = params.getAll("id");
  const note = params.get("note");

  const rows = useQuery({
    queryKey: ["selection", ids],
    enabled: ids.length > 0,
    queryFn: async () => {
      const { data, error } = await api.GET("/transactions/selection", {
        params: { query: { id: ids } },
      });
      if (error) throw new Error(errorDetail(error));
      return data;
    },
  });

  return (
    <Card>
      <CardTitle>Shared by your agent</CardTitle>
      {note && <p className="mb-4 text-sm">{note}</p>}
      {ids.length === 0 && (
        <p className="text-sm text-muted-foreground">This link names no transactions.</p>
      )}
      {rows.error && <ErrorText>{rows.error.message}</ErrorText>}
      {rows.data && (
        <Table>
          <thead>
            <tr>
              <Th>Date</Th>
              <Th>Account</Th>
              <Th>Payee</Th>
              <Th>Description</Th>
              <Th>Category</Th>
              <Th className="text-right">Amount</Th>
            </tr>
          </thead>
          <tbody>
            {rows.data.map((t) => (
              <tr key={t.id}>
                <Td>{t.date}</Td>
                <Td>{t.account_path ?? "—"}</Td>
                <Td>{t.payee ?? "—"}</Td>
                <Td>{t.description ?? ""}</Td>
                <Td>{t.category_path ?? "—"}</Td>
                <Td className="text-right tabular-nums">{fmtAmount(t.amount)}</Td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
    </Card>
  );
}
