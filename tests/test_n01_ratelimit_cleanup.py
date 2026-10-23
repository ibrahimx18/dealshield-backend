"""N01: the rate limiter's periodic cleanup must keep auto-creating buckets for new IPs."""
from collections import defaultdict

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.security_middleware import RateLimitMiddleware


def _app():
    a = FastAPI()

    @a.get("/ping")
    def ping():
        return {"ok": True}

    @a.post("/auth/login")
    def login():
        return {"ok": True}

    a.add_middleware(RateLimitMiddleware)
    return a


def test_new_ip_after_cleanup_is_not_500():
    app = _app()
    for i in range(1005):  # > 1000 distinct IPs triggers cleanup
        ip = f"10.{i // 250}.{i % 250}.7"
        r = TestClient(app, client=(ip, 1234)).get("/ping")
        assert r.status_code == 200, (i, r.status_code)
    # Brand-new IPs on every bucket type after cleanup ran
    assert TestClient(app, client=("203.0.113.250", 1)).get("/ping").status_code == 200
    assert TestClient(app, client=("203.0.113.251", 1)).post("/auth/login").status_code == 200


def test_cleanup_keeps_defaultdicts():
    mw = RateLimitMiddleware(_app())
    mw._cleanup(0.0)
    for d in (mw.general_attempts, mw.login_attempts, mw.two_factor_attempts):
        assert isinstance(d, defaultdict)
        assert len(d["198.51.100.9"]) == 0
