from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from datetime import datetime
from app.dependencies import get_db
from app.schemas.schemas import EscrowCreate, EscrowShip, EscrowOut, EscrowListResponse
from app.models.models import EscrowTransaction, User, Listing, WalletTx
from app.routers.auth import get_current_user
from app.core.config import settings
from app.core.notifications import notify_escrow_event

router = APIRouter()

# Insurance fee: 1.5% of item value
INSURANCE_RATE = 0.015

def _tx_dict(tx: EscrowTransaction) -> dict:
    return {
        "id": tx.id,
        "listing_id": tx.listing_id,
        "listing_title": tx.listing_title,
        "category": tx.category,
        "amount": tx.amount,
        "commission": tx.commission,
        "status": tx.status,
        "buyer_id": tx.buyer_id,
        "seller_id": tx.seller_id,
        "buyer_name": tx.buyer_name,
        "seller_name": tx.seller_name,
        "created_at": tx.created_at.isoformat() if tx.created_at else None,
        "completed_at": tx.completed_at.isoformat() if tx.completed_at else None,
        "insured": tx.insured,
        "logistics_provider": tx.logistics_provider or "",
        "tracking_number": tx.tracking_number or "",
        "insurance_fee": tx.insurance_fee or 0,
    }

@router.post("/create", response_model=EscrowOut)
def create_escrow(escrow_in: EscrowCreate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    try:
        listing_id = int(escrow_in.listing_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid listing ID")

    listing = db.query(Listing).filter(Listing.id == listing_id).first()
    if not listing:
        raise HTTPException(status_code=404, detail="Listing not found")
    if listing.seller_id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot buy your own listing")

    total = listing.price
    # Add insurance fee if requested
    insurance_fee = 0
    if escrow_in.insured:
        insurance_fee = round(total * INSURANCE_RATE, 2)
        total += insurance_fee

    if current_user.wallet_balance < total:
        raise HTTPException(status_code=400, detail=f"Insufficient wallet balance. Need ₦{total:,.0f} (item + insurance). Please deposit funds first.")

    current_user.wallet_balance -= total
    commission = round(listing.price * settings.COMMISSION_RATE, 2)

    tx = EscrowTransaction(
        listing_id=listing.id,
        listing_title=listing.title,
        category=listing.category,
        amount=listing.price,
        commission=commission,
        status="funds_deposited",
        buyer_id=current_user.id,
        seller_id=listing.seller_id,
        buyer_name=current_user.name,
        seller_name=listing.seller_name,
        insured=escrow_in.insured,
        insurance_fee=insurance_fee,
    )
    db.add(tx)
    db.add(WalletTx(user_id=current_user.id, amount=-total, type="escrow_hold",
                    description=f"Escrow deposit for {listing.title}" + (" (insured)" if escrow_in.insured else "")))
    db.commit()
    db.refresh(tx)
    notify_escrow_event(_tx_dict(tx), "escrow_created")
    return _tx_dict(tx)

@router.get("/transactions", response_model=EscrowListResponse)
def get_transactions(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    txs = db.query(EscrowTransaction).filter(
        (EscrowTransaction.buyer_id == current_user.id) |
        (EscrowTransaction.seller_id == current_user.id)
    ).order_by(EscrowTransaction.created_at.desc()).all()
    return {"transactions": [_tx_dict(t) for t in txs]}

@router.get("/{tx_id}", response_model=EscrowOut)
def get_transaction(tx_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    tx = db.query(EscrowTransaction).filter(EscrowTransaction.id == tx_id).first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if current_user.id not in (tx.buyer_id, tx.seller_id):
        raise HTTPException(status_code=403, detail="Not authorized")
    return _tx_dict(tx)

@router.post("/{tx_id}/mark-shipped", response_model=EscrowOut)
def mark_shipped(tx_id: int, ship_in: EscrowShip, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    tx = db.query(EscrowTransaction).filter(EscrowTransaction.id == tx_id).first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if tx.seller_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only seller can mark as shipped")
    if tx.status != "funds_deposited":
        raise HTTPException(status_code=400, detail="Can only ship after funds deposited")
    tx.status = "shipped"
    tx.logistics_provider = ship_in.logistics_provider
    tx.tracking_number = ship_in.tracking_number
    db.commit()
    db.refresh(tx)
    notify_escrow_event(_tx_dict(tx), "escrow_shipped")
    return _tx_dict(tx)

@router.post("/{tx_id}/confirm-receipt", response_model=EscrowOut)
def confirm_receipt(tx_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    tx = db.query(EscrowTransaction).filter(EscrowTransaction.id == tx_id).first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if tx.buyer_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only buyer can confirm receipt")
    if tx.status != "shipped":
        raise HTTPException(status_code=400, detail="Can only confirm after shipping")
    # Release funds to seller (amount - commission)
    seller = db.query(User).filter(User.id == tx.seller_id).first()
    seller.wallet_balance += tx.amount - tx.commission
    tx.status = "delivered"
    tx.completed_at = datetime.utcnow()
    seller.total_deals += 1
    buyer = db.query(User).filter(User.id == tx.buyer_id).first()
    buyer.total_deals += 1
    # Update badge tiers
    from app.routers.auth import _update_badge
    _update_badge(seller)
    _update_badge(buyer)
    db.add(WalletTx(user_id=seller.id, amount=tx.amount - tx.commission, type="escrow_release",
                    description=f"Escrow release for {tx.listing_title}"))
    db.commit()
    db.refresh(tx)
    notify_escrow_event(_tx_dict(tx), "escrow_delivered")
    return _tx_dict(tx)

@router.post("/{tx_id}/dispute", response_model=EscrowOut)
def dispute(tx_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    tx = db.query(EscrowTransaction).filter(EscrowTransaction.id == tx_id).first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if current_user.id not in (tx.buyer_id, tx.seller_id):
        raise HTTPException(status_code=403, detail="Not authorized")
    if tx.status not in ("funds_deposited", "shipped"):
        raise HTTPException(status_code=400, detail="Cannot dispute at this stage")
    tx.status = "disputed"
    db.commit()
    db.refresh(tx)
    notify_escrow_event(_tx_dict(tx), "escrow_disputed")
    return _tx_dict(tx)

@router.post("/{tx_id}/cancel", response_model=EscrowOut)
def cancel(tx_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    tx = db.query(EscrowTransaction).filter(EscrowTransaction.id == tx_id).first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if current_user.id not in (tx.buyer_id, tx.seller_id):
        raise HTTPException(status_code=403, detail="Not authorized")
    if tx.status in ("shipped", "delivered", "disputed"):
        raise HTTPException(status_code=400, detail="Cannot cancel at this stage")
    # Refund buyer (item + insurance)
    buyer = db.query(User).filter(User.id == tx.buyer_id).first()
    refund = tx.amount + (tx.insurance_fee or 0)
    buyer.wallet_balance += refund
    tx.status = "cancelled"
    tx.completed_at = datetime.utcnow()
    db.add(WalletTx(user_id=buyer.id, amount=refund, type="escrow_refund",
                    description=f"Escrow refund for {tx.listing_title}"))
    db.commit()
    db.refresh(tx)
    notify_escrow_event(_tx_dict(tx), "escrow_cancelled")
    return _tx_dict(tx)
