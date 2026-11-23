"""structuredContent contracts for the widget-carrying tools (issues #73-#77).

Each widget tool returns ``Annotated[CallToolResult, <X>Result]``: FastMCP
derives the declared outputSchema from the TypedDict and validates
``structuredContent`` against it on every call, so drift between a payload
builder and its published contract fails loudly instead of silently.

Amount fields are decimal strings end-to-end (guardrail 1). List fields are
capped server-side with ``truncated`` flags (guardrail 6); full-resolution
series live behind ``purser://`` resources, where the token budget does not
apply.
"""

from typing import Any, NotRequired, TypedDict

# --------------------------------------------------------------------------
# forecast_balance (#73)
# --------------------------------------------------------------------------


class ForecastLowRange(TypedDict):
    """The low under the estimate range band (#247, decision 85): the
    pessimistic pass's minimum (low, on low_date) and the optimistic pass's
    minimum (high)."""

    low: str
    high: str
    low_date: str


class ForecastLow(TypedDict):
    date: str
    balance: str
    # Present only when an estimated event in the horizon has a band.
    range: NotRequired[ForecastLowRange]


class ForecastRange(TypedDict):
    low: str
    high: str


class ForecastEventOut(TypedDict):
    certainty: NotRequired[dict[str, Any]]
    date: str
    name: NotRequired[str]
    amount: NotRequired[str]
    balance_after: NotRequired[str]
    transaction_id: str | None
    # Present only when true (token budget, guardrail 6): expanded from the
    # rule beyond the materialized horizon / dated before today and still
    # due, landing on day 0 (#235).
    projected: NotRequired[bool]
    overdue: NotRequired[bool]
    # Only for an amount the user or an agent supplied (decision 80):
    # 'statement' (a recorded card statement balance) or 'override' (a
    # pinned instance). Absent = the rule's fixed amount or estimate.
    basis: NotRequired[str]
    # An estimate's band (#247): min/max of the newest settled amounts and
    # this one. Present only for a ranged estimated event.
    range: NotRequired[ForecastRange]


class ForecastCoverage(TypedDict):
    """What the projection is made of (#235). schedules_active == 0 means
    the line is flat by construction, not a quiet month. matches_pending
    counts instances left out as paid because an open match proposal names
    a live bank row already in the balance (decision 76);
    matches_pending_net is their net, present only when nonzero."""

    schedules_active: int
    instances: int
    projected: int
    suggestions_open: int
    matches_pending: int
    matches_pending_net: NotRequired[str]


class ForecastOmitted(TypedDict):
    """Every movement events does not list (#354): below materiality or
    past the budget. start_balance + Σ events.amount + net == end_balance."""

    count: int
    net: str


class ForecastResult(TypedDict):
    account: str
    as_of: str
    horizon_days: int
    start_balance: str
    end_balance: str
    min_balance: str | None
    low: ForecastLow
    coverage: ForecastCoverage
    # Fitted to the character budget, the low's event always included, in
    # date order (#354, decision 77).
    events: list[ForecastEventOut]
    # True only when the budget (not materiality) left a movement out.
    truncated: bool
    omitted: NotRequired[ForecastOmitted]
    daily_uri: str
    links: NotRequired[dict[str, str]]

    # --------------------------------------------------------------------------
    # list_recurring (#74)
    # --------------------------------------------------------------------------
    # Fields this answer withheld because the call asked (withhold; #437, #613).
    withheld: NotRequired[list[str]]


class RecurringChange(TypedDict):
    date: str
    delta: str


class RecurringChanges12Mo(TypedDict):
    count: int
    delta: str


class RecurringItem(TypedDict):
    schedule_id: str
    payee: str
    cadence: str
    amount: NotRequired[str]
    monthly_equiv: NotRequired[str]
    next_due: str | None
    source: str
    last_change: NotRequired[RecurringChange | None]


class RecurringResult(TypedDict):
    monthly_total: str
    annualized_total: str
    count: int
    changes_12mo: RecurringChanges12Mo
    items: list[RecurringItem]
    truncated: bool
    suggestions_open: int  # inbox rows awaiting a decision (#233)

    # --------------------------------------------------------------------------
    # net_worth series mode (#75)
    # --------------------------------------------------------------------------
    # Fields this answer withheld because the call asked (withhold; #437, #613).
    withheld: NotRequired[list[str]]


class NetWorthChange(TypedDict):
    period: str
    delta: str
    pct: str | None


class NetWorthPoint(TypedDict):
    date: str
    assets: str
    liabilities: str
    net: str


class AttributionBucket(TypedDict):
    bucket: str
    delta: str


class NetWorthSeriesResult(TypedDict):
    current: str
    change: NetWorthChange
    series: list[NetWorthPoint]
    attribution: list[AttributionBucket]
    series_uri: str
    links: NotRequired[dict[str, str]]

    # --------------------------------------------------------------------------
    # spending_summary baseline mode (#76)
    # --------------------------------------------------------------------------
    # Fields this answer withheld because the call asked (withhold; #437, #613).
    withheld: NotRequired[list[str]]


class SpendingMover(TypedDict):
    payee: str
    delta: str
    count: int
    baseline_count: int


class SpendingBaselineRow(TypedDict):
    key: str
    amount: str
    baseline: str
    delta: str
    top_payee_mover: NotRequired[SpendingMover | None]


class SpendingBaselineResult(TypedDict):
    group_by: str
    period: str
    baseline: str
    grand_total: str
    baseline_total: str
    rows: list[SpendingBaselineRow]
    truncated: bool
    links: NotRequired[dict[str, str]]

    # --------------------------------------------------------------------------
    # whats_new (#77)
    # --------------------------------------------------------------------------
    # Fields this answer withheld because the call asked (withhold; #437, #613).
    withheld: NotRequired[list[str]]


class NotableTransaction(TypedDict):
    payee: str | None
    # A payee-less row's label (#437).
    description: NotRequired[str | None]
    amount: NotRequired[str]
    transaction_id: str


class DigestTransactions(TypedDict):
    count: int
    total: str
    notable: list[NotableTransaction]
    truncated: bool


class DigestVanishedPendings(TypedDict):
    # Pending rows the bank dropped without a posted successor (#282): all
    # counted, those above the materiality bar itemized (largest first).
    count: int
    notable: list[NotableTransaction]
    truncated: bool


class DigestMatch(TypedDict):
    name: NotRequired[str]
    amount: NotRequired[str]
    variance: NotRequired[str]
    match_id: str


class DigestPriceChange(TypedDict):
    payee: str
    delta: NotRequired[str]
    schedule_id: str


class DigestForecastFlag(TypedDict):
    account: str
    low: str | None
    date: str


class DigestBalanceDrift(TypedDict):
    account: str
    delta: str
    balance_date: str | None
    # The ledger basis the delta is measured on — pending_inclusive or
    # posted_only (the bank's balance leaves pendings out; decision 64).
    # Present only when the event recorded one (guardrail 6).
    basis: NotRequired[str]
    # True when ``basis`` is the account's admin-set pin rather than learned
    # from its sync history (decision 83); absent otherwise.
    pinned: NotRequired[bool]


class DigestStalePrice(TypedDict):
    # A symbol a manual position holds whose close turned stale (older than
    # the staleness threshold) or that has no close at all (price_as_of
    # null): one line per price.stale event (#390, decision 89).
    symbol: str
    price_as_of: str | None


class UpcomingItem(TypedDict):
    date: str
    name: NotRequired[str]
    amount: NotRequired[str]
    transaction_id: str


class DigestUpcoming(TypedDict):
    count: int
    total: str
    items: list[UpcomingItem]
    truncated: bool


class DigestSync(TypedDict):
    failures: int


class DigestUnpricedHolding(TypedDict):
    # A feed position the feed sends with no value (a plan-only fund) that
    # no stored close prices: one line per holding.unpriced event (#392,
    # decision 92). record_price(symbol, date, close) under its ticker
    # values it, unless unpriceable.
    account: str
    symbol: str
    quantity: str | None
    as_of: str | None  # the date of the set it sits in
    # Present only when true: the feed symbol is no ticker (a description),
    # so no recorded close can value it; never advise record_price.
    unpriceable: NotRequired[bool]


class DigestOpenPeriod(TypedDict):
    # A brokerage account switched back to the feed whose manual period no
    # fetch has ended (#422): its frozen hand-tracked set still counts.
    account: str
    since: str | None  # the day of the switch


class DigestFeedLogin(TypedDict):
    # A bank login reporting a con.* issue (con.auth: log in again at the
    # bank through SimpleFIN Bridge) on the latest sync (#480).
    institution: str | None
    code: str | None
    message: str | None


class DigestFeedFailing(TypedDict):
    # A mapped account reporting an act.* issue (act.failed, act.missingdata:
    # usually clears on a later sync) on the latest sync.
    account: str | None
    institution: str | None
    code: str | None
    message: str | None


class DigestFeedStale(TypedDict):
    # A mapped account whose bank balance date is older than feed_stale_days:
    # the bank has stopped updating (often a login that needs attention).
    account: str | None
    institution: str | None
    balance_date: str | None
    days: int | None


class DigestFeed(TypedDict):
    # Open now, from each connection's latest sync — repeated every call
    # until fixed, unlike the event-driven sections.
    needs_login: list[DigestFeedLogin]
    failing: list[DigestFeedFailing]
    stale: list[DigestFeedStale]
    truncated: bool


class WhatsNewResult(TypedDict):
    cursor: str | None
    transactions: DigestTransactions
    matches: list[DigestMatch]
    matches_truncated: bool
    price_changes: list[DigestPriceChange]
    price_changes_truncated: bool
    forecast_flags: list[DigestForecastFlag]
    balance_drifts: list[DigestBalanceDrift]
    balance_drifts_truncated: bool
    pendings_vanished: DigestVanishedPendings
    stale_prices: list[DigestStalePrice]
    stale_prices_truncated: bool
    unpriced_holdings: list[DigestUnpricedHolding]
    unpriced_holdings_truncated: bool
    open_manual_periods: list[DigestOpenPeriod]
    open_manual_periods_truncated: bool
    upcoming: DigestUpcoming
    sync: DigestSync
    feed: DigestFeed
    events_truncated: bool

    # --------------------------------------------------------------------------
    # balance_history (#199)
    # --------------------------------------------------------------------------
    # Fields this answer withheld because the call asked (withhold; #437, #613).
    withheld: NotRequired[list[str]]


class BalanceHistoryChange(TypedDict):
    period: str
    delta: str
    pct: str | None


class BalanceHistoryExtreme(TypedDict):
    date: str
    balance: str


class BalanceHistoryDensity(TypedDict):
    interval_days: int
    points: int


class BalanceHistorySeries(TypedDict):
    """Parallel arrays, not a list of objects: at 31 points the repeated
    `{"date": …, "balance": …}` keys cost ~600 chars of pure overhead and
    push the payload past guardrail 6's budget. Same information, index-
    aligned, and it is the shape a chart wants anyway."""

    dates: list[str]
    balances: list[str]


class BalanceHistoryMover(TypedDict):
    date: str
    payee: str | None
    amount: NotRequired[str]
    transaction_id: str


class BalanceHistoryResult(TypedDict):
    account: str
    start: str
    end: str
    currency: str
    method: str
    current: str
    change: BalanceHistoryChange
    # End-of-day balances over EVERY day with activity in the window, never
    # the sampled subset — that is what makes publishing a downsampled series
    # honest — and never an intra-day row (decision 63). On the series' own
    # basis (`method`, decision 129): a brokerage or loan account also ranks
    # its snapshot or valuation days. Null only when the window holds no
    # activity at all.
    low: BalanceHistoryExtreme | None
    high: BalanceHistoryExtreme | None
    density: BalanceHistoryDensity
    transactions: int
    series: BalanceHistorySeries
    movers: list[BalanceHistoryMover]
    series_uri: str

    # --------------------------------------------------------------------------
    # list_holdings / record_position (#390, decisions 88/89)
    # --------------------------------------------------------------------------
    # Fields this answer withheld because the call asked (withhold; #437, #613).
    withheld: NotRequired[list[str]]


class HoldingPosition(TypedDict):
    """One itemized position. Keys that would be null are left out (guardrail
    6), except market_value: null there means "no price", never 0."""

    symbol: str
    quantity: str | None  # null when the feed sent a value without a share count
    market_value: str | None
    # Manual rows, and feed rows the feed values at 0 (decision 92): the close
    # the value uses and its date.
    price: NotRequired[str]
    price_as_of: NotRequired[str]
    # A feed row valued from a stored close: that close's source (manual or
    # the provider).
    price_source: NotRequired[str]
    # Present only when true: the close is older than the staleness threshold.
    stale: NotRequired[bool]
    # Present only when true: a close, but the value does not fit an amount
    # (market_value null). Check the share count; no close fixes it.
    value_out_of_range: NotRequired[bool]
    # Present only when true: a feed row with no value whose symbol is no
    # ticker (a description); never record a price for it.
    unpriceable: NotRequired[bool]
    # Present only on a hand-tracked position whose trade's cash has not
    # landed yet: that cash (a sale's proceeds +, a funded buy's cost -),
    # included in market_value until the cash date (decision 104).
    in_transit: NotRequired[str]
    # Present only when the cost basis is known.
    unrealized_gain: NotRequired[str]
    # One named account's pages only: the total basis, null = unknown.
    cost_basis: NotRequired[str | None]


class HoldingsAccount(TypedDict):
    account: str
    source: str  # feed | manual: where the newest set's rows come from
    as_of: str | None  # the newest snapshot set's date; null = none yet
    market_value: str  # the set's total (what net worth adds, unless closed)
    positions: int  # positions held in that set
    stale_price: int  # of them, priced on a stale close
    no_price: int  # of them, with no price at all (adds nothing)
    # Of them, with a close but a value too large to store (adds nothing):
    # the share count is the likely error, not a missing close.
    value_out_of_range: int
    # Of them, feed rows with no value whose symbol is no ticker (the feed
    # sent a description): no recorded close can value them.
    unpriceable: int
    # The largest, fitted to the response budget; for one named account,
    # one page of its positions (in symbol order) after the call's cursor.
    top: list[HoldingPosition]
    # Present only when true: a named closed account, its last set shown but
    # left out of totals (net worth no longer counts it).
    closed: NotRequired[bool]
    # Present only when the account has unvested award units (#393): per
    # symbol, informational, never inside market_value, totals or net worth.
    unvested: NotRequired[list["HoldingsUnvested"]]
    # Symbols with unvested units beyond those itemized (see list_awards).
    unvested_more: NotRequired[int]


class HoldingsUnvested(TypedDict):
    symbol: str
    units: str
    value: str | None
    next_vest_date: str | None
    next_vest_units: str | None
    unscheduled: str
    complete: bool


class HoldingsTotals(TypedDict):
    market_value: str


class HoldingsResult(TypedDict):
    accounts: list[HoldingsAccount]
    # Every open account's total; with account named, that account's own
    # (0.00 when it is closed).
    totals: HoldingsTotals
    # True when a held position (or a whole account) is not itemized; call
    # list_holdings(account=...) to page through one account's positions.
    # With account named: a later page remains.
    truncated: bool
    # One named account only: the cursor of the next page (the last symbol
    # listed; pages go in symbol order), null on the last.
    next_cursor: NotRequired[str | None]
    # Fields this answer withheld because the call asked (withhold; #437, #613).
    withheld: NotRequired[list[str]]


class RecordPositionResult(TypedDict):
    account: str
    symbol: str
    position_id: str
    event_id: str
    created: bool  # this call opened the position
    as_of: str
    quantity: str  # held on as_of after this call
    quantity_today: str  # held today (later events may differ from as_of)
    price: str | None  # the close in effect today
    price_as_of: str | None
    market_value: str | None  # today's value; null without a price
    stale_price: bool
    # Present when the position adds nothing to net worth: no price yet
    # (until a close arrives: record_price, or the daily provider fetch), or
    # a value that does not fit an amount (value_out_of_range: check the
    # share count; no close fixes it).
    warning: NotRequired[str]
    # Present only when true: a close, but the value does not fit an amount.
    value_out_of_range: NotRequired[bool]


# --------------------------------------------------------------------------
# record_sale (#390 phase 1b, decision 103)
# --------------------------------------------------------------------------


class SalePosition(TypedDict):
    position_id: str  # for reverse_position_event / reopen_position
    symbol: str
    quantity_before: str  # held today before the sale
    quantity_after: str  # held today after it


class SaleCash(TypedDict):
    # What lands on the account, to the cent; omitted (and named in
    # `withheld`) while single amounts are withheld (decision 91).
    amount: NotRequired[str]
    settle_on: str
    transaction_id: str | None  # the cash row (Purser's, or the linked one); null on a dry run


class SaleCashFound(TypedDict):
    """The row the double-count check found: the account already shows this
    cash. Without force_cash a real call is refused (cash_already_recorded);
    with it, this is the row the cash posts over."""

    transaction_id: str
    date: str
    amount: NotRequired[str]  # omitted while single amounts are withheld


class SaleLots(TypedDict):
    dispositions: int
    unallocated: str
    terms: list[str]
    cost_basis: NotRequired[str | None]  # null when any lot's basis is unknown
    proceeds: NotRequired[str | None]
    realized_gain: NotRequired[str | None]


class RecordSaleResult(TypedDict):
    position: SalePosition
    cash: SaleCash | None  # null when proceeds_to is none
    linked: bool  # the cash is a row the account already showed (transaction_id)
    dry_run: bool
    event_id: str | None  # the sell event; null on a dry run
    # Dry runs only: the row the double-count check found, or null.
    already_recorded: NotRequired[SaleCashFound | None]
    # With force_cash: the row of the same amount the cash was (or, on a dry
    # run, would be) posted over, so the override is never silent; else null.
    cash_probe_overridden: SaleCashFound | None
    # Fields this answer withheld because the call asked (withhold; #437, #613).
    withheld: NotRequired[list[str]]
    # The lots it drew on (#393): count, terms and the summed basis /
    # proceeds / gain where known.
    lots: NotRequired[SaleLots | None]


# --------------------------------------------------------------------------
# record_split (#624, decision 127)
# --------------------------------------------------------------------------


class SplitPosition(TypedDict):
    position_id: str
    symbol: str
    quantity_before: str  # held the day before the effective date
    quantity_after: str  # what those shares became (before any cash in lieu)


class SplitCashInLieu(TypedDict):
    event_id: str  # the sell of the fraction, reversed with the split
    fraction: str  # the fractional share paid in cash
    # The cash, to the cent; omitted (and named in `withheld`) while single
    # amounts are withheld (decision 91).
    amount: NotRequired[str]
    settle_on: str
    transaction_id: str | None  # the cash row (Purser's, or the linked one)
    linked: bool  # the cash is a row the account already showed
    lots: NotRequired[SaleLots | None]  # the lots the fraction drew on (#393)


class RecordSplitResult(TypedDict):
    position: SplitPosition
    ratio: str  # numerator:denominator, new shares for old
    effective_date: str
    event_id: str  # the split event (reverse_position_event undoes it)
    cash_in_lieu: SplitCashInLieu | None  # null when the broker paid none
    withheld: NotRequired[list[str]]


# --------------------------------------------------------------------------
# reverse_position_event (#390 phase 1b, decision 103)
# --------------------------------------------------------------------------


class ReversedPosition(TypedDict):
    position_id: str
    symbol: str
    quantity_after: str  # held today after the reversal


class ReversedCash(TypedDict):
    transaction_id: str  # the trade's cash row (Purser's, or the linked one)
    undone_by: str  # the correction's replacement that undid it


class ReversePositionEventResult(TypedDict):
    reverses_id: str  # the event reversed
    reversal_id: str  # the reversal row written
    kind: str
    occurred_on: str
    position: ReversedPosition
    cash: ReversedCash | None  # null when the event moved no cash
    # A split's cash-in-lieu sale, reversed with it (#624); absent otherwise.
    cash_in_lieu: NotRequired[ReversedCash | None]


class ReopenPositionResult(TypedDict):
    position_id: str
    symbol: str
    account: str
    quantity: str  # held today (a closed position holds nothing)


# --------------------------------------------------------------------------
# Lots, captures and awards (#393, decision 107)
# --------------------------------------------------------------------------


class LotAllocationInput(TypedDict):
    """One lot a sale or transfer out draws on: ``lot`` is the lot id or the
    broker's lot reference (matched by blind index), or the word
    ``unallocated`` for shares outside every lot."""

    lot: str
    quantity: str


class CaptureLotInput(TypedDict):
    acquired_on: str  # YYYY-MM-DD
    quantity: str  # remaining on as_of
    external_ref: NotRequired[str]  # the broker's lot reference: the lot's identity
    plan: NotRequired[str]
    ordinal: NotRequired[int]  # tells lots of one date and plan apart without a reference
    basis_per_share: NotRequired[str]
    cost_basis: NotRequired[str]  # total basis of the remaining shares
    acquisition: NotRequired[str]


class CapturePositionInput(TypedDict):
    symbol: str
    lots: list[CaptureLotInput]
    name: NotRequired[str]


class CaptureVestInput(TypedDict):
    vest_date: str
    quantity: str
    ordinal: NotRequired[int]


class CaptureAwardInput(TypedDict):
    grant_ref: str
    symbol: str
    granted_on: str
    granted_quantity: str
    cancelled_quantity: NotRequired[str]
    vested_quantity: NotRequired[str]
    plan: NotRequired[str]
    schedule: NotRequired[list[CaptureVestInput]]


class LotLine(TypedDict):
    lot_id: str
    acquired_on: str
    acquisition: str
    status: str  # open | closed | voided | reversed
    remaining: str
    term_if_sold_today: str  # short | long
    basis_per_share: NotRequired[str | None]  # null: unknown; withheld with single amounts
    plan: NotRequired[str | None]
    external_ref: NotRequired[str | None]
    remaining_basis: NotRequired[str | None]
    vest_linked: NotRequired[bool]


class LotFreshness(TypedDict):
    as_of: str
    captured_at: str
    completeness: str


class LotsSummary(TypedDict):
    position_id: str | None
    symbol: str
    quantity: str
    tracks_lots: bool
    open_lots: int
    lots_quantity: str
    unallocated: str
    basis_unknown_quantity: str
    cost_basis: str | None  # null unless every share has a known basis
    capture: LotFreshness | None  # capture freshness (not the quote's)
    price_as_of: str | None  # quote freshness


class ListLotsResult(TypedDict):
    summary: LotsSummary
    lots: list[LotLine]
    total: int
    next_cursor: str | None
    withheld: NotRequired[list[str]]


class PlanItem(TypedDict):
    kind: str  # a change kind or a conflict code
    symbol: NotRequired[str]
    row: NotRequired[int | None]
    lot_id: NotRequired[str | None]
    quantity: NotRequired[str | None]
    message: NotRequired[str]


class CaptureResult(TypedDict):
    dry_run: bool
    applied: bool
    retry: bool  # the exact request was applied before: nothing written
    repeat_of: str | None  # the same holdings observed again: freshness only
    plan_hash: str  # pass as expect_hash to commit this plan
    capture_id: str | None
    counts: dict[str, int]
    conflicts: int
    first_changes: list[PlanItem]
    first_conflicts: list[PlanItem]
    unchanged: int
    stale_symbols: list[str]
    skipped_symbols: list[str]
    withheld: NotRequired[list[str]]


class UnvestedLine(TypedDict):
    symbol: str
    units: str  # remaining unvested units (scheduled ones where remaining is unknown)
    value: str | None  # informational; never in net worth
    price_as_of: str | None
    next_vest_date: str | None
    next_vest_units: str | None
    unscheduled: str  # of the units, those with no vest date yet
    complete: bool  # false: some award's remaining units are unknown


class AwardLine(TypedDict):
    award_id: str
    symbol: str
    granted_on: str
    granted: str
    cancelled: str
    vested: str
    unvested: str
    unvested_known: bool
    unscheduled: str | None  # unvested units with no vest date; null: unknown
    next_vest_date: str | None
    next_vest_units: str | None
    due_unrecorded: int
    grant_ref: NotRequired[str | None]
    plan: NotRequired[str | None]


class AwardVestLine(TypedDict):
    vest_id: str
    vest_date: str
    ordinal: int
    quantity: str
    status: str
    lot_id: str | None


class ListAwardsResult(TypedDict):
    unvested: list[UnvestedLine]  # first page only, largest first
    unvested_truncated: bool  # more symbols than fit; GET /accounts/{id}/awards has all
    awards: list[AwardLine]
    vests: NotRequired[list[AwardVestLine]]  # one award named: a page of its schedule
    total: int
    next_cursor: str | None
    capture: LotFreshness | None
    note: str
    withheld: NotRequired[list[str]]


class RecordVestResult(TypedDict):
    award_id: str
    vest_id: str | None
    lot_id: str | None
    event_id: str | None
    position_id: str | None
    linked: bool
    already_recorded: bool
    dry_run: bool
    gross: str
    withheld_units: str
    net: str
    unvested_after: str | None


class TermTotalsOut(TypedDict):
    disposals: int
    quantity: str
    proceeds: str
    cost_basis: str
    realized_gain: str
    unknown_basis_quantity: str
    unknown_proceeds_quantity: str


class RealizedGainsResult(TypedDict):
    account: str
    year: int
    symbol: str | None
    short: TermTotalsOut
    long: TermTotalsOut
    unallocated_quantity: str
    note: str


class LotChangeResult(TypedDict):
    lot_id: str
    position_id: str
    status: str
    remaining: str
    basis_per_share: str | None
    cost_basis_known: bool


class TransferOutResult(TypedDict):
    position_id: str
    symbol: str
    event_id: str
    quantity_after: str
    lots: int


class VestStatusResult(TypedDict):
    vest_id: str
    award_id: str
    status: str
