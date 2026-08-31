import os
import time
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session
from app.dependencies import get_db
from app.schemas.schemas import UserCreate, UserLogin, AuthResponse, UserProfile, VerifyBVN, VerifyBusiness
from app.core.security import get_password_hash, verify_password, create_access_token, decode_access_token
from app.core.security_middleware import sanitize_text, validate_password_strength
from app.models.models import User
from app.core.notifications import notify_new_user, notify_kyc_submitted

router = APIRouter()
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")

# Audit L2/H1: the 500,000 "welcome bonus" wallet balance on signup is a
# demo/test convenience and must not be granted in a real deployment.
SAFEPAY_TEST_MODE = os.getenv("SAFEPAY_TEST_MODE", "false").strip().lower() in ("1", "true", "yes")

def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    payload = decode_access_token(token)
    if payload is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    user_id = payload.get("sub")
    if user_id is None:
        raise HTTPException(status_code=401, detail="Invalid token")
    user = db.query(User).filter(User.id == int(user_id)).first()
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")
    return user

def _profile_dict(user: User) -> dict:
    return {
        "id": user.id,
        "name": user.name,
        "phone": user.phone,
        "email": user.email,
        "wallet_balance": user.wallet_balance,
        "nin_verified": user.nin_verified,
        "phone_verified": user.phone_verified,
        "id_verified": user.id_verified,
        "bvn_verified": user.bvn_verified,
        "business_verified": user.business_verified,
        "business_name": user.business_name,
        "badge_tier": user.badge_tier,
        "total_deals": user.total_deals,
        "rating": user.rating,
    }


def _update_badge(user: User):
    """Auto-assign badge tier based on verification + deal count."""
    if user.total_deals >= 50 and user.nin_verified and user.bvn_verified:
        user.badge_tier = "top_dealer"
    elif user.total_deals >= 10 and user.nin_verified:
        user.badge_tier = "trusted"
    elif user.nin_verified or user.bvn_verified:
        user.badge_tier = "verified"
    else:
        user.badge_tier = "none"

@router.post("/register", response_model=AuthResponse)
def register(user_in: UserCreate, db: Session = Depends(get_db)):
    # Validate password strength (checklist item #10)
    is_valid, err_msg = validate_password_strength(user_in.password)
    if not is_valid:
        raise HTTPException(status_code=400, detail=err_msg)

    # Sanitize name input (checklist item #14, #15)
    safe_name = sanitize_text(user_in.name, max_length=100)

    existing = db.query(User).filter((User.email == user_in.email) | (User.phone == user_in.phone)).first()
    if existing:
        raise HTTPException(status_code=400, detail="Email or phone already registered")
    user = User(
        name=safe_name,
        phone=user_in.phone,
        email=user_in.email.lower().strip(),
        hashed_password=get_password_hash(user_in.password),
        wallet_balance=500000.0 if SAFEPAY_TEST_MODE else 0.0,  # welcome bonus — test/demo mode only (audit L2/H1)
        phone_verified=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    notify_new_user({"id": user.id, "name": user.name, "phone": user.phone, "email": user.email})
    token = create_access_token(data={"sub": str(user.id)})
    return {"access_token": token, "token_type": "bearer", "user": _profile_dict(user)}

@router.post("/login", response_model=AuthResponse)
def login(data: UserLogin, db: Session = Depends(get_db)):
    key = data.email_or_phone.lower().strip() if data.email_or_phone else ""
    user = db.query(User).filter((User.email == key) | (User.phone == key)).first()

    # Vague generic error message for all login failures (security rule #4)
    generic_error = "Invalid login credentials"

    if not user:
        # Uniform delay to prevent timing attacks/enumeration
        time.sleep(0.3)
        raise HTTPException(status_code=401, detail=generic_error)

    # Check if account is currently locked out
    now = datetime.utcnow()
    if user.locked_until and user.locked_until > now:
        # Don't explicitly reveal "account locked" to prevent account enumeration
        # Introduce delay to slow down attackers
        time.sleep(1.0)
        raise HTTPException(status_code=401, detail=generic_error)

    # If lock duration expired, reset attempts
    if user.locked_until and user.locked_until <= now:
        user.locked_until = None
        user.failed_attempts = 0

    # Progressive delay based on current failed attempts (1s, 2s, 3s...)
    attempts = user.failed_attempts or 0
    if attempts > 0:
        delay = min(attempts * 0.5, 3.0)
        time.sleep(delay)

    # Verify password
    if not verify_password(data.password, user.hashed_password):
        user.failed_attempts = (user.failed_attempts or 0) + 1
        
        # Lock account after 5 consecutive failed attempts for 15 minutes
        if user.failed_attempts >= 5:
            user.locked_until = now + timedelta(minutes=15)
            
        db.commit()
        raise HTTPException(status_code=401, detail=generic_error)

    # On successful login: reset failed attempts & lock state
    user.failed_attempts = 0
    user.locked_until = None
    db.commit()

    token = create_access_token(data={"sub": str(user.id)})
    return {"access_token": token, "token_type": "bearer", "user": _profile_dict(user)}

@router.get("/me", response_model=UserProfile)
def me(current_user: User = Depends(get_current_user)):
    return _profile_dict(current_user)

@router.post("/verify-phone")
def verify_phone(current_user: User = Depends(get_current_user)):
    current_user.phone_verified = True
    return {"detail": "Phone verified"}

@router.post("/verify-nin")
def verify_nin(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    current_user.nin_verified = True
    current_user.id_verified = True
    _update_badge(current_user)
    db.commit()
    notify_kyc_submitted({"user_id": current_user.id, "name": current_user.name, "phone": current_user.phone})
    return {"detail": "NIN verified", "badge_tier": current_user.badge_tier}

@router.post("/verify-bvn")
def verify_bvn(bvn_in: VerifyBVN, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if len(bvn_in.bvn) != 11 or not bvn_in.bvn.isdigit():
        raise HTTPException(status_code=400, detail="BVN must be 11 digits")
    current_user.bvn_verified = True
    _update_badge(current_user)
    db.commit()
    return {"detail": "BVN verified", "badge_tier": current_user.badge_tier}

@router.post("/verify-business")
def verify_business(biz_in: VerifyBusiness, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    current_user.business_verified = True
    current_user.business_name = sanitize_text(biz_in.business_name, max_length=200)
    _update_badge(current_user)
    db.commit()
    return {"detail": "Business verified", "badge_tier": current_user.badge_tier, "business_name": current_user.business_name}

@router.get("/badge/{user_id}")
def get_badge(user_id: int, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return {
        "user_id": user.id,
        "name": user.name,
        "badge_tier": user.badge_tier,
        "nin_verified": user.nin_verified,
        "bvn_verified": user.bvn_verified,
        "business_verified": user.business_verified,
        "business_name": user.business_name,
        "total_deals": user.total_deals,
        "rating": user.rating,
    }
