"""The search text cache (#141, DESIGN §9).

Descriptions, posting memos and payee names are encrypted at rest (decision
36), so SQL cannot match words in them. Decrypting every candidate row per
search measured 1.1 s at 50k rows and 4.7 s at 200k. Instead the ``serve``
process keeps a decrypted copy of each transaction's searchable text in
memory. It is never written anywhere, so nothing new is stored at rest: the
trust boundary is the running process, which already holds the key and
already caches decrypted payee, schedule and rule names (decision 125).

A search scans memory first and hands only the matching ids to SQL, which
applies every other filter, the register order, the exact total and the page.
Status, dates, amounts and accounts are not cached: SQL decides them, so a
status change (a void, a supersede) needs no cache work.

Freshness, without a hook on any writer (so rows the separate ``purser
worker`` process writes are seen too):

- **New rows** carry ``created_at`` = the start of the transaction that
  inserted them. The cache holds every row created before a watermark ``W``.
  Each refresh first sets the next watermark to the earlier of now and the
  start of the oldest transaction still open in this database (any client
  session, ours included), then reads every row created at or after the old
  ``W``. A row whose inserting transaction is still open therefore stays at
  or after the watermark until that transaction ends, and is read again on
  every refresh until then (its text can still change; if it rolls back, it
  is missing from the next read and dropped).
- **Rows whose text can still change** are pending and expected rows:
  posted rows are immutable in the database (migration 0005), and void rows
  are immutable lineage. Each is kept with a fingerprint (the row's and its
  postings' ``xmin`` and ``ctid``, which change on every update, even twice
  in one transaction) and re-read when it moves; a row seen posted or void
  is frozen for good.
- **Payees** are only ever inserted, never renamed or deleted; new ones are
  picked up by id when the table's (count, newest ``xmin``) moves.
- **Reconcile** (a background thread every ten minutes) compares the count
  and an id checksum of the rows before the watermark with the cache, and
  rebuilds it on a mismatch: that catches deletes and anything written
  outside these rules (a restore, another database role).

Until the background build has finished, searches use the decrypt-per-row
path, so the first query never waits. The cache is keyed to the encryption
key's fingerprint and the database URL, and is never used across either.

Matching is ``search_query.text_matches`` over the folded payee name,
description and memos; ``tests/test_search_cache.py`` holds the scan to it.
"""

import sys
import threading
import time
import uuid
from array import array
from bisect import bisect_right
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from purser import crypto
from purser.core.search_query import Text, fold
from purser.logging import get_logger

log = get_logger(__name__)

# Rows per closed chunk of a field. Chunks keep one non-ASCII row from
# widening the whole field's string (CPython stores a str at the width of its
# widest character), and let new rows append without copying the rest.
CHUNK_ROWS = 4096
# Between two rows of a chunk. Matches that would cross it are rejected, so
# any character may appear in the text itself.
SEP = "\n"
# Statuses whose text can still change in place.
MUTABLE = frozenset({"pending", "expected"})
RECONCILE_SECONDS = 600.0
RETRY_SECONDS = 60.0
# When pg_stat_activity hides another session's transaction start (another
# database role without pg_read_all_stats), the watermark also stays this
# far behind now; reconcile catches anything older.
HIDDEN_OVERLAP = timedelta(minutes=10)
# Rebuild (compact) when this many slots are dead and they are a quarter of
# the cache.
COMPACT_MIN_DEAD = 5000


def form(value: str | None) -> str:
    """The cached form of a text: ``search_query.fold``, the form every
    query clause is folded to."""
    return fold(value) if value else ""


class _Field:
    """One searchable field: a text per row, in chunks, each row tagged with
    its slot. Closed chunks are one string with ``SEP`` between rows and the
    row offsets in an ``array``, so a term is found with ``str.find`` rather
    than a Python loop over rows; the open chunk is a list until it fills."""

    __slots__ = ("_closed", "_open_rows", "_open_slots", "chars")

    def __init__(self) -> None:
        self._closed: list[tuple[str, array[int], array[int]]] = []
        self._open_rows: list[str] = []
        self._open_slots: array[int] = array("I")
        self.chars = 0

    def add(self, slot: int, value: str) -> None:
        self._open_rows.append(value)
        self._open_slots.append(slot)
        self.chars += len(value)
        if len(self._open_rows) >= CHUNK_ROWS:
            self._close()

    def _close(self) -> None:
        starts: array[int] = array("I")
        offset = 0
        for row in self._open_rows:
            starts.append(offset)
            offset += len(row) + 1
        starts.append(offset)  # one past the last row, as if SEP followed it
        self._closed.append((SEP.join(self._open_rows), starts, self._open_slots))
        self._open_rows = []
        self._open_slots = array("I")

    def find(self, term: str, out: set[int]) -> None:
        """Add the slot of every row whose text contains ``term``."""
        size = len(term)
        for hay, starts, slots in self._closed:
            at = hay.find(term)
            while at != -1:
                row = bisect_right(starts, at) - 1
                after = starts[row + 1]  # where the next row starts
                if at + size < after:  # ends before the separator: inside the row
                    out.add(slots[row])
                    at = hay.find(term, after)
                else:
                    at = hay.find(term, at + 1)
        for value, slot in zip(self._open_rows, self._open_slots, strict=True):
            if term in value:
                out.add(slot)

    def rows(self) -> Iterable[tuple[str, int]]:
        """(text, slot) of every row, closed chunks first (tests, rebuilds)."""
        for hay, starts, slots in self._closed:
            for row, slot in enumerate(slots):
                yield hay[starts[row] : starts[row + 1] - 1], slot
        yield from zip(self._open_rows, self._open_slots, strict=True)

    def nbytes(self) -> int:
        """Approximate memory: the strings as CPython stores them, plus the
        offset and slot arrays."""
        total = sum(sys.getsizeof(h) + 4 * (len(s) + len(t)) for h, s, t in self._closed)
        return total + sum(sys.getsizeof(v) for v in self._open_rows) + 4 * len(self._open_slots)


@dataclass(slots=True)
class _Tracked:
    """A row whose text may still change: created at or after the watermark
    (its transaction may still be open) or pending/expected."""

    slot: int
    fingerprint: str
    created_at: datetime
    mutable: bool


# Every transaction row the cache reads: the text, and a fingerprint for the
# rows it keeps watching. The fingerprint changes whenever the row or one of
# its postings is updated, inserted or deleted (ctid moves on every update,
# even twice in one transaction).
_ROW_SELECT = """
SELECT t.id, t.description_enc, t.payee_id, t.status, t.created_at,
       CASE WHEN {watch} THEN
         t.xmin::text || ':' || t.ctid::text || '|' || COALESCE((
           SELECT string_agg(p.xmin::text || ':' || p.ctid::text, ',' ORDER BY p.ctid)
             FROM postings p WHERE p.transaction_id = t.id), '')
       END AS fingerprint
  FROM transactions t
"""

# Before reading rows: now, and the start of the oldest transaction still
# open in this database (any client session, ours included, whether or not
# it has written yet). ``hidden`` counts sessions whose state this role may
# not see.
_GUARD = text(
    """
SELECT statement_timestamp() AS now,
       (SELECT min(xact_start) FROM pg_stat_activity
         WHERE datname = current_database() AND backend_type = 'client backend') AS oldest,
       (SELECT count(*) FROM pg_stat_activity
         WHERE datname = current_database() AND backend_type = 'client backend'
           AND state IS NULL) AS hidden,
       (SELECT count(*) FROM payees) AS payee_count,
       (SELECT max(xmin::text::bigint) FROM payees) AS payee_xmin
"""
)

# The first four bytes of an id as an integer: the reconcile checksum.
_ID_PART_SQL = "('x' || substr(id::text, 1, 8))::bit(32)::bigint"


def _id_part(key: bytes) -> int:
    return int.from_bytes(key[:4], "big")


class SearchText:
    """The decrypted, casefolded search text of every transaction (all
    statuses), for one database and one encryption key."""

    def __init__(self, *, key_fingerprint: str, database: str) -> None:
        self.key_fingerprint = key_fingerprint
        self.database = database
        self._lock = threading.RLock()
        self._ids = bytearray()  # 16 bytes per slot
        self._dead: set[int] = set()
        self._description = _Field()
        self._memo = _Field()
        self._slot_payee: array[int] = array("i")  # payee index per slot, -1 none
        self._payee_index: dict[uuid.UUID, int] = {}
        self._payee_names: list[str] = []
        self._payee_slots: list[array[int]] = []
        self._payee_state: tuple[int, int | None] = (0, None)
        self._payee_refs: dict[str, uuid.UUID] = {}  # py_ reference -> id
        self._tracked: dict[bytes, _Tracked] = {}
        self._live = 0
        self._id_sum = 0
        self.watermark: datetime | None = None
        self.built_seconds = 0.0

    # -- reading the database ------------------------------------------------

    def _guard(self, db: Session | Connection) -> tuple[datetime, Any]:
        row = db.execute(_GUARD).one()
        watermark: datetime = row.now
        if row.oldest is not None and row.oldest < watermark:
            watermark = row.oldest
        if row.hidden:
            watermark = min(watermark, row.now - HIDDEN_OVERLAP)
        return watermark, row

    def _load_payees(self, db: Session | Connection, ids: Iterable[uuid.UUID] | None) -> None:
        """Add payees by id (``None``: every payee not yet known)."""
        if ids is None:
            rows = db.execute(text("SELECT id, name_enc FROM payees")).all()
        else:
            wanted = [i for i in set(ids) if i not in self._payee_index]
            if not wanted:
                return
            rows = db.execute(
                text("SELECT id, name_enc FROM payees WHERE id = ANY(:ids)"), {"ids": wanted}
            ).all()
        for payee_id, name_enc in rows:
            if payee_id in self._payee_index:
                continue
            self._payee_index[payee_id] = len(self._payee_names)
            self._payee_refs[crypto.payee_ref(payee_id)] = payee_id
            self._payee_names.append(form(crypto.decrypt(name_enc) if name_enc else None))
            self._payee_slots.append(array("I"))

    def _memos(
        self, db: Session | Connection, ids: Sequence[uuid.UUID] | None
    ) -> dict[uuid.UUID, list[str]]:
        sql = "SELECT transaction_id, memo_enc FROM postings WHERE memo_enc IS NOT NULL"
        params: dict[str, Any] = {}
        if ids is not None:
            if not ids:
                return {}
            sql += " AND transaction_id = ANY(:ids)"
            params["ids"] = list(ids)
        out: dict[uuid.UUID, list[str]] = {}
        for tx_id, memo_enc in db.execute(text(sql + " ORDER BY transaction_id, id"), params):
            out.setdefault(tx_id, []).append(form(crypto.decrypt(memo_enc)))
        return out

    # -- writing the cache ---------------------------------------------------

    def _append(self, row: Any, memos: list[str] | None) -> int:
        slot = len(self._ids) // 16
        key = row.id.bytes
        self._ids += key
        description = crypto.decrypt(row.description_enc) if row.description_enc else None
        self._description.add(slot, form(description))
        if memos:
            self._memo.add(slot, SEP.join(memos))
        payee = self._payee_index.get(row.payee_id, -1) if row.payee_id is not None else -1
        self._slot_payee.append(payee)
        if payee >= 0:
            self._payee_slots[payee].append(slot)
        self._live += 1
        self._id_sum += _id_part(key)
        return slot

    def _kill(self, slot: int) -> None:
        if slot in self._dead:
            return
        self._dead.add(slot)
        self._live -= 1
        self._id_sum -= _id_part(bytes(self._ids[slot * 16 : slot * 16 + 16]))

    def _track(self, row: Any, slot: int, watermark: datetime) -> None:
        key = row.id.bytes
        mutable = row.status in MUTABLE
        if mutable or row.created_at >= watermark:
            self._tracked[key] = _Tracked(slot, row.fingerprint or "", row.created_at, mutable)
        else:
            self._tracked.pop(key, None)

    def _build(self, conn: Connection) -> None:
        started = time.perf_counter()
        watermark, guard = self._guard(conn)
        self._load_payees(conn, None)
        self._payee_state = (guard.payee_count, guard.payee_xmin)
        memos = self._memos(conn, None)
        watch = "t.status IN ('pending', 'expected') OR t.created_at >= :w"
        result = conn.execution_options(yield_per=5000).execute(
            text(_ROW_SELECT.format(watch=watch)), {"w": watermark}
        )
        for partition in result.partitions():
            for row in partition:
                if row.payee_id is not None and row.payee_id not in self._payee_index:
                    self._load_payees(conn, [row.payee_id])
                slot = self._append(row, memos.get(row.id))
                self._track(row, slot, watermark)
        self.watermark = watermark
        self.built_seconds = time.perf_counter() - started

    def refresh(self, session: Session) -> None:
        """Bring the cache up to date, on the caller's session (so a request
        that wrote rows and then searches sees them)."""
        with self._lock:
            self._refresh(session)

    def _refresh(self, session: Session) -> None:
        old = self.watermark
        assert old is not None, "refresh before build"
        watermark, guard = self._guard(session)
        if (guard.payee_count, guard.payee_xmin) != self._payee_state:
            # Payees are only ever inserted (resolve_payee), never renamed:
            # the new ones are the ids not known yet.
            self._load_payees(session, session.execute(text("SELECT id FROM payees")).scalars())
            self._payee_state = (guard.payee_count, guard.payee_xmin)
        watched = [k for k, t in self._tracked.items() if t.mutable and t.created_at < old]
        rows = session.execute(
            text(
                _ROW_SELECT.format(watch="TRUE") + " WHERE t.created_at >= :w OR t.id = ANY(:ids)"
            ),
            {"w": old, "ids": [uuid.UUID(bytes=k) for k in watched]},
        ).all()
        seen: set[bytes] = set()
        changed = []
        for row in rows:
            key = row.id.bytes
            seen.add(key)
            tracked = self._tracked.get(key)
            if tracked is not None and tracked.fingerprint == row.fingerprint:
                continue
            changed.append(row)
        # A watched row that is gone was deleted, or its transaction rolled
        # back: every tracked row created at or after ``old`` is in that read.
        for key, tracked in list(self._tracked.items()):
            if key not in seen and (tracked.created_at >= old or tracked.mutable):
                self._kill(tracked.slot)
                del self._tracked[key]
        if changed:
            self._load_payees(session, [r.payee_id for r in changed if r.payee_id is not None])
            memos = self._memos(session, [r.id for r in changed])
            for row in changed:
                tracked = self._tracked.get(row.id.bytes)
                if tracked is not None:
                    self._kill(tracked.slot)
                slot = self._append(row, memos.get(row.id))
                self._track(row, slot, watermark)
        for key, tracked in list(self._tracked.items()):
            if not tracked.mutable and tracked.created_at < watermark:
                del self._tracked[key]  # its transaction ended: frozen
        self.watermark = watermark

    # -- searching -----------------------------------------------------------

    def _uuids(self, slots: Iterable[int]) -> list[uuid.UUID]:
        ids = self._ids
        return [uuid.UUID(bytes=bytes(ids[s * 16 : s * 16 + 16])) for s in slots]

    def _find(self, clause: Text) -> set[int]:
        """Slots whose text holds ``clause.text`` in the clause's field (dead
        slots included: callers drop them)."""
        hits: set[int] = set()
        if clause.field in ("any", "desc"):
            self._description.find(clause.text, hits)
        if clause.field in ("any", "memo"):
            self._memo.find(clause.text, hits)
        if clause.field in ("any", "payee"):
            for index, name in enumerate(self._payee_names):
                if clause.text in name:
                    hits.update(self._payee_slots[index])
        return hits

    def match(
        self, session: Session, clauses: Sequence[Text]
    ) -> tuple[list[uuid.UUID] | None, list[uuid.UUID]]:
        """After a refresh on ``session``: with a positive clause, the ids of
        every row satisfying all clauses (``search_query.text_matches``) and
        no exclusions; with negative clauses only, ``None`` (every row) and
        the ids to exclude. A superset in status and scope: SQL narrows it."""
        with self._lock:
            self._refresh(session)
            positive = sorted((c for c in clauses if not c.negated), key=lambda c: -len(c.text))
            negative = [c for c in clauses if c.negated]
            if positive:
                hits: set[int] | None = None
                for clause in positive:  # longest first: usually the rarest
                    found = self._find(clause)
                    hits = found if hits is None else hits & found
                    if not hits:
                        return [], []
                assert hits is not None
                for clause in negative:
                    hits -= self._find(clause)
                hits -= self._dead
                return self._uuids(hits), []
            excluded: set[int] = set()
            for clause in negative:
                excluded |= self._find(clause)
            excluded -= self._dead
            return None, self._uuids(excluded)

    def payee_for_ref(self, session: Session, ref: str) -> uuid.UUID | None:
        """The payee a ``py_`` reference names, after a refresh."""
        with self._lock:
            self._refresh(session)
            return self._payee_refs.get(ref)

    # -- reconcile -----------------------------------------------------------

    def consistent(self, session: Session) -> bool:
        """Refresh, then compare the rows created before the watermark with
        the database: count and an id checksum."""
        with self._lock:
            self._refresh(session)
            watermark = self.watermark
            assert watermark is not None
            count, checksum = session.execute(
                text(
                    f"SELECT count(*), COALESCE(sum({_ID_PART_SQL}), 0) "
                    "FROM transactions WHERE created_at < :w"
                ),
                {"w": watermark},
            ).one()
            live, total = self._live, self._id_sum
            for key, tracked in self._tracked.items():
                if tracked.created_at >= watermark and tracked.slot not in self._dead:
                    live -= 1
                    total -= _id_part(key)
            return (count, int(checksum)) == (live, total)

    def needs_compaction(self) -> bool:
        dead = len(self._dead)
        return dead >= COMPACT_MIN_DEAD and dead * 4 >= len(self._ids) // 16

    # -- introspection (tests, logs) -----------------------------------------

    def texts(self, tx_id: uuid.UUID) -> list[tuple[str, str, str]]:
        """The live cached (description, memo, payee) of a row, for tests:
        one entry, or none when the row is not cached."""
        key = tx_id.bytes
        out = []
        at = self._ids.find(key)
        while at != -1:
            slot = at // 16
            if at % 16 == 0 and slot not in self._dead:
                description = next((v for v, s in self._description.rows() if s == slot), "")
                memo = next((v for v, s in self._memo.rows() if s == slot), "")
                payee = self._slot_payee[slot]
                out.append((description, memo, self._payee_names[payee] if payee >= 0 else ""))
            at = self._ids.find(key, at + 1)
        return out

    def stats(self) -> dict[str, Any]:
        slots = len(self._ids) // 16
        approx = (
            len(self._ids)
            + self._description.nbytes()
            + self._memo.nbytes()
            + 4 * slots * 2  # slot payee + payee slot lists
            + sum(len(n) for n in self._payee_names)
        )
        return {
            "rows": self._live,
            "dead": len(self._dead),
            "payees": len(self._payee_names),
            "tracked": len(self._tracked),
            "mib": round(approx / 2**20, 1),
            "seconds": round(self.built_seconds, 2),
        }


# ---------------------------------------------------------------------------
# The process's cache
# ---------------------------------------------------------------------------

_store: SearchText | None = None
_thread: threading.Thread | None = None


def _database(engine: Engine) -> str:
    return engine.url.render_as_string(hide_password=True)


def ready() -> SearchText | None:
    """The process's cache, when it is built for this key and database."""
    store = _store
    if store is None:
        return None
    from purser.db import get_engine

    if store.key_fingerprint != crypto.key_fingerprint() or store.database != _database(
        get_engine()
    ):
        return None
    return store


def build(engine: Engine | None = None) -> SearchText:
    """Read every transaction into a new cache, from one snapshot."""
    from purser.db import get_engine

    engine = engine or get_engine()
    store = SearchText(key_fingerprint=crypto.key_fingerprint(), database=_database(engine))
    with engine.connect() as conn:
        conn = conn.execution_options(isolation_level="REPEATABLE READ")
        with conn.begin():
            store._build(conn)
    return store


def warm(engine: Engine | None = None) -> SearchText:
    """Build the cache and make it the process's."""
    global _store
    store = build(engine)
    _store = store
    log.info("search text cache built", **store.stats())
    return store


def reset() -> None:
    global _store
    _store = None


def reconcile_once() -> bool:
    """One reconcile pass: rebuild when the cache disagrees with the
    database or holds too many dead slots. Returns whether it rebuilt."""
    store = ready()
    if store is None:
        warm()
        return True
    from purser.db import get_sessionmaker

    with get_sessionmaker()() as session:
        try:
            consistent = store.consistent(session)
        finally:
            session.rollback()
    if consistent and not store.needs_compaction():
        return False
    log.info("search text cache rebuilding", consistent=consistent, **store.stats())
    warm()
    return True


def _run() -> None:
    delay = 0.0
    while True:
        if delay:
            time.sleep(delay)
        try:
            reconcile_once()
            delay = RECONCILE_SECONDS
        except Exception:  # never let the cache take serve down
            log.exception("search text cache refresh failed; searches decrypt per row")
            delay = RETRY_SECONDS


def start_background() -> None:
    """Build the cache in a daemon thread and reconcile it every ten minutes
    (``purser serve``). Searches decrypt per row until it is ready."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _thread = threading.Thread(target=_run, name="search-text", daemon=True)
    _thread.start()
