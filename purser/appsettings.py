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
# Reviewer guidance is narrative text with no 1:1 Settings attribute, so it is
# a standalone key like the sync schedule — and encrypted, because it is prose
# the user wrote (issue #175).
REVIEW_GUIDANCE_KEY = "review_guidance"
# The price provider's API key (#390, decision 89) is a standalone secret key,
# not an overlay: there is deliberately no PURSER_* env default for it. Purser
# is the party that sends it, so it lives only here, Fernet-encrypted and
# write-only like the other secrets.
PRICE_API_KEY = "price_api_key"
# Keys that overlay Settings attributes 1:1 (names must match config.Settings).
OVERLAY_KEYS = (
    "transfer_auto_link",
    "privacy_purchase",
    "privacy_amount",
    "ai_provider",
    "ai_model",
    "ai_base_url",
    "ai_api_key",
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
    "ai_review",
    "review_max_age_days",
    "price_provider",
    "price_stale_days",
)
ALL_KEYS = (SYNC_SCHEDULE_KEY, REVIEW_GUIDANCE_KEY, PRICE_API_KEY, *OVERLAY_KEYS)
SECRET_KEYS = frozenset({"ai_api_key", "oidc_client_secret", PRICE_API_KEY})
# Encrypted at rest but NOT write-only: unlike a secret, the user needs to read
# their own guidance back to edit it.
ENCRYPTED_KEYS = SECRET_KEYS | {REVIEW_GUIDANCE_KEY}
CACHE_TTL_SECONDS = 30.0

_MASK_SET = "set"
_MASK_UNSET = "unset"

_lock = threading.Lock()
_cache: tuple[float, dict[str, Any]] | None = None


class SyncSchedule(BaseModel):
    """User-facing SimpleFIN sync schedule (UTC).

    Presets rather than raw cron. ``hour``/``minute`` apply to ``daily`` only;
    the interval presets are anchored so every choice lands a fresh sync just
    before the 06:45 UTC matching job, and ``hourly`` (24 runs/day) sits
    exactly at the SimpleFIN Bridge budget — the per-connection fetch guard
    fails soft beyond it.
    """

    model_config = ConfigDict(extra="forbid")

    preset: Literal["daily", "every_12h", "every_6h", "hourly"] = "daily"
    hour: int | None = Field(None, ge=0, le=23)
    minute: int | None = Field(None, ge=0, le=59)

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


def review_guidance() -> list[str]:
    """The reviewer checklist: the user's override, else the shipped default.

    Narrative text, so it round-trips through the encrypted branch of
    set_values — but it is not a SECRET, and _masked leaves it readable so the
    user can edit what they wrote.
    """
    from purser.review import DEFAULT_GUIDANCE

    raw = overrides().get(REVIEW_GUIDANCE_KEY)
    if not raw:
        return list(DEFAULT_GUIDANCE)
    if isinstance(raw, str):
        return [line for line in raw.splitlines() if line.strip()]
    return [str(line) for line in raw]


def price_api_key() -> str | None:
    """The price provider's API key, or None when it is not set. Read for the
    provider call only; it never leaves the server and is never logged."""
    raw = overrides().get(PRICE_API_KEY)
    return raw if isinstance(raw, str) and raw.strip() else None


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
        if key in ENCRYPTED_KEYS:
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
