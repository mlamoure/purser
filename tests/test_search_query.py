"""The search query grammar (#141, decision 135): a pure parser, one syntax
for the register box, REST ``q`` and MCP ``q``."""

from datetime import date
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from purser.core.search_query import (
    MAX_FUZZY,
    MAX_QUERY_CHARS,
    MAX_TERMS,
    AmountRange,
    QueryError,
    Text,
    fold,
    parse,
    text_matches,
)


class TestFold:
    @pytest.mark.parametrize(
        ("raw", "folded"),
        [
            ("STARBUCKS #1234", "starbucks 1234"),
            ("7-Eleven", "7 eleven"),
            ("Café  Ünïcode", "cafe unicode"),
            ("  a.b,c  ", "a b c"),
            ("Straße", "strasse"),
            ("東京 Ramen", "東京 ramen"),  # letters of every script are kept
            ("line one\nline two", "line one line two"),
            ("***", ""),
        ],
    )
    def test_examples(self, raw: str, folded: str) -> None:
        assert fold(raw) == folded

    @given(st.text())
    def test_idempotent_and_single_spaced(self, raw: str) -> None:
        once = fold(raw)
        assert fold(once) == once
        assert "  " not in once and once == once.strip()
        assert "\n" not in once


class TestWords:
    def test_words_and_phrases(self) -> None:
        q = parse('whole foods "blue cup" -refund')
        assert q.text == (
            Text("any", "whole"),
            Text("any", "foods"),
            Text("any", "blue cup"),
            Text("any", "refund", negated=True),
        )

    def test_a_word_with_punctuation_is_one_contiguous_text(self) -> None:
        assert parse("7-eleven").text == (Text("any", "7 eleven"),)

    def test_empty_and_punctuation_only_clauses_are_ignored(self) -> None:
        q = parse('  -  "" # payee: ~ ')
        assert q.text == () and q.payee_refs == ()

    def test_an_unfinished_quote_runs_to_the_end(self) -> None:
        assert parse('"whole fo').text == (Text("any", "whole fo"),)


class TestFields:
    def test_text_fields(self) -> None:
        q = parse('payee:costco desc:"card purchase" memo:lunch -description:refund')
        assert q.text == (
            Text("payee", "costco"),
            Text("desc", "card purchase"),
            Text("memo", "lunch"),
            Text("desc", "refund", negated=True),
        )

    def test_field_names_ignore_case(self) -> None:
        assert parse("PAYEE:costco").text == (Text("payee", "costco"),)

    def test_payee_references(self) -> None:
        q = parse("payee:py_abcdefgh23 -py_zzzzzzzzzz")
        assert q.payee_refs == (("py_abcdefgh23", False), ("py_zzzzzzzzzz", True))
        assert q.text == ()
        # Quoted, it is text, not a reference.
        assert parse('payee:"py_abcdefgh23"').text == (Text("payee", "py abcdefgh23"),)

    def test_account_and_category(self) -> None:
        q = parse('account:checking -category:"dining out"')
        assert q.accounts == (("checking", False),)
        assert q.categories == (("dining out", True),)

    def test_an_unknown_field_is_text(self) -> None:
        assert parse("REF:12345 ach:payroll").text == (
            Text("any", "ref 12345"),
            Text("any", "ach payroll"),
        )

    @pytest.mark.parametrize("q", ["paye:costco", "categroy:dining", "acount:x", "memos:y"])
    def test_a_near_miss_names_the_fields(self, q: str) -> None:
        with pytest.raises(QueryError, match="the fields are payee:"):
            parse(q)

    def test_a_quoted_near_miss_is_text(self) -> None:
        assert parse('"paye:costco"').text == (Text("any", "paye costco"),)


class TestAmounts:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("42.10", AmountRange(Decimal("42.10"), Decimal("42.10"))),
            ("$1,200.50", AmountRange(Decimal("1200.50"), Decimal("1200.50"))),
            (">100", AmountRange(minimum=Decimal("100"), min_exclusive=True)),
            (">=100", AmountRange(minimum=Decimal("100"))),
            ("<20", AmountRange(maximum=Decimal("20"), max_exclusive=True)),
            ("<=20", AmountRange(maximum=Decimal("20"))),
            ("10..20", AmountRange(Decimal("10"), Decimal("20"))),
            ("10..", AmountRange(minimum=Decimal("10"))),
            ("..20", AmountRange(maximum=Decimal("20"))),
        ],
    )
    def test_forms(self, value: str, expected: AmountRange) -> None:
        (got,) = parse(f"amount:{value}").amounts
        assert got == expected
        for bound in (got.minimum, got.maximum):
            assert bound is None or isinstance(bound, Decimal)

    @pytest.mark.parametrize("value", ["abc", "1e5", "NaN", "Infinity", "..", "12.3.4"])
    def test_not_a_number(self, value: str) -> None:
        with pytest.raises(QueryError, match="amount"):
            parse(f"amount:{value}")

    def test_a_sign_points_to_is_debit(self) -> None:
        with pytest.raises(QueryError, match="is:debit"):
            parse("amount:-42.10")

    def test_an_empty_range_matches_nothing(self) -> None:
        assert parse("amount:20..10").never
        assert parse("amount:>5 amount:<=5").never is False  # two clauses: SQL decides


class TestDates:
    @pytest.mark.parametrize(
        ("value", "start", "end"),
        [
            ("2026", date(2026, 1, 1), date(2026, 12, 31)),
            ("2026-02", date(2026, 2, 1), date(2026, 2, 28)),
            ("2024-02", date(2024, 2, 1), date(2024, 2, 29)),
            ("2026-10-05", date(2026, 10, 5), date(2026, 10, 5)),
            (">=2026-01-01", date(2026, 1, 1), None),
            (">2026-01", date(2026, 2, 1), None),
            ("<2026", None, date(2025, 12, 31)),
            ("<=2026-03", None, date(2026, 3, 31)),
            ("2026-01..2026-03", date(2026, 1, 1), date(2026, 3, 31)),
            ("2026-06..", date(2026, 6, 1), None),
        ],
    )
    def test_forms(self, value: str, start: date | None, end: date | None) -> None:
        q = parse(f"date:{value}")
        assert (q.start, q.end) == (start, end)

    def test_clauses_intersect(self) -> None:
        q = parse("date:2026 date:>=2026-06-15")
        assert (q.start, q.end) == (date(2026, 6, 15), date(2026, 12, 31))
        assert parse("date:2025 date:2026").never

    @pytest.mark.parametrize("value", ["yesterday", "2026-13", "2026-02-30", "26-01-01"])
    def test_not_a_date(self, value: str) -> None:
        with pytest.raises(QueryError, match="date"):
            parse(f"date:{value}")


class TestStatusAndIs:
    def test_status(self) -> None:
        assert parse("status:pending,posted").statuses == {"pending", "posted"}
        assert parse("status:pending status:pending,void").statuses == {"pending"}
        assert parse("status:pending status:posted").never
        with pytest.raises(QueryError, match="not a status"):
            parse("status:cleared")

    def test_is(self) -> None:
        q = parse("is:needs-review is:debit")
        assert (q.needs_review, q.direction) == (True, "debit")
        assert parse("IS:Needs-Review").needs_review is True
        assert parse("is:debit is:credit").never
        with pytest.raises(QueryError, match="not supported"):
            parse("is:transfer")
        # The review-mark terms were removed (#694).
        for removed in ("unreviewed", "reviewed"):
            with pytest.raises(QueryError, match="needs-review"):
                parse(f"is:{removed}")

    @pytest.mark.parametrize("q", ["-amount:5", "-date:2026", "-status:void", "-is:debit"])
    def test_structured_clauses_take_no_minus(self, q: str) -> None:
        with pytest.raises(QueryError, match="not supported"):
            parse(q)


class TestNeverCrashes:
    @given(st.text(max_size=60))
    def test_any_text_parses_or_names_its_fix(self, q: str) -> None:
        try:
            parsed = parse(q)
        except QueryError as exc:
            assert str(exc)
            return
        for clause in parsed.text:
            assert clause.text and clause.text == fold(clause.text)


class TestTextMatches:
    def test_any_field_and_scopes(self) -> None:
        row = {"payee": "blue cup", "description": "pos purchase 1234", "memos": ["team lunch"]}
        assert text_matches(parse("blue lunch").text, **row)
        assert text_matches(parse("purch -refund").text, **row)
        assert not text_matches(parse("payee:lunch").text, **row)
        assert text_matches(parse("memo:lunch desc:pos payee:cup").text, **row)
        assert not text_matches(parse("-team").text, **row)
        assert not text_matches(parse('"cup pos"').text, **row)  # never across fields


class TestBounds:
    """A query and a typo-tolerant word are bounded (#698): finding the
    spellings one edit away grows with the word's length squared."""

    def test_a_long_query_is_refused_saying_so(self) -> None:
        with pytest.raises(QueryError, match=str(MAX_QUERY_CHARS)):
            parse("a" * (MAX_QUERY_CHARS + 1))
        parse("a" * MAX_QUERY_CHARS)

    def test_too_many_terms_are_refused_saying_so(self) -> None:
        with pytest.raises(QueryError, match=str(MAX_TERMS)):
            parse(" ".join(["ab"] * (MAX_TERMS + 1)))
        assert len(parse(" ".join(["ab"] * MAX_TERMS)).text) == MAX_TERMS

    def test_a_tilde_on_a_very_long_word_is_exact(self) -> None:
        (longest,) = parse("a" * MAX_FUZZY + "~").text
        assert longest.fuzzy
        (longer,) = parse("a" * (MAX_FUZZY + 1) + "~").text
        assert not longer.fuzzy


class TestTypoTolerance:
    def test_a_tilde_asks_for_one_typo(self) -> None:
        (clause,) = parse("starbuks~").text
        assert (clause.text, clause.fuzzy) == ("starbuks", True)
        (scoped,) = parse("payee:Costko~").text
        assert (scoped.field, scoped.text, scoped.fuzzy, scoped.raw) == (
            "payee",
            "costko",
            True,
            "Costko",
        )

    @pytest.mark.parametrize("q", ['"starbuks~"', "ab~", "7-eleven~", "store12~", "~"])
    def test_only_one_word_of_three_letters_without_digits(self, q: str) -> None:
        assert not any(c.fuzzy for c in parse(q).text)

    def test_matching(self) -> None:
        clauses = parse("starbuks~").text
        row = {"payee": "", "memos": []}
        assert text_matches(clauses, description="starbucks 12", **row)
        assert text_matches(clauses, description="pos starbuk", **row)
        assert not text_matches(clauses, description="sbarbukx x", **row)  # two edits
        assert not text_matches(clauses, description="starbuck5", **row)  # digits: no typo
        assert not text_matches(parse("starbuks").text, description="starbucks", **row)

    @given(st.text(alphabet="abc", max_size=6), st.text(alphabet="abc", max_size=6))
    def test_one_edit_is_the_edit_distance(self, a: str, b: str) -> None:
        from purser.core.search_query import _distance, one_edit_away, within_one_edit

        assert within_one_edit(a, b) == (_distance(a, b) <= 1)
        assert (b in one_edit_away(a, "abc")) == (_distance(a, b) == 1)
