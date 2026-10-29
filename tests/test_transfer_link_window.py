"""Per-rule date windows for declared transfer pairs (issue #210, DESIGN decision 66).

A ``transfer_link`` rule may set ``window_days`` for the account pair it
declares. The sweep applies it in two stages — the standard ±3-day window
first, exactly as before, then the widened window over the rows still without
a partner — so a wider window never changes what the standard one decides, and
every undeclared pair keeps ±3 days. The activation confirm counts the sweep's
own plan, so the projected count is the linked count.

Synthetic data only. Every seeded date is a fixed day (offsets from DAY0), so
nothing here depends on today or on where it falls in the month (#262).
"""

import uuid
from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy import Engine, text

import purser.db
from purser.core import matching as core_matching
from tests.test_matching import _imported
from tests.test_mcp import (  # noqa: F401 (fixture re-export)
    LiveServer,
    _mcp_call,
    seeded_tokens,
)

DAY0 = date(2026, 5, 4)
CHECKING, CARD, SAVINGS = "assets.checking", "liabilities.card", "assets.savings"
# Descriptors with no name signal: no transfer keyword, and no token naming
# the counterparty account ("Everyday", "Rewards", "Reserve").
OUT_TEXT = "EXAMPLE BANK WEB DEBIT"
IN_TEXT = "THANK YOU FOR YOUR REMITTANCE"


def _day(offset: int) -> date:
    return DAY0 + timedelta(days=offset)


@pytest.fixture
def seeded(admin_client: TestClient) -> dict[str, str]:
    ids: dict[str, str] = {}
    for name, type_, path in (
        ("Everyday", "checking", CHECKING),
        ("Rewards", "credit_card", CARD),
        ("Reserve", "savings", SAVINGS),
    ):
        resp = admin_client.post("/accounts", json={"name": name, "type": type_, "path": path})
        assert resp.status_code == 201, resp.text
        ids[path] = resp.json()["id"]
    return ids


def _pair(
    client: TestClient,
    engine: Engine,
    amount: str,
    out_day: int,
    in_day: int,
    *,
    to: str = CARD,
    out_text: str = OUT_TEXT,
    in_text: str = IN_TEXT,
) -> tuple[str, str]:
    """One-sided posted bank rows: ``amount`` leaves checking on ``out_day``
    and arrives on ``to`` on ``in_day``."""
    out_id = _imported(client, engine, CHECKING, f"-{amount}", _day(out_day), payee=out_text)
    in_id = _imported(client, engine, to, amount, _day(in_day), payee=in_text)
    return out_id, in_id


def _rule(client: TestClient, seeded: dict[str, str], **extra: object) -> str:
    body = {
        "type": "transfer_link",
        "pair_account_a": seeded[CHECKING],
        "pair_account_b": seeded[CARD],
        **extra,
    }
    resp = client.post("/rules", json=body)
    assert resp.status_code == 201, resp.text
    rule_id: str = resp.json()["id"]
    return rule_id


def _projected(client: TestClient, rule_id: str) -> int:
    """The activation confirm's number, read off its refusal."""
    refused = client.post(f"/rules/{rule_id}/state", json={"state": "active"})
    assert refused.status_code == 422, refused.text
    return int(refused.json()["detail"].split("would link ")[1].split(" ")[0])


def _activate(client: TestClient, rule_id: str) -> int:
    projected = _projected(client, rule_id)
    ok = client.post(
        f"/rules/{rule_id}/state", json={"state": "active", "confirm_links": projected}
    )
    assert ok.status_code == 200, ok.text
    return projected


def _sweep() -> dict[str, int]:
    """The nightly auto-link pass, committed — what activation feeds."""
    from purser import transfers

    with purser.db.get_sessionmaker()() as session:
        stats = transfers.auto_link_pass(session)
        session.commit()
    return stats


def _merged_into(engine: Engine, tx_id: str) -> str | None:
    """The transfer a bank row was voided into, or None while it is live."""
    with engine.connect() as conn:
        status, superseded_by = conn.execute(
            text("SELECT status, superseded_by::text FROM transactions WHERE id = :id"),
            {"id": tx_id},
        ).one()
    if status == "void":
        assert superseded_by is not None
        return str(superseded_by)
    assert status == "posted"
    return None


# --------------------------------------------------------------------------
# Pure core: detect_transfer_pairs with a per-pair window
# --------------------------------------------------------------------------

ACCOUNTS = [uuid.UUID(int=n) for n in range(1, 4)]


@st.composite
def match_inputs(draw: st.DrawFn) -> list[core_matching.MatchInput]:
    """Small pools with deliberate collisions: three accounts, three amounts
    of either sign, dates inside three weeks — so pairs, near-misses and
    competing candidates all occur."""
    rows = draw(
        st.lists(
            st.tuples(
                st.sampled_from(ACCOUNTS),
                st.sampled_from([Decimal("-50.00"), Decimal("-75.25"), Decimal("-120.00")]),
                st.booleans(),
                st.integers(min_value=0, max_value=20),
            ),
            max_size=12,
        )
    )
    return [
        core_matching.MatchInput(
            id=uuid.UUID(int=1000 + n),
            date=_day(offset),
            amount=amount if negative else -amount,
            normalized_payee="",
            account_id=account,
        )
        for n, (account, amount, negative, offset) in enumerate(rows)
    ]


def _windows(draw_map: dict[tuple[uuid.UUID, uuid.UUID], int | None]) -> core_matching.PairWindow:
    def window(out: core_matching.MatchInput, inc: core_matching.MatchInput) -> int | None:
        assert out.account_id is not None and inc.account_id is not None
        return draw_map.get((out.account_id, inc.account_id))

    return window


WINDOW_MAPS = st.dictionaries(
    st.tuples(st.sampled_from(ACCOUNTS), st.sampled_from(ACCOUNTS)),
    st.one_of(st.none(), st.integers(min_value=0, max_value=14)),
)


class TestPairWindowCore:
    @given(rows=match_inputs())
    @settings(max_examples=200, deadline=None)
    def test_the_default_window_is_the_global_one(
        self, rows: list[core_matching.MatchInput]
    ) -> None:
        """Omitting ``window`` and passing the global window for every pair
        are the same detector — the standard stage is untouched."""
        assert core_matching.detect_transfer_pairs(rows) == core_matching.detect_transfer_pairs(
            rows, window=lambda out, inc: core_matching.TRANSFER_WINDOW_DAYS
        )

    @given(rows=match_inputs(), windows=WINDOW_MAPS)
    @settings(max_examples=300, deadline=None)
    def test_every_pair_respects_its_own_window(
        self,
        rows: list[core_matching.MatchInput],
        windows: dict[tuple[uuid.UUID, uuid.UUID], int | None],
    ) -> None:
        """Whatever the per-pair windows: exact opposite amounts, two
        different accounts, a gap inside THAT pair's window (None never
        pairs), and no row used twice."""
        by_id = {r.id: r for r in rows}
        window = _windows(windows)
        pairs = core_matching.detect_transfer_pairs(rows, window=window)
        used = [p.outgoing_id for p in pairs] + [p.incoming_id for p in pairs]
        assert len(used) == len(set(used))
        for pair in pairs:
            out, inc = by_id[pair.outgoing_id], by_id[pair.incoming_id]
            assert out.amount < 0 and inc.amount == -out.amount == pair.amount
            assert out.account_id != inc.account_id
            limit = window(out, inc)
            assert limit is not None
            assert pair.date_gap_days == abs((inc.date - out.date).days) <= limit

    def test_a_window_is_per_pair(self) -> None:
        out = core_matching.MatchInput(
            id=uuid.uuid4(),
            date=_day(0),
            amount=Decimal("-184.50"),
            normalized_payee="",
            account_id=ACCOUNTS[0],
        )
        declared_in = core_matching.MatchInput(
            id=uuid.uuid4(),
            date=_day(4),
            amount=Decimal("184.50"),
            normalized_payee="",
            account_id=ACCOUNTS[1],
        )
        other_in = core_matching.MatchInput(
            id=uuid.uuid4(),
            date=_day(4),
            amount=Decimal("184.50"),
            normalized_payee="",
            account_id=ACCOUNTS[2],
        )
        # Only the ACCOUNTS[0] -> ACCOUNTS[1] pair gets 5 days.
        window = _windows({(ACCOUNTS[0], ACCOUNTS[1]): 5})
        (pair,) = core_matching.detect_transfer_pairs([out, other_in, declared_in], window=window)
        assert (pair.incoming_id, pair.date_gap_days) == (declared_in.id, 4)
        # The standard window pairs neither: 4 days is one too many.
        assert core_matching.detect_transfer_pairs([out, other_in, declared_in]) == []


class TestDeclaredPairWindow:
    @pytest.mark.parametrize(
        ("window_days", "links"),
        [(None, 0), (3, 0), (4, 1), (5, 1), (14, 1)],
    )
    def test_four_days_apart_links_only_inside_the_rules_window(
        self,
        seeded: dict[str, str],
        client: TestClient,
        migrated_engine: Engine,
        window_days: int | None,
        links: int,
    ) -> None:
        """The #210 case: a declared checking -> card autopay whose legs
        posted 4 days apart. Unset (the standard 3 days) leaves it exactly as
        unlinked as before; a window of 4 or more links it — the boundary is
        inclusive."""
        out_id, in_id = _pair(client, migrated_engine, "184.50", 0, 4)
        extra = {} if window_days is None else {"window_days": window_days}
        rule_id = _rule(client, seeded, **extra)

        assert _activate(client, rule_id) == links
        stats = _sweep()
        assert stats["transfers_auto_linked"] == links

        merged = _merged_into(migrated_engine, out_id)
        assert (merged is not None) == bool(links)
        assert _merged_into(migrated_engine, in_id) == merged
        if merged is not None:
            row = client.get(f"/transactions/{merged}").json()
            assert row["is_transfer"] is True
            assert row["origin"] == "system"
            assert row["status"] == "posted"
            assert Decimal(row["amount"]) in (Decimal("-184.50"), Decimal("184.50"))

    def test_widening_a_disabled_rule_then_reactivating_links_the_pair(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        """The edit path: an active rule's window is frozen, so widening goes
        disable -> edit -> activate, and the re-activation confirm counts the
        newly reachable pair."""
        out_id, _ = _pair(client, migrated_engine, "184.50", 0, 4)
        rule_id = _rule(client, seeded)
        assert _activate(client, rule_id) == 0
        _sweep()
        assert _merged_into(migrated_engine, out_id) is None

        frozen = client.patch(f"/rules/{rule_id}", json={"window_days": 5})
        assert frozen.status_code == 422
        assert "disable it" in frozen.json()["detail"]
        # Same window, spelled explicitly: nothing moves, so nothing is refused.
        assert client.patch(f"/rules/{rule_id}", json={"window_days": 3}).status_code == 200
        # A note is not scope.
        assert client.patch(f"/rules/{rule_id}", json={"note": "card autopay"}).status_code == 200

        assert client.post(f"/rules/{rule_id}/state", json={"state": "disabled"}).status_code == 200
        edited = client.patch(f"/rules/{rule_id}", json={"window_days": 5})
        assert edited.status_code == 200, edited.text
        assert edited.json()["state"] == "disabled"
        assert edited.json()["window_days"] == 5

        assert _activate(client, rule_id) == 1
        assert _sweep()["transfers_auto_linked"] == 1
        assert _merged_into(migrated_engine, out_id) is not None

    def test_a_narrow_window_vouches_only_for_close_legs(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        """window_days below the standard 3 narrows the pair: the rule links a
        no-signal pair only when its own window covers the gap."""
        close_out, _ = _pair(client, migrated_engine, "61.10", 0, 1)
        far_out, _ = _pair(client, migrated_engine, "62.20", 10, 12)
        rule_id = _rule(client, seeded, window_days=1)

        assert _activate(client, rule_id) == 1
        assert _sweep()["transfers_auto_linked"] == 1
        assert _merged_into(migrated_engine, close_out) is not None
        assert _merged_into(migrated_engine, far_out) is None
        # Not linked, still a confirm-first candidate in the standard window.
        candidates = client.get("/matching/transfer-candidates").json()
        assert [c["outgoing_id"] for c in candidates] == [far_out]

    def test_composes_with_amount_exact(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        autopay_out, _ = _pair(client, migrated_engine, "184.50", 0, 4)
        other_out, _ = _pair(client, migrated_engine, "90.00", 20, 24)
        rule_id = _rule(client, seeded, window_days=5, amount_exact="184.50")

        assert _activate(client, rule_id) == 1
        _sweep()
        assert _merged_into(migrated_engine, autopay_out) is not None
        assert _merged_into(migrated_engine, other_out) is None


class TestStandardWindowUnchanged:
    def test_undeclared_pairs_keep_three_days(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        """A 14-day window on checking <-> card changes nothing for checking
        <-> savings: a signal-bearing pair 4 days apart still does not link,
        3 days apart still does, and the candidate list is the standard one."""
        signal_far_out, _ = _pair(
            client,
            migrated_engine,
            "75.25",
            0,
            4,
            to=SAVINGS,
            out_text="TRANSFER TO RESERVE",
            in_text="TRANSFER FROM EVERYDAY",
        )
        signal_near_out, _ = _pair(
            client,
            migrated_engine,
            "60.00",
            10,
            13,
            to=SAVINGS,
            out_text="TRANSFER TO RESERVE",
            in_text="TRANSFER FROM EVERYDAY",
        )
        plain_near_out, _ = _pair(client, migrated_engine, "33.30", 20, 22, to=SAVINGS)
        declared_too_far_out, _ = _pair(client, migrated_engine, "48.00", 30, 45)
        rule_id = _rule(client, seeded, window_days=14)

        # Nothing on the declared pair is within 14 days: the confirm says 0.
        assert _activate(client, rule_id) == 0
        stats = _sweep()
        assert stats["transfers_auto_linked"] == 1  # the 3-day signal pair only
        assert _merged_into(migrated_engine, signal_near_out) is not None
        assert _merged_into(migrated_engine, signal_far_out) is None
        assert _merged_into(migrated_engine, plain_near_out) is None
        assert _merged_into(migrated_engine, declared_too_far_out) is None
        candidates = client.get("/matching/transfer-candidates").json()
        assert [c["outgoing_id"] for c in candidates] == [plain_near_out]

    def test_a_widened_window_never_takes_a_row_the_standard_window_paired(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        """The staging guarantee. The card row sits 4 days BEFORE the
        checking out-row, so a single greedy pass with per-pair windows would
        hand the out-row to it (earliest-dated fit). Staged, the standard
        window links the same-day savings transfer first, exactly as it would
        with no rule at all, and the card row is left alone."""
        card_in = _imported(client, migrated_engine, CARD, "42.00", _day(1), payee=IN_TEXT)
        out_id, savings_in = _pair(
            client,
            migrated_engine,
            "42.00",
            5,
            5,
            to=SAVINGS,
            out_text="TRANSFER TO RESERVE",
            in_text="TRANSFER FROM EVERYDAY",
        )
        rule_id = _rule(client, seeded, window_days=5)

        assert _activate(client, rule_id) == 0
        assert _sweep()["transfers_auto_linked"] == 1
        merged = _merged_into(migrated_engine, out_id)
        assert merged is not None
        assert _merged_into(migrated_engine, savings_in) == merged
        assert _merged_into(migrated_engine, card_in) is None

    def test_a_widened_absorb_never_preempts_a_standard_merge(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        """The standard stage finishes — absorb AND pair — before anything
        widened runs. Two same-day bank rows that name the payment merge with
        each other exactly as they would with no rule, rather than being
        absorbed 4 days back into an older manual transfer of the same amount."""
        manual = client.post(
            "/transactions/transfer",
            json={
                "from_account": CHECKING,
                "to_account": CARD,
                "amount": "250.00",
                "date": _day(40).isoformat(),
                "status": "posted",
            },
        )
        assert manual.status_code == 201, manual.text
        out_id, in_id = _pair(client, migrated_engine, "250.00", 44, 44, out_text="PAYMENT SENT")
        rule_id = _rule(client, seeded, window_days=5)

        assert _activate(client, rule_id) == 1
        assert _sweep() == {"transfers_absorbed": 0, "transfers_auto_linked": 1}
        merged = _merged_into(migrated_engine, out_id)
        assert merged is not None and merged != manual.json()["id"]
        assert _merged_into(migrated_engine, in_id) == merged

    def test_an_unlinked_widened_pair_stays_unlinked(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        """Unlink suppression holds in the widened stage too."""
        out_id, _ = _pair(client, migrated_engine, "184.50", 0, 4)
        _activate(client, _rule(client, seeded, window_days=5))
        _sweep()
        merged = _merged_into(migrated_engine, out_id)
        assert merged is not None

        resp = client.post(f"/transactions/{merged}/unlink-transfer")
        assert resp.status_code == 200, resp.text
        assert _sweep()["transfers_auto_linked"] == 0


class TestPreviewEqualsCommit:
    def test_projected_count_is_the_linked_count_with_a_wider_window(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        """Preview == activation confirm == what the sweep links, with a
        window wider than the standard one — merges in both stages AND an
        absorb into a transfer that was waiting for its card side."""
        near_out, _ = _pair(client, migrated_engine, "120.00", 0, 2)  # standard stage
        far_out, _ = _pair(client, migrated_engine, "184.50", 10, 14)  # widened stage
        too_far_out, _ = _pair(client, migrated_engine, "75.25", 20, 27)  # beyond 5 days
        manual = client.post(
            "/transactions/transfer",
            json={
                "from_account": CHECKING,
                "to_account": CARD,
                "amount": "250.00",
                "date": _day(40).isoformat(),
                "status": "posted",
            },
        )
        assert manual.status_code == 201, manual.text
        late_card_row = _imported(client, migrated_engine, CARD, "250.00", _day(44), IN_TEXT)

        preview = client.post(
            "/rules/preview",
            json={
                "pair_account_a": seeded[CHECKING],
                "pair_account_b": seeded[CARD],
                "window_days": 5,
            },
        )
        assert preview.status_code == 200, preview.text
        body = preview.json()
        assert body["type"] == "transfer_link"
        assert body["window_days"] == 5
        assert body["links"] == 3
        assert sorted((s["kind"], s["gap_days"]) for s in body["sample"]) == [
            ("absorb", 4),
            ("merge", 2),
            ("merge", 4),
        ]

        rule_id = _rule(client, seeded, window_days=5)
        projected = _activate(client, rule_id)
        assert projected == body["links"] == 3

        stats = _sweep()
        assert stats["transfers_auto_linked"] + stats["transfers_absorbed"] == projected
        assert stats == {"transfers_absorbed": 1, "transfers_auto_linked": 2}
        assert _merged_into(migrated_engine, near_out) is not None
        assert _merged_into(migrated_engine, far_out) is not None
        assert _merged_into(migrated_engine, too_far_out) is None
        assert _merged_into(migrated_engine, late_card_row) == manual.json()["id"]

        # Steady state: everything the rule reaches is linked, so a second
        # preview of the same pair projects nothing.
        again = client.post(
            "/rules/preview",
            json={
                "pair_account_a": seeded[CHECKING],
                "pair_account_b": seeded[CARD],
                "window_days": 5,
            },
        ).json()
        assert again["links"] == 0


# --------------------------------------------------------------------------
# MCP: propose_rule / preview_rule / list_rules carry the window
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def live_server(migrated_engine: Engine) -> Iterator[LiveServer]:
    server = LiveServer()
    server.start()
    yield server
    server.stop()


class TestMcpRuleTools:
    @pytest.fixture
    def card(
        self,
        seeded_tokens: dict[str, str],  # noqa: F811
        client: TestClient,
    ) -> None:
        resp = client.post(
            "/accounts", json={"name": "Rewards", "type": "credit_card", "path": CARD}
        )
        assert resp.status_code == 201, resp.text

    def test_an_agent_previews_and_proposes_a_widened_pair(
        self,
        seeded_tokens: dict[str, str],  # noqa: F811
        card: None,
        live_server: LiveServer,
        client: TestClient,
        migrated_engine: Engine,
    ) -> None:
        """An agent previews the pair with and without a window, proposes the
        rule as a draft, and the human's activation confirm then asks for the
        very count the agent's preview reported."""
        _pair(client, migrated_engine, "184.50", 0, 4)
        standard, widened, proposed = _mcp_call(
            live_server.url,
            seeded_tokens["agent"],
            [
                ("preview_rule", {"pair_accounts": [CHECKING, CARD]}),
                ("preview_rule", {"pair_accounts": [CHECKING, CARD], "window_days": 5}),
                (
                    "propose_rule",
                    {"type": "transfer_link", "pair_accounts": [CHECKING, CARD], "window_days": 5},
                ),
            ],
        )
        assert (standard["window_days"], standard["links"]) == (3, 0)
        assert (widened["window_days"], widened["links"]) == (5, 1)
        (sample,) = widened["sample"]
        assert sample["kind"] == "merge"
        assert sample["gap_days"] == 4
        assert sample["amount"] == "184.5000"
        assert sample["dates"] == [_day(0).isoformat(), _day(4).isoformat()]

        assert proposed["state"] == "draft"
        assert proposed["window_days"] == 5
        assert proposed["when"] == "legs <= 5 days apart"
        assert proposed["then"] == f"link {CHECKING} <-> {CARD}"
        assert _projected(client, proposed["id"]) == widened["links"]

        (listed,) = _mcp_call(live_server.url, seeded_tokens["reader"], [("list_rules", {})])
        (row,) = listed["rules"]
        assert row["id"] == proposed["id"]
        assert row["window_days"] == 5

    def test_list_rules_shows_a_window_only_on_pair_rules(
        self,
        seeded_tokens: dict[str, str],  # noqa: F811
        card: None,
        live_server: LiveServer,
    ) -> None:
        _mcp_call(
            live_server.url,
            seeded_tokens["agent"],
            [
                ("propose_rule", {"type": "transfer_link", "pair_accounts": [CHECKING, CARD]}),
                ("propose_rule", {"type": "categorize", "category": "expenses.dining"}),
            ],
        )
        (listed,) = _mcp_call(live_server.url, seeded_tokens["reader"], [("list_rules", {})])
        by_type = {r["type"]: r for r in listed["rules"]}
        assert by_type["transfer_link"]["window_days"] == 3
        assert by_type["transfer_link"]["when"] == "legs <= 3 days apart"
        assert "window_days" not in by_type["categorize"]

    def test_a_predicate_preview_takes_effective_from(
        self,
        seeded_tokens: dict[str, str],  # noqa: F811
        live_server: LiveServer,
    ) -> None:
        """preview_rule gained effective_from for pairs; a text predicate
        honours it too, the way propose_rule always has."""
        predicate = {"match_field": "payee", "match_mode": "contains", "match_text": "restaurants"}
        every, none = _mcp_call(
            live_server.url,
            seeded_tokens["reader"],
            [
                ("preview_rule", predicate),
                ("preview_rule", {**predicate, "effective_from": "2099-01-01"}),
            ],
        )
        assert every["matched"] > 0
        assert none["matched"] == 0
        assert "links" not in every

    def test_bad_windows_are_refused_and_nothing_is_drafted(
        self,
        seeded_tokens: dict[str, str],  # noqa: F811
        card: None,
        live_server: LiveServer,
        client: TestClient,
    ) -> None:
        pair = [CHECKING, CARD]
        results = _mcp_call(
            live_server.url,
            seeded_tokens["agent"],
            [
                (
                    "propose_rule",
                    {"type": "transfer_link", "pair_accounts": pair, "window_days": 15},
                ),
                (
                    "propose_rule",
                    {"type": "transfer_link", "pair_accounts": pair, "window_days": -1},
                ),
                (
                    "propose_rule",
                    {"type": "transfer_link", "pair_accounts": pair, "window_days": 2.5},
                ),
                (
                    "propose_rule",
                    {"type": "categorize", "category": "expenses.dining", "window_days": 5},
                ),
                ("preview_rule", {"window_days": 5}),
                ("preview_rule", {"pair_accounts": pair, "window_days": 15}),
                ("preview_rule", {"pair_accounts": pair, "window_days": 2.5}),
                (
                    "preview_rule",
                    {
                        "pair_accounts": pair,
                        "match_field": "payee",
                        "match_mode": "contains",
                        "match_text": "remittance",
                    },
                ),
                ("preview_rule", {"pair_accounts": [CHECKING]}),
            ],
        )
        for result in results:
            assert "error" in result, result
        assert "between 0 and 14" in results[0]["error"]
        assert "only to transfer_link" in results[3]["error"]
        assert client.get("/rules").json() == []
