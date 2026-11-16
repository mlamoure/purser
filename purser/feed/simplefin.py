"""SimpleFIN protocol client — no SDK, just the protocol (DESIGN §7).

Claim flow: a setup token is a base64-encoded claim URL; POSTing to the
claim URL (once) returns the access URL, which embeds credentials and is
stored encrypted. Access URLs and setup tokens are secrets: never log them.
"""

import base64
import binascii
from contextlib import nullcontext
from datetime import UTC, date, datetime
from typing import Any

import httpx

from purser.feed import envelope

BRIDGE_MAX_RANGE_DAYS = 90  # Bridge caps ranges at 90 days
# The Bridge's recommended span for one /accounts request (#490): longer ones
# draw a "Requested date range exceeds recommended range of 45 days. In the
# future, this may be capped." warning. Every multi-day fetch pages at it.
BRIDGE_PAGE_DAYS = 45
BRIDGE_MAX_FETCHES_PER_DAY = 24  # Bridge allows <=24 requests/day


class SimpleFinError(Exception):
    pass


def decode_setup_token(setup_token: str) -> str:
    """A setup token is base64(claim URL)."""
    try:
        claim_url = base64.b64decode(setup_token.strip(), validate=True).decode()
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise SimpleFinError("setup token is not valid base64") from exc
    if not claim_url.startswith("https://"):
        raise SimpleFinError("claim URL must be https")
    return claim_url


def claim_access_url(setup_token: str, *, client: httpx.Client | None = None) -> str:
    """Exchange the one-time setup token for the access URL."""
    claim_url = decode_setup_token(setup_token)
    try:
        with nullcontext(client) if client is not None else httpx.Client(timeout=30) as http:
            resp = http.post(claim_url)
    except httpx.HTTPError as exc:
        # Sanitized on purpose: httpx exception text can embed the claim URL.
        raise SimpleFinError(f"claim request failed: {type(exc).__name__}") from exc
    if resp.status_code != 200:
        raise SimpleFinError(f"claim failed with HTTP {resp.status_code}")
    access_url = resp.text.strip()
    if not access_url.startswith("https://"):
        raise SimpleFinError("claim response did not contain an https access URL")
    return access_url


def _utc_midnight(day: date) -> int:
    """A window bound as epoch seconds at UTC midnight (#471). Imported rows
    are dated by their epoch's UTC date, so the window is drawn in the same
    frame; a naive ``datetime(...).timestamp()`` would follow the host's local
    zone instead."""
    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp())


def fetch_accounts(
    access_url: str,
    *,
    start: date | None = None,
    end: date | None = None,
    include_pending: bool = True,
    accounts: list[str] | None = None,
    version: int = envelope.PROTOCOL_VERSION,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """GET /accounts with the (credentialed) access URL. Returns the payload
    verbatim — the caller archives it before anything interprets it.

    ``version`` asks for that protocol version (#479). A server that does not
    know it chooses its own default, so the payload may still be v1: callers
    read it through ``purser.feed.envelope``, which understands both.

    ``accounts`` narrows the fetch via the protocol's repeatable ``account``
    param (external ids). Advisory: a bridge may ignore it and return every
    account — callers must not rely on the payload being filtered."""
    params: list[tuple[str, str]] = [("version", str(version))]
    if start is not None:
        params.append(("start-date", str(_utc_midnight(start))))
    if end is not None:
        params.append(("end-date", str(_utc_midnight(end))))
    if include_pending:
        params.append(("pending", "1"))
    for external_id in accounts or ():
        params.append(("account", external_id))
    try:
        with nullcontext(client) if client is not None else httpx.Client(timeout=60) as http:
            # tuple(): httpx's params typing is invariant over list items.
            resp = http.get(f"{access_url.rstrip('/')}/accounts", params=tuple(params))
    except httpx.HTTPError as exc:
        # Sanitized on purpose: httpx exception text can embed the credentialed
        # access URL — report the exception type only, never str(exc).
        raise SimpleFinError(f"accounts fetch failed: {type(exc).__name__}") from exc
    if resp.status_code != 200:
        raise SimpleFinError(f"accounts fetch failed with HTTP {resp.status_code}")
    try:
        payload: dict[str, Any] = resp.json()
    except ValueError as exc:
        raise SimpleFinError("accounts response was not valid JSON") from exc
    return payload
