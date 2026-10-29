"""Price providers (decision 89). A provider sees ticker symbols only — never
quantities, accounts or values — and answers daily closes.

Ships with what is implemented: ``tiingo``. Another provider is a class here
plus a wider ``security_prices.source`` CHECK and a wider
``price_answered.source`` CHECK: the job stores a provider's closes and
the date ranges it answered for good in one savepoint, so a provider missing
from either stores nothing for that window (the window fails, the others go
on).

Secrets: the API key travels only in the request's Authorization header. It
is never logged, never part of an exception message, and never chained into a
traceback (httpx errors are re-raised ``from None`` as a ``PriceFetchError``
carrying the symbol and status code).
"""

import json
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
from typing import Protocol
from urllib.parse import quote

import httpx

from purser.core import holdings as core

TIINGO = "tiingo"


class PriceFetchError(Exception):
    """One symbol's fetch failed. The message names the symbol and the HTTP
    status (or the transport failure's class name) and nothing else."""

    def __init__(self, symbol: str, status: int | None, reason: str) -> None:
        detail = f"HTTP {status}" if status is not None else reason
        super().__init__(f"{symbol}: price fetch failed ({detail})")
        self.symbol = symbol
        self.status = status
        self.reason = reason


class Closes(list[tuple[date, Decimal]]):
    """A provider's closes for one window. ``dropped`` counts the rows it left
    out because their close does not fit a price (NUMERIC(20,6)): one absurd
    row costs that row, never the window (decision 89)."""

    def __init__(self, rows: list[tuple[date, Decimal]] | None = None, dropped: int = 0) -> None:
        super().__init__(rows or [])
        self.dropped = dropped


class PriceProvider(Protocol):
    name: str

    def closes(self, symbol: str, start: date, end: date) -> list[tuple[date, Decimal]]:
        """Daily closes of ``symbol`` from ``start`` to ``end`` inclusive
        (a ``Closes`` when the provider drops rows)."""
        ...


class TiingoProvider:
    """Tiingo end-of-day prices: ``GET /tiingo/daily/{ticker}/prices``.

    Reads the UNADJUSTED ``close``: a quantity recorded before a split times
    the close of that day is that day's value, which an adjusted series would
    misstate. Parses the body with ``json.loads(..., parse_float=Decimal)`` —
    a float never touches a price (guardrail 1)."""

    name = TIINGO
    base_url = "https://api.tiingo.com/tiingo/daily"

    def __init__(
        self, api_key: str, *, client: httpx.Client | None = None, timeout: float = 20.0
    ) -> None:
        self._key = api_key
        self._client = client
        self._timeout = timeout

    def __repr__(self) -> str:  # never the key
        return "TiingoProvider()"

    @staticmethod
    def ticker(symbol: str) -> str:
        """Tiingo spells share classes with a dash (BRK-B), Purser may store
        BRK.B."""
        return symbol.replace(".", "-").lower()

    def closes(self, symbol: str, start: date, end: date) -> Closes:
        url = f"{self.base_url}/{quote(self.ticker(symbol), safe='-')}/prices"
        params = {"startDate": start.isoformat(), "endDate": end.isoformat()}
        headers = {"Authorization": f"Token {self._key}", "Accept": "application/json"}
        try:
            if self._client is not None:
                resp = self._client.get(url, params=params, headers=headers)
            else:
                with httpx.Client(timeout=self._timeout) as client:
                    resp = client.get(url, params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise PriceFetchError(symbol, None, type(exc).__name__) from None
        if resp.status_code == 404:
            raise PriceFetchError(symbol, 404, "not found")
        if resp.status_code != 200:
            raise PriceFetchError(symbol, resp.status_code, "refused")
        try:
            body = json.loads(resp.text, parse_float=Decimal)
        except ValueError:
            raise PriceFetchError(symbol, resp.status_code, "not JSON") from None
        if not isinstance(body, list):
            raise PriceFetchError(symbol, resp.status_code, "unexpected response")
        out = Closes()
        for row in body:
            if not isinstance(row, dict):
                continue
            raw_date, raw_close = row.get("date"), row.get("close")
            if not isinstance(raw_date, str):
                continue
            if isinstance(raw_close, bool) or not isinstance(raw_close, Decimal | int):
                continue
            try:
                day = date.fromisoformat(raw_date[:10])
            except ValueError:
                continue
            if not start <= day <= end:
                continue
            close = self._close(raw_close)
            if close is None:
                out.dropped += 1  # out of range: this row only
                continue
            if close <= 0:
                continue
            out.append((day, close))
        return out

    @staticmethod
    def _close(raw: Decimal | int) -> Decimal | None:
        """A close as Purser stores it (NUMERIC(20,6)), or None when it does
        not fit one. Quantized first, in a local decimal context, and checked
        after: a value just under the limit can round up to it."""
        try:
            with localcontext() as ctx:
                ctx.prec = 60
                value = Decimal(raw)
                if not value.is_finite():
                    return None
                value = value.quantize(core.PRICE_QUANT)
        except (InvalidOperation, ArithmeticError):
            return None
        return value if abs(value) < core.MAX_PRICE else None
