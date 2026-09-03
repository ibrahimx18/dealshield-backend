"""Admin router — Dispute resolution, admin audit logs, and transaction overrides."""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from datetime import datetime
from typing import Optional, List, Literal
from pydantic import BaseModel

from app.dependencies import get_db
from app.models.models import EscrowTransaction, User, WalletTx, AdminAuditLog
from app.routers.auth import get_current_user
from app.core.notifications import notify_escrow_event

router = APIRouter()


class DisputeResolveRequest(BaseModel):
    decision: Literal["release_to_seller", "refund_to_buyer", "split"]
    buyer_split_percent: Optional[float] = None  # Required if decision == 'split' (e.g., 50.0 for 50/50)
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
            "amount": tx.amount,
            "commission": tx.commission,
            "insurance_fee": tx.insurance_fee or 0,
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

    tx.status = "under_investigation"
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
      - 'release_to_seller': Full amount (minus commission) goes to seller.
      - 'refund_to_buyer': Full amount (+ insurance fee) refunded to buyer.
      - 'split': Split funds between buyer and seller based on buyer_split_percent.

    Sets proper terminal status (released/refunded/split_resolution) and resolution metadata.
    Creates immutable AdminAuditLog.
    """
    if not req.reason or len(req.reason.strip()) < 5:
        raise HTTPException(status_code=400, detail="A valid resolution reason (at least 5 characters) is required.")

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

    now = datetime.utcnow()
    tx.admin_resolution = req.decision
    tx.admin_reason = req.reason.strip()
    tx.resolved_at = now

    # Map decision to proper terminal status
    status_map = {
        "release_to_seller": "released",
        "refund_to_buyer": "refunded",
        "split": "split_resolution",
    }
    new_status = status_map.get(req.decision, "closed")
    tx.closed_at = now

    # Atomic conditional update
    rows = db.query(EscrowTransaction).filter(
        EscrowTransaction.id == tx_id,
        EscrowTransaction.status.in_(["disputed", "under_investigation"])
    ).update({
        "status": new_status,
        "completed_at": now,
        "admin_resolution": req.decision,
        "admin_reason": req.reason.strip(),
        "resolved_at": now,
        "closed_at": now,
    }, synchronize_session=False)

    db.flush()
    if rows != 1:
        db.rollback()
        raise HTTPException(status_code=409, detail="Transaction status changed concurrently; aborting dispute resolution.")

    total_pool = tx.amount + (tx.insurance_fee or 0)
    details_str = f"Decision: {req.decision}. Reason: {req.reason.strip()}."

    if req.decision == "release_to_seller":
        seller_payout = tx.amount - tx.commission
        seller.wallet_balance += seller_payout
        seller.total_deals += 1
        buyer.total_deals += 1
        db.add(WalletTx(
            user_id=seller.id, amount=seller_payout, type="escrow_release",
            description=f"Admin dispute resolution payout for {tx.listing_title} (Tx #{tx.id})"
        ))
        details_str += f" Seller credited ₦{seller_payout:,.2f}."

    elif req.decision == "refund_to_buyer":
        buyer_refund = total_pool
        buyer.wallet_balance += buyer_refund
        db.add(WalletTx(
            user_id=buyer.id, amount=buyer_refund, type="escrow_refund",
            description=f"Admin dispute resolution refund for {tx.listing_title} (Tx #{tx.id})"
        ))
        details_str += f" Buyer refunded ₦{buyer_refund:,.2f}."

    elif req.decision == "split":
        if req.buyer_split_percent is None or not (0 <= req.buyer_split_percent <= 100):
            db.rollback()
            raise HTTPException(status_code=400, detail="buyer_split_percent must be between 0 and 100 for split decision.")

        buyer_pct = req.buyer_split_percent / 100.0
        seller_pct = 1.0 - buyer_pct
        buyer_share = round(total_pool * buyer_pct, 2)
        seller_share = round(total_pool * seller_pct, 2)

        if buyer_share > 0:
            buyer.wallet_balance += buyer_share
            db.add(WalletTx(
                user_id=buyer.id, amount=buyer_share, type="escrow_refund",
                description=f"Admin dispute split refund ({req.buyer_split_percent}%) for {tx.listing_title} (Tx #{tx.id})"
            ))
        if seller_share > 0:
            seller.wallet_balance += seller_share
            db.add(WalletTx(
                user_id=seller.id, amount=seller_share, type="escrow_release",
                description=f"Admin dispute split payout ({100 - req.buyer_split_percent:.1f}%) for {tx.listing_title} (Tx #{tx.id})"
            ))
        details_str += f" Split: Buyer ₦{buyer_share:,.2f} ({req.buyer_split_percent}%), Seller ₦{seller_share:,.2f}."

    # Record Immutable Audit Log
    audit_log = AdminAuditLog(
        admin_id=admin.id,
        action=f"resolve_dispute_{req.decision}",
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
        "amount": tx.amount,
        "status": tx.status,
        "buyer_id": tx.buyer_id,
        "seller_id": tx.seller_id
    }
    notify_escrow_event(tx_dict, "escrow_dispute_resolved")

    return {
        "status": "success",
        "message": f"Dispute for transaction #{tx_id} resolved with decision: {req.decision}",
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
