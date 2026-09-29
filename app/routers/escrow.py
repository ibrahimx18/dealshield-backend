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
import random  # only used by the legacy account generator (removed in B03 commit)
import hashlib
import hmac
import secrets
from decimal import Decimal
from fastapi import APIRouter, Depends, HTTPException, Request, Header
from sqlalchemy.orm import Session
from datetime import datetime, timedelta, timezone


def _utcnow():
    """Return timezone-naive UTC datetime to match PostgreSQL columns."""
    return datetime.utcnow()


def _strip_tz(dt):
    """Strip timezone info from a datetime for safe comparison."""
    if dt is None:
        return None
    if hasattr(dt, 'tzinfo') and dt.tzinfo is not None:
        return dt.replace(tzinfo=None)
    return dt
from app.dependencies import get_db
from app.schemas.schemas import (
    EscrowCreate, EscrowShip, EscrowOut, EscrowListResponse, EscrowDispute,
    EscrowFulfill, FacilitatedDealCreate, FacilitatorAcceptTerms, VirtualAccountOut,
    ReleaseOTPRequest,
)
from app.models.models import (EscrowTransaction, User, Listing, WalletTx, AuditLog, VirtualAccount,
                               PaymentReference, PaymentWebhookEvent, WebhookQuarantine)
from sqlalchemy.exc import IntegrityError
from app.routers.auth import get_current_user, _update_badge
from app.core.config import settings
from app.core.notifications import notify_escrow_event
from app.core.security_middleware import sanitize_text
from app.core.money import q, require_amount, money_out, to_decimal, ZERO, MAX_AMOUNT
from app.core.wallet import debit_wallet, credit_wallet

router = APIRouter()

# Insurance fee: 1.5% of item value
INSURANCE_RATE = Decimal("0.015")
# Gateway fee: 1.5% of total (Paystack/Flutterwave standard), capped at ₦50,000
GATEWAY_FEE_RATE = Decimal("0.015")
GATEWAY_FEE_CAP = Decimal("50000.00")

# Cancellation/Dispute flat fee — DealShield charges ₦5,000 when a funded deal
# is cancelled or a dispute results in refund/split. Deducted from buyer's refund.
CANCELLATION_FEE = Decimal("5000.00")

# Deadline constants
ACCEPT_DEADLINE_HOURS = 48       # Seller must accept within 48h
PAYMENT_DEADLINE_HOURS = 24      # Buyer must fund within 24h after seller accepts
BUYER_REVIEW_DAYS = 1             # Auto-release after 24 hours if buyer is silent


def _calc_gateway_fee(amount) -> Decimal:
    """Payment gateway fee: 1.5% of amount, capped at NGN 50,000 (Decimal, 2dp)."""
    return q(min(to_decimal(amount) * GATEWAY_FEE_RATE, GATEWAY_FEE_CAP))


def _new_otp() -> str:
    return str(secrets.randbelow(1_000_000)).zfill(6)


def _transition(db: Session, tx: EscrowTransaction, values: dict, from_status: str | None = None):
    """Conditional state change: UPDATE ... WHERE id=:id AND status=:current.
    Raises 409 if another request changed the row first (B08)."""
    current = from_status or tx.status
    rows = db.query(EscrowTransaction).filter(
        EscrowTransaction.id == tx.id, EscrowTransaction.status == current,
    ).update(values, synchronize_session=False)
    if rows != 1:
        db.rollback()
        raise HTTPException(status_code=409, detail="Transaction status changed concurrently")
    db.flush()
    db.refresh(tx)


def _buyer_total(tx: EscrowTransaction) -> Decimal:
    """Principal (+facilitator fee) + insurance, before gateway share."""
    total = to_decimal(tx.amount) + to_decimal(tx.insurance_fee)
    if tx.is_facilitated:
        total += to_decimal(tx.facilitator_fee)
    return q(total)


def _funding_quote(tx: EscrowTransaction) -> dict:
    """Server-authoritative funding amounts with invariant checks (B01)."""
    require_amount(tx.amount, what="Deal amount")
    require_amount(tx.insurance_fee or 0, allow_zero=True, what="Insurance fee")
    require_amount(tx.facilitator_fee or 0, allow_zero=True, what="Facilitator fee")
    require_amount(tx.commission or 0, allow_zero=True, what="Commission")
    total = _buyer_total(tx)
    gateway_fee = _calc_gateway_fee(total)
    buyer_share = q(gateway_fee / 2)
    seller_share = q(gateway_fee - buyer_share)
    commission = ZERO if tx.is_facilitated else to_decimal(tx.commission)
    seller_payout = q(to_decimal(tx.amount) - commission - seller_share)
    if seller_payout < 0:
        raise HTTPException(status_code=400, detail="Fees must not exceed the seller payout")
    total_with_gateway = require_amount(total + buyer_share, what="Funding total")
    return {"total": total, "gateway_fee": gateway_fee, "buyer_gateway_share": buyer_share,
            "seller_gateway_share": seller_share, "total_with_gateway": total_with_gateway,
            "seller_payout": seller_payout}


def _tx_dict(tx: EscrowTransaction, viewer_id: int | None = None) -> dict:
    m = money_out
    return {
        "id": tx.id,
        "listing_id": tx.listing_id,
        "listing_title": tx.listing_title,
        "category": tx.category,
        "amount": m(tx.amount),
        "commission": m(tx.commission),
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
        "insurance_fee": m(tx.insurance_fee),
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
        "facilitator_fee": m(tx.facilitator_fee),
        "dealshield_cut": m(tx.dealshield_cut),
        "facilitator_payout": m(tx.facilitator_payout),
        "cancellation_fee": m(tx.cancellation_fee),
        "gateway_fee": m(tx.gateway_fee),
        "buyer_gateway_share": m(tx.buyer_gateway_share),
        "seller_gateway_share": m(tx.seller_gateway_share),
        "buyer_accepted_terms": tx.buyer_accepted_terms,
        "seller_accepted_terms": tx.seller_accepted_terms,
        # B16: the release OTP is only ever shown to the buyer.
        "release_otp": (tx.release_otp or "") if viewer_id is not None and viewer_id == tx.buyer_id else "",
        "release_otp_expiry": tx.release_otp_expiry.isoformat() if tx.release_otp_expiry else None,
        "share_token": tx.share_token or "",
    }


def _log_audit(db: Session, actor_id: int | None, action: str, request: Request | None,
              target_type: str = "escrow_transaction", target_id: int | None = None, details: str = ""):
    # B18: system actions use actor_id=None (never 0, which violates the FK).
    log = AuditLog(
        actor_id=actor_id, action=action, target_type=target_type,
        target_id=target_id,
        ip_address=request.client.host if request is not None and request.client else None,
        details=details,
    )
    db.add(log)


def _calc_commission(category: str, price, bag_count: int | None = None) -> Decimal:
    price = require_amount(price, what="Price")
    if category == "cement":
        bags = bag_count or 600
        if bags <= 0:
            raise HTTPException(status_code=400, detail="bag_count must be positive")
        commission = q(Decimal(bags) * 10)
    elif price < Decimal("500000"):
        commission = q(price * Decimal("0.015"))
    elif price < Decimal("5000000"):
        commission = q(price * Decimal("0.010"))
    elif price < Decimal("20000000"):
        commission = q(price * Decimal("0.005"))
    else:
        commission = Decimal("100000.00")  # Max cap
    if commission < 0 or commission > price:
        raise HTTPException(status_code=400, detail="Commission must not exceed the deal amount")
    return commission


def _release_funds(db: Session, tx: EscrowTransaction):
    """Release escrow funds (atomic credits, B08).
    Normal deal: seller gets amount - commission - seller gateway share.
    Facilitated deal: seller gets amount - seller gateway share; facilitator 90% of fee, DealShield 10%.
    """
    seller = db.query(User).filter(User.id == tx.seller_id).first()
    buyer = db.query(User).filter(User.id == tx.buyer_id).first()
    commission = ZERO if tx.is_facilitated else to_decimal(tx.commission)
    seller_payout = q(to_decimal(tx.amount) - commission - to_decimal(tx.seller_gateway_share))
    if seller_payout < 0:
        raise HTTPException(status_code=400, detail="Seller payout would be negative")

    credit_wallet(db, seller.id, seller_payout)
    seller.total_deals = (seller.total_deals or 0) + 1
    buyer.total_deals = (buyer.total_deals or 0) + 1
    _update_badge(seller)
    _update_badge(buyer)
    db.add(WalletTx(
        user_id=seller.id, amount=seller_payout, type="escrow_release",
        description=f"Escrow release for {tx.listing_title}" + (" (facilitated)" if tx.is_facilitated else "")
    ))

    fee = to_decimal(tx.facilitator_fee)
    if tx.is_facilitated and fee > 0:
        dealshield_cut = q(fee * Decimal("0.10"))
        facilitator_payout = q(fee - dealshield_cut)
        tx.dealshield_cut = q(to_decimal(tx.dealshield_cut) + dealshield_cut)
        tx.facilitator_payout = facilitator_payout
        if tx.facilitator_id:
            facilitator = db.query(User).filter(User.id == tx.facilitator_id).first()
            if facilitator:
                credit_wallet(db, facilitator.id, facilitator_payout)
                db.add(WalletTx(
                    user_id=facilitator.id, amount=facilitator_payout, type="facilitator_payout",
                    description=f"Facilitator payout for {tx.listing_title} (90% of NGN {fee:,.2f} fee)"
                ))


def _refund_buyer(db: Session, tx: EscrowTransaction, partial_amount=None):
    """Refund buyer (full or partial), minus the NGN 5,000 cancellation fee for funded deals."""
    buyer = db.query(User).filter(User.id == tx.buyer_id).first()
    if partial_amount is not None:
        refund = require_amount(partial_amount, allow_zero=True, what="Refund")
    else:
        refund = q(_buyer_total(tx) + to_decimal(tx.buyer_gateway_share))

    if refund > CANCELLATION_FEE:
        refund = q(refund - CANCELLATION_FEE)
        tx.cancellation_fee = CANCELLATION_FEE
    else:
        tx.cancellation_fee = refund
        refund = ZERO
    tx.dealshield_cut = q(to_decimal(tx.dealshield_cut) + to_decimal(tx.cancellation_fee))

    credit_wallet(db, buyer.id, refund)
    db.add(WalletTx(
        user_id=buyer.id, amount=refund, type="escrow_refund",
        description=f"Escrow refund for {tx.listing_title} (NGN {to_decimal(tx.cancellation_fee):,.2f} cancellation fee deducted)"
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

    # KYC gate: buyers must have completed identity verification before transacting
    if not current_user.kyc_verified:
        raise HTTPException(
            status_code=403,
            detail="Identity verification required before you can transact. Submit your NIN or BVN via /auth/kyc/submit.",
        )

    price = require_amount(listing.price, what="Listing price")  # B01 service-layer re-check
    insurance_fee = q(price * INSURANCE_RATE) if escrow_in.insured else ZERO

    commission = _calc_commission(listing.category, price, escrow_in.bag_count)

    now = datetime.utcnow()
    tx = EscrowTransaction(
        listing_id=listing.id,
        listing_title=listing.title,
        category=listing.category,
        amount=price,
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
    return _tx_dict(tx, current_user.id)


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
    if tx.is_facilitated:
        # B14: facilitated deals need both stored consents via /accept-terms
        raise HTTPException(status_code=400, detail="Facilitated deals require both parties to accept via accept-terms")
    if tx.status != "created":
        raise HTTPException(status_code=400, detail=f"Cannot accept - current status: {tx.status}")

    now = datetime.utcnow()
    if tx.accept_deadline and now > _strip_tz(tx.accept_deadline):
        _transition(db, tx, {"status": "expired", "closed_at": now})
        db.commit()
        raise HTTPException(status_code=400, detail="Accept deadline expired")

    _transition(db, tx, {"status": "seller_accepted", "accepted_at": now,
                         "payment_deadline": now + timedelta(hours=PAYMENT_DEADLINE_HOURS)})

    _log_audit(db, current_user.id, "escrow_accept", request, target_id=tx.id)
    db.commit()
    db.refresh(tx)
    notify_escrow_event(_tx_dict(tx), "escrow_accepted")
    return _tx_dict(tx, current_user.id)


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
    _transition(db, tx, {"status": "seller_declined", "closed_at": now})

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
        raise HTTPException(status_code=400, detail=f"Cannot fund - current status: {tx.status}")
    if tx.is_facilitated and not (tx.buyer_accepted_terms and tx.seller_accepted_terms):
        raise HTTPException(status_code=400, detail="Both parties must accept the facilitated deal terms before funding")

    now = datetime.utcnow()
    if tx.payment_deadline and now > _strip_tz(tx.payment_deadline):
        _transition(db, tx, {"status": "expired", "closed_at": now})
        db.commit()
        raise HTTPException(status_code=400, detail="Payment deadline expired")

    quote = _funding_quote(tx)  # B01: strict >0, bounded, nonnegative payout
    total_with_gateway = quote["total_with_gateway"]

    # Claim the escrow row first (only one funding request can win) ...
    _transition(db, tx, {
        "status": "funded", "funded_at": now,
        "gateway_fee": quote["gateway_fee"],
        "buyer_gateway_share": quote["buyer_gateway_share"],
        "seller_gateway_share": quote["seller_gateway_share"],
    }, from_status="seller_accepted")
    # ... then debit atomically: UPDATE users SET bal = bal - x WHERE id = :id AND bal >= x (B08).
    try:
        debit_wallet(db, current_user.id, total_with_gateway)
    except HTTPException:
        db.rollback()
        raise
    db.add(WalletTx(
        user_id=current_user.id, amount=-total_with_gateway, type="escrow_hold",
        description=f"Escrow deposit for {tx.listing_title}" + (", insured" if tx.insured else "")
    ))

    _log_audit(db, current_user.id, "escrow_fund", request, target_id=tx.id,
               details=f"Amount: {total_with_gateway} (gateway fee: {quote['gateway_fee']})")
    db.commit()
    db.refresh(tx)
    notify_escrow_event(_tx_dict(tx), "escrow_funded")
    return _tx_dict(tx, current_user.id)


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
    _transition(db, tx, {"status": "seller_fulfilling", "fulfilment_started_at": now,
                         "logistics_provider": fulfill_in.logistics_provider,
                         "tracking_number": fulfill_in.tracking_number})

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
    # Generate 6-digit release OTP for buyer (CSPRNG)
    _transition(db, tx, {"status": "buyer_review", "buyer_review_started_at": now,
                         "buyer_review_deadline": now + timedelta(days=BUYER_REVIEW_DAYS),
                         "release_otp": _new_otp(), "release_otp_expiry": now + timedelta(hours=24)})

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


# ── 7b. RELEASE WITH OTP — Buyer enters OTP to release funds ──

@router.post("/{tx_id}/release-otp", response_model=EscrowOut)
def release_with_otp(tx_id: int, otp_in: ReleaseOTPRequest, request: Request,
                     current_user: User = Depends(get_current_user),
                     db: Session = Depends(get_db)):
    """Buyer submits the 6-digit OTP to release funds to seller.
    Alternative to manual /approve — provides OTP-based verification.
    """
    tx = db.query(EscrowTransaction).filter(EscrowTransaction.id == tx_id).first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if tx.buyer_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the buyer can release with OTP")
    if tx.status != "buyer_review":
        raise HTTPException(status_code=400, detail=f"Cannot release — current status: {tx.status}")
    if not tx.release_otp:
        raise HTTPException(status_code=400, detail="No OTP generated for this transaction")
    if tx.release_otp_expiry and datetime.utcnow() > _strip_tz(tx.release_otp_expiry):
        raise HTTPException(status_code=400, detail="OTP has expired. Contact support or use manual approval.")

    if otp_in.otp.strip() != tx.release_otp:
        raise HTTPException(status_code=400, detail="Invalid OTP code")

    now = datetime.utcnow()
    # Clear OTP after use
    tx.release_otp = ""
    tx.release_otp_expiry = None

    rows = db.query(EscrowTransaction).filter(
        EscrowTransaction.id == tx_id,
        EscrowTransaction.status == "buyer_review",
    ).update({"status": "buyer_approved", "completed_at": now}, synchronize_session=False)
    db.flush()
    if rows != 1:
        db.rollback()
        raise HTTPException(status_code=409, detail="Transaction status changed concurrently")
    db.refresh(tx)

    _release_funds(db, tx)
    tx.status = "released"
    tx.closed_at = now

    _log_audit(db, current_user.id, "escrow_otp_release", request, target_id=tx.id)
    db.commit()
    db.refresh(tx)
    notify_escrow_event(_tx_dict(tx), "escrow_released")
    return _tx_dict(tx)


# ── 7c. GET RELEASE OTP — Buyer retrieves their OTP (for demo/testing) ──

@router.get("/{tx_id}/release-otp")
def get_release_otp(tx_id: int, current_user: User = Depends(get_current_user),
                    db: Session = Depends(get_db)):
    """Buyer retrieves their release OTP. In production, this would be sent via SMS/email.
    For now, returns it directly (demo mode).
    """
    tx = db.query(EscrowTransaction).filter(EscrowTransaction.id == tx_id).first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if tx.buyer_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the buyer can view their OTP")
    if tx.status != "buyer_review":
        raise HTTPException(status_code=400, detail=f"OTP not available — current status: {tx.status}")
    if not tx.release_otp:
        raise HTTPException(status_code=400, detail="No OTP generated")
    if tx.release_otp_expiry and datetime.utcnow() > _strip_tz(tx.release_otp_expiry):
        raise HTTPException(status_code=400, detail="OTP has expired")
    return {"otp": tx.release_otp, "expires_at": tx.release_otp_expiry.isoformat() if tx.release_otp_expiry else None}


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

    _transition(db, tx, {"status": "disputed",
                         "dispute_reason": sanitize_text(dispute_in.reason, max_length=1000),
                         "dispute_evidence": dispute_in.evidence,
                         "dispute_initiated_by": "buyer" if current_user.id == tx.buyer_id else "seller"})

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
    was_funded = tx.status == "funded"
    # Claim the state change first so a racing release/fulfil cannot also act (B08)
    _transition(db, tx, {"status": "cancelled", "closed_at": now})
    if was_funded:
        _refund_buyer(db, tx)

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
        _transition(db, tx, {"status": "expired", "closed_at": now})
        expired_count += 1
        _log_audit(db, None, "escrow_auto_expire", request, target_id=tx.id,
                   details="Seller did not accept in time")

    # Expire unfunded deals
    unfunded = db.query(EscrowTransaction).filter(
        EscrowTransaction.status == "seller_accepted",
        EscrowTransaction.payment_deadline < now,
    ).all()
    for tx in unfunded:
        _transition(db, tx, {"status": "expired", "closed_at": now})
        expired_count += 1
        _log_audit(db, None, "escrow_auto_expire", request, target_id=tx.id,
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
    return {"transactions": [_tx_dict(t, current_user.id) for t in txs]}


# ── 14. GET SINGLE TRANSACTION ──

@router.get("/{tx_id}", response_model=EscrowOut)
def get_transaction(tx_id: int, current_user: User = Depends(get_current_user),
                    db: Session = Depends(get_db)):
    tx = db.query(EscrowTransaction).filter(EscrowTransaction.id == tx_id).first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if current_user.id not in (tx.buyer_id, tx.seller_id, tx.facilitator_id):
        raise HTTPException(status_code=403, detail="Not authorized")
    return _tx_dict(tx, current_user.id)


# ── FACILITATOR ENDPOINTS ──

@router.post("/facilitate/create", response_model=EscrowOut)
def facilitator_create_deal(deal_in: FacilitatedDealCreate, request: Request,
                            current_user: User = Depends(get_current_user),
                            db: Session = Depends(get_db)):
    """Facilitator creates a deal between a buyer and seller.
    Facilitator sets the deal amount (agreed between buyer & seller for goods)
    and their facilitation fee (what they charge for brokering).
    On release: seller gets full deal amount, facilitator gets 90% of their fee,
    DealShield keeps 10% of the facilitator's fee.
    """
    # KYC gate: facilitators must be identity-verified before brokering deals
    if not current_user.kyc_verified:
        raise HTTPException(
            status_code=403,
            detail="Identity verification required before you can facilitate deals. Submit your NIN or BVN via /auth/kyc/submit.",
        )
    # B01: re-check at the service layer (schema already enforces Decimal/bounds)
    deal_amount = require_amount(deal_in.deal_amount, what="Deal amount")
    facilitator_fee = require_amount(deal_in.facilitator_fee, allow_zero=True, what="Facilitator fee")

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

    insurance_fee = q(deal_amount * INSURANCE_RATE) if deal_in.insured else ZERO

    now = datetime.utcnow()
    tx = EscrowTransaction(
        listing_id=None,  # No listing for facilitated deals
        listing_title=sanitize_text(deal_in.title, max_length=200),
        category=deal_in.category,
        amount=deal_amount,
        commission=ZERO,  # No escrow commission for facilitated deals
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
        facilitator_fee=facilitator_fee,
        accept_deadline=now + timedelta(hours=ACCEPT_DEADLINE_HOURS),
        share_token=secrets.token_urlsafe(16),
    )
    db.add(tx)
    db.flush()

    _log_audit(db, current_user.id, "facilitator_create_deal", request, target_id=tx.id,
               details=f"Facilitated: {deal_in.title}, Deal: ₦{deal_in.deal_amount:,.0f}, Fee: ₦{deal_in.facilitator_fee:,.0f}, Buyer: {buyer.name}, Seller: {seller.name}")
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
        if tx.seller_id != current_user.id:
            raise HTTPException(status_code=403, detail="Only the seller can mark fulfilment")
        _transition(db, tx, {"status": "funded", "funded_at": datetime.utcnow()})
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
        if tx.buyer_id != current_user.id:
            raise HTTPException(status_code=403, detail="Only the buyer can confirm receipt")
        now = datetime.utcnow()
        _transition(db, tx, {"status": "buyer_review", "buyer_review_started_at": now,
                             "buyer_review_deadline": now + timedelta(days=BUYER_REVIEW_DAYS),
                             "release_otp": _new_otp(), "release_otp_expiry": now + timedelta(hours=24)})
    return buyer_approve(tx_id, request, current_user, db)


# ── VIRTUAL ACCOUNT ENDPOINTS ──

def _generate_account_number() -> str:
    """Generate a 10-digit NUBAN-format account number.

    NUBAN format: 9-digit serial + 1 checksum digit.
    The checksum is computed using the standard CBN NUBAN algorithm:
    for a 9-digit serial N = n1 n2 ... n9, checksum = (3*n1 + 7*n2 + 3*n3 + 3*n4 + 7*n5 + 3*n6 + 3*n7 + 7*n8 + 3*n9) mod 10, then 10 - result mod 10.
    """
    serial = random.randint(100000000, 999999999)  # 9-digit serial
    digits = [int(d) for d in str(serial)]
    # NUBAN check digit algorithm (CBN standard)
    weights = [3, 7, 3, 3, 7, 3, 3, 7, 3]
    check_sum = sum(w * d for w, d in zip(weights, digits))
    check_digit = (10 - (check_sum % 10)) % 10
    return f"{serial}{check_digit}"


def _va_dict(va: VirtualAccount) -> dict:
    return {
        "id": va.id,
        "escrow_tx_id": va.escrow_tx_id,
        "account_number": va.account_number,
        "bank_name": va.bank_name,
        "bank_code": va.bank_code,
        "account_name": va.account_name,
        "provider": va.provider,
        "status": va.status,
        "expected_amount": va.expected_amount,
        "expires_at": va.expires_at.isoformat() if va.expires_at else None,
        "created_at": va.created_at.isoformat() if va.created_at else None,
        "updated_at": va.updated_at.isoformat() if va.updated_at else None,
    }


@router.post("/{tx_id}/generate-account", response_model=VirtualAccountOut)
def generate_virtual_account(tx_id: int, request: Request,
                             current_user: User = Depends(get_current_user),
                             db: Session = Depends(get_db)):
    """Generate a dedicated virtual account for an escrow transaction.
    The buyer can transfer the exact expected amount directly to this account.
    The account auto-expires when the payment deadline passes.
    """
    tx = db.query(EscrowTransaction).filter(EscrowTransaction.id == tx_id).first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if tx.buyer_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the buyer can generate a virtual account")
    if tx.status not in ("seller_accepted", "payment_pending", "created"):
        raise HTTPException(status_code=400, detail=f"Cannot generate account — current status: {tx.status}")

    # Check if a virtual account already exists for this transaction
    existing = db.query(VirtualAccount).filter(
        VirtualAccount.escrow_tx_id == tx_id,
        VirtualAccount.status == "active",
    ).first()
    if existing:
        # Return existing account
        return _va_dict(existing)

    # Calculate total buyer must pay
    if tx.is_facilitated:
        total = tx.amount + tx.facilitator_fee + (tx.insurance_fee or 0)
    else:
        total = tx.amount + (tx.insurance_fee or 0)

    # Generate unique account number
    for _ in range(10):
        acct_no = _generate_account_number()
        if not db.query(VirtualAccount).filter(VirtualAccount.account_number == acct_no).first():
            break
    else:
        raise HTTPException(status_code=500, detail="Failed to generate unique account number")

    # Set expiry to payment deadline
    expires_at = tx.payment_deadline

    va = VirtualAccount(
        escrow_tx_id=tx_id,
        account_number=acct_no,
        bank_name="DealShield MFB",
        bank_code="999",
        account_name=f"DEALSHIELD/{current_user.name}/{tx_id}",
        provider=settings.PAYMENT_PROVIDER,
        status="active",
        expected_amount=total,
        expires_at=expires_at,
    )
    db.add(va)

    # If the escrow is in 'created' or 'seller_accepted', set it to 'payment_pending'
    if tx.status in ("created", "seller_accepted"):
        tx.status = "payment_pending"

    _log_audit(db, current_user.id, "virtual_account_generate", request,
               target_id=tx_id, details=f"Account: {acct_no}, Amount: {total}")
    db.commit()
    db.refresh(va)
    return _va_dict(va)


@router.get("/{tx_id}/account", response_model=VirtualAccountOut)
def get_virtual_account(tx_id: int,
                        current_user: User = Depends(get_current_user),
                        db: Session = Depends(get_db)):
    """Get the virtual account details for a transaction."""
    tx = db.query(EscrowTransaction).filter(EscrowTransaction.id == tx_id).first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if current_user.id not in (tx.buyer_id, tx.seller_id):
        raise HTTPException(status_code=403, detail="Not authorized")

    va = db.query(VirtualAccount).filter(
        VirtualAccount.escrow_tx_id == tx_id,
    ).order_by(VirtualAccount.created_at.desc()).first()
    if not va:
        raise HTTPException(status_code=404, detail="No virtual account for this transaction")

    # Auto-expire if payment deadline has passed
    now = datetime.utcnow()
    if va.status == "active" and va.expires_at and now > va.expires_at:
        va.status = "expired"
        db.commit()
        db.refresh(va)

    return _va_dict(va)


@router.post("/{tx_id}/payment-intent")
def create_payment_intent(tx_id: int, request: Request,
                          current_user: User = Depends(get_current_user),
                          db: Session = Depends(get_db)):
    """B04: create a provider reference bound to this escrow with the exact
    server-computed amount. The webhook only funds an escrow via such an intent.
    Returns 503 when the provider is not configured."""
    if not settings.PAYSTACK_SECRET_KEY:
        raise HTTPException(status_code=503, detail="External payment is unavailable: provider not configured")
    tx = db.query(EscrowTransaction).filter(EscrowTransaction.id == tx_id).first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if tx.buyer_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the buyer can pay")
    if tx.status not in FUNDABLE_STATES:
        raise HTTPException(status_code=400, detail=f"Cannot pay - current status: {tx.status}")
    if tx.is_facilitated and not (tx.buyer_accepted_terms and tx.seller_accepted_terms):
        raise HTTPException(status_code=400, detail="Both parties must accept the facilitated deal terms before funding")
    quote = _funding_quote(tx)
    reference = f"DSX_{tx.id}_{secrets.token_urlsafe(12)}"
    db.add(PaymentReference(reference=reference, user_id=current_user.id, amount=quote["total_with_gateway"],
                            provider=WEBHOOK_PROVIDER, status="pending", escrow_tx_id=tx.id, currency="NGN"))
    _log_audit(db, current_user.id, "escrow_payment_intent", request, target_id=tx.id,
               details=f"ref {reference}, NGN {quote['total_with_gateway']}")
    db.commit()
    return {"reference": reference, "amount": money_out(quote["total_with_gateway"]),
            "amount_kobo": int(quote["total_with_gateway"] * 100), "currency": "NGN",
            "provider": WEBHOOK_PROVIDER}


# B04: only this event type means "money was received". transfer.success is an
# OUTGOING payout and dedicated_account.assigned is account provisioning.
FUNDING_EVENT_TYPES = {"charge.success"}
WEBHOOK_PROVIDER = "paystack"
FUNDABLE_STATES = ("seller_accepted", "payment_pending")


def _quarantine(db: Session, *, event_id, reference, event_type, tx_id, expected, received,
                currency, reason: str, body: bytes, event_row: PaymentWebhookEvent | None):
    db.add(WebhookQuarantine(
        provider=WEBHOOK_PROVIDER, event_id=event_id, reference=reference, event_type=event_type,
        escrow_tx_id=tx_id, expected_amount=expected, received_amount=received, currency=currency,
        reason=reason, payload_sha256=hashlib.sha256(body).hexdigest(),
    ))
    if event_row is not None:
        event_row.status = "quarantined"
        event_row.reason = reason
    _log_audit(db, None, "payment_webhook_quarantined", None, target_id=tx_id,
               details=f"{reason} (ref={reference}, event={event_id})")
    db.commit()
    # HTTP 200 so the provider stops retrying; no funds moved.
    return {"status": "quarantined", "reason": reason}


@router.post("/webhook/payment")
async def escrow_payment_webhook(request: Request,
                                  x_paystack_signature: str = Header(None, alias="x-paystack-signature"),
                                  db: Session = Depends(get_db)):
    """Provider (Paystack) funding webhook - B04.

    1. Raw-body HMAC-SHA512 with the provider secret is mandatory.
    2. Only `charge.success` with data.status == "success" is a funding event.
    3. (provider, event id) and (provider, reference) are unique -> replays are no-ops.
    4. The reference must be a pending funding intent bound to exactly one escrow.
    5. Amount (kobo) and currency must equal the server-computed expected total exactly.
    Any mismatch -> quarantine row + audit log, HTTP 200, no money movement.
    """
    body = await request.body()

    secret = settings.PAYSTACK_SECRET_KEY
    if not secret:
        raise HTTPException(status_code=503, detail="Payment provider not configured")
    computed = hmac.new(secret.encode("utf-8"), body, hashlib.sha512).hexdigest()
    if not x_paystack_signature or not hmac.compare_digest(computed, x_paystack_signature.strip().lower()):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    try:
        payload = json.loads(body.decode("utf-8")) if body else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Invalid JSON payload")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    event_type = str(payload.get("event") or "")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    if event_type not in FUNDING_EVENT_TYPES or data.get("status") != "success":
        return {"status": "ignored", "event": event_type}

    event_id = str(data.get("id") or "").strip()
    reference = str(data.get("reference") or "").strip()
    currency = str(data.get("currency") or "").strip().upper()
    if not event_id or not reference:
        return _quarantine(db, event_id=event_id or None, reference=reference or None, event_type=event_type,
                           tx_id=None, expected=None, received=None, currency=currency,
                           reason="missing event id or reference", body=body, event_row=None)

    try:
        raw_amount = data.get("amount")
        if isinstance(raw_amount, bool) or not isinstance(raw_amount, (int, str)):
            raise ValueError
        received = (Decimal(str(raw_amount)) / 100).quantize(Decimal("0.01"))
        if not received.is_finite() or received <= 0 or received > MAX_AMOUNT:
            raise ValueError
    except Exception:
        received = None

    # Idempotency: claim the event id first. A replay hits the unique constraint.
    event_row = PaymentWebhookEvent(provider=WEBHOOK_PROVIDER, event_id=event_id, reference=reference,
                                    event_type=event_type, amount=received, currency=currency,
                                    status="received")
    try:
        with db.begin_nested():
            db.add(event_row)
            db.flush()
    except IntegrityError:
        db.rollback()
        return {"status": "duplicate", "event_id": event_id}

    if received is None:
        return _quarantine(db, event_id=event_id, reference=reference, event_type=event_type, tx_id=None,
                           expected=None, received=None, currency=currency,
                           reason="invalid amount", body=body, event_row=event_row)

    intent = db.query(PaymentReference).filter(
        PaymentReference.reference == reference, PaymentReference.provider == WEBHOOK_PROVIDER,
    ).first()
    if intent is None or intent.escrow_tx_id is None:
        return _quarantine(db, event_id=event_id, reference=reference, event_type=event_type, tx_id=None,
                           expected=None, received=received, currency=currency,
                           reason="reference does not map to an escrow funding intent", body=body,
                           event_row=event_row)
    tx = db.query(EscrowTransaction).filter(EscrowTransaction.id == intent.escrow_tx_id).first()
    event_row.escrow_tx_id = intent.escrow_tx_id
    if tx is None or tx.buyer_id != intent.user_id:
        return _quarantine(db, event_id=event_id, reference=reference, event_type=event_type,
                           tx_id=intent.escrow_tx_id, expected=to_decimal(intent.amount), received=received,
                           currency=currency, reason="escrow missing or buyer mismatch", body=body,
                           event_row=event_row)
    if intent.status != "pending":
        return _quarantine(db, event_id=event_id, reference=reference, event_type=event_type, tx_id=tx.id,
                           expected=to_decimal(intent.amount), received=received, currency=currency,
                           reason=f"funding intent already {intent.status}", body=body, event_row=event_row)
    if tx.status not in FUNDABLE_STATES or (
            tx.is_facilitated and not (tx.buyer_accepted_terms and tx.seller_accepted_terms)):
        return _quarantine(db, event_id=event_id, reference=reference, event_type=event_type, tx_id=tx.id,
                           expected=to_decimal(intent.amount), received=received, currency=currency,
                           reason=f"escrow not fundable in status {tx.status}", body=body, event_row=event_row)

    try:
        quote = _funding_quote(tx)
    except HTTPException as exc:
        return _quarantine(db, event_id=event_id, reference=reference, event_type=event_type, tx_id=tx.id,
                           expected=None, received=received, currency=currency,
                           reason=f"invalid escrow amounts: {exc.detail}", body=body, event_row=event_row)
    expected = quote["total_with_gateway"]
    if currency != (intent.currency or "NGN") or currency != "NGN":
        return _quarantine(db, event_id=event_id, reference=reference, event_type=event_type, tx_id=tx.id,
                           expected=expected, received=received, currency=currency,
                           reason="currency mismatch", body=body, event_row=event_row)
    if received != expected or to_decimal(intent.amount) != expected:
        return _quarantine(db, event_id=event_id, reference=reference, event_type=event_type, tx_id=tx.id,
                           expected=expected, received=received, currency=currency,
                           reason="amount mismatch", body=body, event_row=event_row)

    # Reconciled: consume the intent and fund the escrow atomically (conditional updates).
    now = datetime.utcnow()
    claimed = db.query(PaymentReference).filter(
        PaymentReference.id == intent.id, PaymentReference.status == "pending",
    ).update({"status": "consumed"}, synchronize_session=False)
    rows = db.query(EscrowTransaction).filter(
        EscrowTransaction.id == tx.id, EscrowTransaction.status.in_(FUNDABLE_STATES),
    ).update({"status": "funded", "funded_at": now, "gateway_fee": quote["gateway_fee"],
              "buyer_gateway_share": quote["buyer_gateway_share"],
              "seller_gateway_share": quote["seller_gateway_share"]}, synchronize_session=False)
    if claimed != 1 or rows != 1:
        db.rollback()
        # Record the lost race in a fresh transaction; still no money moved.
        db.add(PaymentWebhookEvent(provider=WEBHOOK_PROVIDER, event_id=event_id, reference=reference,
                                   event_type=event_type, amount=received, currency=currency,
                                   escrow_tx_id=tx.id, status="quarantined",
                                   reason="concurrent state change"))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
        return {"status": "quarantined", "reason": "concurrent state change"}

    db.query(VirtualAccount).filter(VirtualAccount.escrow_tx_id == tx.id, VirtualAccount.status == "active") \
        .update({"status": "paid", "updated_at": now}, synchronize_session=False)
    event_row.status = "applied"
    db.add(WalletTx(
        user_id=tx.buyer_id, amount=received, type="escrow_fund_external",
        description=f"External payment for {tx.listing_title} (ref {reference})",
    ))
    _log_audit(db, None, "escrow_funded_external", None, target_id=tx.id,
               details=f"Provider event {event_id}, ref {reference}, NGN {received}")
    db.commit()
    db.refresh(tx)
    notify_escrow_event(_tx_dict(tx), "escrow_funded")
    return {"status": "success", "escrow_id": tx.id, "new_status": "funded"}


# ── DEAL SHARE LINK — Get deal by share token (public, no auth) ──

@router.get("/shared/{share_token}")
def get_shared_deal(share_token: str, db: Session = Depends(get_db)):
    """Public endpoint — anyone with the share token can view deal details.
    Used by facilitators to share deal links with buyers/sellers.
    Does NOT require authentication — only shows limited info.
    """
    tx = db.query(EscrowTransaction).filter(EscrowTransaction.share_token == share_token).first()
    if not tx:
        raise HTTPException(status_code=404, detail="Deal not found or link is invalid")
    if not tx.is_facilitated:
        raise HTTPException(status_code=404, detail="Deal not found")
    # Return limited public info
    return {
        "id": tx.id,
        "title": tx.listing_title,
        "category": tx.category,
        "deal_amount": money_out(tx.amount),
        "facilitator_fee": money_out(tx.facilitator_fee),
        "facilitator_name": tx.facilitator_name,
        "status": tx.status,
        "is_facilitated": True,
        "buyer_name": tx.buyer_name,
        "seller_name": tx.seller_name,
        "gateway_fee": money_out(tx.gateway_fee),
        "insurance_fee": money_out(tx.insurance_fee),
        "accept_deadline": tx.accept_deadline.isoformat() if tx.accept_deadline else None,
        "payment_deadline": tx.payment_deadline.isoformat() if tx.payment_deadline else None,
        "created_at": tx.created_at.isoformat() if tx.created_at else None,
    }
