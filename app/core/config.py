import os
from dotenv import load_dotenv

# Load .env BEFORE anything else
load_dotenv()

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    SECRET_KEY: str = ""
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24  # 24 hours
    ALGORITHM: str = "HS256"
    DATABASE_URL: str = "sqlite:///./data/safepay.db"
    COMMISSION_RATE: float = 0.025
    SAFEPAY_CORS_ORIGINS: str = "http://localhost:8080"
    OLLAMA_URL: str = "http://127.0.0.1:11434"
    OLLAMA_MODEL: str = "llama3.2:3b"
    BOT_TOKEN: str = ""
    WEBHOOK_SECRET: str = ""
    # Payment provider for virtual accounts (paystack, flutterwave)
    PAYMENT_PROVIDER: str = "paystack"
    PAYSTACK_SECRET_KEY: str = ""
    PAYSTACK_PUBLIC_KEY: str = ""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Alias env var names to settings fields
    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings):
        # Reorder: dotenv first, then env, then init — so .env takes priority
        return (dotenv_settings, env_settings, init_settings, file_secret_settings)


# Map SAFEPAY_-prefixed env vars to our field names via os.environ
# (pydantic-settings v2 reads env vars matching field names case-insensitively)
_key = os.getenv("SAFEPAY_SECRET_KEY", "")
if _key:
    os.environ["SECRET_KEY"] = _key

_wsecret = os.getenv("SAFEPAY_WEBHOOK_SECRET", "")
if _wsecret:
    os.environ["WEBHOOK_SECRET"] = _wsecret

settings = Settings()

# Force check in production/live environments
ENVIRONMENT = os.getenv("ENVIRONMENT", "development")
if ENVIRONMENT == "production":
    if "CHANGE-ME" in settings.SECRET_KEY or len(settings.SECRET_KEY) < 32:
        raise RuntimeError("FATAL: SAFEPAY_SECRET_KEY must be configured with a secure 32+ char key in production!")
    if not settings.WEBHOOK_SECRET or settings.WEBHOOK_SECRET == "safepay_webhook_secret_2026":
        raise RuntimeError("FATAL: SAFEPAY_WEBHOOK_SECRET must be explicitly set in production!")
