"""Fixture hygiene: no real-looking secrets anywhere in the test fixtures, and
money is never a float.

Scans ``tests/fixtures/**`` for PANs (Luhn-valid digit runs), secret
assignments, bearer/JWT tokens, and non-``@example.com`` emails; and loads
every JSON fixture with a ``parse_float`` hook that raises, so a float literal
in any money field is mechanically impossible.
"""

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import NoReturn

ROOT = Path(__file__).resolve().parent.parent
SCAN_DIRS = [ROOT / "tests" / "fixtures"]
TEXT_SUFFIXES = {".json", ".md", ".qif", ".csv", ".txt"}

PAN_RUN = re.compile(r"\d{13,19}")
SECRET_ASSIGN = re.compile(
    r"(?i)\b(password|passwd|secret|api[_-]?key|access[_-]?token|client[_-]?secret|authorization)"
    r"\b\s*[:=]\s*\S+"
)
BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{16,}")
JWT = re.compile(r"\beyJ[A-Za-z0-9._\-]{10,}")
EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def _iter_files() -> Iterator[Path]:
    for base in SCAN_DIRS:
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_file():
                yield path


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = ord(ch) - 48
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def test_no_pan_secret_bearer_or_foreign_email() -> None:
    for path in _iter_files():
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        text = path.read_text(encoding="utf-8")
        for run in PAN_RUN.findall(text):
            assert not _luhn_ok(run), f"Luhn-valid PAN-like run in {path}: {run}"
        assert not SECRET_ASSIGN.search(text), f"secret assignment found in {path}"
        assert not BEARER.search(text), f"bearer-shaped token found in {path}"
        assert not JWT.search(text), f"JWT-shaped token found in {path}"
        for email in EMAIL.findall(text):
            assert email.endswith("@example.com"), f"non-example email in {path}: {email}"


def _raise_on_float(value: str) -> NoReturn:
    raise AssertionError(f"float literal in a JSON fixture (money must be a string): {value!r}")


def test_json_fixtures_have_no_float_literals() -> None:
    for path in _iter_files():
        if path.suffix.lower() != ".json":
            continue
        json.loads(path.read_text(encoding="utf-8"), parse_float=_raise_on_float)
