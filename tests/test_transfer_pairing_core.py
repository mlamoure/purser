"""Transfer pairing in core (DESIGN §5): the greedy opposite-amount pairing
that the auto-link plan, the candidate list and the inbox cards share
(#386 review, item 12).

The pairing used to rescan, and re-sort, every incoming row for every
outgoing row — quadratic, and paid twice per pass once the candidate cards
were synced after the plan. It now looks only at incoming rows of the
matching amount. The output must not change: the property below compares
it with the previous algorithm, kept here verbatim as the reference.

Pure functions, no database.
"""

import uuid
from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from purser.core import matching as core_matching
from purser.core.matching import MatchInput, PairWindow, TransferCandidate


def _reference_pairs(
    transactions: list[MatchInput], *, window: PairWindow | None = None
) -> list[TransferCandidate]:
    """``detect_transfer_pairs`` as it was before the amount buckets."""
    candidates: list[TransferCandidate] = []
    outs = [t for t in transactions if t.amount < 0]
    ins = [t for t in transactions if t.amount > 0]
    used: set[UUID] = set()
    for out in sorted(outs, key=lambda t: t.date):
        for inc in sorted(ins, key=lambda t: t.date):
            if inc.id in used:
                continue
            if not core_matching._may_pair(out, inc):
                continue
            if inc.amount != -out.amount:
                continue
            limit = core_matching.TRANSFER_WINDOW_DAYS if window is None else window(out, inc)
            if limit is None:
                continue
            gap = abs((inc.date - out.date).days)
            if gap <= limit:
                candidates.append(
                    TransferCandidate(
                        outgoing_id=out.id,
                        incoming_id=inc.id,
                        amount=inc.amount,
                        date_gap_days=gap,
                    )
                )
                used.add(inc.id)
                break
    return candidates


DAY0 = date(2026, 3, 2)
ACCOUNTS = [uuid.UUID(int=n) for n in range(1, 4)]
# Equal values written differently ("40" and "40.00") must still pair.
MAGNITUDES = [Decimal("40"), Decimal("40.00"), Decimal("125.50"), Decimal("9.99")]


@st.composite
def _rows(draw: st.DrawFn) -> list[MatchInput]:
    count = draw(st.integers(min_value=0, max_value=24))
    rows = []
    for _ in range(count):
        account = draw(st.sampled_from([*ACCOUNTS, None]))
        held_for = draw(st.sampled_from([None, None, None, *ACCOUNTS]))
        rows.append(
            MatchInput(
                id=draw(st.uuids()),
                date=DAY0 + timedelta(days=draw(st.integers(min_value=0, max_value=20))),
                amount=draw(st.sampled_from(MAGNITUDES)) * draw(st.sampled_from([1, -1])),
                normalized_payee="",
                account_id=account,
                pair_account_id=held_for,
            )
        )
    return rows


def _uneven_window(out: MatchInput, inc: MatchInput) -> int | None:
    """A per-pair window like the widened stage's: some pairs ruled out,
    others given their own limit — a pure function of the pair."""
    key = (out.date.toordinal() + 3 * inc.date.toordinal()) % 9
    return None if key == 0 else key


class TestBucketedPairing:
    @settings(max_examples=300)
    @given(rows=_rows(), widened=st.booleans())
    def test_it_pairs_exactly_as_the_full_scan_did(
        self, rows: list[MatchInput], widened: bool
    ) -> None:
        window = _uneven_window if widened else None

        assert core_matching.detect_transfer_pairs(rows, window=window) == _reference_pairs(
            rows, window=window
        )

    def test_it_only_considers_rows_of_the_matching_amount(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """300 same-day transfers of distinct amounts, the incoming rows
        listed in reverse: a full scan weighs each outgoing row against every
        incoming row before its own (~45,000 checks); the buckets weigh it
        against its counterpart alone."""
        amounts = [Decimal(n + 1).scaleb(-2) + Decimal("10") for n in range(300)]
        rows = [MatchInput(uuid.uuid4(), DAY0, -a, "", ACCOUNTS[0]) for a in amounts]
        rows += [MatchInput(uuid.uuid4(), DAY0, a, "", ACCOUNTS[1]) for a in reversed(amounts)]
        calls = 0
        real = core_matching._may_pair

        def counting(out: MatchInput, inc: MatchInput) -> bool:
            nonlocal calls
            calls += 1
            return real(out, inc)

        monkeypatch.setattr(core_matching, "_may_pair", counting)

        pairs = core_matching.detect_transfer_pairs(rows)

        assert len(pairs) == 300
        assert calls <= 2 * 300


@st.composite
def _same_amount_history(draw: st.DrawFn) -> list[MatchInput]:
    """Years of rows that share one or two round amounts — a monthly
    savings sweep, a fixed card payment — the bucket the binary search
    enters instead of scanning (#386 review round 2, item 10)."""
    count = draw(st.integers(min_value=0, max_value=60))
    amounts = draw(st.sampled_from([[Decimal("500")], [Decimal("500"), Decimal("500.00")]]))
    rows = []
    for _ in range(count):
        rows.append(
            MatchInput(
                id=draw(st.uuids()),
                date=DAY0 + timedelta(days=draw(st.integers(min_value=0, max_value=1500))),
                amount=draw(st.sampled_from(amounts)) * draw(st.sampled_from([1, -1])),
                normalized_payee="",
                account_id=draw(st.sampled_from([*ACCOUNTS, None])),
                pair_account_id=draw(st.sampled_from([None, None, None, *ACCOUNTS])),
            )
        )
    return rows


def _widest_window(out: MatchInput, inc: MatchInput) -> int | None:
    """Per-pair windows up to the widest a rule may declare (decision 66)."""
    key = (out.date.toordinal() + 5 * inc.date.toordinal()) % 16
    return None if key == 15 else key


class TestTheBucketsDateWindow:
    @settings(max_examples=300)
    @given(rows=_same_amount_history(), widened=st.booleans())
    def test_it_pairs_exactly_as_the_full_scan_did(
        self, rows: list[MatchInput], widened: bool
    ) -> None:
        window = _widest_window if widened else None

        assert core_matching.detect_transfer_pairs(rows, window=window) == _reference_pairs(
            rows, window=window
        )

    def test_a_common_amount_looks_only_at_nearby_rows(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ten years of a monthly 500.00 deposit, and as many 500.00
        withdrawals, each 20 days after a deposit: no two are a transfer.
        The bucket holds all 120 deposits; walking it, each withdrawal would
        weigh every deposit (14,400 checks). Entered by binary search, each
        weighs only the deposits within the widest window of it."""
        rows: list[MatchInput] = []
        for month in range(120):
            deposit = DAY0 + timedelta(days=30 * month)
            rows.append(MatchInput(uuid.uuid4(), deposit, Decimal("500"), "", ACCOUNTS[1]))
            rows.append(
                MatchInput(
                    uuid.uuid4(), deposit + timedelta(days=20), Decimal("-500"), "", ACCOUNTS[0]
                )
            )
        calls = 0
        real = core_matching._may_pair

        def counting(out: MatchInput, inc: MatchInput) -> bool:
            nonlocal calls
            calls += 1
            return real(out, inc)

        monkeypatch.setattr(core_matching, "_may_pair", counting)

        assert core_matching.detect_transfer_pairs(rows) == []
        assert calls <= 2 * 120

    def test_a_window_wider_than_the_widest_is_clamped(self) -> None:
        """No pair is ever farther apart than TRANSFER_WINDOW_MAX_DAYS, even
        when a window function asks for more."""
        out = MatchInput(uuid.uuid4(), DAY0, Decimal("-500"), "", ACCOUNTS[0])
        far = MatchInput(
            uuid.uuid4(),
            DAY0 + timedelta(days=core_matching.TRANSFER_WINDOW_MAX_DAYS + 1),
            Decimal("500"),
            "",
            ACCOUNTS[1],
        )

        assert core_matching.detect_transfer_pairs([out, far], window=lambda o, i: 365) == []
