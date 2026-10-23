"""N16: refresh token in an HttpOnly Secure SameSite=Strict cookie scoped to /auth;
body token kept behind AUTH_REFRESH_BODY_COMPAT."""
import pytest

from conftest import PASSWORD, register, new_client


@pytest.fixture
def insecure_cookie(monkeypatch):
    # TestClient talks plain http://testserver; a Secure cookie would never be sent back.
    monkeypatch.setenv("AUTH_COOKIE_SECURE", "false")


def _cookie_header(resp):
    return [v for k, v in resp.headers.multi_items() if k.lower() == "set-cookie"]


def test_login_sets_httponly_strict_cookie():
    u = register()
    c = new_client()
    r = c.post("/auth/login", json={"email_or_phone": u.email, "password": PASSWORD})
    assert r.status_code == 200
    sc = " ".join(_cookie_header(r)).lower()
    assert "ds_refresh=" in sc and "httponly" in sc and "secure" in sc
    assert "samesite=strict" in sc and "path=/auth" in sc


def test_refresh_via_cookie_rotates_and_reuse_fails(insecure_cookie):
    u = register()
    c = new_client()
    r = c.post("/auth/login", json={"email_or_phone": u.email, "password": PASSWORD})
    old = c.cookies.get("ds_refresh")
    assert old
    r = c.post("/auth/refresh")  # no body: cookie only
    assert r.status_code == 200, r.text
    new = c.cookies.get("ds_refresh")
    assert new and new != old
    # replaying the old cookie value is reuse -> 401 and session revoked
    c2 = new_client()
    c2.cookies.set("ds_refresh", old, path="/auth")
    assert c2.post("/auth/refresh").status_code == 401
    assert c.post("/auth/refresh").status_code == 401  # family revoked


def test_logout_revokes_cookie_session_and_clears_cookie(insecure_cookie):
    u = register()
    c = new_client()
    c.post("/auth/login", json={"email_or_phone": u.email, "password": PASSWORD})
    token = c.cookies.get("ds_refresh")
    r = c.post("/auth/logout")
    assert r.status_code == 200 and r.json()["revoked"] is True
    assert any("ds_refresh=" in h and ("max-age=0" in h.lower() or "expires=" in h.lower())
               for h in _cookie_header(r))
    c2 = new_client()
    c2.cookies.set("ds_refresh", token, path="/auth")
    assert c2.post("/auth/refresh").status_code == 401


def test_body_compat_off_hides_and_refuses_body_token(monkeypatch):
    monkeypatch.setenv("AUTH_REFRESH_BODY_COMPAT", "false")
    u = register()
    assert u.refresh == ""  # not returned in JSON
    c = new_client()
    r = c.post("/auth/login", json={"email_or_phone": u.email, "password": PASSWORD})
    assert r.json()["refresh_token"] == ""
    monkeypatch.setenv("AUTH_REFRESH_BODY_COMPAT", "true")
    u2 = register()
    monkeypatch.setenv("AUTH_REFRESH_BODY_COMPAT", "false")
    assert new_client().post("/auth/refresh", json={"refresh_token": u2.refresh}).status_code == 401


def test_body_compat_on_still_works():
    u = register()
    assert u.refresh
    assert new_client().post("/auth/refresh", json={"refresh_token": u.refresh}).status_code == 200
