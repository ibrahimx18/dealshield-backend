"""Isolated test harness: disposable SQLite file, dummy secrets, no network.

Environment is fixed BEFORE the app is imported (settings/flags are read at import).
"""
import hashlib
import hmac
import itertools
import json
import os
import socket
import tempfile

_TMP = tempfile.mkdtemp(prefix="ds-tests-")
os.environ.update({
    "DATABASE_URL": "sqlite:///" + os.path.join(_TMP, "test.db").replace("\\", "/"),
    "ENVIRONMENT": "test",
    "SECRET_KEY": "test-secret-key-not-real-0123456789abcdef",
    "SAFEPAY_SECRET_KEY": "",
    "WEBHOOK_SECRET": "test-webhook-secret",
    "SAFEPAY_WEBHOOK_SECRET": "",
    "PAYSTACK_SECRET_KEY": "sk_test_dummy_not_real",
    "FLUTTERWAVE_SECRET_KEY": "",
    "DEALSHIELD_KYC_KEY": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    "SAFEPAY_TEST_MODE": "false",
    "DEALSHIELD_SEED_DEMO": "false",
    "SMTP_HOST": "",
    "SMS_PROVIDER": "none",
})

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Any outbound socket connection fails the test (providers must be mocked)."""
    real_connect = socket.socket.connect

    def guard(sock, addr, *a, **k):
        host = addr[0] if isinstance(addr, tuple) else addr
        if host in ("127.0.0.1", "::1", "localhost"):  # asyncio self-pipe on Windows
            return real_connect(sock, addr, *a, **k)
        raise RuntimeError("network access is disabled in tests")

    def guard_cc(addr, *a, **k):
        raise RuntimeError("network access is disabled in tests")
    monkeypatch.setattr(socket.socket, "connect", guard)
    monkeypatch.setattr(socket, "create_connection", guard_cc)


from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app.core.database import SessionLocal, engine  # noqa: E402
from app.models.models import User  # noqa: E402
from app.core.wallet import credit_wallet  # noqa: E402

PASSWORD = "Str0ngPassw0rd!"
_ip = itertools.count(1)
_n = itertools.count(1)


def new_client() -> TestClient:
    """Fresh client with its own IP so the per-IP auth rate limiter does not interfere."""
    i = next(_ip)
    return TestClient(app, client=(f"10.{i // 250}.{i % 250}.1", 50000))


class U:
    def __init__(self, client, data, email, phone):
        self.c = client
        self.id = data["user"]["id"]
        self.access = data["access_token"]
        self.refresh = data["refresh_token"]
        self.email = email
        self.phone = phone

    @property
    def h(self):
        return {"Authorization": f"Bearer {self.access}"}

    def get(self, url, **kw):
        return self.c.get(url, headers=self.h, **kw)

    def post(self, url, json=None, **kw):
        return self.c.post(url, headers=self.h, json=json, **kw)


def register(kyc: bool = True, balance: str | None = None) -> U:
    n = next(_n)
    c = new_client()
    email, phone = f"user{n}@example.com", f"0800000{n:04d}"
    r = c.post("/auth/register", json={"name": f"User {n}", "phone": phone, "email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    u = U(c, r.json(), email, phone)
    db = SessionLocal()
    try:
        if kyc:  # simulate an approved reviewer decision
            db.query(User).filter(User.id == u.id).update({"kyc_verified": True, "kyc_status": "verified"})
        if balance:
            credit_wallet(db, u.id, balance)
        db.commit()
    finally:
        db.close()
    return u


def balance_of(user_id: int):
    db = SessionLocal()
    try:
        return db.query(User.wallet_balance).filter(User.id == user_id).scalar()
    finally:
        db.close()


def make_listing(seller: U, price="100000.00", category="cars") -> int:
    r = seller.post("/listings", {"category": category, "title": "Test item", "price": price})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def accepted_escrow(buyer: U, seller: U, price="100000.00") -> int:
    lid = make_listing(seller, price)
    r = buyer.post("/escrow/create", {"listing_id": lid})
    assert r.status_code == 200, r.text
    tx = r.json()["id"]
    r = seller.post(f"/escrow/{tx}/accept")
    assert r.status_code == 200, r.text
    return tx


def sign(body: bytes) -> str:
    return hmac.new(os.environ["PAYSTACK_SECRET_KEY"].encode(), body, hashlib.sha512).hexdigest()


def webhook(client, payload: dict, signature: str | None = None):
    body = json.dumps(payload).encode()
    return client.post("/escrow/webhook/payment", content=body,
                       headers={"x-paystack-signature": signature or sign(body),
                                "content-type": "application/json"})
