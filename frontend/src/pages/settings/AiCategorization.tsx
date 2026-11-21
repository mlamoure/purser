import { useId, useState } from "react";
import { Badge, Button, Card, CardTitle, ErrorText, Input, Label } from "../../components/ui";
import { Saved, selectClass, useSaveConfig, type ConfigOut, type ConfigUpdate } from "./saveConfig";

type AiProvider = NonNullable<ConfigUpdate["ai_provider"]>;

/** The optional provider that categorizes new transactions. Settings ->
 * Categories shows it to admins (#542): it decides how new rows get the
 * categories that page manages. */
export function AiCard({ initial }: { initial: ConfigOut }) {
  const [provider, setProvider] = useState<AiProvider>(
    (initial.values.ai_provider as AiProvider) ?? "none",
  );
  const [model, setModel] = useState(initial.values.ai_model ?? "");
  const [baseUrl, setBaseUrl] = useState(initial.values.ai_base_url);
  const [apiKey, setApiKey] = useState("");
  const [error, setError] = useState<string | null>(null);
  const save = useSaveConfig(setError);
  const id = useId();

  return (
    <Card>
      <CardTitle>AI categorization</CardTitle>
      <p className="mb-4 text-sm text-muted-foreground">
        Optional bring-your-own provider for categorizing new transactions. Purser is fully
        functional with this off.
      </p>
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        <div className="space-y-1">
          <Label htmlFor={`${id}-provider`}>Provider</Label>
          <select
            id={`${id}-provider`}
            className={selectClass}
            value={provider}
            onChange={(e) => setProvider(e.target.value as AiProvider)}
          >
            <option value="none">none (off)</option>
            <option value="openai">OpenAI-compatible</option>
            <option value="anthropic">Anthropic</option>
          </select>
        </div>
        {provider !== "none" && (
          <>
            <div className="space-y-1">
              <Label htmlFor={`${id}-model`}>Model</Label>
              <Input
                id={`${id}-model`}
                value={model}
                onChange={(e) => setModel(e.target.value)}
                placeholder="e.g. gpt-4.1-mini"
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor={`${id}-base-url`}>Base URL</Label>
              <Input
                id={`${id}-base-url`}
                type="url"
                value={baseUrl}
                onChange={(e) => setBaseUrl(e.target.value)}
                placeholder="https://api.openai.com/v1"
              />
            </div>
            <div className="space-y-1">
              <div className="flex items-center gap-2">
                <Label htmlFor={`${id}-api-key`}>API key</Label>
                <Badge variant={initial.ai_api_key_set ? "green" : "default"}>
                  {initial.ai_api_key_set ? "set" : "not set"}
                </Badge>
              </div>
              <Input
                id={`${id}-api-key`}
                type="password"
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                placeholder={initial.ai_api_key_set ? "enter a new key to replace" : "sk-…"}
              />
            </div>
          </>
        )}
      </div>
      <Button
        className="mt-4"
        disabled={save.isPending}
        onClick={() => {
          setError(null);
          const body: ConfigUpdate = { ai_provider: provider };
          if (provider !== "none") {
            body.ai_model = model.trim() || null;
            body.ai_base_url = baseUrl.trim();
            if (apiKey.trim()) body.ai_api_key = apiKey.trim();
          }
          save.mutate(body, { onSuccess: () => setApiKey("") });
        }}
      >
        Save
      </Button>
      <Saved visible={save.isSuccess} />
      <ErrorText>{error}</ErrorText>
    </Card>
  );
}

