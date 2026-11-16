"""Lots, dispositions, captures and awards (issue #393, decision 107).

Amends decision 8 and DESIGN section 13: lot-level accounting was a v1
non-goal ("lots deferred, nullable lot_id hedge"). The owner approved lot
accounting and equity awards on 2026-11-15 for hand-tracked positions only:
a feed account still holds snapshots, and Purser still computes no tax and
no TWR/IRR. Everything here is a sub-ledger of a position; the share count
stays the history of ``position_events``, so the projection, prices and net
worth read nothing new.

- ``position_events``: two kinds join. ``vest`` adds the NET shares of an
  award vest (``gross_quantity`` = ``quantity`` + ``withheld_quantity``; its
  ``price`` is the fair market value per share); ``transfer_out`` removes
  shares with no price and no cash (a transfer to another broker, or a
  decrease the owner cannot price), never invented as a sale. Neither moves
  cash (0043's shape CHECKs already keep cash on buys and sells).
- ``holdings_captures``: one row per applied bulk observation of a broker's
  lots or awards (as-of date, captured-at instant, completeness, keyed hashes
  of the exact request and of its economic content, the plan hash it was
  committed under, the raw payload's ``raw_fetches`` row, the encrypted
  source note and result). Append-only.
- ``raw_fetches.source`` gains ``capture``: the verbatim capture is archived
  encrypted before anything it plans is written (raw-first, guardrail 2).
- ``lots``: a tax lot of one position. Opened by exactly one event (a buy
  with a lot, a vest) or adopted from exactly one capture. The broker's lot
  reference and the plan label are encrypted with a blind index (account
  identifiers and narrative, decision 91). Basis is both-or-neither:
  per-share and total, NULL when unknown (never 0, never an average). Identity
  columns are immutable by trigger; only the reference (once), the basis and
  the void columns may change, through the audited service.
- ``lot_dispositions``: append-only; each ties shares of one lot (or of the
  position's unallocated remainder, ``lot_id`` NULL) to one ``sell`` or
  ``transfer_out`` event. A reversed event's dispositions stay and count for
  nothing (readers filter them), like a reversed event.
- ``awards`` / ``award_vests``: grants and their vest schedule. Nothing that
  values holdings or net worth reads them. A vested row names the lot of its
  net shares, except a vest whose every unit was withheld (net 0: no lot).

The downgrade refuses while any lot, disposition, capture, award, vest or
``vest`` / ``transfer_out`` event exists: it would destroy them (restore a
pre-0046 backup instead).

DESIGN.md sections 3.1, 8, 12 and 13 are updated in the same PR.

Revision ID: 0046
Revises: 0045
Create Date: 2026-11-15

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0046"
down_revision: str | None = "0045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SYMBOL = "'^[A-Z0-9][A-Z0-9.-]{0,14}$'"

UPGRADE_DDL = f"""
ALTER TABLE position_events DROP CONSTRAINT position_events_kind_values;
ALTER TABLE position_events
  ADD CONSTRAINT position_events_kind_values
  CHECK (kind IN ('set_quantity', 'buy', 'sell', 'vest', 'transfer_out'));
ALTER TABLE position_events
  ADD COLUMN gross_quantity NUMERIC(20,6),
  ADD COLUMN withheld_quantity NUMERIC(20,6);
ALTER TABLE position_events
  ADD CONSTRAINT position_events_vest_shape
  CHECK (
    (kind = 'vest') = (gross_quantity IS NOT NULL)
    AND (kind = 'vest') = (withheld_quantity IS NOT NULL)
    AND (kind <> 'vest' OR (
      withheld_quantity >= 0 AND gross_quantity = quantity + withheld_quantity AND fees IS NULL
    ))
  );
ALTER TABLE position_events
  ADD CONSTRAINT position_events_transfer_out_shape
  CHECK (kind <> 'transfer_out' OR (price IS NULL AND fees IS NULL));

ALTER TABLE raw_fetches DROP CONSTRAINT IF EXISTS raw_fetches_source_check;
ALTER TABLE raw_fetches
  ADD CONSTRAINT raw_fetches_source_check
  CHECK (source IN ('simplefin', 'qif', 'csv', 'capture'));

CREATE TABLE holdings_captures (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id      UUID NOT NULL REFERENCES accounts(id),
  kind            TEXT NOT NULL CHECK (kind IN ('lots', 'awards')),
  as_of           DATE NOT NULL,
  captured_at     TIMESTAMPTZ NOT NULL,
  completeness    TEXT NOT NULL CHECK (completeness IN ('complete', 'partial')),
  symbols         TEXT[] NOT NULL,
  request_hash    TEXT NOT NULL,
  content_hash    TEXT NOT NULL,
  plan_hash       TEXT NOT NULL,
  repeat_of       UUID REFERENCES holdings_captures(id),
  raw_fetch_id    UUID NOT NULL REFERENCES raw_fetches(id),
  source_note_enc BYTEA,
  result_enc      BYTEA NOT NULL,
  origin          TEXT NOT NULL CHECK (origin IN ('manual', 'agent')),
  actor_token_id  UUID REFERENCES api_tokens(id),
  created_at      TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE UNIQUE INDEX holdings_captures_request_once
  ON holdings_captures (account_id, kind, request_hash);
CREATE INDEX holdings_captures_account_as_of
  ON holdings_captures (account_id, kind, as_of);

CREATE FUNCTION holdings_captures_append_only() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'holdings captures are append-only evidence';
END $$ LANGUAGE plpgsql;
CREATE TRIGGER holdings_captures_immutable
  BEFORE UPDATE OR DELETE ON holdings_captures
  FOR EACH ROW EXECUTE FUNCTION holdings_captures_append_only();

CREATE TABLE lots (
  id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  position_id        UUID NOT NULL REFERENCES positions(id),
  acquired_on        DATE NOT NULL,
  effective_on       DATE NOT NULL,
  quantity           NUMERIC(20,6) NOT NULL CHECK (quantity > 0),
  basis_per_share    NUMERIC(20,6) CHECK (basis_per_share >= 0),
  cost_basis         NUMERIC(20,4) CHECK (cost_basis >= 0),
  basis_source       TEXT CHECK (basis_source IN ('per_share', 'total', 'both', 'event')),
  acquisition        TEXT NOT NULL CHECK (acquisition IN (
                       'buy', 'rsu_vest', 'plan_purchase', 'espp_purchase',
                       'transfer_in', 'drip', 'gift', 'unknown')),
  plan_enc           BYTEA,
  plan_bidx          TEXT,
  external_ref_enc   BYTEA,
  external_ref_bidx  TEXT,
  ordinal            INTEGER NOT NULL CHECK (ordinal >= 1),
  opened_by_event_id UUID REFERENCES position_events(id),
  capture_id         UUID REFERENCES holdings_captures(id),
  origin             TEXT NOT NULL CHECK (origin IN ('manual', 'agent')),
  actor_token_id     UUID REFERENCES api_tokens(id),
  note_enc           BYTEA,
  voided_at          TIMESTAMPTZ,
  void_note_enc      BYTEA,
  voided_by_token_id UUID REFERENCES api_tokens(id),
  created_at         TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  CONSTRAINT lots_basis_both_or_neither
    CHECK ((basis_per_share IS NULL) = (cost_basis IS NULL)
           AND (basis_per_share IS NULL) = (basis_source IS NULL)),
  CONSTRAINT lots_plan_pair CHECK ((plan_enc IS NULL) = (plan_bidx IS NULL)),
  CONSTRAINT lots_ref_pair CHECK ((external_ref_enc IS NULL) = (external_ref_bidx IS NULL)),
  CONSTRAINT lots_one_origin CHECK ((opened_by_event_id IS NULL) <> (capture_id IS NULL)),
  CONSTRAINT lots_acquired_first CHECK (acquired_on <= effective_on)
);
CREATE UNIQUE INDEX lots_ref_uniq
  ON lots (position_id, external_ref_bidx)
  WHERE external_ref_bidx IS NOT NULL AND voided_at IS NULL;
CREATE UNIQUE INDEX lots_natural_uniq
  ON lots (position_id, acquired_on, COALESCE(plan_bidx, ''), ordinal)
  WHERE voided_at IS NULL;
CREATE UNIQUE INDEX lots_opened_by_once
  ON lots (opened_by_event_id) WHERE opened_by_event_id IS NOT NULL;
CREATE INDEX lots_position ON lots (position_id);

-- Identity is immutable: a wrong lot is voided (or its opening event
-- reversed) and recorded again, never edited. The reference may be set once,
-- the void once; basis changes are gated by the service (no live
-- dispositions) and audited. Ciphertext may be re-wrapped (rotate-key).
CREATE FUNCTION lots_guard() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'lots are never deleted; void the lot instead';
  END IF;
  IF NEW.position_id IS DISTINCT FROM OLD.position_id
     OR NEW.acquired_on IS DISTINCT FROM OLD.acquired_on
     OR NEW.effective_on IS DISTINCT FROM OLD.effective_on
     OR NEW.quantity IS DISTINCT FROM OLD.quantity
     OR NEW.acquisition IS DISTINCT FROM OLD.acquisition
     OR NEW.plan_bidx IS DISTINCT FROM OLD.plan_bidx
     OR NEW.ordinal IS DISTINCT FROM OLD.ordinal
     OR NEW.opened_by_event_id IS DISTINCT FROM OLD.opened_by_event_id
     OR NEW.capture_id IS DISTINCT FROM OLD.capture_id
     OR NEW.origin IS DISTINCT FROM OLD.origin
     OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
    RAISE EXCEPTION 'a lot''s identity and quantity are immutable; void it and record it again';
  END IF;
  IF OLD.external_ref_bidx IS NOT NULL
     AND NEW.external_ref_bidx IS DISTINCT FROM OLD.external_ref_bidx THEN
    RAISE EXCEPTION 'a lot''s reference is set once';
  END IF;
  IF OLD.voided_at IS NOT NULL AND (
       NEW.voided_at IS DISTINCT FROM OLD.voided_at
       OR NEW.basis_per_share IS DISTINCT FROM OLD.basis_per_share
       OR NEW.cost_basis IS DISTINCT FROM OLD.cost_basis) THEN
    RAISE EXCEPTION 'a voided lot is read-only';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
CREATE TRIGGER lots_guard
  BEFORE UPDATE OR DELETE ON lots
  FOR EACH ROW EXECUTE FUNCTION lots_guard();

CREATE TABLE lot_dispositions (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  event_id      UUID NOT NULL REFERENCES position_events(id),
  position_id   UUID NOT NULL REFERENCES positions(id),
  lot_id        UUID REFERENCES lots(id),
  disposed_on   DATE NOT NULL,
  quantity      NUMERIC(20,6) NOT NULL CHECK (quantity > 0),
  cost_basis    NUMERIC(20,4) CHECK (cost_basis >= 0),
  proceeds      NUMERIC(20,4),
  realized_gain NUMERIC(20,4),
  term          TEXT CHECK (term IN ('short', 'long')),
  method        TEXT NOT NULL CHECK (method IN ('specific', 'fifo', 'lifo', 'hifo')),
  created_at    TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  CONSTRAINT lot_dispositions_unallocated_shape
    CHECK (lot_id IS NOT NULL OR (cost_basis IS NULL AND realized_gain IS NULL AND term IS NULL)),
  CONSTRAINT lot_dispositions_lot_has_term CHECK (lot_id IS NULL OR term IS NOT NULL),
  CONSTRAINT lot_dispositions_gain_shape
    CHECK (realized_gain IS NULL OR (cost_basis IS NOT NULL AND proceeds IS NOT NULL))
);
CREATE UNIQUE INDEX lot_dispositions_event_lot
  ON lot_dispositions (event_id, lot_id) WHERE lot_id IS NOT NULL;
CREATE UNIQUE INDEX lot_dispositions_event_unallocated
  ON lot_dispositions (event_id) WHERE lot_id IS NULL;
CREATE INDEX lot_dispositions_lot ON lot_dispositions (lot_id);
CREATE INDEX lot_dispositions_position ON lot_dispositions (position_id);

CREATE FUNCTION lot_dispositions_append_only() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'lot dispositions are append-only; reverse the event instead';
END $$ LANGUAGE plpgsql;
CREATE TRIGGER lot_dispositions_immutable
  BEFORE UPDATE OR DELETE ON lot_dispositions
  FOR EACH ROW EXECUTE FUNCTION lot_dispositions_append_only();

CREATE TABLE awards (
  id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id               UUID NOT NULL REFERENCES accounts(id),
  symbol                   TEXT NOT NULL CHECK (symbol ~ {_SYMBOL}),
  grant_ref_enc            BYTEA NOT NULL,
  grant_ref_bidx           TEXT NOT NULL,
  granted_on               DATE NOT NULL,
  plan_enc                 BYTEA,
  granted_quantity         NUMERIC(20,6) NOT NULL CHECK (granted_quantity > 0),
  cancelled_quantity       NUMERIC(20,6) NOT NULL DEFAULT 0
                             CHECK (cancelled_quantity >= 0),
  reported_vested_quantity NUMERIC(20,6) CHECK (reported_vested_quantity >= 0),
  reported_as_of           DATE,
  origin                   TEXT NOT NULL CHECK (origin IN ('manual', 'agent')),
  actor_token_id           UUID REFERENCES api_tokens(id),
  note_enc                 BYTEA,
  created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT awards_cancelled_within CHECK (cancelled_quantity <= granted_quantity)
);
CREATE UNIQUE INDEX awards_grant_uniq ON awards (account_id, grant_ref_bidx);

CREATE TABLE award_vests (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  award_id        UUID NOT NULL REFERENCES awards(id),
  vest_date       DATE NOT NULL,
  ordinal         INTEGER NOT NULL DEFAULT 1 CHECK (ordinal >= 1),
  quantity        NUMERIC(20,6) NOT NULL CHECK (quantity > 0),
  status          TEXT NOT NULL CHECK (status IN ('scheduled', 'vested', 'forfeited')),
  vest_event_id   UUID REFERENCES position_events(id),
  lot_id          UUID REFERENCES lots(id),
  vested_gross    NUMERIC(20,6),
  vested_withheld NUMERIC(20,6),
  vested_net      NUMERIC(20,6),
  vested_fmv      NUMERIC(20,6),
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- A vested row names the lot of its net shares; a vest whose every unit
  -- was withheld (net 0) delivered no shares and has no lot.
  CONSTRAINT award_vests_vested_shape CHECK (
    (lot_id IS NOT NULL) = (status = 'vested' AND vested_net > 0)
    AND (status = 'vested') = (vested_net IS NOT NULL)
    AND (status = 'vested') = (vested_gross IS NOT NULL)
    AND (status = 'vested') = (vested_withheld IS NOT NULL)
    AND (vest_event_id IS NULL OR status = 'vested')
    AND (vested_fmv IS NULL OR status = 'vested')
    AND (status <> 'vested' OR vested_gross = vested_net + vested_withheld)
  )
);
CREATE UNIQUE INDEX award_vests_slot ON award_vests (award_id, vest_date, ordinal);
CREATE UNIQUE INDEX award_vests_lot_once ON award_vests (lot_id) WHERE lot_id IS NOT NULL;
CREATE UNIQUE INDEX award_vests_event_once
  ON award_vests (vest_event_id) WHERE vest_event_id IS NOT NULL;
"""

DOWNGRADE_DDL = """
-- Never lossy: lots, dispositions, captures, awards and the new event kinds
-- are records a downgrade would destroy, so it refuses while any exist
-- (restore a backup taken before 0046 instead).
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM position_events WHERE kind IN ('vest', 'transfer_out'))
     OR EXISTS (SELECT 1 FROM lots)
     OR EXISTS (SELECT 1 FROM lot_dispositions)
     OR EXISTS (SELECT 1 FROM holdings_captures)
     OR EXISTS (SELECT 1 FROM awards)
     OR EXISTS (SELECT 1 FROM award_vests)
     OR EXISTS (SELECT 1 FROM raw_fetches WHERE source = 'capture') THEN
    RAISE EXCEPTION 'lots, awards, captures or vest/transfer_out events exist; a downgrade '
      'below 0046 would destroy them (restore a pre-0046 backup instead)';
  END IF;
END $$;
DROP TABLE award_vests;
DROP TABLE awards;
DROP TABLE lot_dispositions;
DROP FUNCTION lot_dispositions_append_only();
DROP TABLE lots;
DROP FUNCTION lots_guard();
DROP TABLE holdings_captures;
DROP FUNCTION holdings_captures_append_only();
ALTER TABLE raw_fetches DROP CONSTRAINT IF EXISTS raw_fetches_source_check;
ALTER TABLE raw_fetches
  ADD CONSTRAINT raw_fetches_source_check
  CHECK (source IN ('simplefin', 'qif', 'csv'));
ALTER TABLE position_events DROP CONSTRAINT IF EXISTS position_events_transfer_out_shape;
ALTER TABLE position_events DROP CONSTRAINT IF EXISTS position_events_vest_shape;
ALTER TABLE position_events DROP COLUMN withheld_quantity, DROP COLUMN gross_quantity;
ALTER TABLE position_events DROP CONSTRAINT position_events_kind_values;
ALTER TABLE position_events
  ADD CONSTRAINT position_events_kind_values
  CHECK (kind IN ('set_quantity', 'buy', 'sell'));
"""


def upgrade() -> None:
    op.execute(UPGRADE_DDL)


def downgrade() -> None:
    op.execute(DOWNGRADE_DDL)
