"""
DealShield Escrow Router — Full Transaction Lifecycle

Flow:
  CREATED → SELLER_ACCEPTED → PAYMENT_PENDING → FUNDED → SELLER_FULFILLING →
  BUYER_REVIEW → BUYER_APPROVED → RELEASED → CLOSED

  Dispute path: BUYER_REVIEW → DISPUTED → UNDER_INVESTIGATION →
    RELEASED/REFUNDED/SPLIT_RESOLUTION → CLOSED

  Exit paths: CANCELLED (before funding), EXPIRED (deadlines), CLOSED (terminal)
"""
import json
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from datetime import datetime, timedelta
from app.dependencies import get_db
from app.schemas.schemas import EscrowCreate, EscrowShip, EscrowOut, EscrowListResponse, EscrowDispute, EscrowFulfill, FacilitatedDealCreate, FacilitatorAcceptTerms
from app.models.models import EscrowTransaction, User, Listing, WalletTx, AuditLog
from app.routers.auth import get_current_user, _update_badge
from app.core.config import settings
from app.core.notifications import notify_escrow_event
from app.core.security_middleware import sanitize_text

router = APIRouter()

# Insurance fee: 1.5% of item value
INSURANCE_RATE = 0.015

# Deadline constants
ACCEPT_DEADLINE_HOURS = 48       # Seller must accept within 48h
PAYMENT_DEADLINE_HOURS = 24      # Buyer must fund within 24h after seller accepts
BUYER_REVIEW_DAYS = 7            # Auto-release after 7 days if buyer is silent


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
        "accepted_at": tx.accepted_at.isoformat() if tx.accepted_at else None,
        "funded_at": tx.funded_at.isoformat() if tx.funded_at else None,
        "fulfilment_started_at": tx.fulfilment_started_at.isoformat() if tx.fulfilment_started_at else None,
        "buyer_review_started_at": tx.buyer_review_started_at.isoformat() if tx.buyer_review_started_at else None,
        "buyer_review_deadline": tx.buyer_review_deadline.isoformat() if tx.buyer_review_deadline else None,
        "dispute_reason": tx.dispute_reason,
        "dispute_initiated_by": tx.dispute_initiated_by or "",
        "admin_resolution": tx.admin_resolution,
        "admin_reason": tx.admin_reason,
        "resolved_at": tx.resolved_at.isoformat() if tx.resolved_at else None,
        "closed_at": tx.closed_at.isoformat() if tx.closed_at else None,
        "accept_deadline": tx.accept_deadline.isoformat() if tx.accept_deadline else None,
        "payment_deadline": tx.payment_deadline.isoformat() if tx.payment_deadline else None,
        "is_facilitated": tx.is_facilitated,
        "facilitator_id": tx.facilitator_id,
        "facilitator_name": tx.facilitator_name or "",
        "facilitator_fee": tx.facilitator_fee or 0.0,
        "facilitator_fee_pct": tx.facilitator_fee_pct or 0.0,
        "buyer_accepted_terms": tx.buyer_accepted_terms,
        "seller_accepted_terms": tx.seller_accepted_terms,
    }


def _log_audit(db: Session, actor_id: int, action: str, request: Request,
              target_type: str = "escrow_transaction", target_id: int | None = None, details: str = ""):
    log = AuditLog(
        actor_id=actor_id, action=action, target_type=target_type,
        target_id=target_id,
        ip_address=request.client.host if request.client else None,
        details=details,
    )
    db.add(log)


def _calc_commission(category: str, price: float, bag_count: int | None = None) -> float:
    if category == "cement":
        bags = bag_count or 600
        return float(bags * 10)
    if price < 500000:
        return round(price * 0.015, 2)
    elif price < 5000000:
        return round(price * 0.010, 2)
    elif price < 20000000:
        return round(price * 0.005, 2)
    return 100000.0  # Max cap


def _release_funds(db: Session, tx: EscrowTransaction):
    """Release escrow funds to seller (amount - commission).
    If deal is facilitated, facilitator gets their share of the commission.
    """
    seller = db.query(User).filter(User.id == tx.seller_id).first()
    buyer = db.query(User).filter(User.id == tx.buyer_id).first()

    # Calculate facilitator fee (percentage of DealShield commission)
    facilitator_fee = 0.0
    if tx.is_facilitated and tx.facilitator_id and tx.facilitator_fee_pct > 0:
        facilitator_fee = round(tx.commission * (tx.facilitator_fee_pct / 100.0), 2)
        tx.facilitator_fee = facilitator_fee

    # Seller gets amount minus full commission (facilitator paid from commission)
    seller.wallet_balance += tx.amount - tx.commission
    seller.total_deals += 1
    buyer.total_deals += 1
    _update_badge(seller)
    _update_badge(buyer)
    db.add(WalletTx(
        user_id=seller.id, amount=tx.amount - tx.commission, type="escrow_release",
        description=f"Escrow release for {tx.listing_title}"
    ))

    # Pay facilitator their share
    if facilitator_fee > 0 and tx.facilitator_id:
        facilitator = db.query(User).filter(User.id == tx.facilitator_id).first()
        if facilitator:
            facilitator.wallet_balance += facilitator_fee
            db.add(WalletTx(
                user_id=facilitator.id, amount=facilitator_fee, type="facilitator_fee",
                description=f"Facilitator fee for {tx.listing_title} ({tx.facilitator_fee_pct}% of commission)"
            ))


def _refund_buyer(db: Session, tx: EscrowTransaction, partial_amount: float | None = None):
    """Refund buyer (full or partial)."""
    buyer = db.query(User).filter(User.id == tx.buyer_id).first()
    refund = partial_amount if partial_amount is not None else (tx.amount + (tx.insurance_fee or 0))
    buyer.wallet_balance += refund
    db.add(WalletTx(
        user_id=buyer.id, amount=refund, type="escrow_refund",
        description=f"Escrow refund for {tx.listing_title}"
    ))


# ── 1. CREATE — Buyer initiates a deal ──

@router.post("/create", response_model=EscrowOut)
def create_escrow(escrow_in: EscrowCreate, request: Request,
                  current_user: User = Depends(get_current_user),
                  db: Session = Depends(get_db)):
    try:
        listing_id = int(escrow_in.listing_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid listing ID")

    listing = db.query(Listing).filter(Listing.id == listing_id, Listing.is_active == True).first()
    if not listing:
        raise HTTPException(status_code=404, detail="Listing not found or inactive")
    if listing.seller_id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot buy your own listing")

    insurance_fee = 0
    if escrow_in.insured:
        insurance_fee = round(listing.price * INSURANCE_RATE, 2)

    commission = _calc_commission(listing.category, listing.price, escrow_in.bag_count)

    now = datetime.utcnow()
    tx = EscrowTransaction(
        listing_id=listing.id,
        listing_title=listing.title,
        category=listing.category,
        amount=listing.price,
        commission=commission,
        status="created",
        buyer_id=current_user.id,
        seller_id=listing.seller_id,
        buyer_name=current_user.name,
        seller_name=listing.seller_name,
        insured=escrow_in.insured,
        insurance_fee=insurance_fee,
        accept_deadline=now + timedelta(hours=ACCEPT_DEADLINE_HOURS),
    )
    db.add(tx)
    db.flush()

    _log_audit(db, current_user.id, "escrow_create", request, target_id=tx.id,
               details=f"Listing: {listing.title}, Amount: {listing.price}")
    db.commit()
    db.refresh(tx)
    notify_escrow_event(_tx_dict(tx), "escrow_created")
    return _tx_dict(tx)


# ── 2. ACCEPT — Seller accepts the deal ──

@router.post("/{tx_id}/accept", response_model=EscrowOut)
def seller_accept(tx_id: int, request: Request,
                  current_user: User = Depends(get_current_user),
                  db: Session = Depends(get_db)):
    tx = db.query(EscrowTransaction).filter(EscrowTransaction.id == tx_id).first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if tx.seller_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the seller can accept")
    if tx.status != "created":
        raise HTTPException(status_code=400, detail=f"Cannot accept — current status: {tx.status}")

    now = datetime.utcnow()
    if tx.accept_deadline and now > tx.accept_deadline:
        tx.status = "expired"
        tx.closed_at = now
        db.commit()
        raise HTTPException(status_code=400, detail="Accept deadline expired")

    tx.status = "seller_accepted"
    tx.accepted_at = now
    tx.payment_deadline = now + timedelta(hours=PAYMENT_DEADLINE_HOURS)

    _log_audit(db, current_user.id, "escrow_accept", request, target_id=tx.id)
    db.commit()
    db.refresh(tx)
    notify_escrow_event(_tx_dict(tx), "escrow_accepted")
    return _tx_dict(tx)


# ── 3. DECLINE — Seller declines the deal ──

@router.post("/{tx_id}/decline", response_model=EscrowOut)
def seller_decline(tx_id: int, request: Request,
                   current_user: User = Depends(get_current_user),
                   db: Session = Depends(get_db)):
    tx = db.query(EscrowTransaction).filter(EscrowTransaction.id == tx_id).first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if tx.seller_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the seller can decline")
    if tx.status != "created":
        raise HTTPException(status_code=400, detail=f"Cannot decline — current status: {tx.status}")

    now = datetime.utcnow()
    tx.status = "seller_declined"
    tx.closed_at = now

    _log_audit(db, current_user.id, "escrow_decline", request, target_id=tx.id)
    db.commit()
    db.refresh(tx)
    notify_escrow_event(_tx_dict(tx), "escrow_declined")
    return _tx_dict(tx)


# ── 4. FUND — Buyer pays (wallet deduction or external payment) ──

@router.post("/{tx_id}/fund", response_model=EscrowOut)
def fund_escrow(tx_id: int, request: Request,
                current_user: User = Depends(get_current_user),
                db: Session = Depends(get_db)):
    tx = db.query(EscrowTransaction).filter(EscrowTransaction.id == tx_id).first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if tx.buyer_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the buyer can fund")
    if tx.status != "seller_accepted":
        raise HTTPException(status_code=400, detail=f"Cannot fund — current status: {tx.status}")

    now = datetime.utcnow()
    if tx.payment_deadline and now > tx.payment_deadline:
        tx.status = "expired"
        tx.closed_at = now
        db.commit()
        raise HTTPException(status_code=400, detail="Payment deadline expired")

    total = tx.amount + (tx.insurance_fee or 0)
    if current_user.wallet_balance < total:
        raise HTTPException(
            status_code=400,
            detail=f"Insufficient wallet balance. Need ₦{total:,.0f}. Please deposit funds first."
        )

    # Atomic conditional update to prevent double-funding
    rows = db.query(EscrowTransaction).filter(
        EscrowTransaction.id == tx_id,
        EscrowTransaction.status == "seller_accepted",
    ).update({"status": "funded", "funded_at": now}, synchronize_session=False)
    db.flush()
    if rows != 1:
        db.rollback()
        raise HTTPException(status_code=409, detail="Transaction status changed concurrently")

    current_user.wallet_balance -= total
    db.add(WalletTx(
        user_id=current_user.id, amount=-total, type="escrow_hold",
        description=f"Escrow deposit for {tx.listing_title}" + (" (insured)" if tx.insured else "")
    ))

    _log_audit(db, current_user.id, "escrow_fund", request, target_id=tx.id,
               details=f"Amount: {total}")
    db.commit()
    db.refresh(tx)
    notify_escrow_event(_tx_dict(tx), "escrow_funded")
    return _tx_dict(tx)


# ── 5. FULFILL — Seller begins fulfilment ──

@router.post("/{tx_id}/fulfill", response_model=EscrowOut)
def seller_fulfill(tx_id: int, fulfill_in: EscrowFulfill, request: Request,
                   current_user: User = Depends(get_current_user),
                   db: Session = Depends(get_db)):
    tx = db.query(EscrowTransaction).filter(EscrowTransaction.id == tx_id).first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if tx.seller_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the seller can mark fulfilment")
    if tx.status != "funded":
        raise HTTPException(status_code=400, detail=f"Cannot fulfill — current status: {tx.status}")

    now = datetime.utcnow()
    tx.status = "seller_fulfilling"
    tx.fulfilment_started_at = now
    tx.logistics_provider = fulfill_in.logistics_provider
    tx.tracking_number = fulfill_in.tracking_number

    _log_audit(db, current_user.id, "escrow_fulfill", request, target_id=tx.id,
               details=fulfill_in.notes[:200] if fulfill_in.notes else "")
    db.commit()
    db.refresh(tx)
    notify_escrow_event(_tx_dict(tx), "escrow_fulfilling")
    return _tx_dict(tx)


# ── 6. DELIVER — Seller marks goods delivered, buyer review begins ──

@router.post("/{tx_id}/mark-delivered", response_model=EscrowOut)
def mark_delivered(tx_id: int, request: Request,
                   current_user: User = Depends(get_current_user),
                   db: Session = Depends(get_db)):
    tx = db.query(EscrowTransaction).filter(EscrowTransaction.id == tx_id).first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if tx.seller_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the seller can mark as delivered")
    if tx.status != "seller_fulfilling":
        raise HTTPException(status_code=400, detail=f"Cannot deliver — current status: {tx.status}")

    now = datetime.utcnow()
    tx.status = "buyer_review"
    tx.buyer_review_started_at = now
    tx.buyer_review_deadline = now + timedelta(days=BUYER_REVIEW_DAYS)

    _log_audit(db, current_user.id, "escrow_delivered_to_buyer", request, target_id=tx.id)
    db.commit()
    db.refresh(tx)
    notify_escrow_event(_tx_dict(tx), "escrow_buyer_review")
    return _tx_dict(tx)


# ── 7. APPROVE — Buyer approves, funds released ──

@router.post("/{tx_id}/approve", response_model=EscrowOut)
def buyer_approve(tx_id: int, request: Request,
                  current_user: User = Depends(get_current_user),
                  db: Session = Depends(get_db)):
    tx = db.query(EscrowTransaction).filter(EscrowTransaction.id == tx_id).first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if tx.buyer_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the buyer can approve")
    if tx.status != "buyer_review":
        raise HTTPException(status_code=400, detail=f"Cannot approve — current status: {tx.status}")

    now = datetime.utcnow()

    # Atomic conditional update
    rows = db.query(EscrowTransaction).filter(
        EscrowTransaction.id == tx_id,
        EscrowTransaction.status == "buyer_review",
    ).update({"status": "buyer_approved", "completed_at": now}, synchronize_session=False)
    db.flush()
    if rows != 1:
        db.rollback()
        raise HTTPException(status_code=409, detail="Transaction status changed concurrently")
    db.refresh(tx)

    # Release funds to seller
    _release_funds(db, tx)

    # Move to released → closed
    tx.status = "released"
    tx.closed_at = now

    _log_audit(db, current_user.id, "escrow_buyer_approve", request, target_id=tx.id)
    db.commit()
    db.refresh(tx)
    notify_escrow_event(_tx_dict(tx), "escrow_released")
    return _tx_dict(tx)


# ── 8. DISPUTE — Buyer or seller initiates a dispute ──

@router.post("/{tx_id}/dispute", response_model=EscrowOut)
def raise_dispute(tx_id: int, dispute_in: EscrowDispute, request: Request,
                  current_user: User = Depends(get_current_user),
                  db: Session = Depends(get_db)):
    tx = db.query(EscrowTransaction).filter(EscrowTransaction.id == tx_id).first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if current_user.id not in (tx.buyer_id, tx.seller_id):
        raise HTTPException(status_code=403, detail="Not authorized")
    if tx.status not in ("buyer_review", "seller_fulfilling", "funded"):
        raise HTTPException(status_code=400, detail=f"Cannot dispute — current status: {tx.status}")

    tx.status = "disputed"
    tx.dispute_reason = sanitize_text(dispute_in.reason, max_length=1000)
    tx.dispute_evidence = dispute_in.evidence
    tx.dispute_initiated_by = "buyer" if current_user.id == tx.buyer_id else "seller"

    _log_audit(db, current_user.id, "escrow_dispute", request, target_id=tx.id,
               details=f"Initiated by {tx.dispute_initiated_by}: {dispute_in.reason[:200]}")
    db.commit()
    db.refresh(tx)
    notify_escrow_event(_tx_dict(tx), "escrow_disputed")
    return _tx_dict(tx)


# ── 9. CANCEL — Cancel before funding (mutual or unilateral) ──

@router.post("/{tx_id}/cancel", response_model=EscrowOut)
def cancel_escrow(tx_id: int, request: Request,
                  current_user: User = Depends(get_current_user),
                  db: Session = Depends(get_db)):
    tx = db.query(EscrowTransaction).filter(EscrowTransaction.id == tx_id).first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if current_user.id not in (tx.buyer_id, tx.seller_id):
        raise HTTPException(status_code=403, detail="Not authorized")
    if tx.status not in ("created", "seller_accepted", "funded"):
        raise HTTPException(status_code=400, detail=f"Cannot cancel — current status: {tx.status}")

    now = datetime.utcnow()

    # If funded, refund buyer before cancelling
    if tx.status == "funded":
        _refund_buyer(db, tx)

    tx.status = "cancelled"
    tx.closed_at = now

    _log_audit(db, current_user.id, "escrow_cancel", request, target_id=tx.id)
    db.commit()
    db.refresh(tx)
    notify_escrow_event(_tx_dict(tx), "escrow_cancelled")
    return _tx_dict(tx)


# ── 10. AUTO-RELEASE — Process expired buyer review periods ──

@router.post("/process-expired-reviews")
def process_expired_reviews(request: Request,
                            current_user: User = Depends(get_current_user),
                            db: Session = Depends(get_db)):
    """Auto-release funds for transactions where buyer review deadline passed.
    Can be called by any authenticated user (or by a cron job).
    """
    now = datetime.utcnow()
    expired_txs = db.query(EscrowTransaction).filter(
        EscrowTransaction.status == "buyer_review",
        EscrowTransaction.buyer_review_deadline < now,
    ).all()

    released_count = 0
    for tx in expired_txs:
        # Atomic conditional update
        rows = db.query(EscrowTransaction).filter(
            EscrowTransaction.id == tx.id,
            EscrowTransaction.status == "buyer_review",
        ).update({"status": "released", "completed_at": now, "closed_at": now},
                 synchronize_session=False)
        db.flush()
        if rows == 1:
            _release_funds(db, tx)
            released_count += 1
            _log_audit(db, current_user.id, "escrow_auto_release", request,
                       target_id=tx.id, details="Buyer review deadline expired")

    db.commit()
    return {"detail": f"Processed {released_count} expired reviews (auto-released)"}


# ── 11. AUTO-EXPIRE — Process expired accept/payment deadlines ──

@router.post("/process-expired-deadlines")
def process_expired_deadlines(request: Request,
                              current_user: User = Depends(get_current_user),
                              db: Session = Depends(get_db)):
    """Expire transactions where accept or payment deadlines passed."""
    now = datetime.utcnow()
    expired_count = 0

    # Expire unaccepted deals
    unaccepted = db.query(EscrowTransaction).filter(
        EscrowTransaction.status == "created",
        EscrowTransaction.accept_deadline < now,
    ).all()
    for tx in unaccepted:
        tx.status = "expired"
        tx.closed_at = now
        expired_count += 1
        _log_audit(db, 0, "escrow_auto_expire", request, target_id=tx.id,
                   details="Seller did not accept in time")

    # Expire unfunded deals
    unfunded = db.query(EscrowTransaction).filter(
        EscrowTransaction.status == "seller_accepted",
        EscrowTransaction.payment_deadline < now,
    ).all()
    for tx in unfunded:
        tx.status = "expired"
        tx.closed_at = now
        expired_count += 1
        _log_audit(db, 0, "escrow_auto_expire", request, target_id=tx.id,
                   details="Buyer did not fund in time")

    db.commit()
    return {"detail": f"Expired {expired_count} transactions (deadlines passed)"}


# ── 12. CLOSE — Close a released/refunded/split transaction ──

@router.post("/{tx_id}/close", response_model=EscrowOut)
def close_transaction(tx_id: int, request: Request,
                      current_user: User = Depends(get_current_user),
                      db: Session = Depends(get_db)):
    """Close a transaction that has been released, refunded, or split-resolved.
    Only buyer or seller can close their own transactions.
    """
    tx = db.query(EscrowTransaction).filter(EscrowTransaction.id == tx_id).first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if current_user.id not in (tx.buyer_id, tx.seller_id):
        raise HTTPException(status_code=403, detail="Not authorized")
    if tx.status not in ("released", "refunded", "split_resolution", "seller_declined"):
        raise HTTPException(status_code=400, detail=f"Cannot close — current status: {tx.status}")
    if tx.status == "closed":
        return _tx_dict(tx)

    now = datetime.utcnow()
    tx.status = "closed"
    tx.closed_at = now

    _log_audit(db, current_user.id, "escrow_close", request, target_id=tx.id)
    db.commit()
    db.refresh(tx)
    return _tx_dict(tx)


# ── 13. GET TRANSACTIONS — List user's transactions ──

@router.get("/transactions", response_model=EscrowListResponse)
def get_transactions(current_user: User = Depends(get_current_user),
                     db: Session = Depends(get_db)):
    txs = db.query(EscrowTransaction).filter(
        (EscrowTransaction.buyer_id == current_user.id) |
        (EscrowTransaction.seller_id == current_user.id) |
        (EscrowTransaction.facilitator_id == current_user.id)
    ).order_by(EscrowTransaction.created_at.desc()).all()
    return {"transactions": [_tx_dict(t) for t in txs]}


# ── 14. GET SINGLE TRANSACTION ──

@router.get("/{tx_id}", response_model=EscrowOut)
def get_transaction(tx_id: int, current_user: User = Depends(get_current_user),
                    db: Session = Depends(get_db)):
    tx = db.query(EscrowTransaction).filter(EscrowTransaction.id == tx_id).first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if current_user.id not in (tx.buyer_id, tx.seller_id):
        raise HTTPException(status_code=403, detail="Not authorized")
    return _tx_dict(tx)


# ── FACILITATOR ENDPOINTS ──

@router.post("/facilitate/create", response_model=EscrowOut)
def facilitator_create_deal(deal_in: FacilitatedDealCreate, request: Request,
                            current_user: User = Depends(get_current_user),
                            db: Session = Depends(get_db)):
    """Facilitator creates a deal between a buyer and seller.
    No listing required — facilitator sets all terms.
    Both buyer and seller must be registered on DealShield.
    """
    # Validate facilitator fee percentage
    if deal_in.facilitator_fee_pct < 0 or deal_in.facilitator_fee_pct > 50:
        raise HTTPException(status_code=400, detail="Facilitator fee must be between 0% and 50%")

    # Find buyer and seller by phone
    buyer = db.query(User).filter(User.phone == deal_in.buyer_phone, User.is_active == True).first()
    if not buyer:
        raise HTTPException(status_code=404, detail=f"No registered buyer found with phone {deal_in.buyer_phone}")

    seller = db.query(User).filter(User.phone == deal_in.seller_phone, User.is_active == True).first()
    if not seller:
        raise HTTPException(status_code=404, detail=f"No registered seller found with phone {deal_in.seller_phone}")

    if buyer.id == seller.id:
        raise HTTPException(status_code=400, detail="Buyer and seller cannot be the same person")

    if current_user.id in (buyer.id, seller.id):
        raise HTTPException(status_code=400, detail="Facilitator cannot be the buyer or seller")

    # Calculate commission
    commission = _calc_commission(deal_in.category, deal_in.amount)

    insurance_fee = 0
    if deal_in.insured:
        insurance_fee = round(deal_in.amount * INSURANCE_RATE, 2)

    now = datetime.utcnow()
    tx = EscrowTransaction(
        listing_id=0,  # No listing for facilitated deals
        listing_title=sanitize_text(deal_in.title, max_length=200),
        category=deal_in.category,
        amount=deal_in.amount,
        commission=commission,
        status="created",
        buyer_id=buyer.id,
        seller_id=seller.id,
        buyer_name=buyer.name,
        seller_name=seller.name,
        insured=deal_in.insured,
        insurance_fee=insurance_fee,
        is_facilitated=True,
        facilitator_id=current_user.id,
        facilitator_name=current_user.name,
        facilitator_fee_pct=deal_in.facilitator_fee_pct,
        accept_deadline=now + timedelta(hours=ACCEPT_DEADLINE_HOURS),
    )
    db.add(tx)
    db.flush()

    _log_audit(db, current_user.id, "facilitator_create_deal", request, target_id=tx.id,
               details=f"Facilitated deal: {deal_in.title}, Buyer: {buyer.name}, Seller: {seller.name}, Amount: {deal_in.amount}")
    db.commit()
    db.refresh(tx)
    notify_escrow_event(_tx_dict(tx), "escrow_created")
    return _tx_dict(tx)


@router.post("/{tx_id}/accept-terms", response_model=EscrowOut)
def accept_deal_terms(tx_id: int, accept_in: FacilitatorAcceptTerms, request: Request,
                     current_user: User = Depends(get_current_user),
                     db: Session = Depends(get_db)):
    """Buyer or seller accepts the facilitator's deal terms.
    Once both accept, the deal moves to 'seller_accepted' (ready for funding).
    """
    tx = db.query(EscrowTransaction).filter(EscrowTransaction.id == tx_id).first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if not tx.is_facilitated:
        raise HTTPException(status_code=400, detail="This is not a facilitated deal")
    if tx.status != "created":
        raise HTTPException(status_code=400, detail=f"Cannot accept terms — current status: {tx.status}")

    now = datetime.utcnow()

    if accept_in.role == "buyer":
        if current_user.id != tx.buyer_id:
            raise HTTPException(status_code=403, detail="Only the buyer can accept buyer terms")
        if tx.buyer_accepted_terms:
            return _tx_dict(tx)  # Already accepted
        tx.buyer_accepted_terms = True
        tx.buyer_accepted_at = now
        _log_audit(db, current_user.id, "facilitated_buyer_accept", request, target_id=tx.id)

    elif accept_in.role == "seller":
        if current_user.id != tx.seller_id:
            raise HTTPException(status_code=403, detail="Only the seller can accept seller terms")
        if tx.seller_accepted_terms:
            return _tx_dict(tx)  # Already accepted
        tx.seller_accepted_terms = True
        tx.seller_accepted_at = now
        _log_audit(db, current_user.id, "facilitated_seller_accept", request, target_id=tx.id)

    else:
        raise HTTPException(status_code=400, detail="Role must be 'buyer' or 'seller'")

    # If both accepted, move to seller_accepted (ready for funding)
    if tx.buyer_accepted_terms and tx.seller_accepted_terms:
        tx.status = "seller_accepted"
        tx.accepted_at = now
        tx.payment_deadline = now + timedelta(hours=PAYMENT_DEADLINE_HOURS)

    db.commit()
    db.refresh(tx)
    notify_escrow_event(_tx_dict(tx), "escrow_terms_accepted")
    return _tx_dict(tx)


@router.get("/facilitated/transactions", response_model=EscrowListResponse)
def get_facilitated_transactions(current_user: User = Depends(get_current_user),
                                  db: Session = Depends(get_db)):
    """List all deals where the current user is the facilitator."""
    txs = db.query(EscrowTransaction).filter(
        EscrowTransaction.facilitator_id == current_user.id,
        EscrowTransaction.is_facilitated == True,
    ).order_by(EscrowTransaction.created_at.desc()).all()
    return {"transactions": [_tx_dict(t) for t in txs]}


@router.get("/facilitated/stats")
def get_facilitator_stats(current_user: User = Depends(get_current_user),
                          db: Session = Depends(get_db)):
    """Dashboard stats for the facilitator."""
    txs = db.query(EscrowTransaction).filter(
        EscrowTransaction.facilitator_id == current_user.id,
        EscrowTransaction.is_facilitated == True,
    ).all()

    total = len(txs)
    active = sum(1 for t in txs if t.status not in ("closed", "cancelled", "expired", "seller_declined"))
    completed = sum(1 for t in txs if t.status in ("released", "closed"))
    disputed = sum(1 for t in txs if t.status in ("disputed", "under_investigation"))
    total_earned = sum(t.facilitator_fee or 0 for t in txs if t.status in ("released", "closed"))

    pending_acceptance = sum(1 for t in txs if t.status == "created" and
                            not (t.buyer_accepted_terms and t.seller_accepted_terms))

    return {
        "total_deals": total,
        "active_deals": active,
        "completed_deals": completed,
        "disputed_deals": disputed,
        "pending_acceptance": pending_acceptance,
        "total_facilitator_earned": total_earned,
    }


# ── LEGACY ENDPOINTS (backward compat) ──

@router.post("/{tx_id}/mark-shipped", response_model=EscrowOut)
def mark_shipped_legacy(tx_id: int, ship_in: EscrowShip, request: Request,
                        current_user: User = Depends(get_current_user),
                        db: Session = Depends(get_db)):
    """Legacy endpoint — redirects to new fulfill flow."""
    fulfill_in = EscrowFulfill(
        logistics_provider=ship_in.logistics_provider,
        tracking_number=ship_in.tracking_number,
        notes="Legacy mark-shipped",
    )
    # If tx is in old "funds_deposited" status, migrate to new flow
    tx = db.query(EscrowTransaction).filter(EscrowTransaction.id == tx_id).first()
    if tx and tx.status == "funds_deposited":
        tx.status = "funded"
        tx.funded_at = datetime.utcnow()
        db.flush()
    return seller_fulfill(tx_id, fulfill_in, request, current_user, db)


@router.post("/{tx_id}/confirm-receipt", response_model=EscrowOut)
def confirm_receipt_legacy(tx_id: int, request: Request,
                           current_user: User = Depends(get_current_user),
                           db: Session = Depends(get_db)):
    """Legacy endpoint — redirects to new approve flow.
    If tx is in old 'shipped' status, migrate to buyer_review first.
    """
    tx = db.query(EscrowTransaction).filter(EscrowTransaction.id == tx_id).first()
    if tx and tx.status == "shipped":
        now = datetime.utcnow()
        tx.status = "buyer_review"
        tx.buyer_review_started_at = now
        tx.buyer_review_deadline = now + timedelta(days=BUYER_REVIEW_DAYS)
        db.flush()
    return buyer_approve(tx_id, request, current_user, db)
