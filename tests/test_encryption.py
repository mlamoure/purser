"""Encryption at rest is required (issue #93): startup enforcement, the
key↔database canary, and MultiFernet rotation readiness."""

import os
from collections.abc import Iterator

import pytest
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import Engine, text

import purser.config
from purser import crypto
from tests.conftest import TEST_ENCRYPTION_KEY


@pytest.fixture
def restore_key() -> Iterator[None]:
    """Tests below swap or pop PURSER_ENCRYPTION_KEY; always restore the
    suite-wide key afterwards."""
    yield
    os.environ["PURSER_ENCRYPTION_KEY"] = TEST_ENCRYPTION_KEY
    purser.config.get_settings.cache_clear()


def _set_key(value: str | None) -> None:
    if value is None:
        os.environ.pop("PURSER_ENCRYPTION_KEY", None)
    else:
        os.environ["PURSER_ENCRYPTION_KEY"] = value
    purser.config.get_settings.cache_clear()


class TestStartupEnforcement:
    def test_create_app_fails_without_key(self, migrated_engine: Engine, restore_key: None) -> None:
        from purser.api.app import create_app

        _set_key(None)
        with pytest.raises(crypto.EncryptionKeyMissing, match="purser keygen"):
            create_app()

    def test_invalid_key_is_clear_error(self, migrated_engine: Engine, restore_key: None) -> None:
        _set_key("not-a-fernet-key")
        with pytest.raises(crypto.EncryptionKeyInvalid, match="purser keygen"):
            crypto.ensure_key()

    def test_keygen_prints_a_usable_key(self) -> None:
        from typer.testing import CliRunner

        from purser.cli import app

        result = CliRunner().invoke(app, ["keygen"])
        assert result.exit_code == 0
        Fernet(result.output.strip().encode())  # must parse as a Fernet key


class TestCanary:
    def test_canary_written_and_verifies(self, migrated_engine: Engine) -> None:
        with migrated_engine.connect() as conn:
            row = conn.execute(
                text("SELECT sentinel_enc, key_fingerprint FROM encryption_canary")
            ).one()
            assert crypto.decrypt(row.sentinel_enc) == crypto.CANARY_SENTINEL
            assert row.key_fingerprint == crypto.key_fingerprint()
            crypto.verify_canary(conn)  # must not raise

    def test_wrong_key_is_clear_error(self, migrated_engine: Engine, restore_key: None) -> None:
        _set_key(Fernet.generate_key().decode())
        with migrated_engine.connect() as conn, pytest.raises(crypto.EncryptionKeyMismatch) as exc:
            crypto.verify_canary(conn)
        assert crypto.key_fingerprint(TEST_ENCRYPTION_KEY) in str(exc.value)
        assert crypto.key_fingerprint() in str(exc.value)

    def test_rotated_multifernet_still_verifies(
        self, migrated_engine: Engine, restore_key: None
    ) -> None:
        """Comma-separated keys: a new primary with the old key appended can
        still decrypt everything written under the old key."""
        new_primary = Fernet.generate_key().decode()
        ciphertext_old = crypto.encrypt("written-under-old-key")
        _set_key(f"{new_primary},{TEST_ENCRYPTION_KEY}")
        assert crypto.decrypt(ciphertext_old) == "written-under-old-key"
        with migrated_engine.connect() as conn:
            crypto.verify_canary(conn)  # canary was written under the old key
        # ...and new writes use the new primary: the old key alone can't read them.
        ciphertext_new = crypto.encrypt("written-under-new-primary")
        _set_key(TEST_ENCRYPTION_KEY)
        with pytest.raises(InvalidToken):
            crypto.decrypt(ciphertext_new)


class TestFernetCache:
    """The MultiFernet is built once per key material (#658): the same key
    reuses it, and any change to PURSER_ENCRYPTION_KEY — a rotation's
    "new,old", a removed or malformed key — never gets the stale object."""

    def test_same_key_reuses_one_object(self, restore_key: None) -> None:
        _set_key(TEST_ENCRYPTION_KEY)
        first = crypto._fernet()
        assert crypto._fernet() is first
        token = crypto.encrypt("reused")
        assert crypto.decrypt(token) == "reused"
        assert crypto._fernet() is first

    def test_built_once_for_many_calls(
        self, restore_key: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        built: list[int] = []
        real = crypto.MultiFernet

        def counting(fernets: list[Fernet]) -> crypto.MultiFernet:
            built.append(len(fernets))
            return real(fernets)

        monkeypatch.setattr(crypto, "MultiFernet", counting)
        _set_key(f"{Fernet.generate_key().decode()},{TEST_ENCRYPTION_KEY}")
        tokens = [crypto.encrypt(f"row {i}") for i in range(50)]
        assert [crypto.decrypt(t) for t in tokens] == [f"row {i}" for i in range(50)]
        assert built == [2]

    def test_rotation_keys_get_a_new_object(self, restore_key: None) -> None:
        """During a rotation (new,old) old ciphertext still decrypts and new
        writes use the new primary; going back to the old key alone builds a
        third object that cannot read the new writes."""
        _set_key(TEST_ENCRYPTION_KEY)
        old_object = crypto._fernet()
        written_old = crypto.encrypt("written under the old key")

        new_primary = Fernet.generate_key().decode()
        _set_key(f"{new_primary},{TEST_ENCRYPTION_KEY}")
        rotating = crypto._fernet()
        assert rotating is not old_object
        assert crypto.decrypt(written_old) == "written under the old key"
        written_new = crypto.encrypt("written under the new primary")
        assert Fernet(new_primary.encode()).decrypt(written_new) == b"written under the new primary"
        assert crypto.decrypt(crypto.rotate_ciphertext(written_old)) == "written under the old key"

        _set_key(TEST_ENCRYPTION_KEY)
        assert crypto._fernet() is not rotating
        with pytest.raises(InvalidToken):
            crypto.decrypt(written_new)

        _set_key(new_primary)
        assert crypto.decrypt(written_new) == "written under the new primary"
        with pytest.raises(InvalidToken):
            crypto.decrypt(written_old)

    def test_removed_or_invalid_key_is_never_served_from_the_cache(self, restore_key: None) -> None:
        _set_key(TEST_ENCRYPTION_KEY)
        crypto._fernet()
        _set_key(None)
        with pytest.raises(crypto.EncryptionKeyMissing):
            crypto.encrypt("no key")
        _set_key(" , ")
        with pytest.raises(crypto.EncryptionKeyMissing):
            crypto.decrypt(b"anything")
        _set_key("not-a-fernet-key")
        with pytest.raises(crypto.EncryptionKeyInvalid):
            crypto.ensure_key()
        with pytest.raises(crypto.EncryptionKeyInvalid):  # a bad key is never cached
            crypto.ensure_key()
        _set_key(TEST_ENCRYPTION_KEY)
        assert crypto.decrypt(crypto.encrypt("back")) == "back"
