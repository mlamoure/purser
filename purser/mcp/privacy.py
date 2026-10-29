"""Minimal disclosure to agents (issue #437, DESIGN §9).

Everything a tool returns lands in the agent's model-vendor context, so a tool
returns only what the user's privacy defaults allow, plus what the agent asks
for with ``include``. There is no approval step: the agent decides, the tool
guidance teaches it to choose well, and two user settings (both off by
default) set the defaults:

- ``privacy_purchase``: payee names become stable references (``py_...``,
  ``crypto.payee_ref``); descriptions, posting memos, schedule and
  suggestion names and rule text are omitted.
- ``privacy_amount``: single-transaction amounts are omitted. Aggregates
  (totals, balances, net worth, series) stay exact: calling an aggregate
  tool is already the disclosure choice.

External account identifiers are omitted for every agent unless included.
Amounts are exact or omitted, never rounded or noised, and every omitted
field is NAMED in a ``withheld`` list: an agent that sees a field missing
concludes "unknown" and may try to fix it (#176 Q1).

Each tool declares what it emits (``Emits``) on ``audited_tool``, with no
default, like its scope. The wrapper scrubs centrally, before the result is
serialized into text, so neither tier leaks:

1. Declared key paths are dropped; the values dropped are remembered.
2. Every string is then scrubbed BY VALUE: payee names become references,
   and narrative (schedule and suggestion names, rule text, the descriptions
   dropped in step 1) and withheld amounts (the amounts dropped in step 1,
   and the dictionary's bill and rule amounts) become ``[withheld]``. A
   string that is itself a plain number is never value-scrubbed: it is a
   value the tool chose to return (an aggregate), and step 1 already dropped
   the single amounts by path.

Error messages get the full value scrub, plus every amount-shaped number
when amounts are withheld.

Inputs work by reference: a payee reference passed where a payee is named is
resolved to that payee server-side (also inside nested arguments, and as a
rule's whole match text); one written into free text (a description, a memo,
a note, a name) is refused, so a reference can never become a payee's real
name.
"""

import re
import threading
from collections.abc import Iterable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from typing import Any

PAYEE = "payee"
DESCRIPTION = "description"
AMOUNT = "amount"
EXTERNAL_IDS = "external_ids"
FIELDS = (PAYEE, DESCRIPTION, AMOUNT, EXTERNAL_IDS)

WITHHELD_TEXT = "[withheld]"
REF_RE = re.compile(r"\bpy_[a-z2-7]{10}\b")
# Arguments that name a payee: a reference resolves to the payee's name.
PAYEE_ARGS = frozenset({"payee", "payee_name"})
PAYEE_LIST_ARGS = frozenset({"payees"})
# A rule's match text may BE a reference (payee equals X), never contain one.
WHOLE_REF_ARGS = frozenset({"match_text"})
# Free-text arguments: a reference inside one is refused.
FREE_TEXT_ARGS = frozenset({"description", "memo", "note", "text", "name"})
# A name is replaced inside a longer string from this length on (with word
# boundaries); an exact value is always replaced.
MIN_SUBSTRING = 2
_NUMBER_RE = re.compile(r"^[-+]?\d[\d,]*(\.\d+)?$")
# Identifiers are never value-scrubbed: an ltree path (``assets.mortgage``,
# ``expenses.housing.rent``), a URI, a UUID or a reference. Accounts and
# categories stay (the user names them), and a rewritten path or daily_uri
# would break the agent's next call. A payee named exactly like one is
# still replaced (the exact lookup runs first).
_IDENTIFIER_RE = re.compile(
    r"^(?:[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+"
    r"|\[[A-Za-z0-9_.]+\]"
    r"|[a-z][a-z0-9+.-]*://\S*"
    r"|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"|py_[a-z2-7]{10})$"
)
_AMOUNTISH_RE = re.compile(r"(?<![\w.])[-+]?\$?\d[\d,]*\.\d{2,4}(?!\d)")


@dataclass(frozen=True)
class Emits:
    """What a tool's output carries that minimal disclosure governs.

    ``payee``/``narrative``/``rule_text``: the output can carry payee names,
    schedule or suggestion names, or rendered rules, anywhere (scrubbed by
    value). The path tuples name keys, dotted, with ``[]`` stepping into
    each element of a list (``items[].amount``; ``[].amount`` for a
    bare-list result); those keys are dropped when their field is withheld.
    ``balance`` paths are dropped with amounts (a running balance gives each
    row's amount away by subtraction) but, being aggregates, never used to
    scrub other strings."""

    payee: bool = False
    narrative: bool = False
    rule_text: bool = False
    description: tuple[str, ...] = ()
    amount: tuple[str, ...] = ()
    balance: tuple[str, ...] = ()
    external_ids: tuple[str, ...] = ()
    # Top-level keys of static text (guidance) that is never value-scrubbed:
    # a payee named like a common word must not rewrite the conventions.
    skip: tuple[str, ...] = ()
    # The body is scrubbed by what the tool calls (read_series -> the
    # purser:// resources, which honour the call's include): the wrapper adds
    # include and records disclosure for every field, and leaves the body.
    passthrough: bool = False

    def fields(self) -> set[str]:
        if self.passthrough:
            return set(FIELDS)
        out: set[str] = set()
        if self.payee:
            out.add(PAYEE)
        if self.narrative or self.description or self.rule_text:
            out.add(DESCRIPTION)
        if self.amount or self.balance or self.rule_text or self.narrative:
            out.add(AMOUNT)
        if self.external_ids:
            out.add(EXTERNAL_IDS)
        return out


# The fields the running tool call asked for with ``include``: the purser://
# resources read it, so read_series honours the same override.
current_include: ContextVar[set[str] | None] = ContextVar("current_include", default=None)

NOTHING = Emits()
# Errors are scrubbed as if every narrative surface could appear in them.
EVERYTHING = Emits(payee=True, narrative=True, rule_text=True)


class DisclosureError(ValueError):
    """A bad ``include`` value or a payee reference used where it cannot be."""


def parse_include(include: Iterable[str] | None) -> set[str]:
    chosen = set(include or ())
    unknown = chosen - set(FIELDS)
    if unknown:
        raise DisclosureError(
            f"unknown include field(s) {sorted(unknown)}; expected any of {list(FIELDS)}"
        )
    return chosen


def defaults(*, purchase: bool, amount: bool) -> set[str]:
    """The fields withheld by default: the user's two settings, plus external
    identifiers, which no agent task needs."""
    out = {EXTERNAL_IDS}
    if purchase:
        out |= {PAYEE, DESCRIPTION}
    if amount:
        out.add(AMOUNT)
    return out


def needs_dictionary(withheld: set[str], emits: Emits) -> bool:
    """Whether scrubbing this call can need the names dictionary."""
    if withheld & {PAYEE, DESCRIPTION} and (emits.payee or emits.narrative or emits.rule_text):
        return True
    return AMOUNT in withheld and (emits.narrative or emits.rule_text)


# --------------------------------------------------------------------------
# The dictionary: names and single amounts to scrub by value
# --------------------------------------------------------------------------


@dataclass
class Dictionary:
    payees: dict[str, str] = field(default_factory=dict)  # name -> ref
    refs: dict[str, str] = field(default_factory=dict)  # ref -> name
    # Schedule names, open suggestions' draft names and labels.
    narratives: set[str] = field(default_factory=set)
    rule_texts: set[str] = field(default_factory=set)
    rule_amounts: set[str] = field(default_factory=set)
    # Single-item amounts narrative text quotes: schedule amounts and open
    # suggestions' draft amounts ("Gym: monthly ~-40.00 on ...").
    item_amounts: set[str] = field(default_factory=set)
    _compiled: dict[tuple[Any, ...], Any] = field(default_factory=dict)

    def replacer(self, key: tuple[Any, ...], build: Any) -> Any:
        if key not in self._compiled:
            self._compiled[key] = build()
        return self._compiled[key]


_SUGGESTION_AMOUNT_KEYS = (
    "amount",
    "expected_amount",
    "posted_amount",
    "variance",
    "amount_exact",
)
_SUGGESTION_NAME_KEYS = ("name", "label")

_cache_lock = threading.Lock()
_cache: tuple[tuple[Any, ...], Dictionary] | None = None


def _fingerprint(session: Any) -> tuple[Any, ...]:
    """Changes whenever a payee, schedule, rule or suggestion row is added,
    changed or removed: count plus the newest row version (xmin) per table."""
    from sqlalchemy import text

    row = session.execute(
        text(
            "SELECT"
            " (SELECT count(*) FROM payees), (SELECT max(xmin::text::bigint) FROM payees),"
            " (SELECT count(*) FROM schedules), (SELECT max(xmin::text::bigint) FROM schedules),"
            " (SELECT count(*) FROM rules), (SELECT max(xmin::text::bigint) FROM rules),"
            " (SELECT count(*) FROM suggestions),"
            " (SELECT max(xmin::text::bigint) FROM suggestions),"
            " (SELECT count(*) FROM transactions WHERE status IN ('expected', 'pending')),"
            " (SELECT max(xmin::text::bigint) FROM transactions"
            "   WHERE status IN ('expected', 'pending'))"
        )
    ).one()
    return tuple(row)


def load_dictionary(session: Any) -> Dictionary:
    """The current dictionary, rebuilt only when the fingerprint moved (one
    cheap query per call otherwise): payees, schedules, rules and open
    suggestions are decrypted once, not on every tool call."""
    global _cache
    key = _fingerprint(session)
    with _cache_lock:
        if _cache is not None and _cache[0] == key:
            return _cache[1]
    built = _build_dictionary(session)
    with _cache_lock:
        _cache = (key, built)
    return built


def reset_cache() -> None:
    global _cache
    with _cache_lock:
        _cache = None


def _build_dictionary(session: Any) -> Dictionary:
    from sqlalchemy import select

    from purser import crypto
    from purser.models import Payee, Rule, Schedule, Suggestion, Transaction

    out = Dictionary()
    for payee_id, name in session.execute(select(Payee.id, Payee.name)):
        ref = crypto.payee_ref(payee_id)
        out.refs[ref] = name
        if name:
            out.payees[name] = ref
    for name, amount in session.execute(select(Schedule.name, Schedule.amount)):
        if name:
            out.narratives.add(name)
        if amount is not None:
            out.item_amounts.add(str(amount))
    for (draft,) in session.execute(select(Suggestion.draft).where(Suggestion.status == "open")):
        if not isinstance(draft, dict):
            continue
        for key in _SUGGESTION_NAME_KEYS:
            if isinstance(draft.get(key), str) and draft[key]:
                out.narratives.add(draft[key])
        for key in _SUGGESTION_AMOUNT_KEYS:
            if draft.get(key) is not None:
                out.item_amounts.add(str(draft[key]))
    # Forecast events and bill labels name live expected and pending rows by
    # their description when they have one; that set is small (what is
    # still to come), unlike the whole ledger's descriptions, which only
    # appear under declared keys.
    for (description,) in session.execute(
        select(Transaction.description).where(
            Transaction.status.in_(["expected", "pending"]),
            Transaction.superseded_by.is_(None),
        )
    ):
        if description:
            out.narratives.add(description)
    for rule in session.execute(select(Rule)).scalars():
        out.rule_texts |= {t for t in (rule.match_text, rule.payee_name, rule.note) if t}
        out.rule_amounts |= {
            str(a) for a in (rule.amount_exact, rule.amount_min, rule.amount_max) if a is not None
        }
    return out


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------


def resolve_inputs(kwargs: dict[str, Any], dictionary: Dictionary) -> dict[str, Any]:
    """Payee references in payee arguments become the payee's name — at any
    depth (``edits={"payee": ...}``); one written into free text is refused.
    Returns the arguments to call the tool with."""

    def resolve(value: str) -> str:
        name = dictionary.refs.get(value.strip())
        if name is None:
            raise DisclosureError(f"unknown payee reference {value.strip()}")
        return name

    def walk(key: str | None, value: Any) -> Any:
        if isinstance(value, dict):
            return {k: walk(k, v) for k, v in value.items()}
        if isinstance(value, list):
            inner = key if key in PAYEE_LIST_ARGS else None
            if inner is not None:
                return [
                    resolve(v) if isinstance(v, str) and REF_RE.fullmatch(v.strip()) else v
                    for v in value
                ]
            return [walk(None, v) for v in value]
        if not isinstance(value, str) or not REF_RE.search(value):
            return value
        whole = REF_RE.fullmatch(value.strip()) is not None
        if key in PAYEE_ARGS or key in WHOLE_REF_ARGS:
            if whole:
                return resolve(value)
            raise DisclosureError(
                f"{key} takes a payee reference on its own, not inside other text"
            )
        if key in FREE_TEXT_ARGS:
            raise DisclosureError(
                f"{key} cannot contain a payee reference: a reference names an existing "
                "payee and is never written as text"
            )
        return value

    return {k: walk(k, v) for k, v in kwargs.items()}


def has_ref(kwargs: Mapping[str, Any]) -> bool:
    def walk(value: Any) -> bool:
        if isinstance(value, str):
            return REF_RE.search(value) is not None
        if isinstance(value, list):
            return any(walk(v) for v in value)
        if isinstance(value, dict):
            return any(walk(v) for v in value.values())
        return False

    return any(walk(v) for v in kwargs.values())


# --------------------------------------------------------------------------
# Scrubbing
# --------------------------------------------------------------------------


class _Replacer:
    """One alternation regex over every name (case-insensitive) and one over
    every amount spelling, longest alternative first."""

    def __init__(self, names: dict[str, str], amounts: set[str]) -> None:
        self.names = {k.casefold(): v for k, v in names.items()}
        self.names_re = _alternation(
            [n for n in names if len(n) >= MIN_SUBSTRING],
            # Not inside a dotted token either: "mortgage" in assets.mortgage
            # is a path segment, not a mention.
            prefix=r"(?<![\w.])",
            suffix=r"(?!\w)(?!\.\w)",
            flags=re.IGNORECASE,
        )
        self.amounts_re = _alternation(sorted(amounts), prefix=r"(?<![\w.])", suffix=r"(?!\d)")

    def sub(self, value: str) -> tuple[str, set[str]]:
        """The scrubbed string and the fields it replaced."""
        kinds: set[str] = set()

        def name(m: re.Match[str]) -> str:
            replacement = self.names.get(m.group(0).casefold(), WITHHELD_TEXT)
            kinds.add(PAYEE if replacement.startswith("py_") else DESCRIPTION)
            return replacement

        def amount(_: re.Match[str]) -> str:
            kinds.add(AMOUNT)
            return WITHHELD_TEXT

        if self.names_re is not None:
            value = self.names_re.sub(name, value)
        if self.amounts_re is not None:
            value = self.amounts_re.sub(amount, value)
        return value, kinds


def _alternation(
    items: Iterable[str], *, prefix: str, suffix: str, flags: int = 0
) -> re.Pattern[str] | None:
    ordered = sorted(set(items), key=len, reverse=True)
    if not ordered:
        return None
    return re.compile(
        prefix + "(?:" + "|".join(re.escape(i) for i in ordered) + ")" + suffix, flags
    )


@dataclass
class Scrubber:
    """Applies one call's withheld set to a tool result."""

    withheld: set[str]
    emits: Emits
    dictionary: Dictionary
    hits: set[str] = field(default_factory=set)
    dropped_texts: set[str] = field(default_factory=set)
    dropped_amounts: set[str] = field(default_factory=set)
    _call_replacer: "_Replacer | None" = None

    @property
    def named(self) -> list[str]:
        """The fields this response actually withheld: what it names."""
        return sorted(self.hits & self.withheld)

    def fork_item(self) -> "Scrubber":
        """A scrubber for one item of a bare-list result: its paths lose the
        leading ``[].``, which named the list itself."""

        def inner(paths: tuple[str, ...]) -> tuple[str, ...]:
            return tuple(p.removeprefix("[].") for p in paths)

        e = self.emits
        emits = replace(
            e,
            description=inner(e.description),
            amount=inner(e.amount),
            balance=inner(e.balance),
            external_ids=inner(e.external_ids),
        )
        return Scrubber(withheld=self.withheld, emits=emits, dictionary=self.dictionary)

    # -- by value ---------------------------------------------------------
    #
    # Two layers: the dictionary's names and amounts (compiled once per
    # dictionary and declaration, cached on the dictionary), and the values
    # this call dropped (few; compiled per call, or per item of a list).

    def _base(self) -> tuple[dict[str, str], dict[str, str], _Replacer]:
        key = (
            frozenset(self.withheld),
            self.emits.payee,
            self.emits.narrative,
            self.emits.rule_text,
        )

        def build() -> tuple[dict[str, str], dict[str, str], _Replacer]:
            names: dict[str, str] = {}
            if DESCRIPTION in self.withheld:
                if self.emits.narrative or self.emits.payee:
                    names.update(dict.fromkeys(self.dictionary.narratives, WITHHELD_TEXT))
                if self.emits.rule_text:
                    names.update(dict.fromkeys(self.dictionary.rule_texts, WITHHELD_TEXT))
            if PAYEE in self.withheld and self.emits.payee:
                names.update(self.dictionary.payees)  # payees win over a same-named bill
            amounts: set[str] = set()
            if AMOUNT in self.withheld:
                if self.emits.rule_text:
                    amounts |= self.dictionary.rule_amounts
                if self.emits.narrative:
                    amounts |= self.dictionary.item_amounts
            forms = {form for v in amounts for form in amount_forms(v)}
            return names, {k.casefold(): v for k, v in names.items()}, _Replacer(names, forms)

        built: tuple[dict[str, str], dict[str, str], _Replacer] = self.dictionary.replacer(
            key, build
        )
        return built

    def _call(self) -> _Replacer | None:
        if self._call_replacer is None and (self.dropped_texts or self.dropped_amounts):
            texts = (
                dict.fromkeys(self.dropped_texts, WITHHELD_TEXT)
                if DESCRIPTION in self.withheld
                else {}
            )
            forms = (
                {form for v in self.dropped_amounts for form in amount_forms(v)}
                if AMOUNT in self.withheld
                else set()
            )
            self._call_replacer = _Replacer(texts, forms)
        return self._call_replacer

    def text(self, value: str) -> str:
        names, folded, base = self._base()
        stripped = value.strip()
        exact = names.get(value)
        if exact is None and folded:
            exact = folded.get(value.casefold())
        if exact is None and DESCRIPTION in self.withheld and value in self.dropped_texts:
            exact = WITHHELD_TEXT
        if exact is not None:
            self.hits.add(PAYEE if exact.startswith("py_") else DESCRIPTION)
            return exact
        if _NUMBER_RE.match(stripped) or _IDENTIFIER_RE.match(stripped):
            # A number the tool chose to return (path drops did the rest), or
            # an identifier the agent needs intact.
            return value
        scrubbed, kinds = base.sub(value)
        call = self._call()
        if call is not None:
            scrubbed, more = call.sub(scrubbed)
            kinds |= more
        self.hits |= kinds
        return scrubbed

    def error(self, message: str) -> str:
        """Errors get the full value scrub and, when amounts are withheld,
        lose every amount-shaped number."""
        full = Scrubber(withheld=self.withheld, emits=EVERYTHING, dictionary=self.dictionary)
        out, _ = full._base()[2].sub(message)
        if AMOUNT in self.withheld:
            out = _AMOUNTISH_RE.sub(WITHHELD_TEXT, out)
        return out

    # -- structure --------------------------------------------------------

    def _drop_paths(self) -> list[tuple[str, list[str]]]:
        paths: list[tuple[str, str]] = []
        if DESCRIPTION in self.withheld:
            paths += [(DESCRIPTION, p) for p in self.emits.description]
        if AMOUNT in self.withheld:
            paths += [(AMOUNT, p) for p in (*self.emits.amount, *self.emits.balance)]
        if EXTERNAL_IDS in self.withheld:
            paths += [(EXTERNAL_IDS, p) for p in self.emits.external_ids]
        return [(f, p.split(".")) for f, p in paths]

    def collect(self, value: Any) -> None:
        """Remember the values about to be dropped, so the strings that quote
        them (a headline, a label) are scrubbed of them too."""
        if DESCRIPTION in self.withheld:
            for path in self.emits.description:
                for found in _select(value, path.split(".")):
                    if isinstance(found, str) and found:
                        self.dropped_texts.add(found)
        if AMOUNT in self.withheld:
            for path in self.emits.amount:
                for found in _select(value, path.split(".")):
                    self.dropped_amounts |= _leaf_amounts(found)
        self._call_replacer = None

    def structure(self, value: Any) -> Any:
        for field_name, path in self._drop_paths():
            if _drop(value, path):
                self.hits.add(field_name)
        if isinstance(value, dict) and self.emits.skip:
            kept = {k: value[k] for k in self.emits.skip if k in value}
            out = self._strings({k: v for k, v in value.items() if k not in kept})
            return {k: kept.get(k, out.get(k)) for k in value}
        return self._strings(value)

    def _strings(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self._strings(v) for v in value]
        if isinstance(value, dict):
            return {k: self._strings(v) for k, v in value.items()}
        return value


def _leaf_amounts(value: Any) -> set[str]:
    if value is None or isinstance(value, bool):
        return set()
    if isinstance(value, dict):
        return {a for v in value.values() for a in _leaf_amounts(v)}
    if isinstance(value, list):
        return {a for v in value for a in _leaf_amounts(v)}
    return {str(value)}


def _select(value: Any, path: list[str]) -> list[Any]:
    if not path:
        return [value]
    head, rest = path[0], path[1:]
    if head == "[]":
        return [x for v in value for x in _select(v, rest)] if isinstance(value, list) else []
    if head.endswith("[]"):
        key = head[:-2]
        if isinstance(value, dict) and isinstance(value.get(key), list):
            return [x for v in value[key] for x in _select(v, rest)]
        return []
    if isinstance(value, dict) and head in value:
        return _select(value[head], rest)
    return []


def _drop(value: Any, path: list[str]) -> bool:
    """Drop the key at ``path`` everywhere it occurs; True if any was."""
    head, rest = path[0], path[1:]
    if head == "[]":
        if isinstance(value, list):
            return any([_drop(v, rest) for v in value])
        return False
    if head.endswith("[]"):
        key = head[:-2]
        if isinstance(value, dict) and isinstance(value.get(key), list) and rest:
            return any([_drop(v, rest) for v in value[key]])
        return False
    if not isinstance(value, dict) or head not in value:
        return False
    if rest:
        return _drop(value[head], rest)
    del value[head]
    return True


def amount_forms(value: str) -> set[str]:
    """The spellings a headline may use for one amount: as stored, unsigned,
    to the cent, with thousands separators."""
    try:
        d = Decimal(value)
    except InvalidOperation:
        return set()
    forms: set[str] = set()
    for x in (d, abs(d)):
        cents = x.quantize(Decimal("0.01"))
        forms |= {str(x), str(cents), f"{cents:,}", format(x.normalize(), "f")}
    return {f for f in forms if f.lstrip("-") not in {"0", "0.00", "0E-4", "0.0000"}}
