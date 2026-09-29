"""Dispatch router — rider assignment + OTP pickup verification.

Flow:
1. Seller marks shipped → generates 4-digit OTP, assigns rider
2. Rider goes to warehouse, provides OTP
3. Warehouse confirms OTP → order COMPLETED → funds released
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.dependencies import get_db
from app.routers.auth import get_current_user
from app.models.models import User, EscrowTransaction
from app.schemas.schemas import DispatchRider, PickupConfirm
from app.core.money import money_out

router = APIRouter()

# Audit C5: OTP hardening constants.
OTP_MAX_ATTEMPTS = 5
OTP_EXPIRY_HOURS = 24


@router.post("/escrow/{tx_id}/dispatch")
def assign_rider(tx_id: int, rider: DispatchRider, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """B09: legacy dispatch settlement disabled."""
    raise HTTPException(status_code=410, detail="Legacy dispatch settlement is disabled. Use the seller fulfilment and buyer approval flow.")


@router.post("/escrow/{tx_id}/confirm-pickup")
def confirm_pickup(tx_id: int, pickup: PickupConfirm, db: Session = Depends(get_db)):
    """B09: a seller-controlled pickup OTP can never release funds. Buyer approval
    (/escrow/{id}/approve or /release-otp) or admin adjudication is required."""
    raise HTTPException(status_code=410, detail="Pickup OTP cannot authorize settlement. The buyer must approve receipt through escrow.")


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
        "amount": money_out(tx.amount),
        "rider_name": tx.rider_name or "",
        "rider_phone": tx.rider_phone or "",
        "pickup_otp": tx.pickup_otp if tx.seller_id == current_user.id else "",  # only seller sees OTP
        "pickup_confirmed": tx.pickup_confirmed,
        "status": tx.status,
        "dispatched_at": tx.dispatched_at.isoformat() if tx.dispatched_at else None,
    }
