"""The withhold names dictionary is cached under a fingerprint of the tables
it reads (DESIGN §9). The cache must follow every committed change whatever
order concurrent writers commit in (#648 L3): keyed on each table's row count
and newest row version, a rename by an older transaction that committed
after a newer one, and after a build, left the old name in the cache, and the
new name went out in the clear under ``withhold=payee``. Synthetic data only.
"""

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text

import purser.db
from purser import crypto, privacy
from purser.core.ledger import normalize_payee

OLD, NEW, OTHER = "Gossamer Lanterns", "Gossamer Lamps Co", "Tamarisk Florist"


def _payee(conn: object, name: str) -> object:
    return conn.execute(  # type: ignore[attr-defined]
        text("INSERT INTO payees (name_enc, name_bidx) VALUES (:n, :b) RETURNING id"),
        {"n": crypto.encrypt(name), "b": crypto.blind_index(normalize_payee(name))},
    ).scalar_one()


def _rename(conn: object, payee_id: object, name: str) -> None:
    conn.execute(  # type: ignore[attr-defined]
        text("UPDATE payees SET name_enc = :n, name_bidx = :b WHERE id = :id"),
        {"n": crypto.encrypt(name), "b": crypto.blind_index(normalize_payee(name)), "id": payee_id},
    )


def _dictionary() -> privacy.Dictionary:
    with purser.db.get_sessionmaker()() as session:
        return privacy.load_dictionary(session)


@pytest.fixture
def payees(clean_db: None, migrated_engine: Engine) -> Iterator[tuple[object, object]]:
    privacy.reset_cache()
    with migrated_engine.begin() as conn:
        renamed, other = _payee(conn, OLD), _payee(conn, OTHER)
    yield renamed, other
    privacy.reset_cache()


class TestDictionaryCache:
    def test_an_older_writer_committing_after_a_build_still_refreshes_it(
        self, payees: tuple[object, object], migrated_engine: Engine
    ) -> None:
        renamed, other = payees
        assert OLD in _dictionary().payees
        older = migrated_engine.connect()
        newer = migrated_engine.connect()
        try:
            # A renames first and so holds the older transaction id ...
            a = older.begin()
            _rename(older, renamed, NEW)
            # ... B touches another payee with a newer id and commits first.
            with newer.begin():
                _rename(newer, other, f"{OTHER} Ltd")
            # A request builds the dictionary now: B's change, not A's.
            built = _dictionary()
            assert f"{OTHER} Ltd" in built.payees and OLD in built.payees
            a.commit()
        finally:
            older.close()
            newer.close()

        # A's commit moved neither the row count nor the newest row version;
        # the next request must still see the new name.
        names = _dictionary().payees
        assert NEW in names and OLD not in names

        # And a payee scrubbed by value under withhold=payee is the new name.
        scrubber = privacy.Scrubber(
            withheld={privacy.PAYEE},
            emits=privacy.Emits(payee=True),
            dictionary=_dictionary(),
        )
        assert privacy.scrub({"payee": NEW}, scrubber)["payee"].startswith("py_")

    def test_an_unchanged_ledger_reuses_the_build(self, payees: tuple[object, object]) -> None:
        first = _dictionary()
        assert _dictionary() is first
