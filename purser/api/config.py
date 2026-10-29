"""Admin runtime configuration API (issue #102).

GET returns effective values (env defaults + persisted overrides); PUT is a
partial update that persists overrides, audits, and live-reschedules the
sync job when possible; DELETE removes one override so the env default
applies again. Admin scope everywhere. The AI provider API key and the price
provider API key are write-only: they never appear in a response — only their
presence does.
"""

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from purser import appsettings, scheduler
from purser.admin.auth import AdminToken
from purser.appsettings import SyncSchedule
from purser.db import get_session

DbSession = Annotated[Session, Depends(get_session)]

router = APIRouter(prefix="/config", tags=["config"])

# Keys where an explicit null is a meaningful override ("no value"). For the
# OIDC pair, a null override disables SSO even when env configures it.
NULLABLE_KEYS = frozenset({"ai_model", "grafana_url", "oidc_issuer", "oidc_client_id"})


class ConfigValues(BaseModel):
    # ai_api_key / oidc_client_secret are deliberately absent (write-only secrets).
    sync_schedule: SyncSchedule
    transfer_auto_link: bool
    privacy_purchase: bool
    privacy_amount: bool
    ai_provider: str
    ai_model: str | None
    ai_base_url: str
    grafana_url: str | None
    session_ttl_days: int
    session_idle_hours: int
    audit_retention_days: int
    oidc_issuer: str | None
    oidc_client_id: str | None
    oidc_enabled: bool  # derived: issuer + client id + secret all present
    oidc_auto_provision: bool
    # Manually tracked holdings (#390, decision 89); the provider's API key is
    # write-only (price_api_key_set below).
    price_provider: str
    price_stale_days: int


class EncryptionStatus(BaseModel):
    """Read-only posture block (issue #123). Field-level Fernet encryption is
    mandatory (startup fails closed without a working key), so this mostly
    attests rather than warns. Since #137 the narrative layer (payee names,
    descriptions, memos, schedule names, audit/event payloads) is encrypted
    too; only the numeric skeleton (amounts, dates, statuses, ltree paths, ids)
    and the readmodel v2 views over it stay plaintext by design (decision 36).
    Storage-level encryption remains a deployment concern the app can't attest."""

    field_encryption_active: bool
    canary_ok: bool  # the stored sentinel decrypts under the configured key
    key_fingerprint: str
    key_acknowledged_at: datetime | None  # wizard's record-your-key step
    encrypted_scopes: list[str]


class ConfigOut(BaseModel):
    values: ConfigValues
    overridden: list[str]  # keys whose value comes from app_settings
    ai_api_key_set: bool
    oidc_client_secret_set: bool
    price_api_key_set: bool
    sync_reschedule_live: bool  # False -> schedule changes apply at worker restart
    encryption: EncryptionStatus


class ConfigUpdate(BaseModel):
    """Partial update: absent fields are untouched; null is accepted only
    for NULLABLE_KEYS (reset a key to its env default with DELETE)."""

    model_config = ConfigDict(extra="forbid")

    sync_schedule: SyncSchedule | None = None
    transfer_auto_link: bool | None = None
    privacy_purchase: bool | None = None
    privacy_amount: bool | None = None
    ai_provider: Literal["none", "openai", "anthropic"] | None = None
    ai_model: str | None = None
    ai_base_url: str | None = None
    ai_api_key: str | None = None
    grafana_url: str | None = None
    session_ttl_days: int | None = Field(None, ge=1, le=365)
    session_idle_hours: int | None = Field(None, ge=1, le=720)
    audit_retention_days: int | None = Field(None, ge=7, le=3650)
    oidc_issuer: str | None = None
    oidc_client_id: str | None = None
    oidc_client_secret: str | None = None
    oidc_auto_provision: bool | None = None
    price_provider: Literal["none", "tiingo"] | None = None
    # Write-only: stored Fernet-encrypted, never returned (only its presence).
    price_api_key: str | None = None
    price_stale_days: int | None = Field(None, ge=1, le=365)

    @field_validator("ai_base_url", "grafana_url", "oidc_issuer")
    @classmethod
    def _http_url(cls, v: str | None) -> str | None:
        if v is not None and not v.startswith(("http://", "https://")):
            raise ValueError("must be an http(s) URL")
        return v

    @field_validator(
        "ai_api_key", "ai_model", "oidc_client_id", "oidc_client_secret", "price_api_key"
    )
    @classmethod
    def _non_blank(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("must not be blank")
        return v


ENCRYPTED_SCOPES = [
    "raw bank envelopes (raw_fetches.payload_enc)",
    "SimpleFIN access URLs",
    "webhook secrets",
    "write-only config secrets",
    "payee names",
    "transaction descriptions & posting memos",
    "schedule names",
    "audit log payloads (before/after & call detail)",
    "event payloads",
    "transaction content hashes are keyed HMAC (dictionary-attack hardened)",
]


def _encryption_status(session: Session) -> EncryptionStatus:
    from purser import crypto
    from purser.models import EncryptionCanary

    canary = session.execute(select(EncryptionCanary)).scalar_one_or_none()
    canary_ok = False
    acknowledged_at = None
    if canary is not None:
        acknowledged_at = canary.key_acknowledged_at
        try:
            crypto.decrypt(canary.sentinel_enc)
            canary_ok = True
        except Exception:
            canary_ok = False
    return EncryptionStatus(
        field_encryption_active=canary_ok,
        canary_ok=canary_ok,
        key_fingerprint=crypto.key_fingerprint(),
        key_acknowledged_at=acknowledged_at,
        encrypted_scopes=ENCRYPTED_SCOPES,
    )


def _config_out(session: Session) -> ConfigOut:
    eff = appsettings.effective()
    persisted = appsettings.overrides()
    return ConfigOut(
        values=ConfigValues(
            sync_schedule=appsettings.sync_schedule(),
            transfer_auto_link=eff.transfer_auto_link,
            privacy_purchase=eff.privacy_purchase,
            privacy_amount=eff.privacy_amount,
            ai_provider=eff.ai_provider,
            ai_model=eff.ai_model,
            ai_base_url=eff.ai_base_url,
            grafana_url=eff.grafana_url,
            session_ttl_days=eff.session_ttl_days,
            session_idle_hours=eff.session_idle_hours,
            audit_retention_days=eff.audit_retention_days,
            oidc_issuer=eff.oidc_issuer,
            oidc_client_id=eff.oidc_client_id,
            oidc_enabled=eff.oidc_enabled,
            oidc_auto_provision=eff.oidc_auto_provision,
            price_provider=eff.price_provider,
            price_stale_days=eff.price_stale_days,
        ),
        overridden=sorted(persisted),
        ai_api_key_set=bool(eff.ai_api_key),
        oidc_client_secret_set=bool(eff.oidc_client_secret),
        price_api_key_set=appsettings.price_api_key() is not None,
        sync_reschedule_live=scheduler.is_live(),
        encryption=_encryption_status(session),
    )


@router.get("")
def get_config(_: AdminToken, session: DbSession) -> ConfigOut:
    return _config_out(session)


def _check_price_api_key(key: str | None) -> None:
    """A provider key travels in an HTTP header, which takes printable ASCII
    only: a key with a space or another character would fail every fetch.
    Checked here rather than by a field validator, whose 422 would echo the
    key back in the response."""
    if key is not None and not all("!" <= ch <= "~" for ch in key):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "price_api_key must be printable ASCII without spaces (check for a pasted "
            "space, line break or lookalike character)",
        )


@router.put("")
def update_config(body: ConfigUpdate, admin: AdminToken, session: DbSession) -> ConfigOut:
    if "price_api_key" in body.model_fields_set:
        _check_price_api_key(body.price_api_key)
    changes: dict[str, Any] = {}
    for key in body.model_fields_set:
        value = getattr(body, key)
        if value is None and key not in NULLABLE_KEYS:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"{key} cannot be null; DELETE /config/{key} resets it to the env default",
            )
        changes[key] = value.model_dump() if isinstance(value, SyncSchedule) else value
    if changes:
        appsettings.set_values(session, changes, actor_token_id=admin.id)
        session.commit()
        appsettings.invalidate()
        if appsettings.SYNC_SCHEDULE_KEY in changes:
            scheduler.reschedule_sync()
    return _config_out(session)


@router.delete("/{key}")
def reset_config(key: str, admin: AdminToken, session: DbSession) -> ConfigOut:
    if key not in appsettings.ALL_KEYS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown config key: {key}")
    appsettings.reset_value(session, key, actor_token_id=admin.id)
    session.commit()
    appsettings.invalidate()
    if key == appsettings.SYNC_SCHEDULE_KEY:
        scheduler.reschedule_sync()
    return _config_out(session)
