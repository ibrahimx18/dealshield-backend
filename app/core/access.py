"""N07: shared access helpers - admin-or-integration-secret check and a small
per-key in-memory rate limiter (per worker; good enough to stop free-riding)."""
import hmac
import os
import time
from collections import defaultdict, deque
from typing import Optional

from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session

from app.dependencies import get_db


def _integration_secret_ok(value: Optional[str]) -> bool:
    configured = os.getenv("SAFEPAY_WEBHOOK_SECRET", "")
    return bool(configured and value and hmac.compare_digest(value.encode(), configured.encode()))


def build_admin_or_integration():
    """Built lazily to avoid an import cycle with app.routers.auth."""
    from app.routers.auth import optional_oauth2, _live_session
    from app.core.security import decode_access_token
    from app.models.models import User

    def check(x_safepay_secret: Optional[str] = Header(None),
              token: Optional[str] = Depends(optional_oauth2),
              db: Session = Depends(get_db)) -> bool:
        """Non-raising: True for a valid integration secret or a live admin session."""
        if _integration_secret_ok(x_safepay_secret):
            return True
        claims = decode_access_token(token) if token else None
        if claims and _live_session(db, claims):
            user = db.query(User).filter(User.id == int(claims["sub"])).first()
            if user and user.is_active and user.role == "admin":
                return True
        return False

    def dep(ok: bool = Depends(check)) -> bool:
        if not ok:
            raise HTTPException(status_code=403, detail="Admin or integration secret required")
        return True
    dep.check = check
    return dep


class KeyedRateLimiter:
    def __init__(self, limit: int, window_seconds: int):
        self.limit, self.window = limit, window_seconds
        self.hits: dict = defaultdict(deque)

    def check(self, key) -> None:
        now = time.time()
        dq = self.hits[key]
        while dq and dq[0] < now - self.window:
            dq.popleft()
        if len(dq) >= self.limit:
            raise HTTPException(status_code=429, detail="Rate limit exceeded. Please try again later.",
                                headers={"Retry-After": str(self.window)})
        dq.append(now)
        if len(self.hits) > 5000:
            cutoff = now - self.window
            self.hits = defaultdict(deque, {k: d for k, d in self.hits.items() if d and d[-1] >= cutoff})
