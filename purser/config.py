"""Application settings, sourced from PURSER_* environment variables."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PURSER_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://purser:purser@localhost:5432/purser"
    # Fernet key for data encrypted at rest (raw bank envelopes, SimpleFIN
    # access URLs, webhook secrets). REQUIRED at startup — every role command
    # fails fast without it (enforced in purser.crypto, not here, so the error
    # message stays actionable). Accepts comma-separated keys for rotation
    # (first key encrypts). Generate with `purser keygen`.
    encryption_key: str | None = None
    log_level: str = "INFO"
    # Set when running a dedicated `purser worker` so the API process skips jobs.
    disable_scheduler: bool = False
    host: str = "0.0.0.0"
    port: int = 8000
    # External base URL of this instance (cookie security + OIDC redirect URI).
    public_url: str = "http://localhost:8000"
    # Browser-session lifetime. Absolute cap in days — single source for both
    # the token expiry and the cookie max-age. Idle timeout in hours — a
    # session with no request for this long stops authenticating (sliding:
    # any request refreshes it via last_used_at).
    session_ttl_days: int = 7
    session_idle_hours: int = 8
    # Password sign-in throttling (issue #212, DESIGN decision 59), in-memory
    # per process. Failures count per email (casefolded) and per client
    # address; at the limit, POST /auth/login answers 429 + Retry-After
    # without checking the password, for a backoff that starts at
    # login_backoff_seconds and doubles per further failure up to
    # login_backoff_max_seconds. Success clears the email's count, never the
    # address's; a count is forgotten after login_failure_reset_seconds with
    # no failure and no lockout in force. A limit of 0 switches it off.
    login_max_failures: int = 5
    login_ip_max_failures: int = 20
    login_backoff_seconds: int = 30
    login_backoff_max_seconds: int = 900
    login_failure_reset_seconds: int = 3600
    # The largest request body accepted, in bytes (#429): bigger is refused
    # with 413 before it is buffered — FastAPI reads a body before it checks
    # credentials. 32 MiB holds decades of QIF/CSV history. 0 turns it off.
    max_request_bytes: int = 32 * 1024 * 1024
    # Audit retention (issue #122): call + change rows older than this are
    # pruned nightly by audit.prune_daily. Uniform for reads and writes
    # (owner decision, DESIGN decision 31); the API floor (7d) keeps a
    # tamper-evidence window even if an admin turns it all the way down.
    audit_retention_days: int = 90
    # OIDC against a generic OpenID Provider (e.g. Authentik). Enabled when
    # issuer + client id + secret are all set.
    oidc_issuer: str | None = None
    oidc_client_id: str | None = None
    oidc_client_secret: str | None = None
    # Auto-provision unknown OIDC subjects as member users (#211). Off by
    # default: an unknown subject gets "account not provisioned" instead of
    # a write-scoped account minted for anyone the OP will issue a token to.
    oidc_auto_provision: bool = False
    # Deterministic transfer auto-linking at import (DESIGN §5): exact
    # opposite amounts within the window PLUS a transfer-style name signal
    # merge automatically (evented, audited, unlink-able). Off = everything
    # stays a confirm-first candidate.
    transfer_auto_link: bool = True
    # Minimal disclosure to agents (#437, DESIGN §9): both off by default.
    # privacy_purchase returns payees as stable references (py_...) and omits
    # descriptions, memos and schedule names; privacy_amount omits
    # single-transaction amounts (aggregates stay exact). A setting is a
    # default the agent may override per call with `include`, not a ceiling.
    privacy_purchase: bool = False
    privacy_amount: bool = False
    # Optional AI categorization (BYO provider). Off by default; the core is
    # fully functional without it (DESIGN §11).
    ai_provider: str = "none"  # none | openai | anthropic
    ai_base_url: str = "https://api.openai.com/v1"  # any OpenAI-compatible endpoint
    ai_api_key: str | None = None
    ai_model: str | None = None
    # Backend review pass (issue #175, DESIGN §11.2). Gated by ai_provider on
    # top of this: configuring a provider grants nothing by itself.
    ai_review: bool = False
    # The unreviewed set must not grow without bound. Matches
    # audit_retention_days by default, so a review mark never outlives the
    # audit row that explains it.
    review_max_age_days: int = 90
    # MCP Apps presentation layer (issue #72): directory holding the built
    # self-contained widget bundles served as ui://purser/* resources. The
    # image build drops them at /app/widgets; dev falls back to
    # frontend/dist-widgets when this dir has nothing to serve.
    widgets_dir: str = "/app/widgets"
    # Manually tracked holdings (#390, decision 89): the daily-close provider
    # (`none` or `tiingo`) and how many days old a close may be before a
    # position is reported as priced on a stale close. The provider's API key
    # is NOT an env setting: it lives only in the encrypted app settings
    # (Config, Prices), because Purser is the party that sends it.
    price_provider: str = "none"
    price_stale_days: int = 7
    # Optional Grafana base URL for links.grafana deep links on tool
    # responses (readmodel dashboards); links are omitted entirely when unset.
    grafana_url: str | None = None
    # Optional base URL of the deployed docs site (issue #156). search_docs
    # returns absolute links when set, site-relative paths when not. Instance
    # identity like public_url — env-only, never a runtime override.
    docs_url: str | None = None
    # Where this instance's Corresponding Source lives (AGPL-3.0 section 13,
    # #429): GET /source redirects here, the web UI footer links /source, and
    # /llms.txt names the URL. An operator running a modified Purser points it at
    # their own published source. Instance identity like public_url —
    # env-only, never a runtime override.
    source_url: str = "https://github.com/mlamoure/purser"

    @property
    def oidc_enabled(self) -> bool:
        return bool(self.oidc_issuer and self.oidc_client_id and self.oidc_client_secret)

    @property
    def cookie_secure(self) -> bool:
        return self.public_url.startswith("https://")


@lru_cache
def get_settings() -> Settings:
    return Settings()
