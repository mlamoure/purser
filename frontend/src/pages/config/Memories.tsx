import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, errorDetail } from "../../lib/api";
import { Badge, Button, Card, CardTitle, ErrorText, Input, Label } from "../../components/ui";

type Memory = {
  id: string;
  text: string;
  state: string;
  pinned: boolean;
  archived: boolean;
  links: string[];
  stale: boolean;
  rules: string[];
};

/**
 * Config -> Memories. Nuances you tell Purser to remember, served to your
 * agent as context.
 *
 * Drafts are banded at the top because approving one is the moment the
 * containment loop closes: a draft is never served, so nothing an agent (or
 * Purser) wrote can influence a later pass until a human activates it here.
 */
export default function Memories() {
  const qc = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const [text, setText] = useState("");
  const [links, setLinks] = useState("");

  const memories = useQuery({
    queryKey: ["memories"],
    queryFn: async () => {
      const { data, error } = await api.GET("/memories", {
        params: { query: { include_drafts: true } },
      });
      if (error) throw new Error(errorDetail(error));
      return data as Memory[];
    },
  });

  const invalidate = () => qc.invalidateQueries({ queryKey: ["memories"] });

  const create = useMutation({
    mutationFn: async () => {
      const accounts = links
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean);
      const { data, error } = await api.POST("/memories", {
        body: {
          text,
          accounts: accounts.length ? accounts : null,
          pinned: false,
          activate: true,
        },
      });
      if (error) throw new Error(errorDetail(error));
      return data;
    },
    onSuccess: () => {
      setError(null);
      setText("");
      setLinks("");
      invalidate();
    },
    onError: (e: Error) => setError(e.message),
  });

  const act = useMutation({
    mutationFn: async (vars: { id: string; action: "activate" | "archive" }) => {
      const params = { params: { path: { memory_id: vars.id } } };
      const { data, error } =
        vars.action === "activate"
          ? await api.POST("/memories/{memory_id}/activate", params)
          : await api.POST("/memories/{memory_id}/archive", params);
      if (error) throw new Error(errorDetail(error));
      return data;
    },
    onSuccess: () => {
      setError(null);
      invalidate();
    },
    onError: (e: Error) => setError(e.message),
  });

  if (memories.isLoading) return <p className="text-sm text-muted-foreground">Loading…</p>;
  if (memories.error) return <ErrorText>{(memories.error as Error).message}</ErrorText>;

  const all = memories.data ?? [];
  const drafts = all.filter((m) => m.state === "draft");
  const active = all.filter((m) => m.state === "active" && !m.archived);

  return (
    <div className="space-y-6">
      {error && <ErrorText>{error}</ErrorText>}

      {drafts.length > 0 && (
        <Card>
          <CardTitle>Drafts awaiting approval</CardTitle>
          <p className="mb-3 text-sm text-muted-foreground">
            Proposed by an agent or by Purser. A draft is never served as context — approving
            it here is what lets it inform future answers.
          </p>
          <div className="space-y-2">
            {drafts.map((m) => (
              <div key={m.id} className="flex items-start gap-2 text-sm">
                <span className="grow">{m.text}</span>
                <Button size="sm" onClick={() => act.mutate({ id: m.id, action: "activate" })}>
                  Approve
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => act.mutate({ id: m.id, action: "archive" })}
                >
                  Discard
                </Button>
              </div>
            ))}
          </div>
        </Card>
      )}

      <Card>
        <CardTitle>Memories</CardTitle>
        <p className="mb-3 text-sm text-muted-foreground">
          Financial nuance your agent cannot derive from the ledger. Memories inform; they never
          change what lands. Something that should <em>act</em> belongs in a rule.
        </p>
        {active.length === 0 ? (
          <p className="text-sm text-muted-foreground">Nothing recorded yet.</p>
        ) : (
          <div className="space-y-2">
            {active.map((m) => (
              <div key={m.id} className="flex items-start gap-2 text-sm">
                <span className="grow">
                  {m.text}
                  {m.links.length > 0 && (
                    <span className="ml-2 font-mono text-xs text-muted-foreground">
                      {m.links.join(" · ")}
                    </span>
                  )}
                  {m.pinned && (
                    <>
                      {" "}
                      <Badge>pinned</Badge>
                    </>
                  )}
                  {m.stale && (
                    <>
                      {" "}
                      <Badge variant="amber">stale</Badge>
                    </>
                  )}
                  {m.rules.length > 0 && (
                    <>
                      {" "}
                      <Badge>
                        {m.rules.length} rule{m.rules.length === 1 ? "" : "s"}
                      </Badge>
                    </>
                  )}
                </span>
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => act.mutate({ id: m.id, action: "archive" })}
                >
                  Archive
                </Button>
              </div>
            ))}
          </div>
        )}
      </Card>

      <Card>
        <CardTitle>Record a memory</CardTitle>
        <div className="space-y-3">
          <div>
            <Label htmlFor="memory-text">Nuance</Label>
            <Input
              id="memory-text"
              value={text}
              maxLength={500}
              placeholder="the monthly 750.00 to Fidelity College is the 529 contribution"
              onChange={(e) => setText(e.target.value)}
            />
          </div>
          <div>
            <Label htmlFor="memory-links">Accounts (comma-separated paths, optional)</Label>
            <Input
              id="memory-links"
              value={links}
              placeholder="assets.fidelity_college, assets.bank_checking"
              onChange={(e) => setLinks(e.target.value)}
            />
            <p className="mt-1 text-xs text-muted-foreground">
              Relevance is exact: a memory reaches your agent only for the accounts and payees
              it names. Leave blank to make it apply everywhere.
            </p>
          </div>
          <Button disabled={!text.trim() || create.isPending} onClick={() => create.mutate()}>
            Save
          </Button>
        </div>
      </Card>
    </div>
  );
}
