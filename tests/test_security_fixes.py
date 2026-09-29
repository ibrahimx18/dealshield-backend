"""Targeted regressions for the 2026-09-29 security fixes (B01/B04/B05/B06/B07/B08/B11)."""
import threading
from decimal import Decimal

import pytest

from conftest import (SessionLocal, User, accepted_escrow, balance_of, make_listing, new_client,
                      register, webhook, PASSWORD)
from app.core.wallet import debit_wallet
from app.models.models import EscrowTransaction, WebhookQuarantine, PaymentWebhookEvent


# ── B01: price validation ──
@pytest.mark.parametrize("price", ["-1", "-0.01", "0", "0.00", "0.001", "100.001",
                                   "1e20", "99999999999999999", "NaN", "Infinity", "abc"])
def test_bad_listing_price_rejected(price):
    seller = register()
    r = seller.post("/listings", {"category": "cars", "title": "x", "price": price})
    assert r.status_code == 422, (price, r.status_code, r.text)


def test_good_price_accepted_and_serialised_as_string():
    seller = register()
    r = seller.post("/listings", {"category": "cars", "title": "x", "price": "15000"})
    assert r.status_code == 200, r.text
    assert r.json()["price"] == "15000.00"


def test_unverified_user_cannot_list():
    r = register(kyc=False).post("/listings", {"category": "cars", "title": "x", "price": "100"})
    assert r.status_code in (400, 403)


# ── B08: atomic debit ──
def test_concurrent_double_debit_only_once():
    u = register(balance="1000.00")
    barrier = threading.Barrier(2)
    results = []

    def worker():
        db = SessionLocal()
        try:
            barrier.wait()
            debit_wallet(db, u.id, "1000.00")
            db.commit()
            results.append("ok")
        except Exception as exc:  # InsufficientFunds (HTTP 400) for the loser
            db.rollback()
            results.append(type(exc).__name__)
        finally:
            db.close()

    ts = [threading.Thread(target=worker) for _ in range(2)]
    [t.start() for t in ts]
    [t.join(30) for t in ts]
    assert results.count("ok") == 1, results
    assert Decimal(balance_of(u.id)) == Decimal("0.00")


def test_stale_read_debit_rejected():
    u = register(balance="500.00")
    a, b = SessionLocal(), SessionLocal()
    try:
        a.query(User).filter(User.id == u.id).one()  # A reads 500
        debit_wallet(b, u.id, "400.00"); b.commit()   # B spends 400
        with pytest.raises(Exception):
            debit_wallet(a, u.id, "400.00")           # A's stale view must not overdraw
        a.rollback()
    finally:
        a.close(); b.close()
    assert Decimal(balance_of(u.id)) == Decimal("100.00")


def test_escrow_fund_twice_debits_once():
    buyer, seller = register(balance="500000.00"), register()
    tx = accepted_escrow(buyer, seller, "100000.00")
    assert buyer.post(f"/escrow/{tx}/fund").status_code == 200
    after = Decimal(balance_of(buyer.id))
    assert buyer.post(f"/escrow/{tx}/fund").status_code in (400, 409)
    assert Decimal(balance_of(buyer.id)) == after >= 0


# ── B04: webhook ──
def _intent(buyer, tx):
    r = buyer.post(f"/escrow/{tx}/payment-intent")
    assert r.status_code == 200, r.text
    return r.json()


def _event(eid, ref, kobo, currency="NGN", event="charge.success"):
    return {"event": event, "data": {"id": eid, "reference": ref, "amount": kobo,
                                     "currency": currency, "status": "success"}}


def _status(tx):
    db = SessionLocal()
    try:
        return db.query(EscrowTransaction.status).filter(EscrowTransaction.id == tx).scalar()
    finally:
        db.close()


def test_webhook_bad_signature_rejected():
    buyer, seller = register(), register()
    tx = accepted_escrow(buyer, seller)
    it = _intent(buyer, tx)
    r = webhook(new_client(), _event(1, it["reference"], it["amount_kobo"]), signature="00" * 64)
    assert r.status_code == 401
    assert _status(tx) == "seller_accepted"


def test_webhook_wrong_amount_quarantined():
    buyer, seller = register(), register()
    tx = accepted_escrow(buyer, seller)
    it = _intent(buyer, tx)
    r = webhook(new_client(), _event(9001, it["reference"], 100))  # 1 naira
    assert r.status_code == 200 and r.json()["status"] == "quarantined", r.text
    assert _status(tx) == "seller_accepted"
    db = SessionLocal()
    try:
        q = db.query(WebhookQuarantine).filter(WebhookQuarantine.reference == it["reference"]).one()
        assert q.reason == "amount mismatch"
    finally:
        db.close()


def test_webhook_wrong_currency_and_event_type():
    buyer, seller = register(), register()
    tx = accepted_escrow(buyer, seller)
    it = _intent(buyer, tx)
    r = webhook(new_client(), _event(9101, it["reference"], it["amount_kobo"], event="transfer.success"))
    assert r.json()["status"] == "ignored"
    r = webhook(new_client(), _event(9102, it["reference"], it["amount_kobo"], currency="USD"))
    assert r.json()["status"] == "quarantined"
    assert _status(tx) == "seller_accepted"


def test_webhook_unknown_reference_quarantined():
    r = webhook(new_client(), _event(9201, "NOT_A_REF", 1000000))
    assert r.status_code == 200 and r.json()["status"] == "quarantined"


def test_webhook_replay_ignored():
    buyer, seller = register(), register()
    tx = accepted_escrow(buyer, seller)
    it = _intent(buyer, tx)
    c = new_client()
    ev = _event(9301, it["reference"], it["amount_kobo"])
    r1 = webhook(c, ev)
    assert r1.json()["status"] == "success", r1.text
    assert _status(tx) == "funded"
    assert webhook(c, ev).json()["status"] == "duplicate"
    assert webhook(c, _event(9302, it["reference"], it["amount_kobo"])).json()["status"] == "duplicate"
    db = SessionLocal()
    try:
        assert db.query(PaymentWebhookEvent).filter(PaymentWebhookEvent.reference == it["reference"]).count() == 1
    finally:
        db.close()


# ── B05: session-bound JWT ──
def test_revoked_jwt_rejected_after_logout():
    u = register()
    assert u.get("/auth/me").status_code == 200
    r = u.c.post("/auth/logout", headers=u.h, json={"refresh_token": u.refresh})
    assert r.status_code == 200 and r.json()["revoked"] is True
    assert u.get("/auth/me").status_code == 401
    assert u.c.post("/auth/refresh", json={"refresh_token": u.refresh}).status_code == 401


def test_logout_all_revokes_every_session():
    u = register()
    r = new_client().post("/auth/login", json={"email_or_phone": u.email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    other = r.json()["access_token"]
    assert u.post("/auth/logout-all").status_code == 200
    assert new_client().get("/auth/me", headers={"Authorization": f"Bearer {other}"}).status_code == 401


def test_refresh_token_single_use():
    u = register()
    r = u.c.post("/auth/refresh", json={"refresh_token": u.refresh})
    assert r.status_code == 200, r.text
    assert u.c.post("/auth/refresh", json={"refresh_token": u.refresh}).status_code == 401  # reuse


# ── B06/B07: reset token ──
def test_reset_token_not_returned_and_single_use(monkeypatch):
    import app.routers.auth as auth
    sent = {}
    monkeypatch.setattr(auth.notification_service, "notify_password_reset",
                        lambda email, token, name=None: sent.setdefault("t", token))
    u = register()
    c = new_client()
    r = c.post("/auth/password-reset/request", json={"email_or_phone": u.email})
    assert r.status_code == 200 and sent.get("t")
    assert sent["t"] not in r.text and "token" not in r.json()
    new_pw = "An0therPassw0rd!"
    assert c.post("/auth/password-reset/confirm", json={"token": sent["t"], "new_password": new_pw}).status_code == 200
    assert c.post("/auth/password-reset/confirm", json={"token": sent["t"], "new_password": "Thi3dPassw0rd!"}).status_code == 400
    assert u.get("/auth/me").status_code == 401  # reset revoked old sessions
    assert new_client().post("/auth/login", json={"email_or_phone": u.email, "password": new_pw}).status_code == 200


# ── B02 / B03 ──
def test_kyc_submit_fails_closed():
    u = register(kyc=False)
    r = u.post("/auth/kyc/submit", {"id_type": "nin", "id_number": "12345678901", "phone": u.phone})
    assert r.status_code == 200, r.text
    assert r.json()["kyc_status"] == "pending_verification"
    assert r.json().get("kyc_verified") in (False, None)


def test_generate_account_is_503():
    buyer, seller = register(), register()
    tx = accepted_escrow(buyer, seller)
    assert buyer.post(f"/escrow/{tx}/generate-account").status_code == 503


# ── B09 ──
def test_seller_pickup_cannot_release():
    r = new_client().post("/dispatch/escrow/1/confirm-pickup", json={"otp": "123456"})
    assert r.status_code in (404, 410, 422)
    if r.status_code != 404:
        assert r.status_code in (410, 422)


# ── B11: demo seed ──
def test_demo_seed_refused_in_production(monkeypatch):
    import app.main as m
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("DEALSHIELD_SEED_DEMO", "true")
    monkeypatch.setenv("DEALSHIELD_DEMO_PASSWORD", "LongDemoPassw0rd")
    with pytest.raises(RuntimeError):
        m.seed()
    with pytest.raises(RuntimeError):
        m.validate_runtime_flags()


def test_demo_seed_off_by_default(monkeypatch):
    import app.main as m
    monkeypatch.delenv("DEALSHIELD_SEED_DEMO", raising=False)
    monkeypatch.setenv("ENVIRONMENT", "test")
    m.seed()  # no-op
    db = SessionLocal()
    try:
        assert db.query(User).filter(User.email.like("%demo%")).count() == 0
    finally:
        db.close()


def test_demo_seed_requires_password(monkeypatch):
    import app.main as m
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("DEALSHIELD_SEED_DEMO", "true")
    monkeypatch.delenv("DEALSHIELD_DEMO_PASSWORD", raising=False)
    with pytest.raises(RuntimeError):
        m.seed()
