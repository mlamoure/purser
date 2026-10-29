import { ColumnMapForm } from "./ColumnMapForm";
import { MappingTable } from "./MappingTable";
import { ResultCard } from "./ResultCard";
import { SourceForm } from "./SourceForm";
import { useImportFlow } from "./useImportFlow";

/** Shared import flow: paste/upload (QIF or CSV) -> mapping table (targets,
 * cutoffs, invert, live counts, per-account "don't import") -> commit. When a
 * CSV layout isn't recognized, a column-mapping sub-form collects the mapping.
 * Mounted on the /import page and in the first-run wizard's import step.
 * State and API calls live in useImportFlow; the parts here only render. */
export function ImportPanel({ onDone }: { onDone?: () => void }) {
  const flow = useImportFlow();

  if (flow.result) {
    return <ResultCard result={flow.result} onDone={onDone} />;
  }

  return (
    <div className="space-y-4">
      <SourceForm
        content={flow.content}
        format={flow.format}
        dateFormat={flow.dateFormat}
        busy={flow.busy}
        hasPreview={flow.preview !== null}
        error={flow.error}
        onSource={flow.setSource}
        onFormat={flow.chooseFormat}
        onDateFormat={flow.chooseDateFormat}
        onPreview={flow.requestPreview}
      />

      {flow.needsMapping && (
        <ColumnMapForm
          columns={flow.needsMapping}
          initial={flow.columnMap}
          busy={flow.busy}
          onApply={flow.applyColumnMap}
        />
      )}

      {flow.preview && (
        <MappingTable
          preview={flow.preview}
          mappings={flow.mappings}
          accounts={flow.accounts}
          allowSkip={flow.format === "csv"}
          busy={flow.busy}
          error={flow.error}
          onPatch={flow.patchMapping}
          onCreate={flow.createAccount}
          onImport={flow.runImport}
        />
      )}
    </div>
  );
}
