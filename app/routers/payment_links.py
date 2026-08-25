"""Payment links router — sellers generate shareable links for buyers to pay via escrow."""

import secrets
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.dependencies import get_db
from app.schemas.schemas import PaymentLinkCreate, PaymentLinkOut, PaymentLinkListResponse
from app.models.models import PaymentLink, User
from app.routers.auth import get_current_user
from app.core.security_middleware import sanitize_text

router = APIRouter()


def _link_dict(l: PaymentLink, seller_name: str = None) -> dict:
    return {
        "id": l.id,
        "link_code": l.link_code,
        "seller_id": l.seller_id,
        "seller_name": seller_name,
        "title": l.title,
        "description": l.description or "",
        "amount": l.amount,
        "category": l.category,
        "status": l.status,
        "created_at": l.created_at.isoformat() if l.created_at else None,
        "paid_at": l.paid_at.isoformat() if l.paid_at else None,
    }


def _gen_code() -> str:
    """Generate a short unique shareable code (8 chars)."""
    return secrets.token_urlsafe(6)[:8]


@router.post("", response_model=PaymentLinkOut)
def create_payment_link(link_in: PaymentLinkCreate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if link_in.amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive")

    link = PaymentLink(
        link_code=_gen_code(),
        seller_id=current_user.id,
        title=sanitize_text(link_in.title, max_length=200),
        description=sanitize_text(link_in.description, max_length=500),
        amount=link_in.amount,
        category=link_in.category,
    )
    db.add(link)
    db.commit()
    db.refresh(link)
    return _link_dict(link, seller_name=current_user.name)


@router.get("", response_model=PaymentLinkListResponse)
def list_my_links(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    links = db.query(PaymentLink).filter(PaymentLink.seller_id == current_user.id).order_by(PaymentLink.created_at.desc()).all()
    return {"links": [_link_dict(l, seller_name=current_user.name) for l in links]}


@router.get("/{link_code}", response_model=PaymentLinkOut)
def get_payment_link(link_code: str, db: Session = Depends(get_db)):
    link = db.query(PaymentLink).filter(PaymentLink.link_code == link_code).first()
    if not link:
        raise HTTPException(status_code=404, detail="Payment link not found")
    seller = db.query(User).filter(User.id == link.seller_id).first()
    return _link_dict(link, seller_name=seller.name if seller else None)


@router.delete("/{link_id}", response_model=dict)
def deactivate_link(link_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    link = db.query(PaymentLink).filter(PaymentLink.id == link_id, PaymentLink.seller_id == current_user.id).first()
    if not link:
        raise HTTPException(status_code=404, detail="Link not found")
    link.status = "expired"
    db.commit()
    return {"detail": "Link deactivated"}
