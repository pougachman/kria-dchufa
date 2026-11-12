"""Application settings, read from environment variables (and a local .env file in development)."""

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    database_url: str = "sqlite:///./local.db"

    # External services. All optional in M0; each milestone that needs one checks for it.
    anthropic_api_key: str | None = None
    ncbi_api_key: str | None = None
    ncbi_email: str | None = None
    ncbi_tool: str = "kria-dchufa"

    # M2 screening. Haiku through the Message Batches API (50% price, results within 24 hours).
    screening_model: str = "claude-haiku-5-5"
    screening_prompt: str = "screening.v1"

    # M3 approval screen and digest. One editor password until M5 adds users and roles.
    editor_password: str | None = None
    # Email through Resend. onboarding@resend.dev can send only to the Resend account's own
    # address, which is enough for the pilot (the digest goes to Yoav only). Sending to others
    # needs a verified domain and a DIGEST_FROM on it.
    resend_api_key: str | None = None
    digest_from: str = "Kria Dchufa <onboarding@resend.dev>"
    digest_recipients: str | None = None  # comma-separated
    public_url: str = "https://kria-dchufa-production.up.railway.app"

    @property
    def recipients(self) -> list[str]:
        return [r.strip() for r in (self.digest_recipients or "").split(",") if r.strip()]

    @field_validator(
        "anthropic_api_key",
        "ncbi_api_key",
        "ncbi_email",
        "editor_password",
        "resend_api_key",
        "digest_recipients",
        mode="before",
    )
    @classmethod
    def strip_pasted_whitespace(cls, value):
        """Keys pasted into a dashboard often carry a stray space or line break."""
        if isinstance(value, str):
            value = value.strip().strip('"').strip("'").strip()
            return value or None
        return value


def anthropic_key_looks_valid(key: str | None) -> bool:
    """A format check only (Claude API keys start with sk-ant-); never shows the key."""
    return (
        bool(key)
        and key.startswith("sk-ant-")
        and len(key) > 40
        and key.isprintable()
        and (" " not in key)
    )


settings = Settings()
