"""N04: admin dispute settlement - payouts + recorded fees == total held, for every
decision type, facilitated and not; fee lines are added, never overwritten."""
from decimal import Decimal

import pytest

from conftest import SessionLocal, User, register, balance_of
from app.core.settlement import compute_settlement, held_total
from app.models.models import EscrowTransaction
from app.routers.escrow import CANCELLATION_FEE

DECISIONS = [("release_to_seller", None), ("refund_to_buyer", None),
             ("split", "0"), ("split", "33.3"), ("split", "50"), ("split", "100")]


class _Tx:
    def __init__(self, facilitated, amount="250000.00"):
        self.amount = Decimal(amount)
        self.insurance_fee = Decimal("3750.00")
        self.is_facilitated = facilitated
        self.facilitator_fee = Decimal("20000.00") if facilitated else Decimal("0")
        self.commission = Decimal("2500.00")
        self.buyer_gateway_share = Decimal("2050.27")
        self.seller_gateway_share = Decimal("2050.28")


@pytest.mark.parametrize("facilitated", [False, True])
@pytest.mark.parametrize("pct", ["0", "0.01", "12.5", "33.333", "50", "66.67", "99.99", "100"])
@pytest.mark.parametrize("amount", ["250000.00", "4000.00", "1.01"])
def test_pure_settlement_balances(facilitated, pct, amount):
    tx = _Tx(facilitated, amount)
    if Decimal(amount) < Decimal("10000"):
        tx.commission = Decimal("0.01")
        tx.buyer_gateway_share = tx.seller_gateway_share = Decimal("0.01")
    st = compute_settlement(tx, Decimal(pct), CANCELLATION_FEE)
    assert st.assigned == st.held == held_total(tx)
    assert min(st.buyer_refund, st.seller_payout, st.facilitator_payout) >= 0
    assert all(v >= 0 for v in st.fees.values())
    assert st.fees["cancellation_fee"] <= CANCELLATION_FEE


def test_release_matches_normal_release_rule():
    tx = _Tx(False)
    st = compute_settlement(tx, 0, CANCELLATION_FEE)
    assert st.seller_payout == tx.amount - tx.commission - tx.seller_gateway_share
    assert st.buyer_refund == 0 and st.fees["cancellation_fee"] == 0
    ftx = _Tx(True)
    st = compute_settlement(ftx, 0, CANCELLATION_FEE)
    assert st.seller_payout == ftx.amount - ftx.seller_gateway_share  # no commission when facilitated
    assert st.facilitator_payout == Decimal("18000.00")


def _admin():
    a = register()
    db = SessionLocal()
    try:
        db.query(User).filter(User.id == a.id).update({"role": "admin"})
        db.commit()
    finally:
        db.close()
    return a


def _disputed_tx(buyer, seller, facilitator, facilitated):
    t = _Tx(facilitated)
    db = SessionLocal()
    try:
        tx = EscrowTransaction(
            listing_title="Dispute deal", category="cars", amount=t.amount, commission=t.commission,
            status="under_investigation", buyer_id=buyer.id, seller_id=seller.id,
            insurance_fee=t.insurance_fee, is_facilitated=facilitated,
            facilitator_id=facilitator.id if facilitator else None,
            facilitator_fee=t.facilitator_fee, buyer_gateway_share=t.buyer_gateway_share,
            seller_gateway_share=t.seller_gateway_share,
            gateway_fee=t.buyer_gateway_share + t.seller_gateway_share,
            # pre-existing fee line that must be preserved (added to, not overwritten)
            dealshield_cut=Decimal("100.00"), cancellation_fee=Decimal("0.00"),
            facilitator_payout=Decimal("0.00"))
        db.add(tx)
        db.commit()
        return tx.id, held_total(tx)
    finally:
        db.close()


@pytest.mark.parametrize("decision,pct", DECISIONS)
@pytest.mark.parametrize("facilitated", [False, True])
def test_endpoint_payouts_plus_fees_equal_held(decision, pct, facilitated):
    admin, buyer, seller = _admin(), register(), register()
    fac = register() if facilitated else None
    tx_id, held = _disputed_tx(buyer, seller, fac, facilitated)
    before = {u.id: balance_of(u.id) for u in filter(None, (buyer, seller, fac))}
    body = {"decision": decision, "reason": "Evidence reviewed by admin"}
    if pct is not None:
        body["buyer_split_percent"] = pct
    r = admin.post(f"/admin/disputes/{tx_id}/resolve", body)
    assert r.status_code == 200, r.text

    paid = sum(balance_of(uid) - b for uid, b in before.items())
    db = SessionLocal()
    try:
        tx = db.query(EscrowTransaction).filter_by(id=tx_id).one()
        st = compute_settlement(tx, {"release_to_seller": 0, "refund_to_buyer": 100}.get(decision, pct),
                                CANCELLATION_FEE, facilitator_exists=fac is not None)
        recorded_fees = st.dealshield_retained
        assert paid + recorded_fees == held
        # DealShield's recorded lines: pre-existing 100 kept + this settlement's cut and fee
        fac_cut = st.fees.get("facilitator_cut", Decimal(0))
        assert tx.dealshield_cut == Decimal("100.00") + fac_cut + st.fees["cancellation_fee"]
        assert tx.cancellation_fee == st.fees["cancellation_fee"]
        assert balance_of(buyer.id) - before[buyer.id] == st.buyer_refund
        assert balance_of(seller.id) - before[seller.id] == st.seller_payout
        if fac:
            assert balance_of(fac.id) - before[fac.id] == st.facilitator_payout == tx.facilitator_payout
    finally:
        db.close()


def test_split_requires_valid_percent_and_leaves_tx_untouched():
    admin, buyer, seller = _admin(), register(), register()
    tx_id, _ = _disputed_tx(buyer, seller, None, False)
    r = admin.post(f"/admin/disputes/{tx_id}/resolve", {"decision": "split", "reason": "Evidence reviewed",
                                                       "buyer_split_percent": "150"})
    assert r.status_code == 400
    db = SessionLocal()
    try:
        assert db.query(EscrowTransaction.status).filter_by(id=tx_id).scalar() == "under_investigation"
    finally:
        db.close()
