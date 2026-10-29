"""Moving a real account to a new ltree path (issue #284, DESIGN §3.4,
decision 72): `PATCH /accounts/{id}` with `path`.

The account moves with its whole subtree (descendants keep their relative
paths), the new path is validated by the shared #214 validator, an occupied
destination is a 409, a destination under a category root is refused, the
move is audited and evented, open suggestion drafts follow it, and no money
moves. All data is synthetic.
"""

import uuid
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from purser import ledger
from purser.models import Account, AuditLog, Event


@pytest.fixture
def ids(admin_client: TestClient, migrated_engine: Engine) -> dict[str, str]:
    """Admin session; a card at the ltree ROOT (the #284 shape), a brokerage
    with two levels of sub-accounts, a `liabilities` container account, and a
    category tree. Returns path -> id."""
    with migrated_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE balance_watermark SET running_dirty_from = NULL, networth_dirty_from = NULL"
            )
        )
    out: dict[str, str] = {}

    def acct(name: str, type_: str, path: str, parent: str | None = None) -> None:
        body: dict = {"name": name, "type": type_, "path": path}
        if parent is not None:
            body["parent_id"] = out[parent]
        endpoint = "/categories" if type_.startswith("category_") else "/accounts"
        resp = admin_client.post(endpoint, json=body)
        assert resp.status_code == 201, resp.text
        out[path] = resp.json()["id"]

    acct("Card", "credit_card", "card")
    acct("Liabilities", "credit_card", "liabilities")
    acct("Checking", "checking", "assets.checking")
    acct("Brokerage", "brokerage", "assets.brokerage")
    acct("Brokerage cash", "brokerage", "assets.brokerage.cash", parent="assets.brokerage")
    acct("Sweep", "brokerage", "assets.brokerage.cash.sweep", parent="assets.brokerage.cash")
    acct("Expenses", "category_expense", "expenses")
    acct("Dining", "category_expense", "expenses.dining", parent="expenses")
    return out


def _paths(client: TestClient) -> dict[str, str]:
    """id -> path over every account, virtual included."""
    rows = client.get("/accounts", params={"include_virtual": True, "include_hidden": True}).json()
    return {r["id"]: r["path"] for r in rows}


def _spend(client: TestClient, account: str, amount: str) -> str:
    resp = client.post(
        "/transactions",
        json={
            "account": account,
            "category": "expenses.dining",
            "amount": amount,
            "date": "2026-06-15",
            "payee": "Synthetic Diner",
            "status": "posted",
        },
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


class TestMove:
    def test_a_leaf_moves_with_its_history_audited_and_evented(
        self, ids: dict[str, str], client: TestClient, db_session: Session
    ) -> None:
        tx_id = _spend(client, ids["card"], "-14.00")
        resp = client.patch(f"/accounts/{ids['card']}", json={"path": "liabilities.card"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["path"] == "liabilities.card"
        # The container account at the new parent path becomes its parent.
        assert body["parent_id"] == ids["liabilities"]
        assert Decimal(body["balance"]) == Decimal("-14.00")
        # History follows by id; the new path resolves, the old one is gone.
        rows = client.get("/transactions", params={"account": "liabilities.card"}).json()
        assert [r["id"] for r in rows["items"]] == [tx_id]
        assert client.get("/transactions", params={"account": "card"}).status_code == 404

        audit_row = db_session.execute(
            select(AuditLog).where(
                AuditLog.action == "account.move", AuditLog.entity_id == uuid.UUID(ids["card"])
            )
        ).scalar_one()
        assert audit_row.before_after["before"] == {"path": "card", "parent_id": None}
        assert audit_row.before_after["after"]["path"] == "liabilities.card"
        assert audit_row.before_after["after"]["moved"] == [
            {"id": ids["card"], "from": "card", "to": "liabilities.card"}
        ]
        event = db_session.execute(
            select(Event).where(
                Event.type == "account.moved", Event.entity_id == uuid.UUID(ids["card"])
            )
        ).scalar_one()
        assert event.payload == {
            "path": "liabilities.card",
            "from_path": "card",
            "accounts_moved": 1,
        }

    def test_children_move_with_their_parent_and_keep_relative_paths(
        self, ids: dict[str, str], client: TestClient
    ) -> None:
        resp = client.patch(
            f"/accounts/{ids['assets.brokerage']}", json={"path": "investments.taxable"}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["parent_id"] is None  # nothing lives at `investments`
        paths = _paths(client)
        assert paths[ids["assets.brokerage"]] == "investments.taxable"
        assert paths[ids["assets.brokerage.cash"]] == "investments.taxable.cash"
        assert paths[ids["assets.brokerage.cash.sweep"]] == "investments.taxable.cash.sweep"
        # Parents inside the subtree are unchanged: the structure moved whole.
        cash = client.get(f"/accounts/{ids['assets.brokerage.cash']}").json()
        assert cash["parent_id"] == ids["assets.brokerage"]
        # Nothing else moved.
        assert paths[ids["assets.checking"]] == "assets.checking"
        assert paths[ids["expenses.dining"]] == "expenses.dining"

    def test_moving_up_over_a_repeated_label_does_not_collide(
        self, ids: dict[str, str], client: TestClient
    ) -> None:
        """a.b -> a while a.b.b exists: a single UPDATE would collide with a
        row it had not moved yet (the unique index checks row by row)."""
        for name, path in (("Outer", "nest.b"), ("Inner", "nest.b.b")):
            resp = client.post("/accounts", json={"name": name, "type": "savings", "path": path})
            assert resp.status_code == 201, resp.text
        before = {v: k for k, v in _paths(client).items()}
        resp = client.patch(f"/accounts/{before['nest.b']}", json={"path": "nest"})
        assert resp.status_code == 200, resp.text
        after = _paths(client)
        assert after[before["nest.b"]] == "nest"
        assert after[before["nest.b.b"]] == "nest.b"

    def test_same_path_is_a_no_op(
        self, ids: dict[str, str], client: TestClient, db_session: Session
    ) -> None:
        resp = client.patch(f"/accounts/{ids['card']}", json={"path": "card"})
        assert resp.status_code == 200, resp.text
        assert (
            db_session.execute(select(AuditLog).where(AuditLog.action == "account.move")).first()
            is None
        )

    def test_rename_and_move_in_one_call(self, ids: dict[str, str], client: TestClient) -> None:
        resp = client.patch(
            f"/accounts/{ids['card']}", json={"path": "liabilities.travel", "name": "Travel"}
        )
        assert resp.status_code == 200, resp.text
        assert (resp.json()["path"], resp.json()["name"]) == ("liabilities.travel", "Travel")


class TestRefusals:
    @pytest.mark.parametrize(
        ("target", "fragment"),
        [
            pytest.param("assets.checking", "already exists", id="exact-collision"),
            pytest.param("assets", "already sits beneath it", id="occupied-subtree"),
        ],
    )
    def test_occupied_destination_is_409(
        self, ids: dict[str, str], client: TestClient, target: str, fragment: str
    ) -> None:
        resp = client.patch(f"/accounts/{ids['card']}", json={"path": target})
        assert resp.status_code == 409, resp.text
        assert fragment in resp.json()["detail"]
        assert _paths(client)[ids["card"]] == "card"

    @pytest.mark.parametrize(
        ("account", "target", "fragment"),
        [
            pytest.param("card", "liabilities..card", "segment 2 is empty", id="malformed"),
            pytest.param("card", "liabilities.my card", "contains ' '", id="space"),
            pytest.param("card", "expenses.card", "category root", id="category-root"),
            pytest.param(
                "assets.brokerage",
                "assets.brokerage.cash.sweep.deeper",
                "under itself",
                id="under-itself",
            ),
            pytest.param("expenses.dining", "dining", "PATCH /categories", id="category-itself"),
        ],
    )
    def test_refused_moves_are_422_and_change_nothing(
        self,
        ids: dict[str, str],
        client: TestClient,
        account: str,
        target: str,
        fragment: str,
    ) -> None:
        before = _paths(client)
        resp = client.patch(f"/accounts/{ids[account]}", json={"path": target})
        assert resp.status_code == 422, resp.text
        assert fragment in resp.json()["detail"]
        assert _paths(client) == before

    def test_a_subtree_holding_a_category_does_not_move(
        self, ids: dict[str, str], client: TestClient
    ) -> None:
        resp = client.post(
            "/categories",
            json={
                "name": "Fees",
                "type": "category_expense",
                "path": "assets.brokerage.fees",
                "parent_id": ids["assets.brokerage"],
            },
        )
        assert resp.status_code == 201, resp.text
        resp = client.patch(f"/accounts/{ids['assets.brokerage']}", json={"path": "invest"})
        assert resp.status_code == 422
        assert "categories keep their paths" in resp.json()["detail"]

    def test_requires_write_scope(self, ids: dict[str, str], client: TestClient) -> None:
        agent = client.post("/tokens", json={"name": "agent", "scopes": ["read"]})
        headers = {"Authorization": f"Bearer {agent.json()['token']}"}
        resp = client.patch(
            f"/accounts/{ids['card']}", json={"path": "liabilities.card"}, headers=headers
        )
        assert resp.status_code == 403


class TestNothingElseMoves:
    def test_balances_and_net_worth_are_untouched(
        self, ids: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        _spend(client, ids["card"], "-40.00")
        _spend(client, ids["assets.checking"], "-2.50")
        networth_before = client.get("/networth").json()
        # Clear the cursors the spends left, so the move's own marks show.
        with migrated_engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE balance_watermark"
                    " SET running_dirty_from = NULL, networth_dirty_from = NULL"
                )
            )
        resp = client.patch(f"/accounts/{ids['card']}", json={"path": "liabilities.card"})
        assert resp.status_code == 200, resp.text
        with migrated_engine.connect() as conn:
            cursor = conn.execute(
                text("SELECT networth_dirty_from FROM balance_watermark")
            ).scalar_one_or_none()
        assert cursor is None  # a move marks no balances (§3.5)
        after = client.get("/networth").json()
        assert after["net_worth"] == networth_before["net_worth"]
        assert after["liabilities"] == networth_before["liabilities"]

    def test_open_suggestion_drafts_follow_the_move(
        self, ids: dict[str, str], client: TestClient
    ) -> None:
        agent = client.post("/tokens", json={"name": "agent", "scopes": ["read", "write"]})
        headers = {"Authorization": f"Bearer {agent.json()['token']}"}
        proposed = client.post(
            "/schedules",
            json={
                "name": "Synthetic annual fee",
                "account": "card",
                "rrule": "FREQ=YEARLY",
                "amount": "-95.00",
                "propose": True,
            },
            headers=headers,
        )
        assert proposed.status_code == 201, proposed.text
        sid = proposed.json()["suggestion_id"]

        resp = client.patch(f"/accounts/{ids['card']}", json={"path": "liabilities.card"})
        assert resp.status_code == 200, resp.text
        # A new account squatting the OLD path must not capture the schedule.
        squatter = client.post(
            "/accounts", json={"name": "Squatter", "type": "credit_card", "path": "card"}
        )
        assert squatter.status_code == 201, squatter.text

        (inbox_row,) = [s for s in client.get("/suggestions").json() if s["id"] == sid]
        assert inbox_row["draft"]["account"] == "liabilities.card"
        accepted = client.post(f"/suggestions/{sid}/decide", json={"decision": "accept"})
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["schedule"]["account_id"] == ids["card"]


# --------------------------------------------------------------------------
# Property: a move either happens exactly as asked, or nothing changes
# --------------------------------------------------------------------------

_labels = st.sampled_from(["a", "b", "c"])
_paths_strategy = st.lists(
    st.lists(_labels, min_size=1, max_size=3).map(".".join), min_size=1, max_size=8, unique=True
)


class TestMoveProperty:
    @settings(
        max_examples=40,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(existing=_paths_strategy, data=st.data())
    def test_move_is_exact_or_refused(
        self, db_session: Session, existing: list[str], data: st.DataObject
    ) -> None:
        """Over random trees built from a three-letter alphabet (so repeated
        labels like a.b.b are common): an accepted move re-roots exactly the
        subtree — every descendant keeps its relative path, ids are stable,
        nothing outside moves — and a refused one changes nothing."""
        db_session.rollback()  # each example starts from the committed state
        suffix = uuid.uuid4().hex[:8]
        root = f"prop{suffix}"
        created = {}
        for path in sorted(existing, key=lambda p: (p.count("."), p)):
            full = f"{root}.{path}"
            created[full] = ledger.create_account(
                db_session, name=path, type_="savings", path=full, actor_token_id=None
            ).id
        source = data.draw(st.sampled_from(sorted(created)))
        target = f"{root}.{data.draw(st.lists(_labels, min_size=1, max_size=3).map('.'.join))}"
        assume(target != source)  # the no-op move is pinned by its own test
        before = {
            a.id: a.path
            for a in db_session.execute(
                select(Account).where(Account.path.op("<@")(text(f"'{root}'::ltree")))
            ).scalars()
        }
        try:
            ledger.move_account(
                db_session, account_id=created[source], path=target, actor_token_id=None
            )
            moved = True
        except ledger.LedgerError:
            moved = False
        db_session.expire_all()
        after = {
            a.id: a.path
            for a in db_session.execute(
                select(Account).where(Account.path.op("<@")(text(f"'{root}'::ltree")))
            ).scalars()
        }
        assert set(after) == set(before)  # ids are stable; nothing appears or vanishes
        for account_id, old_path in before.items():
            inside = old_path == source or old_path.startswith(source + ".")
            if moved and inside:
                assert after[account_id] == target + old_path[len(source) :]
            else:
                assert after[account_id] == old_path
        assert len(set(after.values())) == len(after)  # paths stay unique
