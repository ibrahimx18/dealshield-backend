"""N02: DB-backed per-user 2FA lockout, failures-only counting, trusted-proxy client IP."""
from datetime import datetime, timedelta

import pyotp
from starlette.requests import Request

from conftest import SessionLocal, User, register, new_client
from app.core.security import create_2fa_temp_token, credential_version
from app.core import client_ip


def _enable_2fa(u):
    secret = pyotp.random_base32()
    db = SessionLocal()
    try:
        db.query(User).filter(User.id == u.id).update({"totp_secret": secret, "totp_enabled": True})
        db.commit()
        user = db.query(User).filter(User.id == u.id).one()
        temp = create_2fa_temp_token(user.id, credential_version(user.hashed_password, user.totp_secret))
    finally:
        db.close()
    return secret, temp


def _user(uid):
    db = SessionLocal()
    try:
        return db.query(User).filter(User.id == uid).one()
    finally:
        db.close()


def test_five_wrong_codes_lock_user_even_with_correct_code():
    u = register()
    secret, temp = _enable_2fa(u)
    c = new_client()
    codes = [c.post("/auth/login/2fa", json={"temp_token": temp, "totp_code": "000000"}).status_code
             for _ in range(5)]
    assert codes == [401, 401, 401, 401, 429]
    # Correct code is refused while locked
    r = c.post("/auth/login/2fa", json={"temp_token": temp, "totp_code": pyotp.TOTP(secret).now()})
    assert r.status_code == 429 and "Retry-After" in r.headers
    row = _user(u.id)
    assert row.totp_failed_attempts == 5 and row.totp_locked_until is not None


def test_lock_is_per_user_not_per_ip():
    u = register()
    _, temp = _enable_2fa(u)
    for _ in range(5):  # five different IPs
        new_client().post("/auth/login/2fa", json={"temp_token": temp, "totp_code": "000000"})
    assert _user(u.id).totp_locked_until is not None


def test_success_resets_and_successes_do_not_count():
    u = register()
    secret, temp = _enable_2fa(u)
    c = new_client()
    for _ in range(3):
        assert c.post("/auth/login/2fa", json={"temp_token": temp, "totp_code": "000000"}).status_code == 401
    r = c.post("/auth/login/2fa", json={"temp_token": temp, "totp_code": pyotp.TOTP(secret).now()})
    assert r.status_code == 200, r.text
    assert _user(u.id).totp_failed_attempts == 0
    # Many successful logins from one IP never trip the per-IP 2FA limit
    for _ in range(6):
        r = c.post("/auth/login/2fa", json={"temp_token": temp, "totp_code": pyotp.TOTP(secret).now()})
        assert r.status_code == 200, r.text


def test_expired_lock_resets_counter():
    u = register()
    secret, temp = _enable_2fa(u)
    db = SessionLocal()
    try:
        db.query(User).filter(User.id == u.id).update(
            {"totp_failed_attempts": 5, "totp_locked_until": datetime.utcnow() - timedelta(minutes=1)})
        db.commit()
    finally:
        db.close()
    r = new_client().post("/auth/login/2fa", json={"temp_token": temp, "totp_code": "000000"})
    assert r.status_code == 401  # counted fresh: 1 failure, not a new lock
    assert _user(u.id).totp_failed_attempts == 1


def _req(headers, peer="10.0.0.1"):
    return Request({"type": "http", "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
                    "client": (peer, 1)})


def test_client_ip_ignores_proxy_headers_by_default(monkeypatch):
    monkeypatch.delenv("TRUSTED_PROXY", raising=False)
    r = _req({"CF-Connecting-IP": "1.2.3.4", "X-Forwarded-For": "5.6.7.8"})
    assert client_ip.get_client_ip(r) == "10.0.0.1"


def test_client_ip_trusts_headers_only_when_enabled(monkeypatch):
    monkeypatch.setenv("TRUSTED_PROXY", "1")
    assert client_ip.get_client_ip(_req({"CF-Connecting-IP": "1.2.3.4"})) == "1.2.3.4"
    # right-most XFF entry (the one our proxy appended), forged left entries ignored
    assert client_ip.get_client_ip(_req({"X-Forwarded-For": "9.9.9.9, 5.6.7.8"})) == "5.6.7.8"
    # garbage falls back to the peer
    assert client_ip.get_client_ip(_req({"CF-Connecting-IP": "not-an-ip"})) == "10.0.0.1"
