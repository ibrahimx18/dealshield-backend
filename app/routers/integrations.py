"""
SafePay Admin/Integration API
Endpoints for n8n workflows and external integrations.
Protected by a shared secret key.
"""
import os
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, Header
from sqlalchemy.orm import Session
from sqlalchemy import func, extract
from app.dependencies import get_db
from app.models.models import User, Listing, EscrowTransaction, WalletTx, MarketPrice
from app.core.config import settings

router = APIRouter()

# Integration secret — n8n must pass this in X-SafePay-Secret header
INTEGRATION_SECRET = os.getenv("SAFEPAY_WEBHOOK_SECRET", "safepay-n8n-2026")


def verify_secret(x_safepay_secret: str = Header(None)):
    """Verify the integration secret for n8n endpoints."""
    if x_safepay_secret != INTEGRATION_SECRET:
        raise HTTPException(status_code=403, detail="Invalid integration secret")
    return True


# ── Deal Statistics (for weekly summary & dashboard) ──

@router.get("/stats/deals", dependencies=[Depends(verify_secret)])
def deal_stats(days: int = 7, db: Session = Depends(get_db)):
    """Get deal statistics for the last N days. Used by n8n weekly summary."""
    since = datetime.utcnow() - timedelta(days=days)
    txs = db.query(EscrowTransaction).filter(EscrowTransaction.created_at >= since).all()

    total_deals = len(txs)
    total_volume = sum(t.amount for t in txs)
    total_commission = sum(t.commission for t in txs)
    total_insurance = sum(t.insurance_fee or 0 for t in txs)

    status_counts = {}
    for t in txs:
        status_counts[t.status] = status_counts.get(t.status, 0) + 1

    completed = [t for t in txs if t.status == "delivered"]
    disputed = [t for t in txs if t.status == "disputed"]
    active = [t for t in txs if t.status in ("funds_deposited", "shipped")]

    # Category breakdown
    by_category = {}
    for t in txs:
        by_category[t.category] = by_category.get(t.category, 0) + 1

    return {
        "period_days": days,
        "total_deals": total_deals,
        "total_volume_ngn": round(total_volume, 2),
        "total_commission_ngn": round(total_commission, 2),
        "total_insurance_ngn": round(total_insurance, 2),
        "status_breakdown": status_counts,
        "completed_deals": len(completed),
        "disputed_deals": len(disputed),
        "active_deals": len(active),
        "category_breakdown": by_category,
        "dispute_rate": round(len(disputed) / total_deals * 100, 1) if total_deals > 0 else 0,
    }


@router.get("/stats/users", dependencies=[Depends(verify_secret)])
def user_stats(db: Session = Depends(get_db)):
    """Get user statistics. Used by n8n."""
    total_users = db.query(User).count()
    verified = db.query(User).filter(User.nin_verified == True).count()
    new_this_week = db.query(User).filter(
        User.created_at >= datetime.utcnow() - timedelta(days=7)
    ).count()

    # Low wallet alerts (balance < 10,000)
    low_wallet_users = db.query(User).filter(User.wallet_balance < 10000).all()

    return {
        "total_users": total_users,
        "verified_users": verified,
        "new_this_week": new_this_week,
        "low_wallet_alerts": [
            {"user_id": u.id, "name": u.name, "phone": u.phone, "balance": u.wallet_balance}
            for u in low_wallet_users
        ],
    }


@router.get("/stats/listings", dependencies=[Depends(verify_secret)])
def listing_stats(db: Session = Depends(get_db)):
    """Get listing statistics."""
    total = db.query(Listing).count()
    verified = db.query(Listing).filter(Listing.verified == True).count()
    by_category = {}
    for l in db.query(Listing).all():
        by_category[l.category] = by_category.get(l.category, 0) + 1

    return {
        "total_listings": total,
        "verified_listings": verified,
        "category_breakdown": by_category,
    }


# ── Price Update Endpoint (for n8n hourly price push) ──

@router.post("/prices/update", dependencies=[Depends(verify_secret)])
def update_prices_batch(prices: dict, db: Session = Depends(get_db)):
    """Batch update market prices. Called by n8n hourly scraper."""
    updated = 0
    for item_name, data in prices.items():
        existing = db.query(MarketPrice).filter(MarketPrice.item == item_name).first()
        if existing:
            existing.price_usd = data.get("price_usd", existing.price_usd)
            existing.price_ngn = data.get("price_ngn", existing.price_ngn)
            existing.change = data.get("change", existing.change)
            existing.trending = data.get("trending", existing.trending)
            existing.updated_at = datetime.utcnow()
            updated += 1
        else:
            entry = MarketPrice(
                item=item_name,
                category=data.get("category", "other"),
                price_usd=data.get("price_usd", 0),
                price_ngn=data.get("price_ngn", 0),
                unit=data.get("unit", "unit"),
                change=data.get("change", 0),
                trending=data.get("trending", False),
                updated_at=datetime.utcnow(),
            )
            db.add(entry)
            updated += 1
    db.commit()
    return {"detail": f"Updated {updated} prices", "count": updated}


# ── Listing Scam Flag (for n8n AI scam detection) ──

@router.post("/listings/{listing_id}/flag", dependencies=[Depends(verify_secret)])
def flag_listing(listing_id: int, flag_data: dict, db: Session = Depends(get_db)):
    """Flag a listing as suspicious (from n8n AI scam detection)."""
    listing = db.query(Listing).filter(Listing.id == listing_id).first()
    if not listing:
        raise HTTPException(status_code=404, detail="Listing not found")

    listing.verified = not flag_data.get("flagged", False)
    db.commit()

    return {
        "listing_id": listing.id,
        "title": listing.title,
        "verified": listing.verified,
        "flagged": flag_data.get("flagged", False),
        "reason": flag_data.get("reason", ""),
        "risk_score": flag_data.get("risk_score", 0),
    }


@router.get("/listings/recent", dependencies=[Depends(verify_secret)])
def recent_listings(limit: int = 20, db: Session = Depends(get_db)):
    """Get recent listings for n8n scam detection scanning."""
    listings = db.query(Listing).order_by(Listing.posted_date.desc()).limit(limit).all()
    return {
        "listings": [
            {
                "id": l.id,
                "title": l.title,
                "description": l.description,
                "price": l.price,
                "category": l.category,
                "location": l.location,
                "seller_name": l.seller_name,
                "seller_rating": l.seller_rating,
                "verified": l.verified,
            }
            for l in listings
        ]
    }


# ─<arg_value> Deal Lookup (for Telegram/WhatsApp support bot) ──

@router.get("/deal/{tx_id}", dependencies=[Depends(verify_secret)])
def lookup_deal(tx_id: int, db: Session = Depends(get_db)):
    """Look up a deal by ID. Used by n8n Telegram support bot."""
    tx = db.query(EscrowTransaction).filter(EscrowTransaction.id == tx_id).first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")

    return {
        "id": tx.id,
        "listing_title": tx.listing_title,
        "category": tx.category,
        "amount": tx.amount,
        "commission": tx.commission,
        "status": tx.status,
        "buyer_name": tx.buyer_name,
        "seller_name": tx.seller_name,
        "insured": tx.insured,
        "logistics_provider": tx.logistics_provider,
        "tracking_number": tx.tracking_number,
        "created_at": tx.created_at.isoformat() if tx.created_at else None,
        "completed_at": tx.completed_at.isoformat() if tx.completed_at else None,
    }


@router.get("/user/{phone}", dependencies=[Depends(verify_secret)])
def lookup_user_by_phone(phone: str, db: Session = Depends(get_db)):
    """Look up user by phone. Used by n8n support bot."""
    user = db.query(User).filter(User.phone == phone).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user_txs = db.query(EscrowTransaction).filter(
        (EscrowTransaction.buyer_id == user.id) | (EscrowTransaction.seller_id == user.id)
    ).order_by(EscrowTransaction.created_at.desc()).limit(5).all()

    return {
        "id": user.id,
        "name": user.name,
        "phone": user.phone,
        "wallet_balance": user.wallet_balance,
        "verified": user.nin_verified,
        "total_deals": user.total_deals,
        "rating": user.rating,
        "recent_deals": [
            {
                "id": t.id,
                "title": t.listing_title,
                "status": t.status,
                "amount": t.amount,
            }
            for t in user_txs
        ],
    }
