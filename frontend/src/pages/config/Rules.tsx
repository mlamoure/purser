import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, errorDetail } from "../../lib/api";
import { Badge, Button, Card, CardTitle, ErrorText, Table, Td, Th } from "../../components/ui";

type RuleState = "draft" | "active" | "disabled" | "archived";

type Rule = {
  id: string;
  type: string;
  state: RuleState;
  priority: number;
  predicate: string;
  action: string;
  applied_count: number;
  note: string | null;
  /** transfer_link only: how many days apart the pair's legs may post; null otherwise. */
  window_days: number | null;
};

const STATE_VARIANT: Record<RuleState, "default" | "green" | "amber" | "red"> = {
  active: "green",
  draft: "amber",
  disabled: "default",
  archived: "default",
};

/**
 * Config -> Rules. Drafts are banded at the top: a draft never executes, so
 * the only thing standing between a proposed rule and a live one is this
 * page. Rules are listed in EVALUATION order, so which one wins is visible
 * here rather than deduced after the fact.
 */
export default function Rules() {
  const qc = useQueryClient();
  const [error, setError] = useState<string | null>(null);

  const rules = useQuery({
    queryKey: ["rules"],
    queryFn: async () => {
      const { data, error } = await api.GET("/rules", {});
      if (error) throw new Error(errorDetail(error));
      return data as Rule[];
    },
  });

  const setState = useMutation({
    mutationFn: async (vars: { id: string; state: RuleState; confirmLinks?: number }) => {
      const { data, error } = await api.POST("/rules/{rule_id}/state", {
        params: { path: { rule_id: vars.id } },
        body: { state: vars.state, confirm_links: vars.confirmLinks ?? null },
      });
      if (error) throw new Error(errorDetail(error));
      return data;
    },
    onSuccess: () => {
      setError(null);
      qc.invalidateQueries({ queryKey: ["rules"] });
    },
    onError: (e: Error) => setError(e.message),
  });

  /**
   * Activating a transfer_link rule is retroactive over all posted history,
   * and a merge is balance-neutral so the drift check cannot catch a bad one.
   * The server refuses without an explicit count and puts the projected
   * number in the error; surface it and ask, rather than retrying blind. The
   * count is taken with the rule's own window, so the dialog names it: a
   * wider window is what brings more history into scope.
   */
  function activate(rule: Rule) {
    if (rule.type !== "transfer_link") {
      setState.mutate({ id: rule.id, state: "active" });
      return;
    }
    setState.mutate(
      { id: rule.id, state: "active" },
      {
        onError: (e: Error) => {
          const projected = Number(e.message.match(/would link (\d+)/)?.[1]);
          if (Number.isNaN(projected)) {
            setError(e.message);
            return;
          }
          const apart =
            rule.window_days === null ? "" : ` up to ${rule.window_days} days apart`;
          const ok = window.confirm(
            `Activating this rule links ${projected} historical pair(s)${apart}.\n\n` +
              "Merging rows is balance-neutral, so the post-sync balance check " +
              "cannot detect a wrong one. Continue?",
          );
          if (ok) setState.mutate({ id: rule.id, state: "active", confirmLinks: projected });
        },
      },
    );
  }

  if (rules.isLoading) return <p className="text-sm text-muted-foreground">Loading…</p>;
  if (rules.error) return <ErrorText>{(rules.error as Error).message}</ErrorText>;

  const all = rules.data ?? [];
  const drafts = all.filter((r) => r.state === "draft");
  const live = all.filter((r) => r.state !== "draft" && r.state !== "archived");
  const archived = all.filter((r) => r.state === "archived");

  return (
    <div className="space-y-6">
      {error && <ErrorText>{error}</ErrorText>}

      {drafts.length > 0 && (
        <Card>
          <CardTitle>Drafts awaiting review</CardTitle>
          <p className="mb-3 text-sm text-muted-foreground">
            A draft never runs. Activate it to put it into the import and nightly passes.
          </p>
          <RuleTable rules={drafts} onActivate={activate} onSetState={setState.mutate} />
        </Card>
      )}

      <Card>
        <CardTitle>Rules</CardTitle>
        <p className="mb-3 text-sm text-muted-foreground">
          In evaluation order — within a kind, the first match wins.
        </p>
        {live.length === 0 ? (
          <p className="text-sm text-muted-foreground">No active rules yet.</p>
        ) : (
          <RuleTable rules={live} onActivate={activate} onSetState={setState.mutate} />
        )}
      </Card>

      {archived.length > 0 && (
        <Card>
          <CardTitle>Archived</CardTitle>
          <p className="mb-3 text-sm text-muted-foreground">
            Kept so audit rows referencing them stay resolvable.
          </p>
          <RuleTable rules={archived} onActivate={activate} onSetState={setState.mutate} />
        </Card>
      )}
    </div>
  );
}

function RuleTable({
  rules,
  onActivate,
  onSetState,
}: {
  rules: Rule[];
  onActivate: (rule: Rule) => void;
  onSetState: (vars: { id: string; state: RuleState }) => void;
}) {
  return (
    <Table>
      <thead>
        <tr>
          <Th>Priority</Th>
          <Th>Kind</Th>
          <Th>When</Th>
          <Th>Then</Th>
          <Th>Applied</Th>
          <Th>State</Th>
          <Th>Actions</Th>
        </tr>
      </thead>
      <tbody>
        {rules.map((rule) => (
          <tr key={rule.id}>
            <Td>{rule.priority}</Td>
            <Td>{rule.type}</Td>
            <Td className="font-mono text-xs">{rule.predicate}</Td>
            <Td className="font-mono text-xs">{rule.action}</Td>
            <Td>{rule.applied_count}</Td>
            <Td>
              <Badge variant={STATE_VARIANT[rule.state] ?? "default"}>{rule.state}</Badge>
            </Td>
            <Td>
              <div className="flex gap-2">
                {rule.state !== "active" && rule.state !== "archived" && (
                  <Button size="sm" onClick={() => onActivate(rule)}>
                    Activate
                  </Button>
                )}
                {rule.state === "active" && (
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => onSetState({ id: rule.id, state: "disabled" })}
                  >
                    Disable
                  </Button>
                )}
                {rule.state !== "archived" && (
                  <Button
                    size="sm"
                    variant="destructive"
                    onClick={() => {
                      if (window.confirm("Archive this rule? It stops running immediately."))
                        onSetState({ id: rule.id, state: "archived" });
                    }}
                  >
                    Archive
                  </Button>
                )}
              </div>
            </Td>
          </tr>
        ))}
      </tbody>
    </Table>
  );
}
