"""N11: release_otp is visible only to the buyer; the GET /release-otp route is demo-only."""
from datetime import datetime, timedelta

from conftest import SessionLocal, register, accepted_escrow
from app.models.models import EscrowTransaction


def _to_review(buyer, seller):
    tx = accepted_escrow(buyer, seller)
    db = SessionLocal()
    try:
        db.query(EscrowTransaction).filter_by(id=tx).update({
            "status": "buyer_review", "release_otp": "654321",
            "release_otp_expiry": datetime.utcnow() + timedelta(hours=24),
            "is_facilitated": True, "share_token": f"share-{tx}-tok",
        })
        db.commit()
    finally:
        db.close()
    return tx


def test_release_otp_blank_for_everyone_but_buyer():
    buyer, seller, other = register(), register(), register()
    tx = _to_review(buyer, seller)
    assert buyer.get(f"/escrow/{tx}").json()["release_otp"] == "654321"
    assert seller.get(f"/escrow/{tx}").json()["release_otp"] == ""
    seller_list = seller.get("/escrow/transactions").json()["transactions"]
    assert all(t["release_otp"] == "" for t in seller_list)
    assert other.get(f"/escrow/{tx}").status_code == 403
    shared = other.c.get(f"/escrow/shared/share-{tx}-tok")
    assert shared.status_code == 200
    assert "654321" not in shared.text and "release_otp" not in shared.json()


def test_get_release_otp_404_outside_demo(monkeypatch):
    buyer, seller = register(), register()
    tx = _to_review(buyer, seller)
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("SAFEPAY_TEST_MODE", "false")
    assert buyer.get(f"/escrow/{tx}/release-otp").status_code == 404
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("SAFEPAY_TEST_MODE", "true")
    assert buyer.get(f"/escrow/{tx}/release-otp").status_code == 404


def test_get_release_otp_demo_mode_buyer_only(monkeypatch):
    buyer, seller = register(), register()
    tx = _to_review(buyer, seller)
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("SAFEPAY_TEST_MODE", "true")
    r = buyer.get(f"/escrow/{tx}/release-otp")
    assert r.status_code == 200 and r.json()["otp"] == "654321"
    assert seller.get(f"/escrow/{tx}/release-otp").status_code == 403
