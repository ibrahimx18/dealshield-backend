"""Dispatch router — rider assignment + OTP pickup verification.

Flow:
1. Seller marks shipped → generates 4-digit OTP, assigns rider
2. Rider goes to warehouse, provides OTP
3. Warehouse confirms OTP → order COMPLETED → funds released
"""

import secrets
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.dependencies import get_db
from app.routers.auth import get_current_user
from app.models.models import User, EscrowTransaction
from app.schemas.schemas import DispatchRider, PickupConfirm

router = APIRouter()

# Audit C5: OTP hardening constants.
OTP_MAX_ATTEMPTS = 5
OTP_EXPIRY_HOURS = 24


@router.post("/escrow/{tx_id}/dispatch")
def assign_rider(tx_id: int, rider: DispatchRider, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Seller assigns a dispatch rider and generates pickup OTP."""
    tx = db.query(EscrowTransaction).filter(EscrowTransaction.id == tx_id).first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if tx.seller_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the seller can assign a rider")
    if tx.status not in ("funds_deposited", "shipped"):
        raise HTTPException(status_code=400, detail=f"Cannot dispatch in {tx.status} state")

    # Generate 6-digit OTP using a CSPRNG (audit C5: was random.randint, 4-digit).
    otp = str(secrets.randbelow(1_000_000)).zfill(6)
    tx.pickup_otp = otp
    tx.otp_attempts = 0
    tx.otp_locked = False
    tx.rider_name = rider.rider_name
    tx.rider_phone = rider.rider_phone
    tx.logistics_provider = rider.logistics_provider or tx.logistics_provider
    tx.status = "shipped"
    tx.dispatched_at = datetime.utcnow()
    db.commit()

    return {
        "message": "Rider assigned. OTP generated.",
        "otp": otp,
        "rider_name": rider.rider_name,
        "rider_phone": rider.rider_phone,
        "order_ref": f"SP-{tx.id}",
        "item": tx.listing_title,
        "amount": tx.amount,
        "warehouse": "Main Warehouse",
    }


@router.post("/escrow/{tx_id}/confirm-pickup")
def confirm_pickup(tx_id: int, pickup: PickupConfirm, db: Session = Depends(get_db)):
    """Rider or warehouse confirms pickup with OTP. No auth needed — OTP is the auth."""
    tx = db.query(EscrowTransaction).filter(EscrowTransaction.id == tx_id).first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if tx.status != "shipped":
        raise HTTPException(status_code=400, detail=f"Order is {tx.status}, not shipped")

    # Audit C5: OTP hardening — lockout after too many wrong attempts.
    if tx.otp_locked:
        raise HTTPException(status_code=423, detail="OTP locked after too many failed attempts. Contact support.")

    # Audit C5: OTP expiry — 24h after dispatch.
    if tx.dispatched_at and datetime.utcnow() - tx.dispatched_at > timedelta(hours=OTP_EXPIRY_HOURS):
        raise HTTPException(status_code=400, detail="OTP has expired. Ask the seller to re-dispatch.")

    if tx.pickup_otp != pickup.otp:
        tx.otp_attempts = (tx.otp_attempts or 0) + 1
        if tx.otp_attempts >= OTP_MAX_ATTEMPTS:
            tx.otp_locked = True
            db.commit()
            raise HTTPException(status_code=423, detail="Too many wrong OTP attempts. OTP is now locked; contact support.")
        db.commit()
        raise HTTPException(status_code=400, detail="Invalid OTP")
    if tx.rider_phone and tx.rider_phone != pickup.rider_phone:
        raise HTTPException(status_code=400, detail="Rider phone does not match")

    # Atomic conditional update — prevents double-release race (audit C4).
    # Read-check-mutate replaced with a single UPDATE ... WHERE status = 'shipped'
    # so only one concurrent confirm-pickup request can claim the row.
    # NOTE: SQLite is dev-only; see escrow.py confirm_receipt() note re: Postgres locking.
    completed_at = datetime.utcnow()
    rows = db.query(EscrowTransaction).filter(
        EscrowTransaction.id == tx_id,
        EscrowTransaction.status == "shipped",
    ).update({"status": "delivered", "completed_at": completed_at, "pickup_confirmed": True}, synchronize_session=False)
    db.flush()
    if rows != 1:
        db.rollback()
        raise HTTPException(status_code=409, detail="Transaction status changed concurrently; refusing to double-release funds")
    db.refresh(tx)

    # Release funds to seller — only reached once the conditional update
    # above has claimed the row exactly once.
    seller = db.query(User).filter(User.id == tx.seller_id).first()
    if seller:
        seller.wallet_balance += tx.amount - tx.commission
        seller.total_deals += 1
        # Update badge
        if seller.total_deals >= 10 and seller.bvn_verified and seller.business_verified:
            seller.badge_tier = "top_dealer"
        elif seller.bvn_verified:
            seller.badge_tier = "trusted"
        elif seller.nin_verified:
            seller.badge_tier = "verified"

    db.commit()

    return {
        "message": "Pickup confirmed. Funds released to seller.",
        "order_ref": f"SP-{tx.id}",
        "status": "delivered",
        "seller_paid": tx.amount - tx.commission,
    }


@router.get("/escrow/{tx_id}/dispatch-info")
def get_dispatch_info(tx_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Get dispatch details for a transaction (buyer or seller only)."""
    tx = db.query(EscrowTransaction).filter(EscrowTransaction.id == tx_id).first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if tx.buyer_id != current_user.id and tx.seller_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized")

    return {
        "order_ref": f"SP-{tx.id}",
        "item": tx.listing_title,
        "amount": tx.amount,
        "rider_name": tx.rider_name or "",
        "rider_phone": tx.rider_phone or "",
        "pickup_otp": tx.pickup_otp if tx.seller_id == current_user.id else "",  # only seller sees OTP
        "pickup_confirmed": tx.pickup_confirmed,
        "status": tx.status,
        "dispatched_at": tx.dispatched_at.isoformat() if tx.dispatched_at else None,
    }
