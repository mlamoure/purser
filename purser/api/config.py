"""Admin runtime configuration API (issue #102).

GET returns effective values (env defaults + persisted overrides); PUT is a
partial update that persists overrides, audits, and live-reschedules the
sync job when possible; DELETE removes one override so the env default
applies again. Admin scope everywhere. The AI provider API key and the price
provider API key are write-only: they never appear in a response — only their
presence does. ``POST /config/ai/models`` and ``POST /config/ai/test`` check
an AI provider setup before or after it is saved (#640, decision 133).
"""

import dataclasses
from datetime import date as date_type
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from purser import ai_providers, appsettings, clock, network, scheduler
from purser.admin.auth import AdminToken, ReadToken
from purser.appsettings import SyncSchedule
from purser.config import Settings, get_settings
from purser.db import get_session
from purser.logging import get_logger

DbSession = Annotated[Session, Depends(get_session)]
log = get_logger(__name__)

router = APIRouter(prefix="/config", tags=["config"])
clock_router = APIRouter(tags=["meta"])

# Keys where an explicit null is a meaningful override ("no value"). For the
# OIDC pair, a null override disables SSO even when env configures it; a null
# AI key is "send no key", even when PURSER_AI_API_KEY sets one (#640: the
# card's Clear key, for a keyless local server).
NULLABLE_KEYS = frozenset(
    {"ai_model", "ai_api_key", "grafana_url", "oidc_issuer", "oidc_client_id"}
)
AiProvider = Literal["none", "openai", "local", "anthropic"]
# Per provider answer: long enough for a model on modest hardware, short
# enough that one stuck call cannot hold a sync's categorize pass for an hour.
AI_TIMEOUT_MIN_SECONDS = 5
AI_TIMEOUT_MAX_SECONDS = 600


class ConfigValues(BaseModel):
    # ai_api_key / oidc_client_secret are deliberately absent (write-only secrets).
    sync_schedule: SyncSchedule
    transfer_auto_link: bool
    ai_provider: str
    ai_model: str | None
    ai_base_url: str
    ai_timeout_seconds: int
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
    # Feed freshness (#480): days a bank balance may age before feed.stale.
    feed_stale_days: int
    # The household time zone (#471, decision 105): an IANA name whose
    # calendar date is the ledger day. Job times stay UTC.
    timezone: str


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


class AiDisclosureOut(BaseModel):
    """What one AI capability sends to the provider, in plain words
    (``ai_providers.DISCLOSURES``, pinned to the prompt builders by a test)."""

    capability: Literal["categorize", "review"]
    when: str = Field(description="The situation, e.g. 'To categorize a transaction ...'.")
    sends: list[str] = Field(description="Each item sent, as a phrase.")


class ConfigOut(BaseModel):
    values: ConfigValues
    overridden: list[str]  # keys whose value comes from app_settings
    ai_api_key_set: bool
    ai_disclosure: list[AiDisclosureOut]
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
    ai_provider: AiProvider | None = Field(
        None,
        description="none (off) · local: a model you run behind an OpenAI-compatible "
        "API (needs ai_base_url; the key is optional) · openai: a hosted "
        "OpenAI-compatible service · anthropic.",
    )
    ai_model: str | None = None
    ai_base_url: str | None = None
    ai_api_key: str | None = Field(
        None, description="Write-only. null stores 'no key' (DELETE returns to the env key)."
    )
    ai_timeout_seconds: int | None = Field(
        None,
        ge=AI_TIMEOUT_MIN_SECONDS,
        le=AI_TIMEOUT_MAX_SECONDS,
        description="Seconds to wait for one provider answer.",
    )
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
    feed_stale_days: int | None = Field(None, ge=1, le=90)
    timezone: str | None = Field(
        None, description="IANA time zone name (e.g. America/New_York); the ledger day follows it"
    )

    @field_validator("timezone")
    @classmethod
    def _iana_timezone(cls, v: str | None) -> str | None:
        if v is not None and not clock.is_valid_timezone(v):
            raise ValueError("must be an IANA time zone name, e.g. America/New_York or UTC")
        return v

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
            ai_provider=eff.ai_provider,
            ai_model=eff.ai_model,
            ai_base_url=eff.ai_base_url,
            ai_timeout_seconds=eff.ai_timeout_seconds,
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
            feed_stale_days=eff.feed_stale_days,
            timezone=eff.timezone,
        ),
        overridden=sorted(persisted),
        ai_api_key_set=bool(eff.ai_api_key),
        ai_disclosure=[
            AiDisclosureOut(
                capability=d.capability, when=d.when, sends=[item.text for item in d.sends]
            )
            for d in ai_providers.DISCLOSURES
        ],
        oidc_client_secret_set=bool(eff.oidc_client_secret),
        price_api_key_set=appsettings.price_api_key() is not None,
        sync_reschedule_live=scheduler.is_live(),
        encryption=_encryption_status(session),
    )


@router.get("")
def get_config(_: AdminToken, session: DbSession) -> ConfigOut:
    return _config_out(session)


class NetworkOut(BaseModel):
    """How this Purser is reached (#632, decision 117). Instance identity,
    read from the environment at startup: nothing here is editable at
    runtime, and switching is done on the host (the setup guide)."""

    reach: network.Reach = Field(
        description=(
            "this_machine: the port is published on loopback only. home_network: on "
            "another address (PURSER_BIND_ADDRESS=0.0.0.0), over plain HTTP. "
            "reverse_proxy: PURSER_PUBLIC_URL is https, so something in front of Purser "
            "terminates TLS. unknown: the deployment does not report how it publishes "
            "the port (Compose passes PURSER_BIND_ADDRESS); not guessed."
        )
    )
    public_url: str = Field(description="PURSER_PUBLIC_URL: the address Purser gives out.")
    mcp_url: str = Field(description="Where agents connect: the public URL plus /mcp.")
    allowed_hosts: list[str] = Field(
        description="PURSER_ALLOWED_HOSTS: other names agents may use (#633)."
    )
    bind_address: str | None = Field(
        description="PURSER_BIND_ADDRESS as the deployment reports it; null when it does not."
    )
    public_url_is_local: bool = Field(
        description="PURSER_PUBLIC_URL names localhost, so other machines cannot use it."
    )
    request_host: str = Field(description="The address (Host) this request was sent to.")
    request_host_accepted: bool = Field(
        description=(
            "Whether agents can connect at request_host: the public URL's host, an allowed "
            "name or a loopback name. False means the web UI is open at an address the MCP "
            "endpoint refuses with 421."
        )
    )
    guide_url: str = Field(
        description="The setup guide's section on reaching Purser from another machine."
    )


@router.get("/network")
def get_network(_: AdminToken, request: Request) -> NetworkOut:
    """How Purser is reached, the address agents connect to, and whether the
    address this request used is one agents can use (#632). Admin scope:
    instance configuration, like the rest of /config."""
    settings = get_settings()
    public = settings.public_url.rstrip("/")
    host = request.headers.get("host", "")
    return NetworkOut(
        reach=network.reach_mode(public, settings.bind_address),
        public_url=public,
        mcp_url=f"{public}/mcp",
        allowed_hosts=list(settings.allowed_hosts),
        bind_address=(settings.bind_address or "").strip() or None,
        public_url_is_local=network.public_url_is_local(public),
        request_host=host,
        request_host_accepted=network.accepts(host, public, settings.allowed_hosts),
        guide_url=network.guide_url(settings.docs_url),
    )


def _check_header_key(name: str, key: str | None) -> None:
    """A provider key travels in an HTTP header, which takes printable ASCII
    only: a key with a space or another character would fail every fetch.
    Checked here rather than by a field validator, whose 422 would echo the
    key back in the response."""
    if key is not None and not all("!" <= ch <= "~" for ch in key):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"{name} must be printable ASCII without spaces (check for a pasted "
            "space, line break or lookalike character)",
        )


def _check_price_api_key(key: str | None) -> None:
    _check_header_key("price_api_key", key)


def _check_local_base_url(provider: str | None, base_url: str | None) -> None:
    """A provider declared local needs its own server's address: the cloud
    default never stands in for it (decision 133)."""
    if provider == "local" and ai_providers.needs_base_url(base_url):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "ai_provider 'local' needs ai_base_url: your server's OpenAI-compatible "
            "address, for example http://localhost:11434/v1",
        )


def _check_timezone_in_database(session: Session, name: str | None) -> None:
    """The readmodel views compute the ledger day in SQL (decision 105), so
    a zone Python knows but this Postgres does not would break them. The two
    tz databases agree on every IANA name in practice; this is the guard for
    when they do not."""
    if name is None:
        return
    known = session.execute(
        text("SELECT 1 FROM pg_timezone_names WHERE name = :name"), {"name": name}
    ).first()
    if known is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"timezone {name!r} is not known to the database's time zone data",
        )


@router.put("")
def update_config(body: ConfigUpdate, admin: AdminToken, session: DbSession) -> ConfigOut:
    if "price_api_key" in body.model_fields_set:
        _check_price_api_key(body.price_api_key)
    if "ai_api_key" in body.model_fields_set:
        _check_header_key("ai_api_key", body.ai_api_key)
    if body.model_fields_set & {"ai_provider", "ai_base_url"}:
        eff = appsettings.effective()
        _check_local_base_url(
            body.ai_provider if "ai_provider" in body.model_fields_set else eff.ai_provider,
            body.ai_base_url if "ai_base_url" in body.model_fields_set else eff.ai_base_url,
        )
    if "timezone" in body.model_fields_set:
        _check_timezone_in_database(session, body.timezone)
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
        # The daily sync time reads in the household zone (decision 111), so a
        # time zone change moves its trigger too.
        if appsettings.SYNC_SCHEDULE_KEY in changes or "timezone" in changes:
            scheduler.reschedule_sync()
    return _config_out(session)


@router.delete("/{key}")
def reset_config(key: str, admin: AdminToken, session: DbSession) -> ConfigOut:
    if key not in appsettings.ALL_KEYS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown config key: {key}")
    if key in ("ai_provider", "ai_base_url"):
        eff, env = appsettings.effective(), get_settings()
        _check_local_base_url(
            env.ai_provider if key == "ai_provider" else eff.ai_provider,
            env.ai_base_url if key == "ai_base_url" else eff.ai_base_url,
        )
    appsettings.reset_value(session, key, actor_token_id=admin.id)
    session.commit()
    appsettings.invalidate()
    if key in (appsettings.SYNC_SCHEDULE_KEY, "timezone"):
        scheduler.reschedule_sync()
    return _config_out(session)


class AiCheckIn(BaseModel):
    """The provider setup to check. Each absent field falls back to the saved
    setting, so the settings card can check what is in its form before it is
    saved. Nothing here is stored."""

    model_config = ConfigDict(extra="forbid")

    provider: Literal["openai", "local", "anthropic"] | None = None
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = Field(
        None, description="A key to use instead of the stored one; never stored by this call."
    )
    use_stored_key: bool = Field(
        True,
        description="With no api_key: send the stored key (true) or no key at all (false). "
        "The stored key goes only to the saved provider and base URL: checking any other "
        "sends no key, and the answer says so (`key_sent`, `message`).",
    )
    timeout_seconds: int | None = Field(None, ge=AI_TIMEOUT_MIN_SECONDS, le=AI_TIMEOUT_MAX_SECONDS)

    @field_validator("base_url")
    @classmethod
    def _http_url(cls, v: str | None) -> str | None:
        if v is not None and not v.startswith(("http://", "https://")):
            raise ValueError("must be an http(s) URL")
        return v

    @field_validator("model")
    @classmethod
    def _non_blank(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("must not be blank")
        return v.strip() if v is not None else v


class AiModelsOut(BaseModel):
    listed: bool = Field(description="False when the server offers no model list (HTTP 404/405).")
    models: list[str] = Field(description=f"Model ids, sorted; at most {ai_providers.MAX_MODELS}.")
    key_sent: bool = Field(description="Whether the request carried an API key.")
    message: str


class AiTestOut(BaseModel):
    ok: bool = Field(description="Reachable, authenticated, and the model answered as expected.")
    reachable: bool
    authenticated: bool | None = Field(description="None when the server's answers do not say.")
    model_listed: bool | None = Field(description="None when the server offers no model list.")
    answered: bool | None = Field(description="The model replied with the JSON Purser asks for.")
    network: Literal["this_machine", "private_network", "public_internet", "unknown"] = Field(
        description="Where the server's address resolves; a 'local' provider on the public "
        "internet gets a warning."
    )
    key_sent: bool
    elapsed_ms: int | None = Field(description="How long the model took to answer.")
    message: str = Field(description="What happened, in one or two sentences; never the key.")
    warning: str | None


STORED_KEY_HELD = (
    "The stored key was not sent: Purser sends it only to the saved provider and address. "
    "Enter this server's key to check with one."
)


@dataclasses.dataclass(frozen=True)
class _AiCheck:
    settings: Settings
    # A stored key exists and the call asked for it, but the destination
    # checked is not the saved one, so no key was sent (#667).
    stored_key_held: bool

    def message(self, text: str) -> str:
        return f"{text} {STORED_KEY_HELD}" if self.stored_key_held else text


def _ai_check_settings(body: AiCheckIn, request: Request) -> _AiCheck:
    """The setup a check runs with: the form's values over the saved ones.
    The stored key goes only where categorization would send it, the saved
    provider at the saved base URL (#667); any other destination gets the
    request's key or none. The call row records the host checked."""
    _check_header_key("api_key", body.api_key)
    saved = appsettings.effective()
    update: dict[str, Any] = {}
    if body.provider is not None:
        update["ai_provider"] = body.provider
    if body.base_url is not None:
        update["ai_base_url"] = body.base_url
    if body.model is not None:
        update["ai_model"] = body.model
    if body.timeout_seconds is not None:
        update["ai_timeout_seconds"] = body.timeout_seconds
    settings = saved.model_copy(update=update)
    if settings.ai_provider not in ai_providers.PROVIDERS or settings.ai_provider == "none":
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "choose an AI provider to check")
    held = False
    if body.api_key is not None:
        key: str | None = body.api_key
    elif body.use_stored_key and saved.ai_api_key:
        target = ai_providers.destination(settings)
        if target is not None and target == ai_providers.destination(saved):
            key = saved.ai_api_key
        else:
            key, held = None, True
    else:
        key = None
    settings = settings.model_copy(update={"ai_api_key": key})
    target = ai_providers.destination(settings)
    request.state.audit_detail = {
        "ai_check": {
            "provider": settings.ai_provider,
            "host": ai_providers.host_of(target[1]) if target else None,
            "key_sent": bool(key),
        }
    }
    return _AiCheck(settings=settings, stored_key_held=held)


@router.post("/ai/models")
def ai_models(body: AiCheckIn, request: Request, _: AdminToken) -> AiModelsOut:
    """The models the provider offers, for picking one (#640). One GET to the
    server's model list; a server without one says so instead of failing.
    The stored key is sent only to the saved provider and base URL (#667)."""
    check = _ai_check_settings(body, request)
    settings = check.settings
    key_sent = bool(settings.ai_api_key)
    try:
        listing = ai_providers.list_models(settings)
    except ai_providers.AiProviderError as exc:
        return AiModelsOut(
            listed=False,
            models=[],
            key_sent=key_sent,
            message=check.message(ai_providers.failure_message(exc, settings)),
        )
    if not listing.listed:
        return AiModelsOut(
            listed=False,
            models=[],
            key_sent=key_sent,
            message=check.message("This server does not list its models: type the model name."),
        )
    return AiModelsOut(
        listed=True,
        models=listing.models,
        key_sent=key_sent,
        message=check.message(f"Found {len(listing.models)} models."),
    )


@router.post("/ai/test")
def ai_test(body: AiCheckIn, request: Request, _: AdminToken) -> AiTestOut:
    """Test connection (#640): reachable, authenticated, and the model
    answers in the JSON shape Purser parses. At most two requests to the
    provider; messages never contain the key or the provider's reply. The
    stored key is sent only to the saved provider and base URL (#667)."""
    check = _ai_check_settings(body, request)
    report = ai_providers.check_connection(check.settings)
    log.info(
        "ai.connection_test",
        provider=check.settings.ai_provider,
        ok=report.ok,
        network=report.network,
        key_sent=report.key_sent,
    )
    out = dataclasses.asdict(report)
    return AiTestOut(**(out | {"message": check.message(report.message)}))


class ClockOut(BaseModel):
    ledger_today: date_type = Field(
        description="Today in the household time zone: the newest date a dated write accepts."
    )
    timezone: str = Field(description="The household time zone (IANA name) the ledger day uses.")


@clock_router.get("/clock")
def get_clock(token: ReadToken) -> ClockOut:
    """The ledger day and the household time zone it follows (#471, decision
    105). Read scope: a client that dates a write (a share count, a position)
    needs the server's "today", which the time zone setting decides and which
    is not the browser's or UTC's date."""
    return ClockOut(ledger_today=clock.ledger_today(), timezone=clock.timezone_name())
