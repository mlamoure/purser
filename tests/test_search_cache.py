# ruff: noqa: F811  (pytest fixtures re-exported from sibling modules)
"""The search text cache (#141, DESIGN §9): the in-memory text a search
scans before SQL applies every other filter. Same ids, same order, same
exact total as filtering the ``q=None`` listing; fresh for rows written by
any connection (another process included), for edits of pending and
expected rows, and after deletes (reconcile). Synthetic data only."""

import os
import random
import uuid
from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st
from sqlalchemy import Engine, create_engine, select, text
from sqlalchemy.orm import Session

import purser.config
from purser import crypto
from purser import ledger as ledger_service
from purser.core import ledger as core
from purser.core.search_query import fold, parse, text_matches
from purser.db import get_sessionmaker
from purser.models import Transaction
from purser.search import textcache
from tests.conftest import TEST_ENCRYPTION_KEY, reset_db, today
from tests.test_mcp import live_server  # noqa: F401 (fixture re-export)
from tests.test_privacy import seeded  # noqa: F401 (fixture re-export)


@pytest.fixture
def cache(migrated_engine: Engine) -> Iterator[None]:
    """The process cache is global: start and end every test without one."""
    textcache.reset()
    yield
    textcache.reset()


def _insert(conn: object, description: str, *, created_at: str | None = None) -> uuid.UUID:
    """A bare posted row (no postings needed for the cache), on any
    connection or session; ``created_at`` overrides the server default."""
    tx_id = uuid.uuid4()
    columns = "id, date, description_enc, status, origin" + (", created_at" if created_at else "")
    values = ":id, :d, :desc, 'posted', 'import'" + (
        ", CAST(:c AS timestamptz)" if created_at else ""
    )
    conn.execute(  # type: ignore[attr-defined]
        text(f"INSERT INTO transactions ({columns}) VALUES ({values})"),
        {"id": tx_id, "d": date(2026, 3, 1), "desc": crypto.encrypt(description), "c": created_at},
    )
    return tx_id


def _ids(store: textcache.SearchText, session: Session, q: str) -> list[uuid.UUID]:
    ids, excluded = store.match(session, parse(q).text)
    assert ids is not None and excluded == []
    return ids


def _matches(q: str) -> set[uuid.UUID]:
    store = textcache.ready()
    assert store is not None
    with get_sessionmaker()() as session:
        try:
            return set(_ids(store, session, q))
        finally:
            session.rollback()


class TestWatermark:
    """New rows reach the cache through ``created_at``, the start of the
    inserting transaction; the watermark never passes an open transaction."""

    def test_a_long_transaction_that_commits_last_is_seen(
        self, clean_db: None, cache: None, migrated_engine: Engine
    ) -> None:
        textcache.warm()
        with migrated_engine.connect() as long, migrated_engine.connect() as short:
            long_row = _insert(long, "LONGIMPORT row")  # starts first, commits last
            short_row = _insert(short, "SHORTEDIT row")
            short.commit()
            assert _matches("row") == {short_row}
            long.commit()
        assert _matches("row") == {short_row, long_row}

    def test_a_transaction_that_writes_after_a_refresh_is_seen(
        self, clean_db: None, cache: None, migrated_engine: Engine
    ) -> None:
        """Open but not yet written (no transaction id): still holds the
        watermark back, so its later insert is not left behind it."""
        textcache.warm()
        with migrated_engine.connect() as slow:
            slow.execute(text("SELECT 1"))  # the transaction starts here
            assert _matches("latecomer") == set()
            late = _insert(slow, "LATECOMER row")
            slow.commit()
        assert _matches("latecomer") == {late}

    def test_rows_another_process_writes_are_seen(
        self, clean_db: None, cache: None, database_url: str
    ) -> None:
        """The split compose: ``purser worker`` imports from its own process
        and pool. A separate engine is a separate backend, as it would be."""
        textcache.warm()
        worker = create_engine(database_url)
        try:
            with worker.begin() as conn:
                imported = _insert(conn, "WORKER IMPORT 42")
        finally:
            worker.dispose()
        assert _matches("worker import") == {imported}

    def test_own_uncommitted_rows_are_seen_and_rolled_back_rows_dropped(
        self, clean_db: None, cache: None
    ) -> None:
        store = textcache.warm()
        with get_sessionmaker()() as session:
            mine = _insert(session, "MINE NOT COMMITTED")
            assert _ids(store, session, "mine") == [mine]
            session.rollback()
        assert _matches("mine") == set()
        assert store.texts(mine) == []


def _seed_api(client: TestClient) -> None:
    for body in (
        {"name": "Checking", "type": "checking", "path": "assets.checking"},
        {"name": "Housing", "type": "category_expense", "path": "expenses.housing"},
        {"name": "Dining", "type": "category_expense", "path": "expenses.dining"},
    ):
        endpoint = "/categories" if body["type"].startswith("category") else "/accounts"
        assert client.post(endpoint, json=body).status_code == 201


def _create(client: TestClient, description: str, status: str = "pending") -> str:
    resp = client.post(
        "/transactions",
        json={
            "account": "assets.checking",
            "category": "expenses.dining",
            "amount": "-12.50",
            "date": "2026-03-02",
            "description": description,
            "status": status,
        },
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


def _search(client: TestClient, q: str, **params: str) -> list[dict]:
    assert textcache.ready() is not None  # the cache path, not the cold one
    resp = client.get("/transactions", params={"q": q, **params})
    assert resp.status_code == 200, resp.text
    page = resp.json()
    assert page["total"] == len(page["items"])
    return list(page["items"])


class TestMutableRows:
    """Pending and expected rows change text in place; posted rows cannot."""

    def test_a_pending_edit(self, admin_client: TestClient, cache: None) -> None:
        _seed_api(admin_client)
        tx = _create(admin_client, "ALPHA ROASTERS")
        textcache.warm()
        assert [i["id"] for i in _search(admin_client, "alpha")] == [tx]
        resp = admin_client.patch(f"/transactions/{tx}", json={"description": "BRAVO BAKERY"})
        assert resp.status_code == 200, resp.text
        assert _search(admin_client, "alpha") == []
        assert [i["id"] for i in _search(admin_client, "bravo")] == [tx]

    def test_an_expected_row_rename(self, admin_client: TestClient, cache: None) -> None:
        _seed_api(admin_client)
        resp = admin_client.post(
            "/schedules",
            json={
                "name": "Lawn service",
                "account": "assets.checking",
                "rrule": "FREQ=WEEKLY",
                "dtstart": today().isoformat(),
                "amount": "-40.00",
                "amount_type": "fixed",
                "category": "expenses.housing",
                "payee": "Example landscaping",
            },
        )
        assert resp.status_code == 201, resp.text
        sid = resp.json()["id"]
        textcache.warm()
        before = _search(admin_client, "lawn", status="expected")
        assert before
        resp = admin_client.patch(f"/schedules/{sid}", json={"name": "Garden care"})
        assert resp.status_code == 200, resp.text
        assert _search(admin_client, "lawn", status="expected") == []
        after = _search(admin_client, "garden", status="expected")
        assert {i["id"] for i in after} == {i["id"] for i in before}

    def test_a_pending_row_edited_then_posted(self, admin_client: TestClient, cache: None) -> None:
        _seed_api(admin_client)
        tx = _create(admin_client, "CHARLIE CAFE")
        textcache.warm()
        assert admin_client.patch(f"/transactions/{tx}", json={"description": "DELTA DINER"})
        assert admin_client.post(f"/transactions/{tx}/post").status_code == 200
        assert _search(admin_client, "charlie") == []
        (row,) = _search(admin_client, "delta")
        assert (row["id"], row["status"]) == (tx, "posted")
        # Posted now: frozen, no longer watched.
        store = textcache.ready()
        assert store is not None and uuid.UUID(tx).bytes not in store._tracked

    def test_a_pending_memo_edit(self, admin_client: TestClient, cache: None) -> None:
        """Memos live on postings: a split's new lines move the fingerprint."""
        _seed_api(admin_client)
        tx = _create(admin_client, "ECHO MARKET")
        store = textcache.warm()
        resp = admin_client.put(
            f"/transactions/{tx}/split",
            json={
                "lines": [
                    {"category": "expenses.dining", "amount": "-10.00", "memo": "Team lunch"},
                    {"category": "expenses.housing", "amount": "-2.50", "memo": "Light bulbs"},
                ]
            },
        )
        assert resp.status_code == 200, resp.text
        _search(admin_client, "echo")  # refreshes
        ((description, memo, _payee),) = store.texts(uuid.UUID(tx))
        assert description == "echo market"
        assert sorted(memo.split(textcache.SEP)) == ["light bulbs", "team lunch"]

    def test_two_edits_in_one_transaction_with_a_search_between(
        self, clean_db: None, cache: None, db_session: Session
    ) -> None:
        """Both versions carry the same xmin; the fingerprint's ctid tells
        them apart."""
        acct = ledger_service.create_account(
            db_session,
            name="Checking",
            type_="checking",
            path="assets.checking",
            actor_token_id=None,
        )
        cat = ledger_service.ensure_category_path(db_session, "expenses.misc", actor_token_id=None)
        tx = ledger_service.create_flat_transaction(
            db_session,
            account_ref=str(acct.id),
            category_ref=str(cat.id),
            amount=Decimal("-5.00"),
            date=date(2026, 3, 3),
            description="FOXTROT ONE",
            requested_status=core.Status.PENDING,
            origin=core.Origin.IMPORT,
            token=None,
        )
        db_session.commit()
        store = textcache.warm()
        tx.description = "GOLF TWO"
        db_session.flush()
        assert _ids(store, db_session, "golf") == [tx.id]
        tx.description = "HOTEL THREE"
        db_session.flush()
        db_session.commit()
        assert _matches("golf") == set()
        assert _matches("hotel") == {tx.id}


class TestReconcile:
    """Count and id checksum of the rows before the watermark: a delete, or
    a row written outside the watermark's rules, triggers a rebuild."""

    def test_after_a_delete(self, clean_db: None, cache: None, migrated_engine: Engine) -> None:
        with migrated_engine.begin() as conn:
            keep = _insert(conn, "INDIA KEEP")
            gone = _insert(conn, "INDIA GONE")
        textcache.warm()
        with migrated_engine.begin() as conn:
            # As the delete-account CLI does: posted rows are guarded.
            conn.execute(text("SET LOCAL session_replication_role = replica"))
            conn.execute(text("DELETE FROM transactions WHERE id = :id"), {"id": gone})
        store = textcache.ready()
        assert store is not None
        with get_sessionmaker()() as session:
            assert not store.consistent(session)
        assert textcache.reconcile_once() is True
        rebuilt = textcache.ready()
        assert rebuilt is not None and rebuilt is not store
        assert _matches("india") == {keep}
        assert textcache.reconcile_once() is False

    def test_a_row_restored_with_an_old_created_at(
        self, clean_db: None, cache: None, migrated_engine: Engine
    ) -> None:
        textcache.warm()
        with migrated_engine.begin() as conn:
            restored = _insert(conn, "JULIET RESTORED", created_at="2020-01-01T00:00:00Z")
        assert _matches("juliet") == set()  # behind the watermark: missed
        assert textcache.reconcile_once() is True
        assert _matches("juliet") == {restored}

    def test_steady_state_with_open_transactions_is_consistent(
        self, clean_db: None, cache: None, migrated_engine: Engine
    ) -> None:
        with migrated_engine.begin() as conn:
            _insert(conn, "KILO ONE")
        textcache.warm()
        with migrated_engine.connect() as open_tx:
            _insert(open_tx, "KILO OPEN")
            with migrated_engine.begin() as conn:
                _insert(conn, "KILO TWO")
            assert textcache.reconcile_once() is False
            open_tx.rollback()
        assert textcache.reconcile_once() is False


class TestKeyRotation:
    def test_a_changed_key_is_never_served_from_the_cache(
        self, clean_db: None, cache: None, migrated_engine: Engine
    ) -> None:
        """A rotation runs with "new,old": the cache built under the old key
        is not used, a cache built under the pair reads the old ciphertext,
        and the rotation's rewrite changes no cached text. The rotation is
        rolled back, so the suite's database keeps its key."""
        with migrated_engine.begin() as conn:
            row = _insert(conn, "LIMA ROTATED")
        textcache.warm()
        new = Fernet.generate_key().decode()
        try:
            os.environ["PURSER_ENCRYPTION_KEY"] = f"{new},{TEST_ENCRYPTION_KEY}"
            purser.config.get_settings.cache_clear()
            assert textcache.ready() is None  # another key: the cold path
            store = textcache.warm()
            assert _matches("lima") == {row}
            with get_sessionmaker()() as session:
                from purser.admin.rotate_key import rotate

                rotate(session)
                assert _ids(store, session, "lima") == [row]
                session.rollback()
        finally:
            os.environ["PURSER_ENCRYPTION_KEY"] = TEST_ENCRYPTION_KEY
            purser.config.get_settings.cache_clear()
            crypto.reset_dek_cache()
        assert textcache.ready() is None


class TestPrivacy:
    """The cache is internal: withholding runs on the response as before, so
    a cache-served search discloses nothing a cold one would not."""

    def test_withheld_fields_stay_out_of_cached_search_results(
        self, seeded: dict, live_server: object, cache: None
    ) -> None:
        from tests.test_privacy import ALL, _assert_clean, _call_all

        textcache.warm()
        calls = [
            ("search_transactions", {"q": "canary-desc", "withhold": ALL}),
            ("search_transactions", {"q": "CANARY", "account": "assets.checking", "withhold": ALL}),
            ("search_transactions", {"q": "desc-alpha", "withhold": ["description", "payee"]}),
        ]
        answers = _call_all(live_server.url, seeded["token"], calls)  # type: ignore[attr-defined]
        assert not [a for a in answers if a["error"]]
        assert textcache.ready() is not None
        _assert_clean(answers[:2])
        assert answers[0]["structured"]["total"] >= 8
        (alpha,) = answers[2]["structured"]["items"]
        assert alpha["payee"].startswith("py_")
        assert "description" not in alpha


# -- parity: the cache engine is a filter of the q=None listing --------------

_WORDS = [
    "AMAZON MKTPL",
    "amazon.com order",
    "Coffee Bar",
    "coffee",
    "WHOLE FOODS MARKET",
    "wholesaler co",
    "Café Ünïcode",
    "STARBUCKS #1234",
    "rent\nline two",
    "東京 Ramen",
    None,
]
_PAYEES = [None, "Blue Cup", "Whole Foods", "Café Rosa", "7-Eleven"]
_MEMOS = [None, "team lunch", "refund pending", "Coffee beans", "gift"]
_TERMS = st.sampled_from(
    [
        "amazon",
        "COFFEE",
        "whole",
        "café",
        "ü",
        "#12",
        "1",
        "cup",
        "lunch",
        "7-eleven",
        "東京",
        "amazn",  # one typo away from words in the corpus
        "cofee",
        "luncj",
        "wholesalr",
        "ramne",
    ]
) | st.text(alphabet="amzoncfé ÜW1#-", min_size=1, max_size=4)


@st.composite
def _queries(draw: st.DrawFn) -> str:
    """Random text clauses: words, phrases, minus, field scopes."""
    clauses = []
    for _ in range(draw(st.integers(1, 3))):
        term = draw(_TERMS)
        scope = draw(st.sampled_from(["", "", "payee:", "desc:", "memo:"]))
        quoted = draw(st.booleans())
        body = f'"{term}"' if quoted else term.replace(" ", "")
        if not body.strip('"'):
            body = "x"
        if not quoted and draw(st.booleans()):
            body += "~"  # typo tolerance (phase 2)
        clauses.append(("-" if draw(st.booleans()) and clauses else "") + scope + body)
    return " ".join(clauses)


_STATUSES = st.sampled_from([None, ["pending"], ["posted"], ["pending", "posted", "void"]])
_OPS = st.lists(
    st.one_of(
        st.tuples(
            st.just("add"),
            st.sampled_from(_WORDS),
            st.sampled_from(_PAYEES),
            st.sampled_from(_MEMOS),
            st.booleans(),
            st.integers(0, 1),
        ),
        st.tuples(st.just("edit"), st.integers(0, 40), st.sampled_from(_WORDS)),
        st.tuples(st.just("memo"), st.integers(0, 40), st.sampled_from(_MEMOS)),
        st.tuples(st.just("payee"), st.integers(0, 40), st.sampled_from(_PAYEES[1:])),
        st.tuples(st.just("post"), st.integers(0, 40)),
        st.tuples(st.just("void"), st.integers(0, 40)),
        st.tuples(st.just("commit")),
    ),
    max_size=12,
)


def _world(session: Session) -> tuple[list[str], str]:
    accounts = [
        str(
            ledger_service.create_account(
                session, name=n, type_="checking", path=f"assets.{n}", actor_token_id=None
            ).id
        )
        for n in ("checking", "savings")
    ]
    cat = ledger_service.ensure_category_path(session, "expenses.misc", actor_token_id=None)
    return accounts, str(cat.id)


def _add(
    session: Session,
    accounts: list[str],
    cat: str,
    word: str | None,
    payee: str | None,
    memo: str | None,
    pending: bool,
    which: int,
) -> uuid.UUID:
    rng = random.Random(f"{word}{payee}{pending}{which}")
    tx = ledger_service.create_flat_transaction(
        session,
        account_ref=accounts[which],
        category_ref=cat,
        amount=Decimal("-3.00"),
        date=date(2026, 1, 1) + timedelta(days=rng.randrange(4)),
        payee_name=payee,
        description=None if word is None else f"{word} {rng.randrange(100)}",
        requested_status=core.Status.PENDING if pending else core.Status.POSTED,
        origin=core.Origin.IMPORT,
        token=None,
    )
    if memo is not None:  # the category leg's memo, while the row is being made
        next(p for p in tx.postings if p.account_id == uuid.UUID(cat)).memo = memo
        session.flush()
    return tx.id


def _brute_force(listing: list[Transaction], q: str) -> list[uuid.UUID]:
    """The reference: the grammar's text clauses over every loaded row."""
    clauses = parse(q).text
    return [
        t.id
        for t in listing
        if text_matches(
            clauses,
            payee=fold(t.payee.name) if t.payee else "",
            description=fold(t.description) if t.description else "",
            memos=[fold(p.memo) for p in t.postings if p.memo],
        )
    ]


class TestParity:
    """#141's acceptance: the cache engine's ids, order and total equal a
    brute-force filter over the q=None listing, for random text clauses
    (words, phrases, minus, payee:/desc:/memo:), edits of descriptions,
    memos and payees (committed or not), statuses, scopes and pages. The
    cold path is held to the same listing."""

    @settings(
        max_examples=50,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
    )
    @given(
        initial=st.lists(
            st.tuples(
                st.sampled_from(_WORDS),
                st.sampled_from(_PAYEES),
                st.sampled_from(_MEMOS),
                st.booleans(),
                st.integers(0, 1),
            ),
            max_size=30,
        ),
        ops=_OPS,
        queries=st.lists(
            st.tuples(
                _queries(),
                _STATUSES,
                st.sampled_from([None, 0, 1]),
                st.booleans(),
                st.integers(0, 40),
                st.integers(1, 40),
            ),
            min_size=1,
            max_size=4,
        ),
    )
    @example(
        initial=[("coffee", "Blue Cup", "team lunch", False, 0), ("rent", None, None, True, 1)],
        ops=[("memo", 1, "refund pending"), ("payee", 1, "Café Rosa"), ("commit",)],
        queries=[
            ("lunch", None, None, False, 0, 10),
            ("memo:refund -payee:blue", None, None, False, 0, 10),
            ("cup -lunch", None, None, False, 0, 10),
            ("payee:rosa", ["pending", "posted", "void"], None, True, 0, 10),
            ("cofee~ -luncj~", None, None, False, 0, 10),
        ],
    )
    def test_the_cache_engine_is_a_filter_of_the_listing(
        self,
        migrated_engine: Engine,
        cache: None,
        initial: list[tuple],
        ops: list[tuple],
        queries: list[tuple],
    ) -> None:
        reset_db(migrated_engine)
        textcache.reset()
        with get_sessionmaker()() as setup:
            accounts, cat = _world(setup)
            for row in initial:
                _add(setup, accounts, cat, *row)
            setup.commit()
        store = textcache.warm()
        with get_sessionmaker()() as session:
            ids = list(session.execute(select(Transaction.id)).scalars())
            for op in ops:
                if op[0] == "add":
                    ids.append(_add(session, accounts, cat, *op[1:]))
                elif op[0] == "commit":
                    session.commit()
                elif ids:
                    tx = session.get(Transaction, ids[op[1] % len(ids)])
                    if tx is None or tx.status != "pending":
                        continue
                    if op[0] == "edit":
                        tx.description = op[2]
                    elif op[0] == "memo":
                        next(p for p in tx.postings if p.account_id == uuid.UUID(cat)).memo = op[2]
                    elif op[0] == "payee":
                        tx.payee = ledger_service.resolve_payee(session, op[2])
                    else:
                        tx.status = "posted" if op[0] == "post" else "void"
                    session.flush()
            for q, statuses, which, collapsed, offset, limit in queries:
                scope = {
                    "account_ref": None if which is None else accounts[which],
                    "statuses": statuses,
                    "include_collapsed": collapsed,
                }
                listing, _ = ledger_service.search_transactions(session, **scope, limit=10_000)
                expected = _brute_force(listing, q)
                for engine in ("cache", "cold"):
                    textcache._store = store if engine == "cache" else None
                    page, total = ledger_service.search_transactions(
                        session, **scope, q=q, limit=limit, offset=offset
                    )
                    assert [t.id for t in page] == expected[offset : offset + limit], (engine, q)
                    assert total == len(expected), (engine, q)
            session.rollback()


# -- benchmark -------------------------------------------------------------

# 10k rows in CI; PURSER_BENCH_LARGE=1 runs 200k (the scale of years of
# imported history). Prints timings; asserts only that both paths agree.
_BENCH_ROWS = 200_000 if os.environ.get("PURSER_BENCH_LARGE") else 10_000
_SYLLABLES = ["ka", "lo", "mi", "ra", "ten", "vo", "zu", "bel", "cor", "dan", "fin", "gar"]
_KINDS = ["GROCERY", "COFFEE", "MARKET", "FUEL", "PHARMACY", "HARDWARE", "BOOKS", "DINER"]
_PREFIXES = ["POS PURCHASE", "CHECKCARD", "DEBIT CARD PURCHASE", "ACH DEBIT", "SQ *", ""]
_CITIES = ["SPRINGFIELD", "RIVERTON", "LAKEVIEW", "FAIRVIEW", "MIDLAND", "OAKDALE"]


def _bench_ledger(conn: object, n: int) -> tuple[uuid.UUID, list[str]]:
    """n synthetic bank-style rows over 20 accounts: generated merchant names
    with Zipf popularity, store numbers, reference ids, memos on 15%."""
    rng = random.Random(7)
    merchants: list[str] = []
    seen: set[str] = set()
    while len(merchants) < max(60, n // 60):
        word = "".join(rng.choice(_SYLLABLES) for _ in range(rng.randint(2, 4))).upper()
        name = f"{word} {rng.choice(_KINDS)}"
        if name not in seen:
            seen.add(name)
            merchants.append(name)
    execute = conn.execute  # type: ignore[attr-defined]
    execute(text("SET LOCAL session_replication_role = replica"))  # bulk load: no triggers
    accounts = [uuid.uuid4() for _ in range(20)]
    categories = [uuid.uuid4() for _ in range(10)]
    execute(
        text(
            "INSERT INTO accounts (id, name, type, path, is_virtual) "
            "VALUES (:id, :n, :t, CAST(:p AS ltree), :v)"
        ),
        [
            {"id": a, "n": f"Bench {i}", "t": "checking", "p": f"assets.b{i}", "v": False}
            for i, a in enumerate(accounts)
        ]
        + [
            {"id": c, "n": f"Cat {i}", "t": "category_expense", "p": f"expenses.c{i}", "v": True}
            for i, c in enumerate(categories)
        ],
    )
    payees = [uuid.uuid4() for _ in merchants]
    execute(
        text("INSERT INTO payees (id, name_enc, name_bidx) VALUES (:id, :n, :b)"),
        [
            {"id": p, "n": crypto.encrypt(m), "b": crypto.blind_index(core.normalize_payee(m))}
            for p, m in zip(payees, merchants, strict=True)
        ],
    )
    weights = [1.0 / (i + 1) ** 0.9 for i in range(len(merchants))]
    for start in range(0, n, 10_000):
        txs, postings = [], []
        for i in range(start, min(n, start + 10_000)):
            m = rng.choices(range(len(merchants)), weights)[0]
            style = rng.random()
            if style < 0.5:
                store_no = rng.randint(100, 9999)
                desc = f"{rng.choice(_PREFIXES)} {merchants[m]} #{store_no} {rng.choice(_CITIES)}"
            elif style < 0.8:
                desc = f"{merchants[m]} {rng.randint(10**5, 10**9)}"
            else:
                desc = merchants[m]
            tx = uuid.uuid4()
            txs.append(
                {
                    "id": tx,
                    "d": date(2016, 1, 1) + timedelta(days=rng.randrange(3650)),
                    "desc": crypto.encrypt(desc.strip()),
                    "p": payees[m],
                }
            )
            amount = Decimal(rng.randint(100, 50_000)) / 100
            memo = crypto.encrypt("work lunch") if rng.random() < 0.15 else None
            postings.append({"tx": tx, "a": accounts[i % 20], "amt": -amount, "m": memo})
            postings.append({"tx": tx, "a": categories[i % 10], "amt": amount, "m": None})
        execute(
            text(
                "INSERT INTO transactions (id, date, description_enc, payee_id, status, origin) "
                "VALUES (:id, :d, :desc, :p, 'posted', 'import')"
            ),
            txs,
        )
        execute(
            text(
                "INSERT INTO postings (transaction_id, account_id, amount, memo_enc) "
                "VALUES (:tx, :a, :amt, :m)"
            ),
            postings,
        )
    return accounts[0], merchants


def _median_ms(fn: object, runs: int = 5) -> tuple[float, object]:
    import statistics
    import time

    timings, out = [], None
    for _ in range(runs):
        started = time.perf_counter()
        out = fn()  # type: ignore[operator]
        timings.append((time.perf_counter() - started) * 1000)
    return statistics.median(timings), out


def test_search_cache_benchmark(
    clean_db: None, cache: None, migrated_engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    import time

    with migrated_engine.begin() as conn:
        account, merchants = _bench_ledger(conn, _BENCH_ROWS)
        conn.execute(text("ANALYZE transactions; ANALYZE postings"))
    word = lambda i: merchants[i].split()[0]  # noqa: E731
    queries = [
        ("rare merchant", word(-1), None),
        ("mid merchant", word(3), None),
        ("common word", "purchase", None),
        ("mid merchant, one account", word(3), str(account)),
    ]
    lines = []
    try:
        with get_sessionmaker()() as session:
            cold = {}
            for label, q, acct in queries:
                runs = 3 if _BENCH_ROWS > 50_000 else 5
                cold[label] = _median_ms(
                    lambda q=q, acct=acct: ledger_service.search_transactions(
                        session, account_ref=acct, q=q
                    ),
                    runs,
                )
            started = time.perf_counter()
            store = textcache.warm()
            build_s = time.perf_counter() - started
            for label, q, acct in queries:
                ms, (rows, total) = _median_ms(
                    lambda q=q, acct=acct: ledger_service.search_transactions(
                        session, account_ref=acct, q=q
                    )
                )
                cold_ms, (cold_rows, cold_total) = cold[label]
                assert (total, [t.id for t in rows]) == (cold_total, [t.id for t in cold_rows])
                scan_ms, ids = _median_ms(lambda q=q: _ids(store, session, q))
                lines.append(
                    f"  {label:27} matches {total:>7}  cold {cold_ms:7.0f} ms  cache {ms:6.1f} ms"
                    f"  (memory {scan_ms:5.1f} ms for {len(ids)} ids)"  # type: ignore[arg-type]
                )
            session.rollback()
        stats = store.stats()
        with capsys.disabled():
            print(
                f"\n[search-cache-bench] n={_BENCH_ROWS}: build {build_s * 1000:.0f} ms, "
                f"~{stats['mib']} MiB, {stats['payees']} payees\n" + "\n".join(lines)
            )
    finally:
        reset_db(migrated_engine)


class TestTypos:
    """``word~`` (phase 2): the cache finds every row with a word one edit
    away, by generated spellings or, for a large alphabet, by comparing
    every word of a nearby length — the same rows either way."""

    def test_both_neighbour_searches_agree(
        self, clean_db: None, cache: None, migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with migrated_engine.begin() as conn:
            rows = {
                "STARBUCKS #12": _insert(conn, "STARBUCKS #12"),
                "starbuck coffee": _insert(conn, "starbuck coffee"),
                "STARBUKS reserve": _insert(conn, "STARBUKS reserve"),
                "starbucks2 store": _insert(conn, "starbucks2 store"),  # a digit: not a word
                "東京都 ラーメン": _insert(conn, "東京都 ラーメン"),
            }
        textcache.warm()
        generated = _matches("starbucks~")
        assert generated == {
            rows["STARBUCKS #12"],
            rows["starbuck coffee"],
            rows["STARBUKS reserve"],
            rows["starbucks2 store"],  # by substring, as without the ~
        }
        assert _matches("starbuk~") >= {rows["STARBUKS reserve"], rows["starbuck coffee"]}
        monkeypatch.setattr(textcache, "MAX_ALPHABET", 1)
        assert _matches("starbucks~") == generated
        assert _matches("東亰都~") == {rows["東京都 ラーメン"]}

    def test_did_you_mean(self, clean_db: None, cache: None, migrated_engine: Engine) -> None:
        with migrated_engine.begin() as conn:
            for _ in range(3):
                _insert(conn, "STARBUCKS #12")
            _insert(conn, "STARBUCKZ")
        store = textcache.warm()
        with get_sessionmaker()() as session:
            assert ledger_service.did_you_mean(session, "starbuks") == "starbucks"
            assert ledger_service.did_you_mean(session, "desc:Starbuks date:2026") == (
                "desc:starbucks date:2026"
            )
            assert ledger_service.did_you_mean(session, "starbucks") is None  # found
            assert ledger_service.did_you_mean(session, "starbuks coffee") is None  # two words
            assert ledger_service.did_you_mean(session, "-starbuks") is None
            assert ledger_service.did_you_mean(session, "memo:starbuks") is None  # not in memos
            assert ledger_service.did_you_mean(session, "zzzzzz") is None
            assert ledger_service.did_you_mean(session, "paye:x") is None  # malformed
            session.rollback()
        textcache.reset()
        with get_sessionmaker()() as session:
            assert ledger_service.did_you_mean(session, "starbuks") is None  # no cache
            session.rollback()
        del store
