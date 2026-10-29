import { useId } from "react";
import { Button, ErrorText, Label } from "../../components/ui";
import { selectClass, type Format } from "./model";

/** The flow's input: paste or choose a QIF/CSV file, pick format and date
 * order, and ask for a preview. */
export function SourceForm({
  content,
  format,
  dateFormat,
  busy,
  hasPreview,
  error,
  onSource,
  onFormat,
  onDateFormat,
  onPreview,
}: {
  content: string;
  format: Format;
  dateFormat: string;
  busy: boolean;
  hasPreview: boolean;
  error: string | null;
  /** New text; `explicitFormat` when a file's extension decides the format. */
  onSource: (value: string, explicitFormat?: Format) => void;
  onFormat: (format: Format) => void;
  onDateFormat: (dateFormat: string) => void;
  onPreview: () => void;
}) {
  const id = useId();
  return (
    <div className="space-y-3">
      <div className="space-y-1">
        <Label htmlFor={`${id}-content`}>Paste a QIF or CSV export, or choose a file</Label>
        <textarea
          id={`${id}-content`}
          className="h-36 w-full rounded-md border border-border bg-card p-3 font-mono text-xs"
          placeholder="!Type:Bank …  or  Date,Payee,Amount,…"
          value={content}
          onChange={(e) => onSource(e.target.value)}
        />
      </div>
      <div className="flex flex-wrap items-center gap-3">
        <input
          type="file"
          aria-label="Choose a QIF or CSV file"
          accept=".qif,.csv,text/plain,text/csv"
          className="text-sm"
          onChange={(e) => {
            const file = e.target.files?.[0];
            if (!file) return;
            const ext = file.name.toLowerCase().endsWith(".csv") ? "csv" : "qif";
            const reader = new FileReader();
            reader.onload = () => onSource(String(reader.result ?? ""), ext);
            reader.readAsText(file);
          }}
        />
        <div className="flex items-center gap-2">
          <Label htmlFor={`${id}-format`}>Format</Label>
          <select
            id={`${id}-format`}
            className={selectClass}
            value={format}
            onChange={(e) => onFormat(e.target.value as Format)}
          >
            <option value="qif">QIF</option>
            <option value="csv">CSV</option>
          </select>
        </div>
        <div className="flex items-center gap-2">
          <Label htmlFor={`${id}-dates`}>Dates</Label>
          <select
            id={`${id}-dates`}
            className={selectClass}
            value={dateFormat}
            onChange={(e) => onDateFormat(e.target.value)}
          >
            <option value="mdy">MM/DD/YYYY</option>
            <option value="dmy">DD/MM/YYYY</option>
          </select>
        </div>
        <Button disabled={busy || !content.trim()} onClick={() => onPreview()}>
          {busy && !hasPreview ? "Reading…" : "Preview"}
        </Button>
      </div>
      <ErrorText>{error}</ErrorText>
    </div>
  );
}
