import { Button } from "../../components/ui";

export function CopyButton({ text, label = "Copy" }: { text: string; label?: string }) {
  return (
    <Button type="button" size="sm" variant="outline" onClick={() => navigator.clipboard.writeText(text)}>
      {label}
    </Button>
  );
}
