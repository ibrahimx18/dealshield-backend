"""N07: AI endpoints need login + rate limit + generic errors; market refresh needs
admin or integration secret; docs/openapi are off in production."""
import importlib
import os
import subprocess
import sys

from conftest import SessionLocal, User, register, new_client
import app.routers.ai as ai
import app.routers.market as market


def _boom(*a, **k):
    raise ConnectionError("http://10.9.8.7:11434 internal ollama refused")


def test_ai_requires_login():
    c = new_client()
    assert c.post("/ai/chat", json={"message": "hi"}).status_code == 401
    assert c.post("/ai/fraud-check", json={"title": "t", "description": "d", "price": 1,
                                            "category": "cars"}).status_code == 401


def test_ai_error_is_generic(monkeypatch):
    monkeypatch.setattr(ai.requests, "post", _boom)
    u = register()
    r = u.post("/ai/chat", {"message": "hi"})
    assert r.status_code == 502
    assert "11434" not in r.text and "ollama" not in r.text.lower()
    r = u.post("/ai/fraud-check", {"title": "t", "description": "d", "price": 1, "category": "cars"})
    assert r.status_code == 502 and "11434" not in r.text


def test_ai_rate_limited(monkeypatch):
    monkeypatch.setattr(ai.requests, "post", _boom)
    monkeypatch.setattr(ai, "_ai_limiter", ai.KeyedRateLimiter(limit=3, window_seconds=600))
    u = register()
    codes = [u.post("/ai/chat", {"message": "hi"}).status_code for _ in range(4)]
    assert codes == [502, 502, 502, 429]


def test_market_refresh_needs_admin_or_secret(monkeypatch):
    calls = []
    monkeypatch.setattr(market, "update_prices_in_db", lambda db: calls.append(1))
    u = register()
    assert new_client().post("/market/refresh").status_code == 403
    assert u.post("/market/refresh").status_code == 403
    db = SessionLocal()
    try:  # make sure the table is non-empty so the first-boot fill does not run
        from app.models.models import MarketPrice
        if db.query(MarketPrice).count() == 0:
            db.add(MarketPrice(item="X", category="gold", price_usd=1, price_ngn=1))
            db.commit()
    finally:
        db.close()
    assert u.get("/market/prices?refresh=true").status_code == 403
    assert u.get("/market/prices").status_code == 200
    assert calls == []
    # conftest leaves the integration secret empty (fails closed); configure one here
    assert new_client().post("/market/refresh", headers={"X-SafePay-Secret": ""}).status_code == 403
    monkeypatch.setenv("SAFEPAY_WEBHOOK_SECRET", "n07-integration-secret-value")
    assert new_client().post("/market/refresh", headers={"X-SafePay-Secret": "wrong"}).status_code == 403
    secret = {"X-SafePay-Secret": "n07-integration-secret-value"}
    assert new_client().post("/market/refresh", headers=secret).status_code == 200
    db = SessionLocal()
    try:
        db.query(User).filter(User.id == u.id).update({"role": "admin"})
        db.commit()
    finally:
        db.close()
    assert u.post("/market/refresh").status_code == 200
    assert u.get("/market/prices?refresh=true").status_code == 200
    assert len(calls) == 3


def test_docs_available_outside_production():
    c = new_client()
    assert c.get("/openapi.json").status_code == 200


def test_docs_disabled_in_production(tmp_path):
    code = (
        "import os\n"
        "os.environ.update({'ENVIRONMENT':'production','DATABASE_URL':'sqlite:///" + str(tmp_path / 'p.db').replace('\\', '/') + "',"
        "'SAFEPAY_SECRET_KEY':'p'*40,'SAFEPAY_WEBHOOK_SECRET':'w'*40,'SECRET_KEY':'p'*40,"
        "'SAFEPAY_TEST_MODE':'false','DEALSHIELD_SEED_DEMO':'false','DEALSHIELD_KYC_KEY':'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA='})\n"
        "from fastapi.testclient import TestClient\n"
        "from app.main import app\n"
        "c = TestClient(app)\n"
        "print([c.get(p).status_code for p in ('/docs','/redoc','/openapi.json')])\n"
    )
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip().splitlines()[-1] == "[404, 404, 404]"
