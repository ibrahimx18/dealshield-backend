from pydantic_settings import BaseSettings
import os

class Settings(BaseSettings):
    # No insecure fallback — app.main.validate_required_secrets() enforces this is set at startup.
    SECRET_KEY: str = os.getenv("SAFEPAY_SECRET_KEY", "")
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24  # 24 hours
    ALGORITHM: str = "HS256"
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///./data/safepay.db")
    COMMISSION_RATE: float = 0.025
    SAFEPAY_CORS_ORIGINS: str = os.getenv("SAFEPAY_CORS_ORIGINS", "http://localhost:8080")
    OLLAMA_URL: str = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
    OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL", "llama3.2:3b")
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
    WEBHOOK_SECRET: str = os.getenv("SAFEPAY_WEBHOOK_SECRET", "")
    # Payment provider for virtual accounts (paystack, flutterwave)
    PAYMENT_PROVIDER: str = os.getenv("PAYMENT_PROVIDER", "paystack")
    PAYSTACK_SECRET_KEY: str = os.getenv("PAYSTACK_SECRET_KEY", "")
    PAYSTACK_PUBLIC_KEY: str = os.getenv("PAYSTACK_PUBLIC_KEY", "")

    class Config:
        env_file = ".env"
        extra = "ignore"

settings = Settings()

# Force check in production/live environments
ENVIRONMENT = os.getenv("ENVIRONMENT", "development")
if ENVIRONMENT == "production":
    if "CHANGE-ME" in settings.SECRET_KEY or len(settings.SECRET_KEY) < 32:
        raise RuntimeError("FATAL: SAFEPAY_SECRET_KEY must be configured with a secure 32+ char key in production!")
    if not settings.WEBHOOK_SECRET or settings.WEBHOOK_SECRET == "safepay_webhook_secret_2026":
        raise RuntimeError("FATAL: SAFEPAY_WEBHOOK_SECRET must be explicitly set in production!")
