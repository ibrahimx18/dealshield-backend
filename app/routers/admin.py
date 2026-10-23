"""Admin router — Dispute resolution, admin audit logs, and transaction overrides."""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from datetime import datetime
from typing import Optional, List, Literal
from decimal import Decimal
from pydantic import BaseModel

from app.dependencies import get_db
from app.models.models import EscrowTransaction, User, WalletTx, AdminAuditLog
from app.routers.auth import get_current_user
from app.routers.escrow import CANCELLATION_FEE, _transition
from app.core.wallet import credit_wallet
from app.core.money import q, to_decimal, ZERO, money_out
from app.core.security_middleware import sanitize_text
from app.core.notifications import notify_escrow_event
from app.core.settlement import compute_settlement

router = APIRouter()


class DisputeResolveRequest(BaseModel):
    decision: Literal["release_to_seller", "refund_to_buyer", "split"]
    buyer_split_percent: Optional[Decimal] = None  # Required if decision == 'split' (e.g., 50.0 for 50/50)
    reason: str  # Mandatory explanation for resolution


class AdminAuditLogOut(BaseModel):
    id: int
    admin_id: int
    action: str
    target_type: str
    target_id: int
    details: str
    created_at: datetime


def require_admin(current_user: User = Depends(get_current_user)) -> User:
    """Dependency to check if the authenticated user has admin privileges."""
    if current_user.role != "admin":
        raise HTTPException(
            status_code=403,
            detail="Admin privileges required to access this endpoint."
        )
    return current_user


@router.get("/disputes", response_model=dict)
def list_disputed_transactions(
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin)
):
    """List all transactions currently in 'disputed' or 'under_investigation' status."""
    disputes = db.query(EscrowTransaction).filter(
        EscrowTransaction.status.in_(["disputed", "under_investigation"])
    ).order_by(EscrowTransaction.created_at.desc()).all()

    result = []
    for tx in disputes:
        buyer = db.query(User).filter(User.id == tx.buyer_id).first()
        seller = db.query(User).filter(User.id == tx.seller_id).first()
        result.append({
            "id": tx.id,
            "listing_title": tx.listing_title,
            "category": tx.category,
            "amount": money_out(tx.amount),
            "commission": money_out(tx.commission),
            "insurance_fee": money_out(tx.insurance_fee),
            "status": tx.status,
            "buyer_id": tx.buyer_id,
            "buyer_name": tx.buyer_name or (buyer.name if buyer else ""),
            "buyer_phone": buyer.phone if buyer else "",
            "seller_id": tx.seller_id,
            "seller_name": tx.seller_name or (seller.name if seller else ""),
            "seller_phone": seller.phone if seller else "",
            "created_at": tx.created_at.isoformat() if tx.created_at else None,
            "updated_at": tx.updated_at.isoformat() if tx.updated_at else None,
        })

    return {"disputes": result, "count": len(result)}


@router.post("/disputes/{tx_id}/investigate", response_model=dict)
def start_investigation(
    tx_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin)
):
    """Move a disputed transaction to 'under_investigation' status."""
    tx = db.query(EscrowTransaction).filter(EscrowTransaction.id == tx_id).first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if tx.status != "disputed":
        raise HTTPException(status_code=400, detail=f"Transaction is in '{tx.status}', not 'disputed'")

    _transition(db, tx, {"status": "under_investigation"}, from_status="disputed")
    db.commit()
    return {"status": "success", "message": f"Transaction #{tx_id} moved to investigation", "new_status": tx.status}


@router.post("/disputes/{tx_id}/resolve", response_model=dict)
def resolve_dispute(
    tx_id: int,
    req: DisputeResolveRequest,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin)
):
    """
    Resolve a disputed/under_investigation escrow transaction.
    Decisions supported:
      - 'release_to_seller': same payout as a normal release (seller: amount - commission
        - seller gateway share; facilitator 90% of fee; DealShield the rest).
      - 'refund_to_buyer': buyer gets everything held minus the NGN 5,000 cancellation fee.
      - 'split': buyer_split_percent of each component refunded (minus the cancellation
        fee), the rest settled like a release. Rules: app/core/settlement.py (N04).

    Sets proper terminal status (released/refunded/split_resolution) and resolution metadata.
    Creates immutable AdminAuditLog.
    """
    if not req.reason or len(req.reason.strip()) < 5:
        raise HTTPException(status_code=400, detail="A valid resolution reason (at least 5 characters) is required.")

    # Sanitize admin input to prevent stored XSS
    req_reason = sanitize_text(req.reason, max_length=1000)
    req_decision = sanitize_text(req.decision, max_length=50)

    tx = db.query(EscrowTransaction).filter(EscrowTransaction.id == tx_id).first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")

    if tx.status not in ("disputed", "under_investigation"):
        raise HTTPException(
            status_code=400,
            detail=f"Transaction {tx_id} is in status '{tx.status}', not 'disputed' or 'under_investigation'."
        )

    buyer = db.query(User).filter(User.id == tx.buyer_id).first()
    seller = db.query(User).filter(User.id == tx.seller_id).first()
    if not buyer or not seller:
        raise HTTPException(status_code=400, detail="Buyer or seller account missing for this transaction.")

    if req_decision == "split" and (req.buyer_split_percent is None or not (0 <= req.buyer_split_percent <= 100)):
        raise HTTPException(status_code=400, detail="buyer_split_percent must be between 0 and 100 for split decision.")

    now = datetime.utcnow()
    tx.admin_resolution = req_decision
    tx.admin_reason = req_reason
    tx.resolved_at = now

    # Map decision to proper terminal status
    status_map = {
        "release_to_seller": "released",
        "refund_to_buyer": "refunded",
        "split": "split_resolution",
    }
    new_status = status_map.get(req_decision, "closed")
    tx.closed_at = now

    # Atomic conditional update
    rows = db.query(EscrowTransaction).filter(
        EscrowTransaction.id == tx_id,
        EscrowTransaction.status.in_(["disputed", "under_investigation"])
    ).update({
        "status": new_status,
        "completed_at": now,
        "admin_resolution": req_decision,
        "admin_reason": req_reason,
        "resolved_at": now,
        "closed_at": now,
    }, synchronize_session=False)

    db.flush()
    if rows != 1:
        db.rollback()
        raise HTTPException(status_code=409, detail="Transaction status changed concurrently; aborting dispute resolution.")

    details_str = f"Decision: {req_decision}. Reason: {req_reason}."
    # N04: one rule for every decision (app/core/settlement.py). Every naira held is
    # assigned to buyer refund, seller payout, facilitator payout or a DealShield fee line.
    buyer_percent = {"release_to_seller": Decimal(0), "refund_to_buyer": Decimal(100)}.get(
        req_decision, to_decimal(req.buyer_split_percent) if req.buyer_split_percent is not None else None)
    facilitator = None
    if tx.is_facilitated and tx.facilitator_id:
        facilitator = db.query(User).filter(User.id == tx.facilitator_id).first()
    try:
        st = compute_settlement(tx, buyer_percent, CANCELLATION_FEE,
                                facilitator_exists=facilitator is not None)
    except ValueError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))

    label = {"release_to_seller": "release", "refund_to_buyer": "refund"}.get(req_decision, f"split {buyer_percent}%")
    if st.buyer_refund > 0:
        credit_wallet(db, buyer.id, st.buyer_refund)
        db.add(WalletTx(user_id=buyer.id, amount=st.buyer_refund, type="escrow_refund",
                        description=f"Admin dispute {label} refund for {tx.listing_title} (Tx #{tx.id}, "
                                    f"NGN {st.fees['cancellation_fee']:,.2f} cancellation fee deducted)"))
    if st.seller_payout > 0:
        credit_wallet(db, seller.id, st.seller_payout)
        db.add(WalletTx(user_id=seller.id, amount=st.seller_payout, type="escrow_release",
                        description=f"Admin dispute {label} payout for {tx.listing_title} (Tx #{tx.id})"))
    if st.facilitator_payout > 0 and facilitator is not None:
        credit_wallet(db, facilitator.id, st.facilitator_payout)
        db.add(WalletTx(user_id=facilitator.id, amount=st.facilitator_payout, type="facilitator_payout",
                        description=f"Admin dispute {label} facilitator payout for {tx.listing_title} (Tx #{tx.id})"))

    # Fee lines are ADDED to what is already recorded, never overwritten.
    fac_cut = st.fees.get("facilitator_cut", ZERO) + st.fees.get("facilitator_fee_unpaid", ZERO)
    tx.cancellation_fee = q(to_decimal(tx.cancellation_fee) + st.fees["cancellation_fee"])
    tx.dealshield_cut = q(to_decimal(tx.dealshield_cut) + fac_cut + st.fees["cancellation_fee"])
    tx.facilitator_payout = q(to_decimal(tx.facilitator_payout) + st.facilitator_payout)

    if req_decision == "release_to_seller":
        seller.total_deals = (seller.total_deals or 0) + 1
        buyer.total_deals = (buyer.total_deals or 0) + 1

    details_str += (f" Held NGN {st.held:,.2f}: buyer NGN {st.buyer_refund:,.2f}, seller NGN {st.seller_payout:,.2f}, "
                    f"facilitator NGN {st.facilitator_payout:,.2f}, DealShield NGN {st.dealshield_retained:,.2f} "
                    f"(" + ", ".join(f"{k} {v:,.2f}" for k, v in st.fees.items()) + ").")

    # Record Immutable Audit Log
    audit_log = AdminAuditLog(
        admin_id=admin.id,
        action=f"resolve_dispute_{req_decision}",
        target_type="escrow_transaction",
        target_id=tx.id,
        details=details_str
    )
    db.add(audit_log)

    db.commit()
    db.refresh(tx)

    tx_dict = {
        "id": tx.id,
        "listing_title": tx.listing_title,
        "amount": money_out(tx.amount),
        "status": tx.status,
        "buyer_id": tx.buyer_id,
        "seller_id": tx.seller_id
    }
    notify_escrow_event(tx_dict, "escrow_dispute_resolved")

    return {
        "status": "success",
        "message": f"Dispute for transaction #{tx_id} resolved with decision: {req_decision}",
        "transaction_id": tx.id,
        "new_status": tx.status,
        "details": details_str
    }


@router.get("/audit-logs", response_model=List[AdminAuditLogOut])
def get_admin_audit_logs(
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
    limit: int = Query(50, le=200)
):
    """Retrieve recent admin audit logs for compliance & transparency."""
    logs = db.query(AdminAuditLog).order_by(AdminAuditLog.created_at.desc()).limit(limit).all()
    return logs


class KYCDecision(BaseModel):
    decision: Literal["verified", "rejected"]
    reason: str


@router.post("/kyc/{user_id}/decision", response_model=dict)
def kyc_decision(user_id: int, req: KYCDecision, db: Session = Depends(get_db),
                 admin: User = Depends(require_admin)):
    """B02: the ONLY path to kyc_status='verified' - a human reviewer (or, later, a
    provider callback) after checking the submitted identity out of band."""
    from app.core.kyc_provider import KYC_PENDING
    if not req.reason or len(req.reason.strip()) < 5:
        raise HTTPException(status_code=400, detail="A review reason (at least 5 characters) is required.")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.kyc_status != KYC_PENDING:
        raise HTTPException(status_code=400, detail=f"KYC is '{user.kyc_status}', not pending_verification")
    verified = req.decision == "verified"
    user.kyc_status = req.decision
    user.kyc_verified = verified
    if user.kyc_id_type == "nin":
        user.nin_verified = verified
        user.id_verified = verified
    elif user.kyc_id_type == "bvn":
        user.bvn_verified = verified
    db.add(AdminAuditLog(admin_id=admin.id, action=f"kyc_{req.decision}", target_type="user",
                         target_id=user.id, details=sanitize_text(req.reason, max_length=500)))
    db.commit()
    return {"user_id": user.id, "kyc_status": user.kyc_status, "kyc_verified": user.kyc_verified}
