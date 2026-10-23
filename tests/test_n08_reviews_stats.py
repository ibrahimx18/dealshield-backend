"""N08: reviews allowed on released/closed (and legacy delivered); stats use current statuses."""
import pytest

from conftest import SessionLocal, register, accepted_escrow, new_client
from app.models.models import EscrowTransaction


def _set_status(tx, status):
    db = SessionLocal()
    try:
        db.query(EscrowTransaction).filter_by(id=tx).update({"status": status})
        db.commit()
    finally:
        db.close()


@pytest.mark.parametrize("status", ["released", "closed", "delivered"])
def test_review_allowed_on_completed(status):
    buyer, seller = register(), register()
    tx = accepted_escrow(buyer, seller)
    _set_status(tx, status)
    r = buyer.post("/reviews", {"escrow_id": tx, "rating": 5, "comment": "Great"})
    assert r.status_code == 200, r.text
    assert r.json()["reviewee_id"] == seller.id


@pytest.mark.parametrize("status", ["funded", "buyer_review", "disputed", "refunded"])
def test_review_refused_on_unfinished(status):
    buyer, seller = register(), register()
    tx = accepted_escrow(buyer, seller)
    _set_status(tx, status)
    assert buyer.post("/reviews", {"escrow_id": tx, "rating": 4}).status_code == 400


def test_deal_stats_use_current_statuses(monkeypatch):
    import app.routers.integrations as integ
    monkeypatch.setattr(integ, "INTEGRATION_SECRET", "n08-secret")
    c = new_client()
    h = {"X-SafePay-Secret": "n08-secret"}
    before = c.get("/api/stats/deals?days=1", headers=h).json()
    buyer, seller = register(), register()
    for status in ("released", "closed", "buyer_review", "funded", "under_investigation"):
        _set_status(accepted_escrow(buyer, seller), status)
    after = c.get("/api/stats/deals?days=1", headers=h).json()
    assert after["completed_deals"] - before["completed_deals"] == 2
    assert after["active_deals"] - before["active_deals"] == 2
    assert after["disputed_deals"] - before["disputed_deals"] == 1
