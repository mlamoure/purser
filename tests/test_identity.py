"""Identity & access: bootstrap, sessions, scoped tokens, redaction, audit."""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from argon2 import extract_parameters
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

import purser.config
from purser.admin import security, service
from tests.conftest import ADMIN_CREDENTIALS


def _setup_admin(client: TestClient) -> None:
    resp = client.post("/auth/setup", json=ADMIN_CREDENTIALS)
    assert resp.status_code == 201, resp.text


def _agent_token(client: TestClient, scopes: list[str], name: str = "agent") -> str:
    resp = client.post("/tokens", json={"name": name, "scopes": scopes})
    assert resp.status_code == 201, resp.text
    return resp.json()["token"]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class TestBootstrapAndSessions:
    def test_setup_creates_admin_and_session(self, clean_db: None, client: TestClient) -> None:
        _setup_admin(client)
        me = client.get("/auth/me")
        assert me.status_code == 200
        assert me.json()["user"]["role"] == "admin"
        assert "admin" in me.json()["scopes"]

    def test_setup_only_once(self, clean_db: None, client: TestClient) -> None:
        _setup_admin(client)
        resp = client.post(
            "/auth/setup", json={"email": "two@example.com", "password": "password123"}
        )
        assert resp.status_code == 409

    def test_login_wrong_password_rejected(self, admin_client: TestClient) -> None:
        admin_client.cookies.clear()
        resp = admin_client.post(
            "/auth/login", json={"email": ADMIN_CREDENTIALS["email"], "password": "wrong-password"}
        )
        assert resp.status_code == 401

    def test_login_email_ignores_case(self, admin_client: TestClient) -> None:
        """#429 D4: the account is found whatever the case of the email."""
        admin_client.cookies.clear()
        local, domain = ADMIN_CREDENTIALS["email"].split("@")
        resp = admin_client.post(
            "/auth/login",
            json={"email": f"{local.upper()}@{domain}", "password": ADMIN_CREDENTIALS["password"]},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["email"] == ADMIN_CREDENTIALS["email"]  # stored as entered

    def test_email_is_unique_regardless_of_case(
        self, admin_client: TestClient, migrated_engine: Engine
    ) -> None:
        from sqlalchemy.exc import IntegrityError

        first = admin_client.post("/users", json={"email": "Pat.Doe@example.com"})
        assert first.status_code == 201
        assert first.json()["email"] == "Pat.Doe@example.com"
        resp = admin_client.post("/users", json={"email": "pat.doe@example.com"})
        assert resp.status_code == 409
        # The database enforces it too, not only the route.
        with pytest.raises(IntegrityError), migrated_engine.begin() as conn:
            conn.execute(text("INSERT INTO users (email) VALUES ('PAT.DOE@example.com')"))

    def test_logout_revokes_session(self, admin_client: TestClient) -> None:
        assert admin_client.post("/auth/logout").status_code == 200
        assert admin_client.get("/auth/me").status_code == 401

    def test_unauthenticated_rejected(self, clean_db: None, client: TestClient) -> None:
        assert client.get("/auth/me").status_code == 401
        assert client.get("/accounts").status_code == 401

    def test_session_kind_ttl_and_current(self, admin_client: TestClient) -> None:
        (session_row,) = admin_client.get("/tokens").json()
        assert session_row["kind"] == "session"
        assert session_row["is_current"] is True
        expires = datetime.fromisoformat(session_row["expires_at"])
        expected = datetime.now(UTC) + service.session_ttl()
        assert abs((expires - expected).total_seconds()) < 300

    def test_session_ttl_configurable(
        self, clean_db: None, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PURSER_SESSION_TTL_DAYS", "1")
        purser.config.get_settings.cache_clear()
        try:
            resp = client.post("/auth/setup", json=ADMIN_CREDENTIALS)
            assert resp.status_code == 201
            assert "max-age=86400" in resp.headers["set-cookie"].lower()
            (session_row,) = client.get("/tokens").json()
            expires = datetime.fromisoformat(session_row["expires_at"])
            expected = datetime.now(UTC) + timedelta(days=1)
            assert abs((expires - expected).total_seconds()) < 300
        finally:
            purser.config.get_settings.cache_clear()

    def test_idle_session_rejected(self, admin_client: TestClient, migrated_engine: Engine) -> None:
        assert admin_client.get("/auth/me").status_code == 200
        # Push the session past the (default 8h) inactivity window.
        with migrated_engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE api_tokens SET last_used_at = now() - interval '9 hours' "
                    "WHERE kind = 'session'"
                )
            )
        assert admin_client.get("/auth/me").status_code == 401


def _count_password_checks(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Record every argon2 verification as (hash checked against, password)."""
    calls: list[tuple[str, str]] = []
    real_verify = security.verify_password

    def counting_verify(password_hash: str, password: str) -> bool:
        calls.append((password_hash, password))
        return real_verify(password_hash, password)

    monkeypatch.setattr(security, "verify_password", counting_verify)
    return calls


def _login(client: TestClient, email: str, password: str) -> Response:
    return client.post("/auth/login", json={"email": email, "password": password})


class TestSignInRevealsNoAccount:
    """Issue #304: an unknown email costs the same argon2 verification as a
    wrong password and gets the same 401, so neither the response time nor
    the answer says whether an account exists."""

    def test_unknown_email_still_runs_the_password_hash(
        self, admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        admin_client.cookies.clear()
        checks = _count_password_checks(monkeypatch)
        assert _login(admin_client, "nobody@example.com", "guess-1234").status_code == 401
        assert checks == [(security.dummy_password_hash(), "guess-1234")]

    def test_account_without_a_password_still_runs_the_password_hash(
        self, admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An invited or SSO-only user: no password hash at all.
        assert (
            admin_client.post("/users", json={"email": "sso-only@example.com"}).status_code == 201
        )
        admin_client.cookies.clear()
        checks = _count_password_checks(monkeypatch)
        assert _login(admin_client, "sso-only@example.com", "guess-1234").status_code == 401
        assert checks == [(security.dummy_password_hash(), "guess-1234")]

    def test_unknown_email_and_wrong_password_get_the_same_answer(
        self, admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert (
            admin_client.post("/users", json={"email": "sso-only@example.com"}).status_code == 201
        )
        admin_client.cookies.clear()
        checks = _count_password_checks(monkeypatch)
        wrong = _login(admin_client, ADMIN_CREDENTIALS["email"], "guess-1234")
        unknown = _login(admin_client, "nobody@example.com", "guess-1234")
        passwordless = _login(admin_client, "sso-only@example.com", "guess-1234")

        # One verification per attempt on every path: the admin's real hash,
        # then the dummy for the two that have none.
        assert [password for _, password in checks] == ["guess-1234"] * 3
        assert checks[0][0] != security.dummy_password_hash()
        assert checks[1][0] == checks[2][0] == security.dummy_password_hash()
        for resp in (unknown, passwordless):
            assert resp.status_code == wrong.status_code == 401
            assert resp.content == wrong.content
            assert dict(resp.headers) == dict(wrong.headers)
        assert wrong.json() == {"detail": "invalid email or password"}

    def test_the_dummy_hash_costs_what_a_real_one_costs(self) -> None:
        dummy = security.dummy_password_hash()
        assert dummy == security.dummy_password_hash()  # fixed: made once per process
        # Same algorithm and cost parameters as a real password's hash, so
        # verifying against it takes the same time.
        assert extract_parameters(dummy) == extract_parameters(
            security.hash_password("correct-horse-battery")
        )
        assert not security.verify_password(dummy, "")
        assert not security.verify_password(dummy, "correct-horse-battery")


class TestScopedTokens:
    def test_token_returned_exactly_once(self, admin_client: TestClient) -> None:
        raw = _agent_token(admin_client, ["read", "write"])
        assert raw.startswith("purser_")
        listing = admin_client.get("/tokens").json()
        assert all("token" not in t for t in listing)

    def test_a_read_token_cannot_pass_the_write_gate(self, admin_client: TestClient) -> None:
        # The write gate is the structural mechanism ledger writes sit behind
        # (#3 wires it, #436 renamed it); a read-only token never passes it,
        # a write token does.
        from purser.admin.auth import WriteToken

        reader = _agent_token(admin_client, ["read"])
        writer = _agent_token(admin_client, ["read", "write"], name="writer")

        gate = FastAPI()

        @gate.post("/ledger-write")
        def ledger_write(token: WriteToken) -> dict[str, str]:
            return {"status": "written"}

        with TestClient(gate) as gated:
            denied = gated.post("/ledger-write", headers=_bearer(reader))
            assert denied.status_code == 403
            assert denied.json()["detail"] == "requires scope: write"
            assert gated.post("/ledger-write", headers=_bearer(writer)).status_code == 200

    def test_non_admin_cannot_mint_tokens(self, admin_client: TestClient) -> None:
        # Minting is admin-only (#207): a leaked non-admin token must not be
        # able to mint clones of itself — not even at or below its own scope.
        raw = _agent_token(admin_client, ["read", "write"])
        escalation = admin_client.post(
            "/tokens",
            json={"name": "escalation", "scopes": ["commit"]},
            headers=_bearer(raw),
        )
        assert escalation.status_code == 403
        clone = admin_client.post(
            "/tokens",
            json={"name": "clone", "scopes": ["read", "write"]},
            headers=_bearer(raw),
        )
        assert clone.status_code == 403

    def test_expired_token_rejected(self, admin_client: TestClient) -> None:
        past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
        resp = admin_client.post(
            "/tokens", json={"name": "stale", "scopes": ["read"], "expires_at": past}
        )
        raw = resp.json()["token"]
        admin_client.cookies.clear()
        assert admin_client.get("/accounts", headers=_bearer(raw)).status_code == 401

    def test_last_used_is_tracked(self, admin_client: TestClient) -> None:
        raw = _agent_token(admin_client, ["read"])
        admin_client.get("/accounts", headers=_bearer(raw))
        tokens = admin_client.get("/tokens").json()
        agent = next(t for t in tokens if t["name"] == "agent")
        assert agent["last_used_at"] is not None

    def test_unknown_scope_rejected(self, admin_client: TestClient) -> None:
        resp = admin_client.post("/tokens", json={"name": "bad", "scopes": ["root"]})
        assert resp.status_code == 422  # unknown scope, rejected by the service

    def test_list_hides_dead_tokens_by_default(self, admin_client: TestClient) -> None:
        past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
        admin_client.post("/tokens", json={"name": "stale", "scopes": ["read"], "expires_at": past})
        _agent_token(admin_client, ["read"], name="fresh")
        names = {t["name"] for t in admin_client.get("/tokens").json()}
        assert "fresh" in names
        assert "stale" not in names
        everything = admin_client.get("/tokens", params={"include_expired": True}).json()
        assert "stale" in {t["name"] for t in everything}

    def test_revoked_token_leaves_default_list(self, admin_client: TestClient) -> None:
        created = admin_client.post("/tokens", json={"name": "doomed", "scopes": ["read"]}).json()
        assert admin_client.delete(f"/tokens/{created['id']}").status_code == 200
        assert "doomed" not in {t["name"] for t in admin_client.get("/tokens").json()}
        everything = admin_client.get("/tokens", params={"include_expired": True}).json()
        assert "doomed" in {t["name"] for t in everything}

    def test_kind_is_not_client_settable(self, admin_client: TestClient) -> None:
        # Origin attribution (human vs agent) keys off kind; a client-supplied
        # kind must be ignored even when the name mimics a session.
        resp = admin_client.post(
            "/tokens",
            json={"name": "session:evil", "scopes": ["read"], "kind": "session"},
        )
        assert resp.status_code == 201
        assert resp.json()["kind"] == "api"

    def test_is_current_flags_the_caller(self, admin_client: TestClient) -> None:
        raw = _agent_token(admin_client, ["read"])
        by_kind = {t["kind"]: t for t in admin_client.get("/tokens").json()}
        assert by_kind["session"]["is_current"] is True
        assert by_kind["api"]["is_current"] is False
        admin_client.cookies.clear()
        # An agent token lists only itself (decision 75, #355).
        (only,) = admin_client.get("/tokens", headers=_bearer(raw)).json()
        assert only["kind"] == "api"
        assert only["is_current"] is True


MEMBER = {"email": "member@example.com", "password": "member-horse-battery"}


def _mint(client: TestClient, scopes: list[str], name: str) -> tuple[str, str]:
    """Mint an API token with the admin session; returns (id, raw secret)."""
    resp = client.post("/tokens", json={"name": name, "scopes": scopes})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"], resp.json()["token"]


def _current_session_id(client: TestClient) -> str:
    return next(t["id"] for t in client.get("/tokens").json() if t["is_current"])


@contextmanager
def _second_client(client: TestClient) -> Iterator[TestClient]:
    """Another browser on the same app, with its own cookie jar. (No `with`:
    the app's lifespan is already running under `client`.)"""
    other = TestClient(client.app)
    try:
        yield other
    finally:
        other.close()


def _revocations(client: TestClient) -> list[dict[str, str]]:
    """`token.revoke` change rows, read with the admin session."""
    resp = client.get("/audit", params={"action": "token.revoke"})
    assert resp.status_code == 200, resp.text
    return [
        {"actor": row["actor_token_id"], "target": row["entity_id"]} for row in resp.json()["items"]
    ]


def _assert_same_404(resp: Response, unknown: Response) -> None:
    """A refused id answers exactly like an id that does not exist."""
    assert resp.status_code == unknown.status_code == 404
    assert resp.content == unknown.content
    assert resp.json() == {"detail": "token not found"}


class TestTokenManagementScope:
    """Decision 75 (#355): a token revokes itself; a session manages its own
    user's tokens; admin scope manages any; an agent token sees and revokes
    only itself."""

    def test_agent_token_cannot_revoke_the_admin_session(self, admin_client: TestClient) -> None:
        session_id = _current_session_id(admin_client)
        _, raw = _mint(admin_client, ["read"], "reader")

        refused = admin_client.delete(f"/tokens/{session_id}", headers=_bearer(raw))
        unknown = admin_client.delete(f"/tokens/{uuid.uuid4()}", headers=_bearer(raw))

        _assert_same_404(refused, unknown)
        assert admin_client.get("/auth/me").status_code == 200  # the session still works
        assert admin_client.get("/accounts", headers=_bearer(raw)).status_code == 200
        assert _revocations(admin_client) == []

    def test_agent_token_cannot_revoke_a_sibling_agent(self, admin_client: TestClient) -> None:
        _, reader = _mint(admin_client, ["read"], "reader")
        sibling_id, sibling = _mint(admin_client, ["read", "write"], "sibling")

        refused = admin_client.delete(f"/tokens/{sibling_id}", headers=_bearer(reader))
        unknown = admin_client.delete(f"/tokens/{uuid.uuid4()}", headers=_bearer(reader))

        _assert_same_404(refused, unknown)
        assert admin_client.get("/accounts", headers=_bearer(sibling)).status_code == 200
        assert admin_client.get("/accounts", headers=_bearer(reader)).status_code == 200
        assert _revocations(admin_client) == []

    def test_agent_token_revokes_itself(self, admin_client: TestClient) -> None:
        reader_id, reader = _mint(admin_client, ["read"], "reader")

        resp = admin_client.delete(f"/tokens/{reader_id}", headers=_bearer(reader))

        assert resp.status_code == 200
        assert resp.json() == {"status": "revoked"}
        assert admin_client.get("/accounts", headers=_bearer(reader)).status_code == 401
        assert _revocations(admin_client) == [{"actor": reader_id, "target": reader_id}]
        assert admin_client.get("/auth/me").status_code == 200  # the admin is untouched

    def test_agent_token_lists_only_itself(self, admin_client: TestClient) -> None:
        reader_id, reader = _mint(admin_client, ["read"], "reader")
        _mint(admin_client, ["read", "write"], "sibling")
        doomed_id, _ = _mint(admin_client, ["read"], "doomed")
        assert admin_client.delete(f"/tokens/{doomed_id}").status_code == 200

        listing = admin_client.get("/tokens", headers=_bearer(reader)).json()
        assert [(t["id"], t["is_current"]) for t in listing] == [(reader_id, True)]
        retained = admin_client.get(
            "/tokens", params={"include_expired": True}, headers=_bearer(reader)
        ).json()
        assert [t["id"] for t in retained] == [reader_id]

    def test_admin_scoped_agent_token_manages_any_token(self, admin_client: TestClient) -> None:
        session_id = _current_session_id(admin_client)
        operator_id, operator = _mint(admin_client, ["admin"], "operator")
        sibling_id, sibling = _mint(admin_client, ["read"], "sibling")

        listing = admin_client.get("/tokens", headers=_bearer(operator)).json()
        assert {session_id, operator_id, sibling_id} <= {t["id"] for t in listing}
        assert (
            admin_client.delete(f"/tokens/{sibling_id}", headers=_bearer(operator)).status_code
            == 200
        )
        assert admin_client.get("/accounts", headers=_bearer(sibling)).status_code == 401
        assert _revocations(admin_client) == [{"actor": operator_id, "target": sibling_id}]

    def test_admin_session_lists_and_revokes_any_token(self, admin_client: TestClient) -> None:
        session_id = _current_session_id(admin_client)
        reader_id, reader = _mint(admin_client, ["read"], "reader")
        sibling_id, _ = _mint(admin_client, ["read", "write"], "sibling")
        assert admin_client.post("/users", json=MEMBER).status_code == 201

        listing = {t["id"] for t in admin_client.get("/tokens").json()}
        assert listing == {session_id, reader_id, sibling_id}

        with _second_client(admin_client) as member:
            assert member.post("/auth/login", json=MEMBER).status_code == 200
            member_session_id = _current_session_id(member)

            # Another user's token, and an agent token of the admin's own user.
            assert admin_client.delete(f"/tokens/{member_session_id}").status_code == 200
            assert member.get("/auth/me").status_code == 401
        assert admin_client.delete(f"/tokens/{reader_id}").status_code == 200
        assert admin_client.get("/accounts", headers=_bearer(reader)).status_code == 401

        assert sorted(_revocations(admin_client), key=lambda r: r["target"]) == sorted(
            [
                {"actor": session_id, "target": member_session_id},
                {"actor": session_id, "target": reader_id},
            ],
            key=lambda r: r["target"],
        )

    def test_member_session_manages_only_its_own_users_tokens(
        self, admin_client: TestClient
    ) -> None:
        admin_session_id = _current_session_id(admin_client)
        agent_id, agent = _mint(admin_client, ["read"], "agent")
        assert admin_client.post("/users", json=MEMBER).status_code == 201

        with _second_client(admin_client) as phone, _second_client(admin_client) as laptop:
            assert phone.post("/auth/login", json=MEMBER).status_code == 200
            assert laptop.post("/auth/login", json=MEMBER).status_code == 200
            phone_id = _current_session_id(phone)
            laptop_id = _current_session_id(laptop)

            # The member sees both of its own sessions and nothing of the admin's.
            assert {t["id"] for t in phone.get("/tokens").json()} == {phone_id, laptop_id}

            # The admin's session and agent token answer like an unknown id.
            unknown = phone.delete(f"/tokens/{uuid.uuid4()}")
            _assert_same_404(phone.delete(f"/tokens/{admin_session_id}"), unknown)
            _assert_same_404(phone.delete(f"/tokens/{agent_id}"), unknown)
            assert admin_client.get("/auth/me").status_code == 200
            assert admin_client.get("/accounts", headers=_bearer(agent)).status_code == 200

            # Its own other session it can revoke.
            assert phone.delete(f"/tokens/{laptop_id}").status_code == 200
            assert laptop.get("/auth/me").status_code == 401
            assert phone.get("/auth/me").status_code == 200

        assert _revocations(admin_client) == [{"actor": phone_id, "target": laptop_id}]


class TestRedaction:
    @pytest.fixture
    def seeded_account(self, clean_db: None, migrated_engine) -> None:  # type: ignore[no-untyped-def]
        with migrated_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO accounts (name, type, path, external_id) "
                    "VALUES ('Citi Card', 'credit_card', 'liabilities.citicard', "
                    "'ACT-9f2e8a71-simplefin')"
                )
            )

    def test_an_api_token_sees_no_external_identifiers(
        self, seeded_account: None, admin_client: TestClient
    ) -> None:
        """#437: what read:redacted used to do is the default for every API
        token — no agent task needs the bank's identifiers."""
        raw = _agent_token(admin_client, ["read", "write"])
        admin_client.cookies.clear()
        resp = admin_client.get("/accounts", headers=_bearer(raw))
        assert resp.status_code == 200
        (account,) = resp.json()
        assert account["external_id"] is None
        assert account["connection_id"] is None
        assert "ACT-9f2e8a71" not in resp.text

    def test_a_signed_in_person_sees_external_id(
        self, seeded_account: None, admin_client: TestClient
    ) -> None:
        resp = admin_client.get("/accounts")
        (account,) = resp.json()
        assert account["external_id"] == "ACT-9f2e8a71-simplefin"

    def test_read_redacted_is_retired(self, admin_client: TestClient) -> None:
        resp = admin_client.post("/tokens", json={"name": "w", "scopes": ["read:redacted"]})
        assert resp.status_code == 422
        assert "use 'read'" in resp.text


class TestSessionCleanup:
    def test_dead_unreferenced_sessions_are_hard_deleted(
        self, admin_client: TestClient, migrated_engine: Engine, db_session: Session
    ) -> None:
        # Session 1 (admin_client's setup) will die having written nothing;
        # this login is session 2, the current one.
        admin_client.cookies.clear()
        assert admin_client.post("/auth/login", json=ADMIN_CREDENTIALS).status_code == 200
        old = next(
            t
            for t in admin_client.get("/tokens").json()
            if t["kind"] == "session" and not t["is_current"]
        )
        with migrated_engine.begin() as conn:
            conn.execute(
                text("UPDATE api_tokens SET expires_at = now() - interval '1 day' WHERE id = :id"),
                {"id": old["id"]},
            )

        deleted, stamped = service.cleanup_sessions(db_session)
        db_session.commit()

        assert deleted == 1
        assert stamped == 0  # already carried its (past) expiry; nothing to stamp
        with migrated_engine.connect() as conn:
            gone = conn.execute(
                text("SELECT count(*) FROM api_tokens WHERE id = :id"), {"id": old["id"]}
            ).scalar_one()
            bookkeeping = conn.execute(
                text("SELECT count(*) FROM audit_log WHERE actor_token_id = :id"),
                {"id": old["id"]},
            ).scalar_one()
            summaries = conn.execute(
                text("SELECT count(*) FROM audit_log WHERE action = 'session.cleanup'")
            ).scalar_one()
        assert gone == 0
        assert bookkeeping == 0  # its auth.login row went with it
        assert summaries == 1
        assert admin_client.get("/auth/me").status_code == 200  # current session untouched

    def test_dead_anchored_sessions_are_stamped_and_retained(
        self, admin_client: TestClient, migrated_engine: Engine, db_session: Session
    ) -> None:
        # The session mints an API token: its token.create audit row makes the
        # session an attribution anchor — it must never be hard-deleted. The
        # token is admin-scoped so its listing still shows the session row (a
        # non-admin agent token lists only itself — decision 75, #355).
        raw = _agent_token(admin_client, ["admin"])
        # Idle-kill the session (expires_at still in the future → needs stamping).
        with migrated_engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE api_tokens SET last_used_at = now() - interval '9 hours' "
                    "WHERE kind = 'session'"
                )
            )

        deleted, stamped = service.cleanup_sessions(db_session)
        db_session.commit()

        assert deleted == 0
        assert stamped == 1
        admin_client.cookies.clear()
        names = {t["name"] for t in admin_client.get("/tokens", headers=_bearer(raw)).json()}
        assert not any(n.startswith("session:") for n in names)  # hidden by default
        everything = admin_client.get(
            "/tokens", params={"include_expired": True}, headers=_bearer(raw)
        ).json()
        session_row = next(t for t in everything if t["kind"] == "session")
        assert datetime.fromisoformat(session_row["expires_at"]) <= datetime.now(UTC)

    def test_a_session_that_recorded_a_position_event_is_retained(
        self, admin_client: TestClient, migrated_engine: Engine, db_session: Session
    ) -> None:
        """position_events.actor_token_id names its author by foreign key
        (#390): such a session is an attribution anchor even once retention
        has pruned its audit rows, so cleanup keeps it instead of tripping
        over the key."""
        account = admin_client.post(
            "/accounts", json={"name": "Broker", "type": "brokerage", "path": "assets.broker"}
        ).json()
        admin_client.patch(f"/accounts/{account['id']}", json={"holdings_source": "manual"})
        resp = admin_client.post(
            f"/accounts/{account['id']}/positions",
            json={"symbol": "TEST", "quantity": "1", "as_of": "2026-01-02"},
        )
        assert resp.status_code == 201, resp.text
        admin_client.cookies.clear()
        assert admin_client.post("/auth/login", json=ADMIN_CREDENTIALS).status_code == 200
        old = next(
            t
            for t in admin_client.get("/tokens").json()
            if t["kind"] == "session" and not t["is_current"]
        )
        with migrated_engine.begin() as conn:
            conn.execute(
                text("UPDATE api_tokens SET expires_at = now() - interval '1 day' WHERE id = :id"),
                {"id": old["id"]},
            )
            # Retention has pruned everything but its own login bookkeeping.
            conn.execute(
                text("DELETE FROM audit_log WHERE actor_token_id = :id AND action <> 'auth.login'"),
                {"id": old["id"]},
            )

        deleted, _stamped = service.cleanup_sessions(db_session)
        db_session.commit()

        assert deleted == 0
        with migrated_engine.connect() as conn:
            kept = conn.execute(
                text("SELECT count(*) FROM api_tokens WHERE id = :id"), {"id": old["id"]}
            ).scalar_one()
        assert kept == 1


class TestAudit:
    def test_identity_mutations_are_audited(self, admin_client: TestClient) -> None:
        _agent_token(admin_client, ["read", "write"])
        admin_client.post(
            "/users",
            json={"email": "member@example.com", "password": "password123"},
        )
        actions = {row["action"] for row in admin_client.get("/audit").json()["items"]}
        assert {"user.create", "auth.login", "token.create"} <= actions
