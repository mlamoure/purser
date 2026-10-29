"""Property-based tests for the pure ledger core (no I/O)."""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from purser.core import ledger as core
from tests.conftest import LEDGER_CURRENCIES, balanced_multi_currency

amounts = st.decimals(
    min_value=Decimal("-9999999.9999"),
    max_value=Decimal("9999999.9999"),
    allow_nan=False,
    allow_infinity=False,
    places=4,
).filter(lambda d: d != 0)


@given(amount=amounts)
def test_expand_flat_always_balances(amount: Decimal) -> None:
    specs = core.expand_flat(
        account_id=uuid.uuid4(), category_id=uuid.uuid4(), amount=amount, currency="USD"
    )
    assert core.is_balanced(specs)
    assert specs[0].amount == core.quantize(amount)
    assert specs[1].amount == -core.quantize(amount)


@given(amount=amounts.filter(lambda d: d > 0))
def test_expand_transfer_always_balances(amount: Decimal) -> None:
    specs = core.expand_transfer(
        from_account_id=uuid.uuid4(), to_account_id=uuid.uuid4(), amount=amount, currency="USD"
    )
    assert core.is_balanced(specs)
    assert specs[0].amount < 0 < specs[1].amount


positive = st.decimals(
    min_value=Decimal("0.0001"),
    max_value=Decimal("9999999.9999"),
    allow_nan=False,
    allow_infinity=False,
    places=4,
)


@st.composite
def cash_and_principal(draw: st.DrawFn) -> tuple[Decimal, Decimal]:
    """A split loan payment's legs inside the bounds: 0 < principal < cash."""
    cash = draw(positive.filter(lambda d: d > Decimal("0.0001")))
    principal = draw(
        st.decimals(min_value=Decimal("0.0001"), max_value=cash - Decimal("0.0001"), places=4)
    )
    return cash, principal


def _legs(specs: list[core.PostingSpec], virtual: set[uuid.UUID]) -> list[core.Leg]:
    return [core.Leg(s.account_id, s.amount, s.account_id in virtual) for s in specs]


class TestSplitLoanPayment:
    """Decision 62: one transaction, the exact cash leg, the exact principal
    leg, one balancing remainder posting — zero-sum by construction."""

    @given(legs=cash_and_principal())
    def test_zero_sum_with_exact_bank_legs(self, legs: tuple[Decimal, Decimal]) -> None:
        cash, principal = legs
        checking, loan, interest = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        specs = core.expand_split_transfer(
            outgoing_account_id=checking,
            outgoing_amount=-cash,
            incoming_account_id=loan,
            incoming_amount=principal,
            remainder_category_id=interest,
            currency="USD",
        )
        assert core.is_balanced(specs)
        by_account = {s.account_id: s.amount for s in specs}
        assert len(by_account) == len(specs) == 3  # one posting per account — no extra loan leg
        assert by_account[checking] == -cash  # the bank's cash leg, exactly
        assert by_account[loan] == principal  # the loan's principal leg, exactly
        assert by_account[interest] == cash - principal > 0
        assert core.transfer_shape(_legs(specs, {interest})) is core.TransferShape.SPLIT

    @given(legs=cash_and_principal())
    def test_remainder_is_what_the_cash_paid_beyond_principal(
        self, legs: tuple[Decimal, Decimal]
    ) -> None:
        cash, principal = legs
        assert core.split_transfer_remainder(-cash, principal) == cash - principal

    @pytest.mark.parametrize(
        ("outgoing", "incoming", "message"),
        [
            pytest.param("-500.00", "500.00", "already cancel", id="equal"),
            pytest.param("-50.00", "650.12", "exceeds", id="greater"),
            pytest.param("-612.40", "0", "must be positive", id="zero"),
            pytest.param("-612.40", "-540.15", "must be positive", id="negative"),
            pytest.param("612.40", "540.15", "must be negative", id="positive-outgoing"),
        ],
    )
    def test_bounds_are_strict(self, outgoing: str, incoming: str, message: str) -> None:
        with pytest.raises(ValueError, match=message):
            core.split_transfer_remainder(Decimal(outgoing), Decimal(incoming))
        with pytest.raises(ValueError, match=message):
            core.expand_split_transfer(
                outgoing_account_id=uuid.uuid4(),
                outgoing_amount=Decimal(outgoing),
                incoming_account_id=uuid.uuid4(),
                incoming_amount=Decimal(incoming),
                remainder_category_id=uuid.uuid4(),
                currency="USD",
            )

    @given(cash=positive, extra=st.decimals(min_value=0, max_value=Decimal("9999.9999"), places=4))
    def test_principal_at_or_above_the_cash_is_never_booked(
        self, cash: Decimal, extra: Decimal
    ) -> None:
        """The review's case: a -50.00 leg against +650.12 must not book
        -600.12 of "interest" — every incoming >= |outgoing| is refused."""
        with pytest.raises(ValueError):
            core.split_transfer_remainder(-cash, cash + extra)


class TestTransferShape:
    """A row is a transfer only when its postings are a shape Purser creates,
    so #245's multi-posting templates are never misread as transfers."""

    @given(amount=amounts.filter(lambda d: d > 0))
    def test_plain_transfer(self, amount: Decimal) -> None:
        specs = core.expand_transfer(
            from_account_id=uuid.uuid4(), to_account_id=uuid.uuid4(), amount=amount, currency="USD"
        )
        assert core.transfer_shape(_legs(specs, set())) is core.TransferShape.PLAIN
        # A correction's reversal leg of a transfer is still a transfer.
        reversal = core.reversal_postings(specs)
        assert core.transfer_shape(_legs(reversal, set())) is core.TransferShape.PLAIN

    @given(amount=amounts)
    def test_flat_row_is_not_a_transfer(self, amount: Decimal) -> None:
        category = uuid.uuid4()
        specs = core.expand_flat(
            account_id=uuid.uuid4(), category_id=category, amount=amount, currency="USD"
        )
        assert core.transfer_shape(_legs(specs, {category})) is None

    @given(legs=cash_and_principal(), data=st.data())
    def test_shape_ignores_posting_order(
        self, legs: tuple[Decimal, Decimal], data: st.DataObject
    ) -> None:
        cash, principal = legs
        interest = uuid.uuid4()
        specs = core.expand_split_transfer(
            outgoing_account_id=uuid.uuid4(),
            outgoing_amount=-cash,
            incoming_account_id=uuid.uuid4(),
            incoming_amount=principal,
            remainder_category_id=interest,
            currency="USD",
        )
        shuffled = data.draw(st.permutations(specs))
        assert core.transfer_shape(_legs(shuffled, {interest})) is core.TransferShape.SPLIT

    @given(
        net=positive,
        deductions=st.lists(positive, min_size=1, max_size=4),
        to_savings=positive,
    )
    def test_multi_posting_paycheck_is_not_a_transfer(
        self, net: Decimal, deductions: list[Decimal], to_savings: Decimal
    ) -> None:
        """#245's paycheck: gross income splits into net pay, deductions and a
        savings leg — two real accounts, still not a transfer."""
        checking, savings, income = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        deduction_ids = [uuid.uuid4() for _ in deductions]
        gross = net + to_savings + sum(deductions, Decimal(0))
        legs = [
            core.Leg(checking, net, False),
            core.Leg(savings, to_savings, False),
            core.Leg(income, -gross, True),
            *(core.Leg(i, d, True) for i, d in zip(deduction_ids, deductions, strict=True)),
        ]
        assert sum(leg.amount for leg in legs) == 0
        assert core.transfer_shape(legs) is None

    @pytest.mark.parametrize(
        "legs",
        [
            pytest.param(
                [
                    ("a", "-100.00", False),
                    ("b", "60.00", False),
                    ("i", "30.00", True),
                    ("f", "10.00", True),
                ],
                id="two-real-two-categories",
            ),
            pytest.param(
                [("a", "-100.00", False), ("b", "60.00", False), ("c", "40.00", False)],
                id="three-real",
            ),
            pytest.param(
                [("a", "-50.00", False), ("b", "650.12", False), ("i", "-600.12", True)],
                id="negative-remainder",
            ),
            pytest.param(
                [("a", "-100.00", False), ("a", "60.00", False), ("i", "40.00", True)],
                id="same-account",
            ),
            pytest.param(
                [("a", "-100.00", False), ("a", "100.00", False)], id="same-account-plain"
            ),
            pytest.param(
                [("a", "-100.00", False), ("b", "60.00", False), ("i", "30.00", True)],
                id="unbalanced-remainder",
            ),
            pytest.param([("a", "-100.00", False)], id="one-leg"),
        ],
    )
    def test_other_shapes_are_not_transfers(self, legs: list[tuple[str, str, bool]]) -> None:
        ids = {name: uuid.uuid4() for name, _, _ in legs}
        built = [core.Leg(ids[name], Decimal(amount), virtual) for name, amount, virtual in legs]
        assert core.transfer_shape(built) is None


@given(amount=amounts)
def test_reversal_cancels_original(amount: Decimal) -> None:
    specs = core.expand_flat(
        account_id=uuid.uuid4(), category_id=uuid.uuid4(), amount=amount, currency="USD"
    )
    reversal = core.reversal_postings(specs)
    assert core.is_balanced(specs + reversal)
    by_account: dict[uuid.UUID, Decimal] = {}
    for p in specs + reversal:
        by_account[p.account_id] = by_account.get(p.account_id, Decimal(0)) + p.amount
    assert all(total == 0 for total in by_account.values())


def _multi_currency_specs(legs: list[tuple[str, Decimal]]) -> list[core.PostingSpec]:
    return [core.PostingSpec(uuid.uuid4(), amount, currency) for currency, amount in legs]


class TestMultiCurrencyBalance:
    """Guardrail 3 with more than one currency: balance is per (transaction,
    currency), so a currency-blind zero total is not enough."""

    @given(legs=balanced_multi_currency())
    def test_every_currency_sums_to_zero(self, legs: list[tuple[str, Decimal]]) -> None:
        specs = _multi_currency_specs(legs)
        assert core.is_balanced(specs)
        totals = core.balance_by_currency(specs)
        assert set(totals) == {currency for currency, _ in legs}
        assert all(total == 0 for total in totals.values())

    @given(legs=balanced_multi_currency(), data=st.data())
    def test_moving_one_posting_to_another_currency_unbalances(
        self, legs: list[tuple[str, Decimal]], data: st.DataObject
    ) -> None:
        i = data.draw(st.integers(min_value=0, max_value=len(legs) - 1), label="moved leg")
        currency, amount = legs[i]
        other = data.draw(st.sampled_from([c for c in LEDGER_CURRENCIES if c != currency]))
        moved = [*legs[:i], (other, amount), *legs[i + 1 :]]
        assert sum((a for _, a in moved), Decimal(0)) == 0  # still zero currency-blind
        assert not core.is_balanced(_multi_currency_specs(moved))

    @given(amount=amounts)
    def test_opposite_amounts_in_two_currencies_do_not_balance(self, amount: Decimal) -> None:
        specs = _multi_currency_specs([("USD", amount), ("EUR", -amount)])
        assert not core.is_balanced(specs)
        assert core.balance_by_currency(specs) == {"USD": amount, "EUR": -amount}


@given(name=st.text(min_size=0, max_size=80))
def test_normalize_payee_is_idempotent(name: str) -> None:
    once = core.normalize_payee(name)
    assert core.normalize_payee(once) == once


def test_normalize_payee_known_cases() -> None:
    assert core.normalize_payee("CHIPOTLE 2419  DENVER CO") == core.normalize_payee(
        "Chipotle #2419, Denver CO"
    )
    assert core.normalize_payee("Café Río") == "cafe rio"


def test_content_hash_identity_and_sensitivity() -> None:
    base = dict(
        external_account_id="ACT-1",
        date=date(2026, 7, 1),
        amount=Decimal("-14.00"),
        description="CHIPOTLE 2419",
    )
    assert core.content_hash(**base) == core.content_hash(**base)  # type: ignore[arg-type]
    # Same content, differently-formatted description -> same identity.
    assert core.content_hash(**{**base, "description": "Chipotle #2419"}) == (  # type: ignore[arg-type]
        core.content_hash(**base)  # type: ignore[arg-type]
    )
    for field, changed in [
        ("external_account_id", "ACT-2"),
        ("date", date(2026, 7, 2)),
        ("amount", Decimal("-14.01")),
        ("description", "SHAKE SHACK"),
    ]:
        assert core.content_hash(**{**base, field: changed}) != core.content_hash(**base)  # type: ignore[arg-type]


class TestOccurrenceOrdinal:
    """Identical rows in one payload are occurrences 1..n (issue #275,
    decision 68); occurrence 1 is the identity every row had before."""

    def _hash(self, description: str = "CHIPOTLE 2419", occurrence: int = 1) -> str:
        return core.content_hash(
            external_account_id="ACT-1",
            date=date(2026, 7, 1),
            amount=Decimal("-14.00"),
            description=description,
            occurrence=occurrence,
        )

    def test_first_occurrence_keeps_the_pre_ordinal_hash(self) -> None:
        # Pinned from the recipe as it stood before the ordinal existed: every
        # identity already stored must hash exactly the same, or the next sync
        # would re-import history.
        golden = "91d1f9f10b85554da6a57a9a7427710e008045b791e39c18ad2da2d8c722bae3"
        assert self._hash() == golden
        assert self._hash(occurrence=1) == golden

    def test_later_occurrences_are_distinct_identities(self) -> None:
        hashes = [self._hash(occurrence=n) for n in range(1, 6)]
        assert len(set(hashes)) == 5
        # Formatting that normalizes away still lands on the same occurrence.
        assert self._hash("Chipotle #2419", 2) == self._hash(occurrence=2)

    @pytest.mark.parametrize("occurrence", [0, -1])
    def test_occurrence_is_one_based(self, occurrence: int) -> None:
        with pytest.raises(ValueError, match="1-based"):
            self._hash(occurrence=occurrence)

    @given(
        first=st.text(max_size=30),
        other=st.text(max_size=30),
        occurrence=st.integers(min_value=2, max_value=50),
    )
    def test_a_qualified_identity_never_equals_an_unqualified_one(
        self, first: str, other: str, occurrence: int
    ) -> None:
        # A normalized description never contains '#' or '|', so no row's
        # first occurrence can collide with another row's n-th.
        assert self._hash(first, occurrence) != self._hash(other)

    @given(keys=st.lists(st.sampled_from("abc"), max_size=30))
    def test_ordinals_number_each_key_in_sequence(self, keys: list[str]) -> None:
        ordinals = core.occurrence_ordinals(keys)
        assert len(ordinals) == len(keys)
        for i, (key, ordinal) in enumerate(zip(keys, ordinals, strict=True)):
            assert ordinal == keys[: i + 1].count(key)
        for key in set(keys):
            assert sorted(o for k, o in zip(keys, ordinals, strict=True) if k == key) == list(
                range(1, keys.count(key) + 1)
            )

    @given(
        payloads=st.lists(
            st.lists(
                st.tuples(
                    st.integers(min_value=1, max_value=3),  # day
                    st.sampled_from([Decimal("-4.50"), Decimal("-12.00")]),
                    st.sampled_from(["Coffee", "COFFEE", "Bagel"]),
                ),
                max_size=8,
            ),
            min_size=1,
            max_size=5,
        )
    )
    def test_import_model_holds_the_max_count_of_each_identity(
        self, payloads: list[list[tuple[int, Decimal, str]]]
    ) -> None:
        """Model of guardrail 3 with duplicates: import each payload into a
        set of identities, skipping hashes that exist. Afterwards every
        identity is present as often as the payload that carried it most,
        and re-importing any payload adds nothing — the overlap property."""

        def row_hash(row: tuple[int, Decimal, str], occurrence: int = 1) -> str:
            day, amount, desc = row
            return core.content_hash(
                external_account_id="ACT-1",
                date=date(2026, 7, day),
                amount=amount,
                description=desc,
                occurrence=occurrence,
            )

        def identities(payload: list[tuple[int, Decimal, str]]) -> list[str]:
            firsts = [row_hash(row) for row in payload]
            ordinals = core.occurrence_ordinals(firsts)
            return [row_hash(row, n) for row, n in zip(payload, ordinals, strict=True)]

        ledger: set[str] = set()
        most: dict[str, int] = {}  # occurrence-1 hash -> largest count in one payload
        for payload in payloads:
            ledger |= set(identities(payload))
            firsts = [row_hash(row) for row in payload]
            for first in set(firsts):
                most[first] = max(most.get(first, 0), firsts.count(first))
            assert len(ledger) == sum(most.values())
        for payload in payloads:
            assert set(identities(payload)) <= ledger


def test_status_machine() -> None:
    S = core.Status
    assert core.can_transition(S.EXPECTED, S.PENDING)
    assert core.can_transition(S.EXPECTED, S.VOID)
    assert core.can_transition(S.PENDING, S.POSTED)
    assert core.can_transition(S.PENDING, S.VOID)
    # Bare posted->void is not a transition: posted rows leave only via
    # linked corrections or void-with-superseded_by (service/DB layer).
    assert not core.can_transition(S.POSTED, S.VOID)
    assert not core.can_transition(S.POSTED, S.PENDING)
    assert not core.can_transition(S.VOID, S.PENDING)
    assert not core.can_transition(S.VOID, S.POSTED)
    assert not core.can_transition(S.PENDING, S.EXPECTED)


def test_path_label() -> None:
    assert core.path_label("Dining Out") == "dining_out"
    assert core.path_label("Citi® Card") == "citi_card"
    with pytest.raises(ValueError):
        core.path_label("!!!")


class TestMaskedVariant:
    """Identity under digit-masking (issue #171, decision 44)."""

    def test_the_real_pair_matches_both_directions(self) -> None:
        unmasked = "MONEY LINE RECEIVED EFT FUNDS RECEIVED ER13529574 (Cash)"
        masked = "MONEY LINE RECEIVED EFT FUNDS RECEIVED ERXXXX9574 (Cash)"
        assert core.masked_variant(unmasked, masked)
        assert core.masked_variant(masked, unmasked)

    def test_identical_strings_are_not_variants(self) -> None:
        # Identical text is the plain content-hash dedup's job, not masking's.
        assert not core.masked_variant("ER1234", "ER1234")

    def test_distinct_visible_digits_never_match(self) -> None:
        assert not core.masked_variant("ER13529574", "ER83389628")
        assert not core.masked_variant("ERXXXX9574", "ERXXXX9628")

    def test_length_mismatch_never_matches(self) -> None:
        assert not core.masked_variant("ER13529574", "ER1352957")
        assert not core.masked_variant("", "X")

    def test_mask_over_nondigit_never_matches(self) -> None:
        assert not core.masked_variant("PAYMENT AB", "PAYMENT XX")

    def test_star_and_lowercase_masks_count(self) -> None:
        assert core.masked_variant("REF 1234", "REF **34")
        assert core.masked_variant("REF 1234", "REF xx34")

    @given(st.text(alphabet=st.characters(codec="ascii"), max_size=40))
    def test_no_string_is_its_own_variant(self, s: str) -> None:
        assert not core.masked_variant(s, s)

    @given(
        st.text(alphabet=st.sampled_from("ABC 0123456789()"), min_size=1, max_size=40).filter(
            lambda s: any(c.isdigit() for c in s)
        ),
        st.data(),
    )
    def test_masking_any_digit_subset_matches(self, s: str, data: st.DataObject) -> None:
        digit_positions = [i for i, c in enumerate(s) if c.isdigit()]
        subset = data.draw(st.lists(st.sampled_from(digit_positions), min_size=1, unique=True))
        masked = "".join("X" if i in subset else c for i, c in enumerate(s))
        assert core.masked_variant(s, masked)
        assert core.masked_variant(masked, s)
