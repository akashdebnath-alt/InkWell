from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str
    anthropic_api_key: str = ""
    jwt_secret_key: str = ""
    fast_model: str = "claude-haiku-5-5"
    smart_model: str = "claude-sonnet-5-5"
    cors_origins: str = "http://localhost:5173"

    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[1] / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def has_anthropic_api_key(self) -> bool:
        value = self.anthropic_api_key.strip()
        return bool(value and value.lower() not in {"your_key_here", "replace_me", "placeholder"})

    @property
    def allowed_origins(self) -> list[str]:
        origins = [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]
        return origins or ["http://localhost:5173"]


settings = Settings()
