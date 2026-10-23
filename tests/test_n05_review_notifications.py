"""N05: buyer is notified when the 24-hour review window starts, reminded shortly
before auto-release (once), and told when funds were auto-released."""
from datetime import datetime, timedelta

import pytest

from conftest import SessionLocal, register, accepted_escrow
import app.core.notifications as notifications
from app.models.models import EscrowTransaction
from app.routers.escrow import BUYER_REVIEW_DAYS


@pytest.fixture
def sent(monkeypatch):
    calls = []
    monkeypatch.setattr(notifications.notification_service, "notify_escrow_status",
                        lambda phone, email, title, status, role: calls.append((email, status, role)))
    return calls


def _to_review(buyer, seller):
    tx = accepted_escrow(buyer, seller)
    assert buyer.post(f"/escrow/{tx}/fund").status_code == 200
    assert seller.post(f"/escrow/{tx}/fulfill", {"logistics_provider": "GIG", "tracking_number": "T1"}).status_code == 200
    r = seller.post(f"/escrow/{tx}/mark-delivered")
    assert r.status_code == 200, r.text
    return tx


def test_review_window_is_24_hours():
    assert BUYER_REVIEW_DAYS == 1


def test_buyer_notified_when_review_starts(sent):
    buyer, seller = register(balance="1000000.00"), register()
    _to_review(buyer, seller)
    assert (buyer.email, "buyer_review", "buyer") in sent


def test_reminder_once_then_auto_release_notice(sent):
    buyer, seller = register(balance="1000000.00"), register()
    tx = _to_review(buyer, seller)
    db = SessionLocal()
    try:  # deadline 1 hour away -> inside the reminder window
        db.query(EscrowTransaction).filter_by(id=tx).update(
            {"buyer_review_deadline": datetime.utcnow() + timedelta(hours=1)})
        db.commit()
    finally:
        db.close()
    sent.clear()
    assert buyer.post("/escrow/process-expired-reviews").status_code == 200
    assert buyer.post("/escrow/process-expired-reviews").status_code == 200
    assert [s for s in sent if s[1] == "buyer_review_reminder"] == [(buyer.email, "buyer_review_reminder", "buyer")]

    db = SessionLocal()
    try:
        db.query(EscrowTransaction).filter_by(id=tx).update(
            {"buyer_review_deadline": datetime.utcnow() - timedelta(minutes=1)})
        db.commit()
    finally:
        db.close()
    r = buyer.post("/escrow/process-expired-reviews")
    assert r.status_code == 200 and r.json()["released"] >= 1
    assert (buyer.email, "auto_released", "buyer") in sent


def test_template_text_says_24_hours(monkeypatch):
    msgs = []
    svc = notifications.NotificationService()
    monkeypatch.setattr(svc, "send_sms", lambda phone, m: msgs.append(m))
    monkeypatch.setattr(svc, "send_email", lambda *a, **k: None)
    svc.notify_escrow_status("080", "a@b.c", "Car", "buyer_review", "buyer")
    assert "24 hours" in msgs[0] and "7 days" not in msgs[0]
