"""Persisted, admin-editable runtime configuration (issue #102).

Environment variables (``purser/config.py``) are the bootstrap defaults;
rows in ``app_settings`` override them. ``effective()`` returns a plain
``Settings`` object with the overrides applied, so consuming call sites
swap ``get_settings()`` for ``appsettings.effective()`` and nothing else.

The registry below is deliberately curated: infrastructure settings
(database URL, encryption key, bind host/port, public URL, log level)
are env-only and never editable at runtime. Secret values are Fernet-
encrypted at rest and write-only through the API.

Overrides are cached per-process for CACHE_TTL_SECONDS (session auth reads
them on every request). Writers must commit and then call ``invalidate()``;
the /config API layer does both.
"""

import json
import threading
import time
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import JSON, select
from sqlalchemy.orm import Session

from purser import audit, crypto
from purser.config import Settings, get_settings
from purser.models import AppSetting

SYNC_SCHEDULE_KEY = "sync_schedule"
# The price provider's API key (#390, decision 89) is a standalone secret key,
# not an overlay: there is deliberately no PURSER_* env default for it. Purser
# is the party that sends it, so it lives only here, Fernet-encrypted and
# write-only like the other secrets.
PRICE_API_KEY = "price_api_key"
# The fallback provider's key (#686, decision 139), stored the same way.
PRICE_FALLBACK_API_KEY = "price_fallback_api_key"
# Which provider each stored key was entered for (#698). The two slots are
# roles (the provider, the fallback); a key is sent only to the provider it
# was entered for, so changing or swapping providers (Settings, PUT/DELETE
# /config, or PURSER_PRICE_PROVIDER) never hands one provider another's key.
# A key stored before the binding existed was Tiingo's, the only provider
# until #686; an unbound fallback key goes nowhere until it is entered again.
PRICE_KEY_BINDINGS = {
    PRICE_API_KEY: "price_api_key_provider",
    PRICE_FALLBACK_API_KEY: "price_fallback_api_key_provider",
}
_UNBOUND_PRICE_KEY_PROVIDER = {PRICE_API_KEY: "tiingo", PRICE_FALLBACK_API_KEY: None}
# Keys that overlay Settings attributes 1:1 (names must match config.Settings).
OVERLAY_KEYS = (
    "transfer_auto_link",
    "ai_provider",
    "ai_model",
    "ai_base_url",
    "ai_api_key",
    # Provider answer timeout (#640, decision 133).
    "ai_timeout_seconds",
    "grafana_url",
    "session_ttl_days",
    "session_idle_hours",
    "audit_retention_days",
    # OIDC is runtime-editable (issue #105): OidcClient is built per request
    # and password login is independent, so a bad value can't lock anyone out.
    # public_url (the redirect-URI base) stays env-only.
    "oidc_issuer",
    "oidc_client_id",
    "oidc_client_secret",
    "oidc_auto_provision",
    # The AI confidence floor and the "no category" review reason (#694,
    # decision 141).
    "ai_min_confidence",
    "review_uncategorized",
    "price_provider",
    "price_fallback_provider",
    "price_stale_days",
    "feed_stale_days",
    # The household time zone (#471, decision 105): the ledger day follows it.
    "timezone",
)
ALL_KEYS = (
    SYNC_SCHEDULE_KEY,
    PRICE_API_KEY,
    PRICE_FALLBACK_API_KEY,
    *PRICE_KEY_BINDINGS.values(),
    *OVERLAY_KEYS,
)
SECRET_KEYS = frozenset({"ai_api_key", "oidc_client_secret", PRICE_API_KEY, PRICE_FALLBACK_API_KEY})
CACHE_TTL_SECONDS = 30.0

_MASK_SET = "set"
_MASK_UNSET = "unset"

_lock = threading.Lock()
_cache: tuple[float, dict[str, Any]] | None = None


class SyncSchedule(BaseModel):
    """User-facing SimpleFIN sync schedule.

    Presets rather than raw cron. ``hour``/``minute`` apply to ``daily`` only
    and are a wall-clock time in the household time zone (``timezone``,
    decision 111): the scheduler reads the daily trigger in that zone, so the
    sync keeps its time on the clock across daylight-saving changes. The
    interval presets stay anchored in UTC so every one lands a fresh sync just
    before the 06:45 UTC matching job, and ``hourly`` (24 runs/day) sits
    exactly at the SimpleFIN Bridge budget — the per-connection fetch guard
    fails soft beyond it.
    """

    model_config = ConfigDict(extra="forbid")

    preset: Literal["daily", "every_12h", "every_6h", "hourly"] = "daily"
    hour: int | None = Field(
        None, ge=0, le=23, description="Daily only: the hour in the household time zone."
    )
    minute: int | None = Field(
        None, ge=0, le=59, description="Daily only: the minute in the household time zone."
    )

    @model_validator(mode="after")
    def _shape(self) -> "SyncSchedule":
        if self.preset == "daily":
            if self.hour is None:
                self.hour = 6
            if self.minute is None:
                self.minute = 30
        elif self.hour is not None or self.minute is not None:
            raise ValueError("hour/minute apply only to the daily preset")
        return self

    @property
    def follows_household_zone(self) -> bool:
        """True when the trigger reads in the household time zone (daily);
        the interval presets read in UTC."""
        return self.preset == "daily"

    def to_cron(self) -> dict[str, int | str]:
        if self.preset == "daily":
            assert self.hour is not None and self.minute is not None  # validator
            return {"hour": self.hour, "minute": self.minute}
        if self.preset == "every_12h":
            return {"hour": "6-23/12", "minute": 30}  # 06:30, 18:30 UTC
        if self.preset == "every_6h":
            return {"hour": "0-23/6", "minute": 30}  # 00/06/12/18:30 UTC
        return {"minute": 30}  # hourly, at :30


DEFAULT_SYNC_SCHEDULE = SyncSchedule()


def invalidate() -> None:
    """Drop the cached overrides (call after committing a write)."""
    global _cache
    with _lock:
        _cache = None


def _decode(row: AppSetting) -> Any:
    if row.value_enc is not None:
        return json.loads(crypto.decrypt(row.value_enc))
    return row.value


def overrides() -> dict[str, Any]:
    """Decoded app_settings rows, cached for CACHE_TTL_SECONDS."""
    global _cache
    with _lock:
        if _cache is not None and time.monotonic() - _cache[0] < CACHE_TTL_SECONDS:
            return _cache[1]
    from purser.db import get_sessionmaker

    with get_sessionmaker()() as session:
        rows = session.execute(select(AppSetting)).scalars().all()
        decoded = {row.key: _decode(row) for row in rows if row.key in ALL_KEYS}
    with _lock:
        _cache = (time.monotonic(), decoded)
    return decoded


def effective() -> Settings:
    """Env-based Settings with any persisted overrides applied."""
    overlay = {k: v for k, v in overrides().items() if k in OVERLAY_KEYS}
    if not overlay:
        return get_settings()
    return get_settings().model_copy(update=overlay)


def price_key_for(provider: str) -> str | None:
    """The stored API key entered for price provider ``provider``, from
    either slot (#686, #698), or None. Read for that provider's call only;
    it never leaves the server and is never logged."""
    values = overrides()
    for slot, binding in PRICE_KEY_BINDINGS.items():
        raw = values.get(slot)
        if not (isinstance(raw, str) and raw.strip()):
            continue
        if values.get(binding, _UNBOUND_PRICE_KEY_PROVIDER[slot]) == provider:
            return raw
    return None


def sync_schedule() -> SyncSchedule:
    raw = overrides().get(SYNC_SCHEDULE_KEY)
    if raw is None:
        return DEFAULT_SYNC_SCHEDULE
    return SyncSchedule.model_validate(raw)


def _masked(key: str, value: Any) -> Any:
    if key in SECRET_KEYS:
        return _MASK_SET if value is not None else _MASK_UNSET
    return value


def set_values(session: Session, changes: dict[str, Any], *, actor_token_id: UUID | None) -> None:
    """Upsert override rows and write one audit entry for the whole change.

    Values must already be validated (the /config API models do). The caller
    commits, then calls ``invalidate()``.
    """
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    for key, value in changes.items():
        if key not in ALL_KEYS:
            raise ValueError(f"unknown config key: {key}")
        row = session.get(AppSetting, key)
        before[key] = _masked(key, _decode(row)) if row is not None else None
        after[key] = _masked(key, value)
        if key in SECRET_KEYS:
            value_col, enc_col = None, crypto.encrypt(json.dumps(value))
        else:
            # JSON.NULL: an explicit null override must land as JSON null,
            # not SQL NULL (which the CHECK reserves for secret rows).
            value_col, enc_col = (JSON.NULL if value is None else value), None
        if row is None:
            session.add(
                AppSetting(key=key, value=value_col, value_enc=enc_col, updated_by=actor_token_id)
            )
        else:
            row.value = value_col
            row.value_enc = enc_col
            row.updated_at = datetime.now(UTC)
            row.updated_by = actor_token_id
    audit.record(
        session,
        actor_token_id=actor_token_id,
        action="config.update",
        entity_type="config",
        before=before,
        after=after,
    )


def reset_value(session: Session, key: str, *, actor_token_id: UUID | None) -> bool:
    """Remove one override (the env default applies again). False = no row.

    The caller commits, then calls ``invalidate()``.
    """
    if key not in ALL_KEYS:
        raise ValueError(f"unknown config key: {key}")
    row = session.get(AppSetting, key)
    if row is None:
        return False
    audit.record(
        session,
        actor_token_id=actor_token_id,
        action="config.reset",
        entity_type="config",
        before={key: _masked(key, _decode(row))},
        after={key: None},
    )
    session.delete(row)
    return True
