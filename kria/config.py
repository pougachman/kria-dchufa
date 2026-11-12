"""Application settings, read from environment variables (and a local .env file in development)."""

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


settings = Settings()
