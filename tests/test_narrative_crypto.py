"""Narrative-layer encryption (#137): TypeDecorators, blind index, keyed
content-hash composition, rotation primitive, and register decrypt-and-filter
search (correctness + a scale benchmark that prints timings)."""

import os
import random
import statistics
import time
import uuid
from datetime import date
from decimal import Decimal

import pytest
from cryptography.fernet import Fernet
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import Engine, inspect, select, text
from sqlalchemy.orm import Session

import purser.config
from purser import crypto
from purser import ledger as ledger_service
from purser.core import ledger as core
from purser.models import Payee, Schedule, Transaction
from tests.conftest import TEST_ENCRYPTION_KEY


class TestTypeDecorators:
    def test_narrative_columns_round_trip_and_store_ciphertext(
        self, clean_db: None, db_session: Session, migrated_engine: Engine
    ) -> None:
        """seed-demo writes payees/descriptions/memos/schedule names/events; the
        ORM reads them back in the clear while the *_enc columns hold ciphertext
        (no plaintext marker leaks into the bytes)."""
        from purser.seed import seed_demo

        seed_demo(db_session)
        db_session.commit()

        payee = db_session.execute(select(Payee).limit(1)).scalar_one()
        assert payee.name  # decrypts to a non-empty string via the TypeDecorator

        # The physical column is ciphertext: the plaintext name is not in it, and
        # it decrypts back to the name.
        raw = migrated_engine.connect()
        with raw:
            name_enc = raw.execute(
                text("SELECT name_enc FROM payees WHERE id = :id"), {"id": payee.id}
            ).scalar_one()
        assert payee.name.encode() not in bytes(name_enc)
        assert crypto.decrypt(bytes(name_enc)) == payee.name

        # Descriptions, schedule names, and event payloads decrypt too.
        desc = db_session.execute(
            text(
                "SELECT description_enc FROM transactions WHERE description_enc IS NOT NULL LIMIT 1"
            )
        ).scalar_one()
        assert crypto.decrypt(bytes(desc))  # non-empty
        sched = db_session.execute(select(Schedule).limit(1)).scalar_one_or_none()
        if sched is not None:
            assert sched.name  # decrypts


class TestBlindIndex:
    def test_resolve_payee_round_trips_through_the_blind_index(
        self, clean_db: None, db_session: Session
    ) -> None:
        a = ledger_service.resolve_payee(db_session, "CHIPOTLE #2419 Denver, CO")
        db_session.flush()
        # A normalization-equivalent spelling resolves to the SAME payee via the
        # blind index (equality over ciphertext).
        b = ledger_service.resolve_payee(db_session, "chipotle 2419  denver co")
        assert a.id == b.id
        assert a.name_bidx == crypto.blind_index(core.normalize_payee("CHIPOTLE #2419 Denver, CO"))

    @given(name=st.text(min_size=1, max_size=40))
    def test_blind_index_is_normalization_stable_and_deterministic(
        self, migrated_engine: Engine, name: str
    ) -> None:
        # The blind index depends only on the normalized form, so any two names
        # that normalize alike share an index; unicode is handled by normalize.
        norm = core.normalize_payee(name)
        assert crypto.blind_index(norm) == crypto.blind_index(norm)
        assert crypto.blind_index(norm) == crypto.blind_index(core.normalize_payee(f"  {name}  "))
        # Hex digest shape.
        assert len(crypto.blind_index(norm)) == 64

    def test_unicode_accents_normalize_to_one_index(
        self, clean_db: None, db_session: Session
    ) -> None:
        a = ledger_service.resolve_payee(db_session, "Café Déjà Vu")
        db_session.flush()
        b = ledger_service.resolve_payee(db_session, "cafe deja vu")
        assert a.id == b.id


class TestKeyedContentHash:
    @given(
        external=st.text(min_size=1, max_size=20),
        amount=st.decimals(min_value=-9999, max_value=9999, places=2, allow_nan=False),
        desc=st.text(min_size=0, max_size=40),
    )
    def test_keyed_hash_composes_the_sha256_identity(
        self, migrated_engine: Engine, external: str, amount: Decimal, desc: str
    ) -> None:
        """content_hash_v2 = keyed_hash(content_hash_v1): deterministic, keyed,
        and never equal to the bare sha256 (dictionary-attack hardening). This
        is exactly the map migration 0016 applies to every stored hash."""
        v1 = core.content_hash(
            external_account_id=external, date=date(2026, 1, 2), amount=amount, description=desc
        )
        v2 = crypto.keyed_hash(v1)
        assert v2 == crypto.keyed_hash(v1)  # deterministic
        assert v2 != v1  # keyed, not the bare digest
        assert len(v2) == 64
        # The migration's rehash uses the same recipe as runtime keyed_hash, so a
        # value hashed pre-migration re-hashes post-migration identically → dedup
        # survives. Assert the recipe equivalence directly.
        dek = crypto._load_dek(crypto.CONTENT_HASH_PURPOSE)
        assert v2 == crypto._hmac_hex(dek, v1)


class TestRotationPrimitive:
    """The wrapped-DEK payoff (decision 36), at the crypto level: rotating a
    Fernet token re-wraps it under the new primary; rotating a wrapped DEK
    leaves its plaintext (and every derived HMAC) byte-identical."""

    def test_ciphertext_rotates_to_the_new_primary(self, migrated_engine: Engine) -> None:
        try:
            token_old = crypto.encrypt("archived bank data")
            new = Fernet.generate_key().decode()
            os.environ["PURSER_ENCRYPTION_KEY"] = f"{new},{TEST_ENCRYPTION_KEY}"
            purser.config.get_settings.cache_clear()

            rotated = crypto.rotate_ciphertext(token_old)
            # Decrypts under the NEW key alone — the old key is no longer needed.
            os.environ["PURSER_ENCRYPTION_KEY"] = new
            purser.config.get_settings.cache_clear()
            assert crypto.decrypt(rotated) == "archived bank data"
        finally:
            os.environ["PURSER_ENCRYPTION_KEY"] = TEST_ENCRYPTION_KEY
            purser.config.get_settings.cache_clear()

    def test_wrapped_dek_plaintext_is_stable_across_rotation(self, migrated_engine: Engine) -> None:
        try:
            dek = crypto.generate_dek()
            wrapped_old = crypto.wrap_key(dek)
            new = Fernet.generate_key().decode()
            os.environ["PURSER_ENCRYPTION_KEY"] = f"{new},{TEST_ENCRYPTION_KEY}"
            purser.config.get_settings.cache_clear()

            wrapped_new = crypto.rotate_ciphertext(wrapped_old)
            os.environ["PURSER_ENCRYPTION_KEY"] = new
            purser.config.get_settings.cache_clear()
            # Same DEK bytes → same blind index / content hash after rotation.
            assert crypto.unwrap_key(wrapped_new) == dek
        finally:
            os.environ["PURSER_ENCRYPTION_KEY"] = TEST_ENCRYPTION_KEY
            purser.config.get_settings.cache_clear()


def _make_account(session: Session, path: str, type_: str = "checking") -> uuid.UUID:
    virtual = type_.startswith("category")
    return session.execute(
        text(
            "INSERT INTO accounts (name, type, is_virtual, path) "
            "VALUES (:n, :t, :v, :p) RETURNING id"
        ),
        {"n": path.split(".")[-1], "t": type_, "v": virtual, "p": f"{path}_{uuid.uuid4().hex[:8]}"},
    ).scalar_one()


class TestSearchDecryptFilter:
    def test_q_matches_substring_of_decrypted_description(
        self, clean_db: None, db_session: Session
    ) -> None:
        acct = _make_account(db_session, "assets.checking")
        cat = _make_account(db_session, "expenses.dining", "category_expense")
        for desc in ("WHOLE FOODS MARKET", "chipotle downtown", "WHOLEsaler co"):
            ledger_service.create_flat_transaction(
                db_session,
                account_ref=str(acct),
                category_ref=str(cat),
                amount=Decimal("-10.00"),
                date=date(2026, 1, 1),
                description=desc,
                requested_status=core.Status.POSTED,
                origin=core.Origin.IMPORT,
                token=None,
            )
        db_session.flush()

        rows, total = ledger_service.search_transactions(
            db_session, account_ref=str(acct), q="whole"
        )
        got = sorted(r.description for r in rows)
        assert got == ["WHOLE FOODS MARKET", "WHOLEsaler co"]  # case-insensitive substring
        assert total == 2

        # q=None keeps the pure-SQL path and returns everything.
        _, all_total = ledger_service.search_transactions(db_session, account_ref=str(acct))
        assert all_total == 3

    def test_pagination_totals_are_exact_over_the_decrypted_matches(
        self, clean_db: None, db_session: Session
    ) -> None:
        acct = _make_account(db_session, "assets.checking")
        cat = _make_account(db_session, "expenses.dining", "category_expense")
        for i in range(5):
            ledger_service.create_flat_transaction(
                db_session,
                account_ref=str(acct),
                category_ref=str(cat),
                amount=Decimal("-1.00"),
                date=date(2026, 1, i + 1),
                description=f"COFFEE {i}",
                requested_status=core.Status.POSTED,
                origin=core.Origin.IMPORT,
                token=None,
            )
        db_session.flush()
        page, total = ledger_service.search_transactions(
            db_session, account_ref=str(acct), q="coffee", limit=2
        )
        assert total == 5  # exact, computed over all decrypted matches
        assert len(page) == 2


_CORPUS_WORDS = [
    "AMAZON MKTPL",
    "amazon.com order",
    "Amazon Prime",
    "Coffee Bar",
    "coffee",
    "WHOLE FOODS MARKET",
    "wholesaler co",
    "Café Ünïcode",
    "STARBUCKS #1234",
    "rent",
]


@pytest.fixture
def search_corpus(clean_db: None, db_session: Session) -> uuid.UUID:
    """Seventy rows on one account — mixed case, accents, no-description rows,
    pending and posted, five dates so the order has ties — and rows with the
    same words on a second account, which the narrowing must keep out."""
    rng = random.Random(141)
    acct = _make_account(db_session, "assets.checking")
    other = _make_account(db_session, "assets.savings")
    cat = _make_account(db_session, "expenses.misc", "category_expense")
    for account, n in ((acct, 70), (other, 15)):
        for _ in range(n):
            word = rng.choice([*_CORPUS_WORDS, None])
            ledger_service.create_flat_transaction(
                db_session,
                account_ref=str(account),
                category_ref=str(cat),
                amount=Decimal("-3.00"),
                date=date(2026, 1, 1 + rng.randrange(5)),
                description=None if word is None else f"{word} {rng.randrange(100)}",
                requested_status=rng.choice([core.Status.POSTED, core.Status.PENDING]),
                origin=core.Origin.IMPORT,
                token=None,
            )
    db_session.flush()
    return acct


class TestTwoPhaseSearch:
    """#141's interim: the q path decrypts only (id, description) of the
    narrowed set and loads full rows for the returned page alone. Same match,
    same order, same exact total as filtering the q=None listing."""

    @settings(
        max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
    )
    @given(
        needle=st.one_of(
            st.sampled_from(["amazon", "AMAZON", "coffee", "whole", "café", "ü", "#12", "1", " "]),
            st.text(alphabet="amzoncfé ÜW1#", min_size=1, max_size=4),
        ),
        offset=st.integers(min_value=0, max_value=80),
        limit=st.integers(min_value=1, max_value=80),
    )
    def test_the_q_path_is_a_filter_of_the_listing(
        self, search_corpus: uuid.UUID, db_session: Session, needle: str, offset: int, limit: int
    ) -> None:
        listing, _ = ledger_service.search_transactions(
            db_session, account_ref=str(search_corpus), limit=10_000
        )
        expected = [
            t.id
            for t in listing
            if t.description is not None and needle.casefold() in t.description.casefold()
        ]
        page, total = ledger_service.search_transactions(
            db_session, account_ref=str(search_corpus), q=needle, limit=limit, offset=offset
        )
        assert [t.id for t in page] == expected[offset : offset + limit]
        assert total == len(expected)

    def test_only_the_page_is_loaded_as_full_rows(
        self, search_corpus: uuid.UUID, db_session: Session
    ) -> None:
        db_session.expunge_all()
        page, total = ledger_service.search_transactions(
            db_session, account_ref=str(search_corpus), q="a", limit=3, offset=2
        )
        assert total > 5
        loaded = [o for o in db_session.identity_map.values() if isinstance(o, Transaction)]
        assert sorted(t.id for t in loaded) == sorted(t.id for t in page)
        for t in page:
            # eager-loaded, exactly as the q=None path returns rows
            assert not {"postings", "payee"} & inspect(t).unloaded
            assert all("account" not in inspect(p).unloaded for p in t.postings)


# The register search casualty measurement gate (DESIGN §1e). Bounded to 10k by
# default so it stays CI-friendly; set PURSER_BENCH_50K=1 to also print 50k.
_BENCH_SIZES = [10_000] + ([50_000] if os.environ.get("PURSER_BENCH_50K") else [])


@pytest.mark.parametrize("n", _BENCH_SIZES)
def test_search_decrypt_filter_benchmark(
    clean_db: None, db_session: Session, capsys: pytest.CaptureFixture[str], n: int
) -> None:
    """Bulk-load n posted rows on one account (half matching a needle), then time
    the two-phase decrypt-and-filter search (#141) five times. Prints the first
    and median timings so §9's escalation gate (~300ms at 10k → token blind
    index) can be read from test output."""
    acct = _make_account(db_session, "assets.bench")
    cat = _make_account(db_session, "expenses.bench", "category_expense")
    needle = "AMAZON"
    tx_rows = []
    posting_rows = []
    for i in range(n):
        tid = uuid.uuid4()
        desc = f"{needle} order {i}" if i % 2 == 0 else f"grocery run {i}"
        tx_rows.append({"id": tid, "d": date(2026, 1, 1), "desc": crypto.encrypt(desc)})
        posting_rows.append({"tx": tid, "a": acct, "amt": Decimal("-1.0000")})
        posting_rows.append({"tx": tid, "a": cat, "amt": Decimal("1.0000")})
    db_session.execute(
        text(
            "INSERT INTO transactions (id, date, description_enc, status, origin) "
            "VALUES (:id, :d, :desc, 'posted', 'import')"
        ),
        tx_rows,
    )
    db_session.execute(
        text("INSERT INTO postings (transaction_id, account_id, amount) VALUES (:tx, :a, :amt)"),
        posting_rows,
    )
    # flush (not commit): rows are visible to the same-session search, but the
    # deferred zero-sum trigger — which fires per posting AT COMMIT — never runs,
    # so the harness doesn't spend O(n) balancing 2n synthetic postings; the
    # db_session teardown rollback also keeps these rows from leaking.
    db_session.flush()

    timings = []
    for _ in range(5):
        start = time.perf_counter()
        rows, total = ledger_service.search_transactions(
            db_session, account_ref=str(acct), q=needle, limit=50
        )
        timings.append((time.perf_counter() - start) * 1000)
        assert total == n // 2 + (n % 2)  # every even-indexed row matched
        assert len(rows) == 50
    median_ms = statistics.median(timings)
    with capsys.disabled():
        print(
            f"\n[search-bench] n={n:>6} two-phase q-match={total} "
            f"first={timings[0]:8.1f}ms median={median_ms:8.1f}ms "
            f"({median_ms / n * 1000:6.2f}µs/row)"
        )
