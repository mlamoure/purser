---
title: Import QIF and CSV
description: Load bank history from a Quicken QIF or CSV export with a mapping-first preview, per-account cutoffs, and content-hash idempotency.
---

When an account has no SimpleFIN feed, or you want history older than the feed
reaches, import a file your bank exports. Purser reads Quicken **QIF** and two
**CSV** layouts through `POST /import/qif` and `POST /import/csv` — a two-step
**preview then commit**, sharing one planner so every rule below applies
identically to QIF and CSV.

## Preview, then commit

The preview is a strict, read-only dry run: it resolves your account mapping,
suggests a cutoff and a sign per account, and returns the **real** counts it
gets by checking content hashes against the database. Its per-account numbers
equal what the commit will do for the same request — an exact rehearsal where
only the commit writes rows. Adjust mapping, cutoff, and invert, then commit the
identical request.

## Mapping first

Every file account maps to one Purser account. The preview drives the account
selector (or inline account creation); a single-account file uses the parser's
`__default__` name. For a multi-account CSV you can import a subset: set a
file account's map value to **null** to skip it. Its rows are removed before
planning and counted `skipped_unmapped`, and any transfer whose counterpart was
skipped degrades to a one-leg entry on the imported side. Per-account skip is a
CSV surface (QIF's request map is not nullable), with one QIF exception: an
investment account (next section) may simply be left out of the map.

## Investment accounts

Purser imports bank registers, not investment activity. A QIF file's
`!Type:Invst` sections (buys, sells, dividends, share moves, and the
investment side of transfers) are skipped and counted `skipped_investment` in
the preview and the commit, and the preview lists the account as `brokerage`.
An investment account you leave **unmapped** stays out entirely: a checking
transfer into it imports as a one-leg entry on the checking side, so the
checking balance matches Quicken's. Map it only if you want those transfers as
two-leg transfers into a Purser account; it then receives the transfers, never
the purchases. A brokerage balance adds the holdings' market value to its
postings (see [the three methods](../../explanation/balances/#the-three-methods)),
so importing the purchases as well would count every contribution twice. The
investment section never books a transfer of its own, so a contribution no
longer reaches checking twice either.

## Cutoffs: manual owns the past, the feed owns the present

Each mapped account may carry a `cutoff` date. A row imports only when its date
is **strictly older** than the cutoff — the feed owns everything from the cutoff
forward, so manual history and SimpleFIN never fight over the same days. The
server **never imposes a cutoff you did not send**; the preview only *suggests*
one: the account's earliest feed-sourced transaction date minus a 3-day margin
(QIF carries transaction dates, while SimpleFIN posted dates run 1-3 business
days later), or null when the feed has not touched the account. This is the
per-request twin of the persistent [`feed_start`](../../how-to/simplefin/) boundary.

## Sign invert

Some banks export a card or loan file with spending as a positive number. A
per-account `invert` flips every row's amount before hashing, category
selection, payee memory, and transfer direction. The preview computes a sign
diagnostic and pre-checks invert when a credit-card or loan target looks
income-positive. **Footgun:** re-importing the same file with invert toggled
produces a disjoint set of content hashes and therefore duplicates. The
preview's `skipped_dedup` count stays 0 across such a flip, which is the tell —
watch it.

## Transfers and the bracket convention

A row whose category is another account in Quicken brackets (`[assets.savings]`)
is a transfer, evaluated against both accounts' effective cutoffs:

- older than both cutoffs -> the two-leg transfer;
- inside both -> skipped (`skipped_cutoff`), the feed owns that window;
- split (older than one side, inside the other) -> degrades to a one-leg flat
  entry on the older side, which the post-import auto-link pass then pairs with
  the feed counterpart (see [Transfers](../../how-to/transfers/)).

Zero-amount marker rows are structurally non-importable (the ledger rejects zero
postings), so the planner filters them into `skipped_zero_amount` before hashing
-- a $0 Quicken row can never fail a commit or half-form a transfer.

## Split transactions

A Quicken row can be split across several categories (`S`, `E` and `$` lines;
its category reads `--Split--`). Purser imports it as one
[split transaction](../../explanation/ledger/#split-transactions): one leg on the
bank account and one leg per category line, so a paycheck's gross pay and
taxes, or a store run that was half groceries, land in their own categories.

- A **transfer line** (`S[Savings]`) is taken out of the split and imported as
  an ordinary transfer to that account, which pairs with the savings account's
  own copy of the row in the same file. The money moves once, and the
  checking balance matches Quicken's. The preview counts these lines
  `split_transfers`; they follow every transfer rule above, cutoffs included.
- A line with no category goes to the uncategorized placeholder, never to the
  payee's usual category. If the lines do not add up to the row's amount, the
  difference goes there too, counted `split_remainders`.
- A split left with a single category line imports as an ordinary row.

Split rows are read-only: they cannot be recategorized or edited in place,
and neither the categorize pass nor a rule's apply-history touches them. A
file imported before split lines were read landed each split row whole under
`expenses.split`; importing it again dedups against those rows, so to get the
lines, purge that import's batch first (`purser admin purge-retired-rows`, see
the [CLI reference](../../reference/cli/)) and import again.

## CSV specifics

`/import/csv` auto-detects two layouts by header row: an **Amex activity export**
(`Date, Description, Amount`) of flat rows into a single account, and a
**Quicken CSV export** (`Date, Payee, Amount[, Category, Memo, Account, Clr,
Scheduled]`) where an `Account` column groups file accounts, a bracket
`Category` is a transfer, and the `Clr` (or `Cleared`) and `Scheduled` columns
drive the rules in the next section. When neither header matches, supply an
explicit `column_map` (`date`, `amount` and `payee`, plus optional `memo`,
`category`, `account`, `cleared` and `scheduled`); until then the preview
returns `422 csv_layout_unrecognized` with the detected columns so a UI can
offer a mapping form. Amounts are parsed as `Decimal` only (accountant
negatives like `(1,234.56)` are read; float-looking garbage is a clear 422).
CSV has no opening-balance convention, so mind any offset against a known
statement balance.

## Reminders and the cleared flag

Quicken can include **reminder** rows in a CSV export: upcoming bills and
deposits its register shows ahead of time, flagged in a `Scheduled` column.
They are not history. The planner skips them, counts them `skipped_scheduled`
in both the preview and the commit, and never creates a row for them — model a
recurring bill as a [schedule](../../how-to/schedules-and-bills/) instead.

The file's **cleared flag** decides each remaining row's status:

- QIF reads the `C` field: `*` or `c` is cleared, `X` or `R` reconciled, and
  no `C` means uncleared. CSV reads the `Clr` or `Cleared` column, ignoring
  case: `c`, `R`, `*`, `X`, `Y`, or the words cleared, reconciled, yes or true
  mark a cleared row; blank, `N`, `U`, or the words uncleared, no or false mean
  uncleared. Any other value is a `422`, never a guess.
- The flag counts **per file account**. In an account that marks at least one
  row cleared, an unmarked row is uncleared. An account with no marks at all —
  any bank-generated export, or a Quicken cash account nobody reconciles —
  carries no clearing information, so all of its rows count as cleared and
  import exactly as they always did. The same goes for a CSV with no cleared
  column.
- Cleared rows land `posted`. **Uncleared rows land `expected`** and are
  counted `uncleared`: the bank has not seen them yet (an uncashed check), so
  they stay out of the balance — which keeps the
  [balance-drift check](../../how-to/simplefin/#watch-accounts-converge) comparing
  like with like — appear in the [forecast](../../explanation/forecasting/) (an
  overdue one lands today), raise `bill.late` like any unmatched expectation,
  and are settled by the [matcher](../../explanation/matching/) when the bank's
  posted row arrives. They never land `pending`: a feed sync voids any
  imported pending row it no longer lists, wherever that row came from.
- A transfer lands `expected` only when no leg in the file is cleared; once
  either bank has seen it, it posts. A split transfer's one-leg entry follows
  its own leg, and opening balances always post.

The preview's counts show both numbers before you commit, and the import page
spells them out.

When a later export **marks** a row cleared that an earlier import left
`expected`, the commit posts that row in place instead of adding another, and
the preview and the result count it `promoted` (once per leg for a transfer,
which promotes when any of its legs is now marked). Only a mark promotes: an
account with no marks carries no clearing information, and neither does a CSV
without a cleared column. Promotion is limited to accounts without a feed — on
a feed-connected account the [matcher](../../explanation/matching/) settles the row
against the bank's posted row when it arrives, and posting it from the file as
well would count that charge twice. Schedule instances and feed rows are never
touched. Anything still expected can be posted by hand
(`POST /transactions/{id}/post`) or voided.

## Idempotency

Both formats are content-hash idempotent over a shared `qif:` namespace. Re-run
any import, or interleave manual imports with SimpleFIN syncs on one account, and
you get **zero** new rows — the same history in either format dedups, expected
rows included, and a reminder stays skipped.

Identical rows are not duplicates. Two coffees on the same day at the same
price are two rows in the file and two in the ledger: the second identical row
in a file is the second **occurrence** of that content, an identity of its own.
The count restarts in every file, so re-importing the file — or a later export
that overlaps it — lands on the same identities and adds nothing. The same
holds for transfers: two identical same-day transfers are two transfers, and so
is a same-day round trip between two accounts. If an older import left only one
row for such a pair, the next file that carries the pair adds the missing one,
once.

Both formats are raw-first: the verbatim upload is archived to `raw_fetches`
before any ledger write, and re-uploading an identical file archives nothing
new. The authoritative spec is
[`DESIGN.md`](https://github.com/mlamoure/purser/blob/main/DESIGN.md)
section 7.2.
