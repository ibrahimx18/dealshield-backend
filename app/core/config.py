from pydantic_settings import BaseSettings
import os

class Settings(BaseSettings):
    # Fixed secret key — set via env var in production
    # Never auto-generate (would invalidate all tokens on restart)
    SECRET_KEY: str = os.getenv("SAFEPAY_SECRET_KEY", "CHANGE-ME-IN-PRODUCTION-use-a-32-char-random-string")
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24  # 24 hours (reduced from 7 days)
    ALGORITHM: str = "HS256"
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///./data/safepay.db")
    COMMISSION_RATE: float = 0.025

    class Config:
        env_file = ".env"

settings = Settings()
