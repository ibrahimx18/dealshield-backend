"""N16: CORS origin list for credentialed requests (HttpOnly refresh cookie).

allow_credentials=True must never be combined with a wildcard origin. Origins come
from SAFEPAY_CORS_ORIGINS (comma separated, exact scheme://host[:port]). Wildcards,
blanks, paths and malformed values are dropped. In production only https origins
are kept.
"""
import os
import re

DEFAULT_ORIGINS = "http://localhost:8080"
_ORIGIN_RE = re.compile(r"^https?://[A-Za-z0-9.-]+(:\d{1,5})?$")


def parse_cors_origins(raw: str | None = None, environment: str | None = None) -> list[str]:
    if raw is None:
        raw = os.getenv("SAFEPAY_CORS_ORIGINS", DEFAULT_ORIGINS)
    if environment is None:
        environment = os.getenv("ENVIRONMENT", "")
    production = environment.strip().lower() == "production"
    origins: list[str] = []
    for item in (raw or "").split(","):
        origin = item.strip().rstrip("/")
        if not origin or "*" in origin or not _ORIGIN_RE.match(origin):
            continue
        if production and not origin.startswith("https://"):
            continue
        if origin not in origins:
            origins.append(origin)
    return origins
