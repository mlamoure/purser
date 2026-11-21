import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, errorDetail } from "../../lib/api";
import { Badge, Button, Card, CardTitle, ErrorText, Input } from "../../components/ui";
import { QueryState } from "../../components/QueryState";
import type { components } from "../../api/schema";
import { configQuery } from "../../lib/config";
import { useIsAdmin } from "../../lib/useIsAdmin";
import { AiCard } from "./AiCategorization";

type AccountOut = components["schemas"]["AccountOut"];

/** Settings -> Categories: the category tree, and for admins the optional AI
 * provider that categorizes new transactions (#542) — it lives in admin-only
 * GET/PUT /config, so a member session never reads it. */
export default function Categories() {
  const isAdmin = useIsAdmin();
  return (
    <div className="space-y-6">
      <CategoryTree />
      {isAdmin && <AiCategorization />}
    </div>
  );
}

function AiCategorization() {
  const config = useQuery(configQuery);
  if (!config.data) return <QueryState query={config} what="the AI categorization setting" />;
  return <AiCard initial={config.data} />;
}

/** Purser ships a finite, described category set (DESIGN §3.2). This tab is
 * its curation surface: rename, re-describe (descriptions steer the AI
 * classifier), add, and delete-when-unused. Paths never change here. */
function CategoryTree() {
  const categories = useQuery({
    queryKey: ["categories"],
    queryFn: async () => {
      const { data, error } = await api.GET("/categories");
      if (error) throw new Error(errorDetail(error));
      return data;
    },
  });

  const data = categories.data;
  if (!data) return <QueryState query={categories} what="categories" />;

  const roots = data.filter((c) => !c.path.includes("."));
  const childrenOf = (parent: AccountOut) =>
    data.filter(
      (c) =>
        c.path.startsWith(`${parent.path}.`) &&
        c.path.split(".").length === parent.path.split(".").length + 1,
    );

  return (
    <div className="space-y-6">
      <p className="text-sm text-muted-foreground">
        Categories are a curated, finite set. Descriptions are written for the AI classifier —
        say what belongs and what does not. Deleting is only possible while a category is
        unused; rename instead once it carries history.
      </p>
      {roots.map((root) => (
        <Card key={root.id}>
          <div className="flex items-center justify-between">
            <CardTitle>{root.name}</CardTitle>
            <Badge>{root.type === "category_income" ? "income" : "expense"}</Badge>
          </div>
          <div className="mt-2 space-y-4">
            {childrenOf(root).map((group) => (
              <div key={group.id} className="space-y-1">
                <CategoryRow category={group} depth={0} />
                {childrenOf(group).map((leaf) => (
                  <CategoryRow key={leaf.id} category={leaf} depth={1} />
                ))}
                <AddChild parent={group} />
              </div>
            ))}
            <AddChild parent={root} label={`Add group under ${root.path}`} />
          </div>
        </Card>
      ))}
    </div>
  );
}

function CategoryRow({ category, depth }: { category: AccountOut; depth: number }) {
  const qc = useQueryClient();
  const [name, setName] = useState(category.name);
  const [description, setDescription] = useState(category.description ?? "");
  const [error, setError] = useState<string | null>(null);
  const dirty = name !== category.name || description !== (category.description ?? "");

  const save = useMutation({
    mutationFn: async () => {
      const { error: err } = await api.PATCH("/categories/{category_id}", {
        params: { path: { category_id: category.id } },
        body: { name, description },
      });
      if (err) throw new Error(errorDetail(err));
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ["categories"] }),
    onError: (e) => setError(e.message),
  });

  const remove = useMutation({
    mutationFn: async () => {
      const { error: err } = await api.DELETE("/categories/{category_id}", {
        params: { path: { category_id: category.id } },
      });
      if (err) throw new Error(errorDetail(err));
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ["categories"] }),
    onError: (e) => setError(e.message),
  });

  return (
    <div className={depth ? "ml-6" : ""}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="w-full shrink-0 break-all font-mono text-xs text-muted-foreground sm:w-56">
          {category.path}
        </span>
        <Input
          className="h-8 w-full sm:w-44"
          aria-label={`Name of ${category.path}`}
          value={name}
          onChange={(e) => {
            setName(e.target.value);
            setError(null);
          }}
        />
        <Input
          className="h-8 w-full sm:w-auto sm:min-w-48 sm:flex-1"
          aria-label={`Description of ${category.path}`}
          value={description}
          placeholder="Description for the classifier…"
          onChange={(e) => {
            setDescription(e.target.value);
            setError(null);
          }}
        />
        {dirty && (
          <Button size="sm" onClick={() => save.mutate()} disabled={save.isPending}>
            Save
          </Button>
        )}
        <Button
          size="sm"
          variant="ghost"
          onClick={() => {
            if (window.confirm(`Delete ${category.path}? Only possible while unused.`))
              remove.mutate();
          }}
        >
          Delete
        </Button>
      </div>
      <ErrorText>{error}</ErrorText>
    </div>
  );
}

function AddChild({ parent, label }: { parent: AccountOut; label?: string }) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [error, setError] = useState<string | null>(null);

  const create = useMutation({
    mutationFn: async () => {
      const { error: err } = await api.POST("/categories", {
        body: {
          name,
          description: description || null,
          type: parent.type,
          parent_id: parent.id,
          currency: "USD",
        },
      });
      if (err) throw new Error(errorDetail(err));
    },
    onSuccess: () => {
      setOpen(false);
      setName("");
      setDescription("");
      qc.invalidateQueries({ queryKey: ["categories"] });
    },
    onError: (e) => setError(e.message),
  });

  if (!open)
    return (
      <div className="ml-6">
        <Button size="sm" variant="ghost" onClick={() => setOpen(true)}>
          + {label ?? `Add under ${parent.path}`}
        </Button>
      </div>
    );
  return (
    <div className="ml-6 flex flex-wrap items-center gap-2">
      <Input
        className="h-8 w-full sm:w-44"
        aria-label={`Name of the new category under ${parent.path}`}
        autoFocus
        value={name}
        placeholder="Name"
        onChange={(e) => setName(e.target.value)}
      />
      <Input
        className="h-8 w-full sm:w-auto sm:min-w-48 sm:flex-1"
        aria-label={`Description of the new category under ${parent.path}`}
        value={description}
        placeholder="Description for the classifier…"
        onChange={(e) => setDescription(e.target.value)}
      />
      <Button size="sm" onClick={() => create.mutate()} disabled={!name.trim() || create.isPending}>
        Create
      </Button>
      <Button size="sm" variant="ghost" onClick={() => setOpen(false)}>
        Cancel
      </Button>
      <ErrorText>{error}</ErrorText>
    </div>
  );
}
