import { Badge, Button, Input, Label, Td } from "../../components/ui";
import {
  ALL_TYPES,
  CREATE,
  investmentOnly,
  SKIP,
  selectClass,
  suggestPath,
  type AccountOut,
  type Counts,
  type Mapping,
  type PreviewAccount,
} from "./model";

function CountCell({ counts }: { counts: Counts }) {
  const cell = (label: string, n: number, strong = false) => (
    <span className={n > 0 && strong ? "font-medium" : "text-muted-foreground"}>
      {label} {n}
    </span>
  );
  return (
    <div className="flex flex-col gap-0.5 font-mono text-xs">
      <span>{cell("in", counts.in_file)}</span>
      <span className="space-x-2">
        {cell("import", counts.would_import, true)}
        {cell("1-leg", counts.one_leg_transfers, true)}
        {cell("promoted", counts.promoted, true)}
        {cell("uncleared", counts.uncleared, true)}
        {cell("split→transfer", counts.split_transfers, true)}
      </span>
      <span className="space-x-2">
        {cell("dedup", counts.skipped_dedup)}
        {cell("cutoff", counts.skipped_cutoff)}
        {cell("skip", counts.skipped_unmapped)}
        {cell("zero", counts.skipped_zero_amount)}
        {cell("reminder", counts.skipped_scheduled)}
        {cell("investment", counts.skipped_investment)}
      </span>
    </div>
  );
}

/** One file account in the mapping table: its target (existing account,
 * "Create new…", or — CSV, and a QIF investment-only account — "Don't
 * import"), cutoff, sign inversion and the preview's live counts. */
export function AccountRow({
  account,
  mapping,
  accounts,
  selected,
  allowSkip,
  busy,
  onPatch,
  onCreate,
}: {
  account: PreviewAccount;
  mapping: Mapping;
  accounts: AccountOut[];
  selected: AccountOut | null;
  allowSkip: boolean;
  busy: boolean;
  onPatch: (change: Partial<Mapping>) => void;
  onCreate: () => void;
}) {
  const simplefin = accounts.filter((a) => a.connection_id);
  const manual = accounts.filter((a) => !a.connection_id);
  const skipped = mapping.target === SKIP;
  const canSkip = allowSkip || investmentOnly(account);
  return (
    <>
      <tr className="align-top">
        <Td>
          <div className="font-medium">
            {account.name === "__default__" ? "(single account)" : account.name}
          </div>
          <div className="mt-1 space-x-1">
            <Badge>{account.type}</Badge>
            <span className="text-xs text-muted-foreground">{account.transactions} rows</span>
          </div>
        </Td>
        <Td>
          <div className="flex items-center gap-2">
            <select
              className={selectClass}
              value={mapping.target}
              onChange={(e) => onPatch({ target: e.target.value })}
            >
              <option value="">— choose —</option>
              {manual.length > 0 && (
                <optgroup label="Accounts">
                  {manual.map((a) => (
                    <option key={a.id} value={a.id}>
                      {a.path}
                    </option>
                  ))}
                </optgroup>
              )}
              {simplefin.length > 0 && (
                <optgroup label="SimpleFIN accounts">
                  {simplefin.map((a) => (
                    <option key={a.id} value={a.id}>
                      {a.path} (SimpleFIN)
                    </option>
                  ))}
                </optgroup>
              )}
              <option value={CREATE}>Create new…</option>
              {canSkip && <option value={SKIP}>Don't import</option>}
            </select>
            {selected?.connection_id && <Badge variant="green">SimpleFIN</Badge>}
          </div>
          {mapping.target === CREATE && (
            <div className="mt-2 space-y-2 rounded-md border border-border bg-muted/30 p-2">
              <div className="space-y-1">
                <Label className="text-xs">Name</Label>
                <Input
                  value={mapping.newName}
                  onChange={(e) => onPatch({ newName: e.target.value })}
                />
              </div>
              <div className="flex gap-2">
                <div className="space-y-1">
                  <Label className="text-xs">Type</Label>
                  <select
                    className={selectClass}
                    value={mapping.newType}
                    onChange={(e) =>
                      onPatch({
                        newType: e.target.value,
                        newPath: suggestPath(mapping.newName, e.target.value),
                      })
                    }
                  >
                    {ALL_TYPES.map((t) => (
                      <option key={t} value={t}>
                        {t}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="flex-1 space-y-1">
                  <Label className="text-xs">Path</Label>
                  <Input
                    value={mapping.newPath}
                    onChange={(e) => onPatch({ newPath: e.target.value })}
                  />
                </div>
              </div>
              <Button size="sm" variant="outline" disabled={busy} onClick={onCreate}>
                Create &amp; select
              </Button>
            </div>
          )}
        </Td>
        <Td>
          <Input
            type="date"
            value={mapping.cutoff}
            disabled={skipped}
            onChange={(e) => onPatch({ cutoff: e.target.value, cutoffTouched: true })}
          />
          <div className="mt-1 text-xs text-muted-foreground">
            {skipped ? "not imported" : mapping.cutoff ? "before this date" : "importing everything"}
          </div>
        </Td>
        <Td>
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={mapping.invert}
              disabled={skipped}
              onChange={(e) => onPatch({ invert: e.target.checked, invertTouched: true })}
            />
            flip signs
          </label>
          {account.sign.suspect && !skipped && (
            <div className="mt-1 text-xs text-amber-600 dark:text-amber-500">
              Looks income-positive for a {selected?.type ?? account.type} — invert suggested.
            </div>
          )}
        </Td>
        <Td>
          <CountCell counts={account.counts} />
        </Td>
      </tr>
    </>
  );
}
