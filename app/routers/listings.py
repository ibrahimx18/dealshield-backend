from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import Optional
from app.dependencies import get_db
from app.schemas.schemas import ListingCreate, ListingOut, ListingListResponse
from app.models.models import Listing, User
from app.routers.auth import get_current_user
from app.core.notifications import notify_listing_event
from app.core.security_middleware import sanitize_text

router = APIRouter()

def _listing_dict(l: Listing) -> dict:
    return {
        "id": l.id,
        "category": l.category,
        "title": l.title,
        "description": l.description or "",
        "price": l.price,
        "location": l.location or "",
        "seller_name": l.seller_name or "",
        "seller_rating": l.seller_rating or "5.0",
        "verified": l.verified,
        "image_path": l.image_path,
        "insured": l.insured,
        "posted_date": l.posted_date.isoformat() if l.posted_date else None,
    }

@router.get("", response_model=ListingListResponse)
def list_listings(category: Optional[str] = Query(None), search: Optional[str] = Query(None), db: Session = Depends(get_db)):
    q = db.query(Listing)
    if category and category != "undefined":
        q = q.filter(Listing.category == category)
    if search:
        term = f"%{search}%"
        q = q.filter(Listing.title.ilike(term) | Listing.description.ilike(term))
    listings = q.order_by(Listing.posted_date.desc()).all()
    return {"listings": [_listing_dict(l) for l in listings]}

@router.get("/{listing_id}", response_model=ListingOut)
def get_listing(listing_id: int, db: Session = Depends(get_db)):
    l = db.query(Listing).filter(Listing.id == listing_id).first()
    if not l:
        raise HTTPException(status_code=404, detail="Listing not found")
    return _listing_dict(l)

@router.post("", response_model=ListingOut)
def create_listing(listing_in: ListingCreate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    listing = Listing(
        category=listing_in.category,
        title=sanitize_text(listing_in.title, max_length=200),
        description=sanitize_text(listing_in.description, max_length=5000),
        price=listing_in.price,
        location=sanitize_text(listing_in.location, max_length=200),
        insured=listing_in.insured,
        seller_id=current_user.id,
        seller_name=current_user.name,
        seller_rating=str(current_user.rating),
        verified=True,  # auto-verify for now
    )
    db.add(listing)
    db.commit()
    db.refresh(listing)
    notify_listing_event(_listing_dict(listing))
    return _listing_dict(listing)
