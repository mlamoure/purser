"""SQLAlchemy ORM models.

The schema itself is owned by hand-written Alembic migrations (DESIGN.md §3 is
authoritative); these models map the tables workstreams actually touch and
grow incrementally with each issue.
"""

import uuid
from datetime import date, datetime
from datetime import date as date_type
from decimal import Decimal
from typing import Any

from sqlalchemy import ForeignKey, Numeric, text

# Aliased: the Memory model maps a column literally named `text`, which would
# otherwise shadow sqlalchemy.text() inside that class body.
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import ARRAY, BYTEA, DATEMULTIRANGE, JSONB, TEXT
from sqlalchemy.dialects.postgresql import UUID as UUIDType
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import UserDefinedType

from purser.crypto import EncryptedJSON, EncryptedText


class LtreeType(UserDefinedType[str]):
    """Minimal ltree mapping; values travel as dotted-path strings."""

    cache_ok = True

    def get_col_spec(self, **kw: Any) -> str:
        return "LTREE"


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    # Unique regardless of case (index on lower(email), migration 0041);
    # stored as entered. Look users up with service.email_matches.
    email: Mapped[str]
    display_name: Mapped[str | None]
    role: Mapped[str] = mapped_column(server_default="member")
    # An SSO link is the pair (issuer, subject), unique per pair (#429, D1).
    # A legacy link may carry a NULL issuer until its next sign-in records it.
    oidc_issuer: Mapped[str | None]
    oidc_subject: Mapped[str | None]
    password_hash: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class ApiToken(Base):
    __tablename__ = "api_tokens"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str]
    kind: Mapped[str] = mapped_column(server_default="api")  # api | session
    token_hash: Mapped[str]
    scopes: Mapped[list[str]] = mapped_column(ARRAY(TEXT))
    expires_at: Mapped[datetime | None]
    last_used_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class WebhookSubscription(Base):
    __tablename__ = "webhook_subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    url: Mapped[str]
    event_types: Mapped[list[str]] = mapped_column(ARRAY(TEXT))
    secret_enc: Mapped[bytes | None] = mapped_column(BYTEA)
    active: Mapped[bool] = mapped_column(server_default=text("true"))
    # Fan-out watermark over events.at (#219, decision 54): scans start here,
    # so a pass never re-walks the whole events table and a NEW subscription
    # (DEFAULT now()) receives no historical backlog.
    cursor_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    subscription_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("webhook_subscriptions.id"))
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id"))
    status: Mapped[str] = mapped_column(server_default="pending")
    attempts: Mapped[int] = mapped_column(server_default=text("0"))
    next_attempt_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    last_attempt_at: Mapped[datetime | None]
    response_status: Mapped[int | None]
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    actor_token_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("api_tokens.id"))
    action: Mapped[str]
    entity_type: Mapped[str]
    entity_id: Mapped[uuid.UUID | None]
    # Narrative payloads are encrypted (#137): they carry names/descriptions.
    before_after: Mapped[dict[str, Any] | None] = mapped_column("before_after_enc", EncryptedJSON)
    # read/write mark call rows (one per HTTP request / MCP tool call, with
    # detail); change marks per-entity before/after rows (issue #122).
    kind: Mapped[str] = mapped_column(server_default="change")
    request_id: Mapped[uuid.UUID | None]
    detail: Mapped[dict[str, Any] | None] = mapped_column("detail_enc", EncryptedJSON)
    # Plaintext skeleton id lifted out of before_after.after (#137): the
    # register's category-provenance badge filters on it (a JSONB path over
    # ciphertext can't). Populated at write time by audit.record().
    replacement_id: Mapped[uuid.UUID | None]
    # On an MCP call row: the fields the agent asked for with `include`
    # beyond the user's privacy defaults (#437). Names only, so plaintext.
    disclosed: Mapped[list[str] | None] = mapped_column(ARRAY(TEXT))


class Connection(Base):
    __tablename__ = "connections"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    provider: Mapped[str] = mapped_column(server_default="simplefin")
    access_url_enc: Mapped[bytes | None] = mapped_column(BYTEA)
    status: Mapped[str] = mapped_column(server_default="active")
    last_fetch_at: Mapped[datetime | None]
    # Historical backfill (issue #126): raw_fetches doesn't store request
    # params, so "how far back have we asked" is unrecoverable — track it here.
    # backfill_cursor is the oldest start-date fetched so far (NULL until the
    # first backfill run); backfill_complete latches once history is exhausted.
    backfill_cursor: Mapped[date | None]
    backfill_complete: Mapped[bool] = mapped_column(server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class RawFetch(Base):
    """APPEND-ONLY (trigger-enforced). Never UPDATE or DELETE through this
    model — there is deliberately no code path that mutates it.

    ``payload_enc`` is Fernet over the canonical plaintext — the sort_keys
    JSON envelope for SimpleFIN fetches, the verbatim uploaded file for
    manual QIF/CSV imports (#127, decision 53) — and ``content_hash`` is
    SHA-256 over that same plaintext, so idempotency is independent of the
    key. ``source`` discriminates; manual rows have no ``connection_id``
    (CHECK-enforced both ways). Read feed rows via ``crypto.decrypt_json``,
    manual rows via ``crypto.decrypt``.
    """

    __tablename__ = "raw_fetches"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    connection_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("connections.id"))
    fetched_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    content_hash: Mapped[str]
    payload_enc: Mapped[bytes] = mapped_column(BYTEA)
    source: Mapped[str] = mapped_column(server_default=text("'simplefin'"))


class EncryptionCanary(Base):
    """Single row (id is CHECKed true) binding PURSER_ENCRYPTION_KEY to this
    database: startup decrypts the sentinel and fails closed on a mismatch.
    ``key_acknowledged_at`` records the wizard's "I recorded my key" step."""

    __tablename__ = "encryption_canary"

    id: Mapped[bool] = mapped_column(primary_key=True, server_default=text("true"))
    sentinel_enc: Mapped[bytes] = mapped_column(BYTEA)
    key_fingerprint: Mapped[str]
    key_acknowledged_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class EncryptionKeys(Base):
    """Wrapped data-encryption keys (#137, decision 36). One row per purpose
    (``blind_index`` / ``content_hash``); ``key_enc`` is a random 32-byte DEK
    Fernet-wrapped by PURSER_ENCRYPTION_KEY. Loaded lazily + cached by
    ``purser.crypto._load_dek``; rotation re-wraps the two rows in place, so
    every blind index and content hash stays byte-identical."""

    __tablename__ = "encryption_keys"

    purpose: Mapped[str] = mapped_column(primary_key=True)
    key_enc: Mapped[bytes] = mapped_column(BYTEA)
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class Schedule(Base):
    __tablename__ = "schedules"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column("name_enc", EncryptedText)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id"))
    payee_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("payees.id"))
    category_path: Mapped[str | None] = mapped_column(LtreeType)
    # Transfer schedules (#232, decision 56): the other real account. Set
    # -> instances are two-leg expected transfers; NULL -> flat instances
    # against category_path. CHECK-enforced: never both.
    counterparty_account_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("accounts.id"))
    rrule: Mapped[str]
    # The RRULE anchor (#231, decision 55): every expansion counts from it, so
    # anchor-sensitive rules (no BYMONTHDAY/BYDAY) are stable across
    # materializations and never drift to the row's creation time.
    dtstart: Mapped[date]
    amount_type: Mapped[str]
    amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    estimate_basis: Mapped[str | None]
    currency: Mapped[str] = mapped_column(server_default="USD")
    # Cache: the earliest live expected instance's date (an overdue one
    # counts — it is still due), else the next rrule occurrence. Refreshed by
    # materialization and by every instance edit (#231).
    next_due: Mapped[date | None]
    autopost_policy: Mapped[str] = mapped_column(server_default="wait_for_match")
    source: Mapped[str]
    status: Mapped[str] = mapped_column(server_default="active")
    ended_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class Suggestion(Base):
    """A thing Purser thinks the user should do (#233, DESIGN decision 57):
    a typed draft of the target object plus the state the target must not
    carry — evidence, confidence, a stable fingerprint, snooze, a scoped
    rejection, and what accepting produced. One row per fingerprint, ever:
    re-detection upserts the open row; a rejected row is durable suppression.
    ``draft``/``evidence``/``decision_note`` are encrypted (narrative layer,
    #137); the skeleton (kind, status, fingerprint, ids) stays plaintext."""

    __tablename__ = "suggestions"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    kind: Mapped[str]
    status: Mapped[str] = mapped_column(server_default="open")
    fingerprint: Mapped[str]
    source: Mapped[str]
    detector: Mapped[str | None]
    detector_version: Mapped[int | None]
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    subject_account_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("accounts.id"))
    subject_payee_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("payees.id"))
    counterparty_account_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("accounts.id"))
    draft: Mapped[dict[str, Any]] = mapped_column("draft_enc", EncryptedJSON)
    evidence: Mapped[dict[str, Any] | None] = mapped_column("evidence_enc", EncryptedJSON)
    snooze_until: Mapped[date_type | None]
    seen_count: Mapped[int] = mapped_column(server_default="1")
    last_seen_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    decided_at: Mapped[datetime | None]
    decided_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("api_tokens.id"))
    decision_note: Mapped[str | None] = mapped_column("decision_note_enc", EncryptedText)
    reject_scope: Mapped[str | None]
    result_entity_type: Mapped[str | None]
    result_entity_id: Mapped[uuid.UUID | None]
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class Payee(Base):
    __tablename__ = "payees"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column("name_enc", EncryptedText)
    # Blind index over normalize_payee(name) (#137): a non-unique plaintext
    # index that replaces the old normalized_name column, so payee identity
    # (resolve_payee, register payee filter, schedule detection) resolves by
    # equality without decrypting. Callers compute crypto.blind_index(
    # core.normalize_payee(name)).
    name_bidx: Mapped[str]
    # Payee ALIASING (#174, decision 46): a retroactive `rename` rule points
    # this payee at the friendly one. Reads resolve through the chain and
    # payee_memory aggregates over the resolved id, so consolidation
    # strengthens memory retroactively — where re-pointing
    # transactions.payee_id could not, migration 0005 having made that column
    # immutable on posted rows. Reversible: clear it.
    merged_into_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("payees.id"))


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    date: Mapped[date]
    payee_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("payees.id"))
    description: Mapped[str | None] = mapped_column("description_enc", EncryptedText)
    status: Mapped[str] = mapped_column(server_default="pending")
    origin: Mapped[str]
    content_hash: Mapped[str | None]
    schedule_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("schedules.id"))
    # Schedule instances (#231, decision 55): ``occurrence_date`` is the rule
    # slot the instance fills — materialization is idempotent on
    # (schedule_id, occurrence_date), so editing ``date`` ("landed two days
    # early") never re-materializes a duplicate. A user skip (void) keeps the
    # slot; a rule-edit/pause resync releases it (sets NULL) so the slot can
    # come back. ``schedule_pinned_at`` marks a token-bearing edit: pinned
    # instances are never amount-refreshed or voided by rule changes.
    occurrence_date: Mapped[date_type | None]
    schedule_pinned_at: Mapped[datetime | None]
    source_fetch_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("raw_fetches.id"))
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("transactions.id"))
    reverses_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("transactions.id"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("api_tokens.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))

    postings: Mapped[list["Posting"]] = relationship(back_populates="transaction")
    payee: Mapped[Payee | None] = relationship()


class Posting(Base):
    __tablename__ = "postings"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    transaction_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("transactions.id"))
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id"))
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 4))
    currency: Mapped[str] = mapped_column(server_default="USD")
    memo: Mapped[str | None] = mapped_column("memo_enc", EncryptedText)

    transaction: Mapped[Transaction] = relationship(back_populates="postings")
    account: Mapped["Account"] = relationship()


class HoldingsSnapshot(Base):
    __tablename__ = "holdings_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id"))
    as_of: Mapped[date]
    symbol: Mapped[str]
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    cost_basis: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    market_value: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    lot_id: Mapped[uuid.UUID | None]  # v2 hedge; always NULL in v1
    raw_fetch_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("raw_fetches.id"))
    # Manually tracked holdings (#390, decision 88): ``feed`` rows come from
    # SimpleFIN; ``manual`` rows are the projection of a position's events and
    # prices (purser.holdings.projection), one per (account, date, position),
    # carrying the close they used and its date. An account's rows are one
    # source at a time (accounts.holdings_source).
    source: Mapped[str] = mapped_column(server_default="feed")
    position_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("positions.id"))
    price: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    price_as_of: Mapped[date | None]


class Position(Base):
    """One symbol held in one manually tracked brokerage account (#390,
    decision 88). Its share count is the history of its ``position_events``;
    at most one OPEN position per (account, symbol) (partial unique index).
    ``opened_on`` is the earliest event date (a reversal never moves it
    later; the price job reads the held span from the effective events);
    ``closed_at`` is set only while the position holds nothing."""

    __tablename__ = "positions"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id"))
    symbol: Mapped[str]
    name: Mapped[str | None]
    opened_on: Mapped[date]
    closed_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class PositionEvent(Base):
    """APPEND-ONLY (trigger-enforced): a correction is a reversal row
    (``reverses_id``), never an edit. ``quantity`` is absolute for
    ``set_quantity`` and the delta for ``buy``/``sell``. ``created_at`` is
    ``clock_timestamp()``, so events recorded in one transaction keep their
    order; the timeline orders by ``(occurred_on, created_at, id)``."""

    __tablename__ = "position_events"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    position_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("positions.id"))
    occurred_on: Mapped[date]
    kind: Mapped[str]  # set_quantity | buy | sell
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    price: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    fees: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    cost_basis: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    reverses_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("position_events.id"))
    replaces_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("position_events.id"))
    origin: Mapped[str]  # manual | agent | import
    note: Mapped[str | None] = mapped_column("note_enc", EncryptedText)
    source_note: Mapped[str | None] = mapped_column("source_note_enc", EncryptedText)
    actor_token_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("api_tokens.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=text("clock_timestamp()"))


class SecurityPrice(Base):
    """A daily close for one symbol from one source (#390, decision 89):
    ``manual`` (recorded by a person or an agent) or a provider. Feed-implied
    prices are computed on read and never stored."""

    __tablename__ = "security_prices"

    symbol: Mapped[str] = mapped_column(primary_key=True)
    price_date: Mapped[date] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(primary_key=True)
    close: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    fetched_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class PriceAnswered(Base):
    """The date ranges a provider has answered for good for one symbol
    (#390, decision 89): closes, or a definitive "nothing" (an empty answer
    or a not-found for dates at least 3 days old). They are the coverage
    ``core.fetch_windows`` subtracts from what the positions need, so a range
    the provider has nothing for (before a listing, a weekend after a
    sell-out, an unknown ticker) is asked for once, and a gap between two
    answered ranges is still asked for. Read and widened through SQL
    (``prices._answered`` / ``_record_answered``)."""

    __tablename__ = "price_answered"

    symbol: Mapped[str] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(primary_key=True)
    answered: Mapped[Any] = mapped_column(DATEMULTIRANGE)
    updated_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class Valuation(Base):
    __tablename__ = "valuations"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id"))
    as_of: Mapped[date]
    value: Mapped[Decimal] = mapped_column(Numeric(20, 4))
    source: Mapped[str]


class NetWorthDaily(Base):
    __tablename__ = "net_worth_daily"

    as_of: Mapped[date] = mapped_column(primary_key=True)
    currency: Mapped[str] = mapped_column(primary_key=True, server_default="USD")
    assets: Mapped[Decimal] = mapped_column(Numeric(20, 4))
    liabilities: Mapped[Decimal] = mapped_column(Numeric(20, 4))
    net_worth: Mapped[Decimal] = mapped_column(Numeric(20, 4))
    computed_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class AccountRunningBalance(Base):
    """Derived running balance per (real account, chain-head pending|posted
    transaction) — decision 37. Rebuildable at any time from postings; never
    authoritative. ``date``/``created_at`` are ordering-key copies for
    join-free register and as-of lookups."""

    __tablename__ = "account_running_balances"

    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    transaction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("transactions.id", ondelete="CASCADE"), primary_key=True
    )
    date: Mapped[date]
    created_at: Mapped[datetime]
    currency: Mapped[str] = mapped_column(server_default="USD")
    delta: Mapped[Decimal] = mapped_column(Numeric(20, 4))
    balance: Mapped[Decimal] = mapped_column(Numeric(20, 4))


class BalanceWatermark(Base):
    """Singleton dirty-watermark (decision 38): ``running_dirty_from`` is
    consumed synchronously at commit (before_commit hook), the networth
    cursor asynchronously by ``networth.consume_watermark``. The row's
    UPDATE lock is the recompute mutex."""

    __tablename__ = "balance_watermark"

    id: Mapped[bool] = mapped_column(primary_key=True, server_default=text("true"))
    running_dirty_from: Mapped[date | None]
    networth_dirty_from: Mapped[date | None]
    updated_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class Match(Base):
    __tablename__ = "matches"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    expected_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("transactions.id"))
    posted_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("transactions.id"))
    score: Mapped[float]
    variance: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    state: Mapped[str]
    decided_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("api_tokens.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class Event(Base):
    __tablename__ = "events"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    type: Mapped[str]
    entity_type: Mapped[str | None]
    entity_id: Mapped[uuid.UUID | None]
    # Encrypted (#137): event payloads carry account paths / descriptions.
    payload: Mapped[dict[str, Any]] = mapped_column("payload_enc", EncryptedJSON)
    # Plaintext structural idempotency id lifted out of payload (#137): the
    # once-only emit guards (transfer pair_key, forecast shortfall key) filter
    # on it, since a JSONB path over ciphertext can't. NULL for events without
    # a dedup identity. Populated at emit time.
    dedup_key: Mapped[str | None]


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    parent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("accounts.id"))
    name: Mapped[str]
    type: Mapped[str]
    is_virtual: Mapped[bool] = mapped_column(server_default=text("false"))
    path: Mapped[str] = mapped_column(LtreeType)
    # One-line, classifier-oriented description (categories; DESIGN §3.2).
    description: Mapped[str | None]
    currency: Mapped[str] = mapped_column(server_default="USD")
    external_id: Mapped[str | None]
    connection_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("connections.id"))
    # Feed sync-from (issue #154, decision 40): the importer skips feed rows
    # dated strictly before it — manual < boundary <= feed (DESIGN §7.1).
    # NULL = unscoped. Real accounts only (CHECK, migration 0018).
    feed_start: Mapped[date | None]
    # Feed sign inversion (issue #170, decision 43): the importer negates
    # this account's feed transaction amounts AFTER content-hashing — row
    # identity stays the bytes the bank sent, so flipping the flag never
    # re-imports history. Real accounts only (CHECK, migration 0019).
    feed_invert: Mapped[bool] = mapped_column(server_default=text("false"))
    # Drift-basis pin (issue #307, decision 83): posted_only | pending_inclusive
    # overrides the basis the sync history selects (decision 64); NULL =
    # learned. Real accounts only (CHECK, migration 0033).
    drift_basis_override: Mapped[str | None]
    # Where a brokerage account's holdings come from (#390, decision 88):
    # ``feed`` (SimpleFIN snapshots) or ``manual`` (positions projected into
    # snapshots). Exclusive per account; brokerage only (CHECK, migration
    # 0036); admin-set like the other feed configuration.
    holdings_source: Mapped[str] = mapped_column(server_default="feed")
    # When holdings_source last changed; NULL until it first does. A feed
    # fetch taken before the switch back to the feed is inside the manual
    # period (the importer skips its holdings); the first taken after it ends
    # the period (projection.end_manual_period), and manual_period_open says
    # one still waits to be ended (set by the switch to manual).
    holdings_source_changed_at: Mapped[datetime | None]
    manual_period_open: Mapped[bool] = mapped_column(server_default=text("false"))
    min_balance: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    # Lifecycle timestamps (real accounts only; CHECK + trigger enforce the
    # invariants — DESIGN §3.4). hidden_at hides an account from the default
    # /accounts listing; closed_at makes it read-only (no new postings).
    hidden_at: Mapped[datetime | None]
    closed_at: Mapped[datetime | None]
    # Latest reconciled date (issue #268, decision 61): a marker kept under the
    # account row lock with account_reconciliations. Never shown without the
    # live in-balance check of that reconciliation (purser.reconciliation).
    reconciled_through: Mapped[date | None]
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class AccountReconciliation(Base):
    """One "the statement said X on D" claim per account and date (#268).

    ``target_balance`` and ``adjustment`` are in ledger sign; the adjustment is
    what Purser posted to reach the target (0 = the ledger already matched, and
    then there is no adjustment transaction — DB CHECK)."""

    __tablename__ = "account_reconciliations"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id"))
    as_of: Mapped[date]
    basis: Mapped[str] = mapped_column(server_default="posted")  # posted | pending_inclusive
    target_balance: Mapped[Decimal] = mapped_column(Numeric(20, 4))
    adjustment: Mapped[Decimal] = mapped_column(Numeric(20, 4))
    adjustment_tx_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("transactions.id"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("api_tokens.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class AppSetting(Base):
    """Admin-editable runtime configuration override (one row per key).

    Env vars are the bootstrap defaults; a row here overrides them. Exactly
    one of value / value_enc is set (DB CHECK): non-secret values are plain
    JSONB, secret values are Fernet over their JSON encoding. Registry of
    valid keys lives in purser/appsettings.py.
    """

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(primary_key=True)
    # none_as_null: secret rows need SQL NULL here (the CHECK demands exactly
    # one of value/value_enc); an explicit JSON null override is written via
    # the sqlalchemy.JSON.NULL sentinel instead.
    value: Mapped[Any | None] = mapped_column(JSONB(none_as_null=True))
    value_enc: Mapped[bytes | None] = mapped_column(BYTEA)
    updated_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    updated_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("api_tokens.id"))


class Rule(Base):
    """A deterministic instruction that runs on new transactions (#174,
    DESIGN decision 46).

    The predicate columns mirror ``purser.core.rules.Predicate``; the service
    layer decrypts ``match_text`` and hands the pure core a frozen Predicate.
    Which action columns may be set is enforced by ``rules_action_shape`` in
    migration 0020 — one shape per ``type``.

    ``state='draft'`` never executes: ``rules.load_active()`` is the only
    loader and filters ``state='active'``, which is what makes the
    propose/commit gate structural rather than a convention.
    """

    __tablename__ = "rules"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    type: Mapped[str]  # categorize | rename | transfer_link
    state: Mapped[str] = mapped_column(server_default="draft")
    priority: Mapped[int] = mapped_column(server_default=text("100"))  # lower runs first

    # -- predicate (every column optional; AND of whatever is set) ----------
    account_ids: Mapped[list[uuid.UUID] | None] = mapped_column(ARRAY(UUIDType))
    match_field: Mapped[str | None]
    match_mode: Mapped[str | None]
    match_text: Mapped[str | None] = mapped_column("match_text_enc", EncryptedText)
    # Blind index over normalize_payee(pattern): duplicate-rule detection and
    # the `equals` fast path without decrypting. prefix/contains need the
    # plaintext, so the active set is decrypted once per pass.
    match_text_bidx: Mapped[str | None]
    amount_exact: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    amount_min: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    amount_max: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    direction: Mapped[str | None]  # debit | credit, on the post-feed_invert sign
    effective_from: Mapped[date | None]

    # -- action (exactly one shape per type) --------------------------------
    # categorize: may name a VIRTUAL account (an ordinary category) or a REAL
    # one, which is the [account] transfer sugar (decision 46, clarification 2).
    category_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("accounts.id"))
    payee_name: Mapped[str | None] = mapped_column("payee_name_enc", EncryptedText)
    pair_account_a: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("accounts.id"))
    pair_account_b: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("accounts.id"))
    # transfer_link only (migration 0029, decision 66): how many days apart the
    # pair's two legs may post; NULL is the global TRANSFER_WINDOW_DAYS.
    # rules_window_shape keeps it NULL on other types and within 0..14.
    window_days: Mapped[int | None]
    # transfer_link only (migration 0030, decision 67): the declared pair links
    # only rows where either leg's payee is in this payee's ALIAS GROUP
    # (rules.alias_group, resolved when the rule loads), so a rename that
    # aliases the payee keeps the rule in scope. NULL is the whole pair.
    # rules_payee_scope_shape keeps it NULL on other types.
    payee_scope_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("payees.id"))

    note: Mapped[str | None] = mapped_column("note_enc", EncryptedText)
    # The #176 interlock (added by migration 0022): the memory that motivated
    # this rule. One-directional — memories know nothing about rules except
    # through this reverse lookup.
    memory_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("memories.id"))
    applied_count: Mapped[int] = mapped_column(server_default=text("0"))
    last_applied_at: Mapped[datetime | None]
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("api_tokens.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class TransactionReview(Base):
    """A review mark — an annotation ABOUT a transaction, not ledger content
    (#175, DESIGN §4, decision 47).

    Deliberately a side table. `transactions_posted_guard` (migration 0005)
    allow-lists only status/superseded_by via to_jsonb, so any new column on
    `transactions` is immutable once posted — exactly the rows that need
    marking. Carving out the guard would spend the property migration 0005
    bought, for something that moves no money.
    """

    __tablename__ = "transaction_reviews"

    transaction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("transactions.id"), primary_key=True
    )
    reviewed_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("api_tokens.id"))
    via: Mapped[str]
    note: Mapped[str | None] = mapped_column("note_enc", EncryptedText)
    # Reviewed, but handed back for a human look. The row LEAVES the unreviewed
    # set — no second queue is recreated — while surfacing in a small flagged
    # list. Stays small by construction: opt-in per row.
    escalated_at: Mapped[datetime | None]
    # The row this mark was copied from, whole (decision 82): a pending's
    # posted successor, a corrected row's replacement, an unlinked bank
    # line's clone. Explains a reviewed_at older than the row itself.
    carried_from: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("transactions.id", ondelete="SET NULL")
    )


class InstanceStatement(Base):
    """A card statement balance recorded against a card-payment instance
    (#351, DESIGN §6, decision 80): the figure as printed (owed, positive),
    its closing date when known, and who recorded it. A side table like
    ``transaction_reviews``: it annotates the instance, whose amount moves
    through the ordinary transfer edit. Deleted with its transaction (FK
    cascade) and by ``reset_instance``; copied onto a clone when the feed
    re-opens the instance (``matching.release_settlement``)."""

    __tablename__ = "instance_statements"

    transaction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("transactions.id", ondelete="CASCADE"), primary_key=True
    )
    statement_balance: Mapped[Decimal] = mapped_column(Numeric(20, 4))
    statement_date: Mapped[date | None]
    source: Mapped[str]  # human | agent (the recording token's kind)
    recorded_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("api_tokens.id"))
    recorded_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class Memory(Base):
    """User-owned financial context served to agents (#176, DESIGN §3.6).

    NON-EXECUTABLE by construction: no code path parses `text`. A nuance that
    should act graduates into a rule (`Rule.memory_id`); the memory stays as
    the rationale, and archiving it never disables those rules.
    """

    __tablename__ = "memories"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=sa_text("gen_random_uuid()")
    )
    text: Mapped[str] = mapped_column("text_enc", EncryptedText)
    # A draft is NEVER served — not in describe(), not in list_memories, not
    # to the reviewer or the classifier. That is what stops an agent (or
    # Purser's own optional authoring tiers) writing its own future context.
    state: Mapped[str] = mapped_column(server_default="active")
    pinned: Mapped[bool] = mapped_column(server_default=sa_text("false"))
    archived_at: Mapped[datetime | None]
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("api_tokens.id"))
    updated_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("api_tokens.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=sa_text("now()"))
    updated_at: Mapped[datetime] = mapped_column(server_default=sa_text("now()"))

    links: Mapped[list["MemoryLink"]] = relationship(
        back_populates="memory", cascade="all, delete-orphan"
    )


class MemoryLink(Base):
    """Exactly one target per row. `accounts` covers both real accounts and
    categories (categories ARE virtual accounts, §3.2), so one nullable pair
    covers all three link kinds the feature needs."""

    __tablename__ = "memory_links"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=sa_text("gen_random_uuid()")
    )
    memory_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("memories.id"))
    account_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("accounts.id"))
    payee_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("payees.id"))

    memory: Mapped[Memory] = relationship(back_populates="links")
