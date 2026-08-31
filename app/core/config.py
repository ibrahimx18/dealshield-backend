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
    # No insecure fallback — app.main.validate_required_secrets() enforces this is set at startup.
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")

    class Config:
        env_file = ".env"
        extra = "ignore"

settings = Settings()
