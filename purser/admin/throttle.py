"""Password sign-in throttling for ``POST /auth/login`` (issue #212, decision 59).

Failed sign-ins are counted per **email** (casefolded) and per **client
address**. Once either count reaches its limit, attempts are refused
(``LoginThrottled`` -> 429 + ``Retry-After``) BEFORE any password check, for
a backoff that starts at ``login_backoff_seconds`` and doubles with each
further failure up to ``login_backoff_max_seconds``. A successful sign-in
clears the email's count but never the address's, so one valid credential
cannot launder a spray across other accounts. A count is forgotten once it has
been quiet — no failure and no lockout in force — for
``login_failure_reset_seconds``.

Attempts are charged on ADMISSION and refunded when the password verifies.
Argon2 verification is deliberately slow, so a check-then-record scheme would
let a burst of concurrent requests all pass the check before the first
failure landed — and re-admit a burst at every lockout expiry.

State is in memory, per process: a restart clears it and each API replica
keeps its own. No DB table on purpose — see DESIGN decision 59.
"""

import ipaddress
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from purser.config import Settings

# Keys tracked at once. Past this, quiet keys go first, then the least
# recently failed unblocked ones — memory stays bounded under a spray of
# random emails or addresses.
MAX_TRACKED_KEYS = 10_000

# 2**30 seconds is far past any sane ceiling, so the cap always wins without
# computing ever-larger integers for a key that keeps failing.
_MAX_DOUBLINGS = 30

EMAIL = "email"
IP = "ip"

Key = tuple[str, str]  # (EMAIL | IP, normalized value)


class LoginThrottled(Exception):
    """The attempt was refused without a password check."""

    def __init__(self, retry_after: int, limited_by: list[str]) -> None:
        super().__init__(f"sign-in throttled; retry after {retry_after}s")
        self.retry_after = retry_after
        self.limited_by = limited_by  # which dimensions are locked: email, ip


@dataclass
class _Count:
    failures: int = 0
    last_failure: float = 0.0
    blocked_until: float = 0.0


@dataclass(frozen=True)
class Attempt:
    """An admitted attempt, already charged as a failure on each key.

    Each charge keeps the key's pre-charge ``blocked_until`` so a refund can
    restore it exactly."""

    charges: tuple[tuple[Key, float], ...]


def normalize_email(email: str) -> str:
    return email.strip().casefold()


def client_key(host: str) -> str:
    """The per-address key for a client host.

    IPv6 clients are grouped by /64 — a subscriber typically holds a whole
    /64 and could otherwise rotate addresses to dodge the limit; an
    IPv4-mapped IPv6 address collapses to its IPv4 form. Non-IP hosts (test
    clients, unix sockets) are used verbatim."""
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return host
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            return str(addr.ipv4_mapped)
        return str(ipaddress.IPv6Network((addr, 64), strict=False))
    return str(addr)


class LoginThrottle:
    """Thread-safe failure tracker; one per app (``app.state.login_throttle``).

    ``clock`` is injectable (monotonic seconds) so tests never sleep."""

    def __init__(
        self,
        *,
        max_failures: int = 5,
        ip_max_failures: int = 20,
        backoff_seconds: int = 30,
        backoff_max_seconds: int = 900,
        reset_seconds: int = 3600,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = MAX_TRACKED_KEYS,
    ) -> None:
        # A limit <= 0 switches that dimension off entirely (no counting).
        self._limits = {EMAIL: max_failures, IP: ip_max_failures}
        self._base = max(1, backoff_seconds)
        self._cap = max(self._base, backoff_max_seconds)
        self._reset = max(0, reset_seconds)
        self._clock = clock
        self._max_keys = max(1, max_keys)
        self._lock = threading.Lock()
        self._counts: dict[Key, _Count] = {}

    @classmethod
    def from_settings(cls, settings: Settings) -> "LoginThrottle":
        return cls(
            max_failures=settings.login_max_failures,
            ip_max_failures=settings.login_ip_max_failures,
            backoff_seconds=settings.login_backoff_seconds,
            backoff_max_seconds=settings.login_backoff_max_seconds,
            reset_seconds=settings.login_failure_reset_seconds,
        )

    def __len__(self) -> int:
        with self._lock:
            return len(self._counts)

    def begin(self, email: str, client: str | None) -> Attempt:
        """Admit an attempt, or raise ``LoginThrottled`` with the seconds to wait.

        An admitted attempt is already charged as a failure: a wrong password
        needs no further call, and ``succeeded()`` refunds the charge. A
        refused attempt changes nothing — retrying while locked out neither
        extends the lockout nor counts as a failure."""
        now = self._clock()
        keys = self._keys(email, client)
        with self._lock:
            wait = 0.0
            limited_by: list[str] = []
            for key in keys:
                count = self._counts.get(key)
                if count is not None and count.blocked_until > now:
                    wait = max(wait, count.blocked_until - now)
                    limited_by.append(key[0])
            if limited_by:
                raise LoginThrottled(max(1, math.ceil(wait)), limited_by)

            charges: list[tuple[Key, float]] = []
            for key in keys:
                count = self._counts.get(key)
                if count is None:
                    self._make_room(now)
                    count = self._counts[key] = _Count()
                elif self._quiet(count, now):
                    count.failures = 0
                charges.append((key, count.blocked_until))
                count.failures += 1
                count.last_failure = now
                limit = self._limits[key[0]]
                if count.failures >= limit:
                    doublings = min(count.failures - limit, _MAX_DOUBLINGS)
                    count.blocked_until = now + min(self._base * 2**doublings, self._cap)
            return Attempt(charges=tuple(charges))

    def succeeded(self, attempt: Attempt) -> None:
        """The password verified: clear the email's count and refund this
        attempt's charge on the address (whose count otherwise stands)."""
        with self._lock:
            for key, blocked_before in attempt.charges:
                if key[0] == EMAIL:
                    self._counts.pop(key, None)
                    continue
                count = self._counts.get(key)
                if count is None:  # evicted meanwhile; nothing left to refund
                    continue
                count.failures = max(0, count.failures - 1)
                # Below the limit no lockout can stand. At or over it, this
                # attempt was the only one in flight on the key (its charge
                # locked every later one out), so restoring the pre-charge
                # value undoes exactly the lockout the charge set.
                if count.failures < self._limits[key[0]]:
                    count.blocked_until = 0.0
                else:
                    count.blocked_until = blocked_before

    def _keys(self, email: str, client: str | None) -> list[Key]:
        keys: list[Key] = []
        if self._limits[EMAIL] > 0:
            keys.append((EMAIL, normalize_email(email)))
        if self._limits[IP] > 0 and client:
            keys.append((IP, client_key(client)))
        return keys

    def _quiet(self, count: _Count, now: float) -> bool:
        """No failure and no lockout in force for the reset period. Measured
        from the END of a lockout, so a capped lockout is never forgotten the
        moment it expires."""
        return now - max(count.last_failure, count.blocked_until) > self._reset

    def _make_room(self, now: float) -> None:
        """Keep the table under ``max_keys`` before a new key goes in: drop
        quiet counts, then the least recently failed unblocked ones. Frees a
        tenth of the table at once so a flood amortizes the sort."""
        if len(self._counts) < self._max_keys:
            return
        for key in [k for k, c in self._counts.items() if self._quiet(c, now)]:
            del self._counts[key]
        excess = len(self._counts) - self._max_keys * 9 // 10
        if excess <= 0:
            return
        victims = sorted(
            self._counts,
            key=lambda k: (self._counts[k].blocked_until > now, self._counts[k].last_failure),
        )
        for key in victims[:excess]:
            del self._counts[key]
