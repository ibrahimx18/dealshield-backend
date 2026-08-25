"""Reviews router — buyer/seller can review each other after a completed deal."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.dependencies import get_db
from app.schemas.schemas import ReviewCreate, ReviewOut, ReviewListResponse
from app.models.models import Review, EscrowTransaction, User
from app.routers.auth import get_current_user
from app.core.security_middleware import sanitize_text

router = APIRouter()


def _review_dict(r: Review, reviewer_name: str = None) -> dict:
    return {
        "id": r.id,
        "escrow_id": r.escrow_id,
        "reviewer_id": r.reviewer_id,
        "reviewee_id": r.reviewee_id,
        "reviewer_name": reviewer_name,
        "rating": r.rating,
        "comment": r.comment or "",
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


@router.post("", response_model=ReviewOut)
def create_review(review_in: ReviewCreate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    tx = db.query(EscrowTransaction).filter(EscrowTransaction.id == review_in.escrow_id).first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if tx.status != "delivered":
        raise HTTPException(status_code=400, detail="Can only review after delivery is confirmed")
    if current_user.id not in (tx.buyer_id, tx.seller_id):
        raise HTTPException(status_code=403, detail="Not part of this transaction")

    # Determine who is being reviewed
    reviewee_id = tx.seller_id if current_user.id == tx.buyer_id else tx.buyer_id

    # Check for duplicate
    existing = db.query(Review).filter(
        Review.escrow_id == review_in.escrow_id,
        Review.reviewer_id == current_user.id
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="You have already reviewed this transaction")

    if review_in.rating < 1 or review_in.rating > 5:
        raise HTTPException(status_code=400, detail="Rating must be 1-5")

    review = Review(
        escrow_id=review_in.escrow_id,
        reviewer_id=current_user.id,
        reviewee_id=reviewee_id,
        rating=review_in.rating,
        comment=sanitize_text(review_in.comment, max_length=500),
    )
    db.add(review)

    # Update reviewee's average rating
    reviewee = db.query(User).filter(User.id == reviewee_id).first()
    all_reviews = db.query(Review).filter(Review.reviewee_id == reviewee_id).all()
    avg = sum(r.rating for r in all_reviews) / len(all_reviews) if all_reviews else review_in.rating
    reviewee.rating = round(avg, 2)

    db.commit()
    db.refresh(review)
    return _review_dict(review, reviewer_name=current_user.name)


@router.get("/user/{user_id}", response_model=ReviewListResponse)
def get_user_reviews(user_id: int, db: Session = Depends(get_db)):
    reviews = db.query(Review).filter(Review.reviewee_id == user_id).order_by(Review.created_at.desc()).all()
    result = []
    for r in reviews:
        reviewer = db.query(User).filter(User.id == r.reviewer_id).first()
        result.append(_review_dict(r, reviewer_name=reviewer.name if reviewer else "Unknown"))
    return {"reviews": result}


@router.get("/transaction/{escrow_id}", response_model=ReviewListResponse)
def get_transaction_reviews(escrow_id: int, db: Session = Depends(get_db)):
    reviews = db.query(Review).filter(Review.escrow_id == escrow_id).all()
    result = []
    for r in reviews:
        reviewer = db.query(User).filter(User.id == r.reviewer_id).first()
        result.append(_review_dict(r, reviewer_name=reviewer.name if reviewer else "Unknown"))
    return {"reviews": result}
