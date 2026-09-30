"""Focused regressions for 2FA throttling and escrow release OTP lockout."""
import os
import tempfile
from pathlib import Path

_DB = Path(tempfile.mkdtemp(prefix="dealshield-otp-test-")) / "test.db"
os.environ.update({
    "DATABASE_URL": f"sqlite:///{_DB}",
    "ENVIRONMENT": "test",
    "SECRET_KEY": "otp-test-secret-key-not-production-1234567890",
    "SAFEPAY_SECRET_KEY": "",
    "SAFEPAY_WEBHOOK_SECRET": "test-webhook-secret",
    "DEALSHIELD_KYC_KEY": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    "SAFEPAY_TEST_MODE": "false",
    "DEALSHIELD_SEED_DEMO": "false",
    "SMTP_HOST": "",
    "SMS_PROVIDER": "none",
})

from fastapi.testclient import TestClient
from app.main import app
from app.core.database import Base, SessionLocal, engine
from app.core.security import create_access_token, get_password_hash
from app.models.models import EscrowTransaction, User

Base.metadata.create_all(bind=engine)


def make_buyer_and_transaction():
    db = SessionLocal()
    suffix = os.urandom(4).hex()
    buyer = User(name="OTP Buyer", phone="0800000" + suffix,
                 email=f"otp-buyer-{suffix}@example.test",
                 hashed_password=get_password_hash("TestPass123!"), is_active=True)
    seller = User(name="OTP Seller", phone="0800001" + suffix,
                  email=f"otp-seller-{suffix}@example.test",
                  hashed_password=get_password_hash("TestPass123!"), is_active=True)
    db.add_all([buyer, seller])
    db.flush()
    tx = EscrowTransaction(listing_title="Test deal", category="cars", amount=1000,
                           commission=0, status="buyer_review", buyer_id=buyer.id,
                           seller_id=seller.id, release_otp="123456",
                           release_otp_attempts=0)
    db.add(tx)
    db.commit()
    ids = buyer.id, seller.id, tx.id
    db.close()
    return ids


def test_2fa_login_is_rate_limited_to_three_per_30m_per_ip():
    ip = "198.51.100.44"
    with TestClient(app, client=(ip, 43210)) as client:
        codes = []
        for _ in range(3):
            response = client.post("/auth/login/2fa", json={"temp_token": "invalid", "totp_code": "000000"})
            codes.append(response.status_code)
        fourth = client.post("/auth/login/2fa", json={"temp_token": "invalid", "totp_code": "000000"})
    assert codes == [401] * 3
    assert fourth.status_code == 429


def test_release_otp_locks_after_five_wrong_attempts_and_persists():
    buyer_id, _, tx_id = make_buyer_and_transaction()
    token = create_access_token(data={"sub": str(buyer_id)})
    with TestClient(app, client=("198.51.100.45", 43210)) as client:
        headers = {"Authorization": f"Bearer {token}"}
        results = [client.post(f"/escrow/{tx_id}/release-otp", json={"otp": "000000"}, headers=headers)
                   for _ in range(5)]
        sixth = client.post(f"/escrow/{tx_id}/release-otp", json={"otp": "000000"}, headers=headers)
    assert [r.status_code for r in results] == [400, 400, 400, 400, 429]
    assert results[-1].headers.get("Retry-After") == "900"
    assert sixth.status_code == 429
    db = SessionLocal()
    try:
        tx = db.query(EscrowTransaction).filter_by(id=tx_id).one()
        assert tx.release_otp_attempts == 5
        assert tx.release_otp_locked_until is not None
        assert tx.status == "buyer_review"
    finally:
        db.close()


def test_only_buyer_can_consume_release_otp_attempts():
    _, seller_id, tx_id = make_buyer_and_transaction()
    token = create_access_token(data={"sub": str(seller_id)})
    with TestClient(app, client=("198.51.100.46", 43210)) as client:
        response = client.post(f"/escrow/{tx_id}/release-otp", json={"otp": "000000"},
                               headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403
    db = SessionLocal()
    try:
        assert db.query(EscrowTransaction.release_otp_attempts).filter_by(id=tx_id).scalar() == 0
    finally:
        db.close()
