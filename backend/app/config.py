from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "postgresql+psycopg://poultry:poultry@db:5432/poultry_trace"
    jwt_secret: str = "change-this-development-secret-before-use-32chars"
    jwt_refresh_secret: str = "change-this-refresh-secret-before-use-32chars"
    access_token_minutes: int = 15
    refresh_token_days: int = 7
    cors_origins: str = "http://localhost:5173,http://localhost:8080"
    environment: str = "development"
    loss_approval_threshold: int = 10
    login_max_attempts: int = 5
    login_lock_minutes: int = 15

    @field_validator("database_url", mode="before")
    @classmethod
    def use_psycopg3_driver(cls, value: str) -> str:
        if value.startswith("postgres://"):
            return "postgresql+psycopg://" + value.removeprefix("postgres://")
        if value.startswith("postgresql://"):
            return "postgresql+psycopg://" + value.removeprefix("postgresql://")
        return value


settings = Settings()
