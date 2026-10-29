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

from typing import NotRequired, TypedDict

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
    # Fields the user's privacy defaults withheld from this answer (#437).
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
    # Fields the user's privacy defaults withheld from this answer (#437).
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
    # Fields the user's privacy defaults withheld from this answer (#437).
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
    # Fields the user's privacy defaults withheld from this answer (#437).
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
    events_truncated: bool

    # --------------------------------------------------------------------------
    # balance_history (#199)
    # --------------------------------------------------------------------------
    # Fields the user's privacy defaults withheld from this answer (#437).
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
    # honest — and never an intra-day row (decision 63). Null only when the
    # window holds no activity at all.
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
    # Fields the user's privacy defaults withheld from this answer (#437).
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
    # Fields the user's privacy defaults withheld from this answer (#437).
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
