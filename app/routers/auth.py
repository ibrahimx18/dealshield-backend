import os
import time
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session
from app.dependencies import get_db
from app.schemas.schemas import (
    UserCreate, UserLogin, AuthResponse, UserProfile,
    VerifyBVN, VerifyBusiness,
    RefreshTokenRequest, TokenResponse,
    EmailVerifyRequest,
    PasswordResetRequest, PasswordResetConfirm,
    ChangePasswordRequest, LogoutRequest,
)
from app.core.security import (
    get_password_hash, verify_password,
    create_access_token, decode_access_token,
    create_refresh_token, decode_refresh_token,
    create_email_verification_token, decode_email_verification_token,
    hash_token, generate_password_reset_token,
)
from app.core.security_middleware import sanitize_text, validate_password_strength
from app.models.models import User, Session as SessionModel, PasswordResetToken, AuditLog
from app.core.notifications import notify_new_user, notify_kyc_submitted

router = APIRouter()
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")

# Test mode — welcome bonus wallet balance (demo only)
SAFEPAY_TEST_MODE = os.getenv("SAFEPAY_TEST_MODE", "false").strip().lower() in ("1", "true", "yes")

# Refresh token TTL
REFRESH_TOKEN_EXPIRE_DAYS = 30
# Password reset token TTL
PASSWORD_RESET_EXPIRE_HOURS = 1
# Session cleanup: max active sessions per user
MAX_ACTIVE_SESSIONS = 5


def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> User:
    payload = decode_access_token(token)
    if payload is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    user_id = payload.get("sub")
    if user_id is None:
        raise HTTPException(status_code=401, detail="Invalid token")
    user = db.query(User).filter(User.id == int(user_id)).first()
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account suspended")
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
        "email_verified": user.email_verified,
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


def _log_audit(db: Session, actor_id: int, action: str, request: Request,
              target_type: str | None = None, target_id: int | None = None, details: str = ""):
    """Helper to write a general audit log entry."""
    log = AuditLog(
        actor_id=actor_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        ip_address=request.client.host if request.client else None,
        details=details,
    )
    db.add(log)


def _create_session_record(db: Session, user_id: int, refresh_token: str,
                           request: Request) -> None:
    """Store the hashed refresh token in the sessions table."""
    # Enforce max active sessions — revoke oldest if over limit
    active_sessions = (
        db.query(SessionModel)
        .filter(SessionModel.user_id == user_id, SessionModel.revoked == False)
        .order_by(SessionModel.created_at.asc())
        .all()
    )
    if len(active_sessions) >= MAX_ACTIVE_SESSIONS:
        for s in active_sessions[:len(active_sessions) - MAX_ACTIVE_SESSIONS + 1]:
            s.revoked = True

    session = SessionModel(
        user_id=user_id,
        token_hash=hash_token(refresh_token),
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent", "")[:500],
        expires_at=datetime.utcnow() + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS),
    )
    db.add(session)


# ── REGISTER ──

@router.post("/register", response_model=AuthResponse)
def register(user_in: UserCreate, request: Request, db: Session = Depends(get_db)):
    is_valid, err_msg = validate_password_strength(user_in.password)
    if not is_valid:
        raise HTTPException(status_code=400, detail=err_msg)

    safe_name = sanitize_text(user_in.name, max_length=100)

    existing = db.query(User).filter((User.email == user_in.email) | (User.phone == user_in.phone)).first()
    if existing:
        raise HTTPException(status_code=400, detail="Email or phone already registered")

    user = User(
        name=safe_name,
        phone=user_in.phone,
        email=user_in.email.lower().strip(),
        hashed_password=get_password_hash(user_in.password),
        wallet_balance=500000.0 if SAFEPAY_TEST_MODE else 0.0,
        phone_verified=True,
        email_verified=False,
        password_changed_at=datetime.utcnow(),
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    # Generate tokens
    access_token = create_access_token(data={"sub": str(user.id)})
    refresh_token = create_refresh_token(data={"sub": str(user.id)})

    # Store session
    _create_session_record(db, user.id, refresh_token, request)
    _log_audit(db, user.id, "register", request)
    db.commit()

    # Generate email verification token (for dev: return it; prod: send via email)
    email_token = create_email_verification_token(user.email)

    notify_new_user({"id": user.id, "name": user.name, "phone": user.phone, "email": user.email})

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "user": _profile_dict(user),
    }


# ── LOGIN ──

@router.post("/login", response_model=AuthResponse)
def login(data: UserLogin, request: Request, db: Session = Depends(get_db)):
    key = data.email_or_phone.lower().strip() if data.email_or_phone else ""
    user = db.query(User).filter((User.email == key) | (User.phone == key)).first()

    generic_error = "Invalid login credentials"

    if not user:
        time.sleep(0.3)
        raise HTTPException(status_code=401, detail=generic_error)

    if not user.is_active:
        time.sleep(0.5)
        raise HTTPException(status_code=403, detail="Account suspended. Contact support.")

    now = datetime.utcnow()
    if user.locked_until and user.locked_until > now:
        time.sleep(1.0)
        raise HTTPException(status_code=401, detail=generic_error)

    if user.locked_until and user.locked_until <= now:
        user.locked_until = None
        user.failed_attempts = 0

    attempts = user.failed_attempts or 0
    if attempts > 0:
        delay = min(attempts * 0.5, 3.0)
        time.sleep(delay)

    if not verify_password(data.password, user.hashed_password):
        user.failed_attempts = (user.failed_attempts or 0) + 1
        if user.failed_attempts >= 5:
            user.locked_until = now + timedelta(minutes=15)
        db.commit()
        raise HTTPException(status_code=401, detail=generic_error)

    # Success
    user.failed_attempts = 0
    user.locked_until = None

    access_token = create_access_token(data={"sub": str(user.id)})
    refresh_token = create_refresh_token(data={"sub": str(user.id)})

    _create_session_record(db, user.id, refresh_token, request)
    _log_audit(db, user.id, "login", request)
    db.commit()

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "user": _profile_dict(user),
    }


# ── GET PROFILE ──

@router.get("/me", response_model=UserProfile)
def me(current_user: User = Depends(get_current_user)):
    return _profile_dict(current_user)


# ── REFRESH TOKEN ──

@router.post("/refresh", response_model=TokenResponse)
def refresh_token(payload: RefreshTokenRequest, request: Request, db: Session = Depends(get_db)):
    """Exchange a valid refresh token for a new access + refresh token pair.
    Implements rotation: old refresh token is revoked, new one issued.
    """
    token_data = decode_refresh_token(payload.refresh_token)
    if token_data is None:
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")

    user_id = int(token_data.get("sub", 0))
    user = db.query(User).filter(User.id == user_id).first()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or suspended")

    # Verify the session exists and is not revoked
    token_hash = hash_token(payload.refresh_token)
    session = (
        db.query(SessionModel)
        .filter(SessionModel.token_hash == token_hash, SessionModel.revoked == False)
        .first()
    )
    if not session:
        raise HTTPException(status_code=401, detail="Session not found or revoked")

    if session.expires_at < datetime.utcnow():
        session.revoked = True
        db.commit()
        raise HTTPException(status_code=401, detail="Session expired")

    # Rotate: revoke old session, create new
    session.revoked = True
    db.flush()  # Ensure revoke is processed before inserting new session

    new_access = create_access_token(data={"sub": str(user.id)})
    new_refresh = create_refresh_token(data={"sub": str(user.id)})
    _create_session_record(db, user.id, new_refresh, request)
    _log_audit(db, user.id, "token_refresh", request)
    db.commit()

    return {
        "access_token": new_access,
        "refresh_token": new_refresh,
        "token_type": "bearer",
    }


# ── LOGOUT / REVOKE ──

@router.post("/logout")
def logout(payload: LogoutRequest, db: Session = Depends(get_db)):
    """Revoke the current session (logout). Invalidates the refresh token."""
    token_hash = hash_token(payload.refresh_token)
    session = (
        db.query(SessionModel)
        .filter(SessionModel.token_hash == token_hash, SessionModel.revoked == False)
        .first()
    )
    if session:
        session.revoked = True
        db.commit()
    return {"detail": "Logged out successfully"}


# ── LOGOUT ALL DEVICES ──

@router.post("/logout-all")
def logout_all(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Revoke all sessions for the current user (logout from all devices)."""
    db.query(SessionModel).filter(
        SessionModel.user_id == current_user.id,
        SessionModel.revoked == False
    ).update({"revoked": True})
    db.commit()
    return {"detail": "Logged out from all devices"}


# ── EMAIL VERIFICATION ──

@router.post("/verify-email")
def verify_email(payload: EmailVerifyRequest, db: Session = Depends(get_db)):
    """Verify email address using the verification token."""
    email = decode_email_verification_token(payload.token)
    if email is None:
        raise HTTPException(status_code=400, detail="Invalid or expired verification token")

    user = db.query(User).filter(User.email == email).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.email_verified:
        return {"detail": "Email already verified"}

    user.email_verified = True
    _update_badge(user)
    db.commit()
    return {"detail": "Email verified successfully"}


@router.post("/resend-verification")
def resend_verification(current_user: User = Depends(get_current_user)):
    """Resend email verification token (for authenticated user)."""
    if current_user.email_verified:
        return {"detail": "Email already verified"}
    token = create_email_verification_token(current_user.email)
    # In production: send via email service. In dev: return token for testing.
    if os.getenv("ENVIRONMENT", "development") != "production":
        return {"detail": "Verification email sent", "token": token}
    # TODO: integrate email service (SMTP/SendGrid) to send token to user
    return {"detail": "Verification email sent"}


# ── PASSWORD RESET ──

@router.post("/password-reset/request")
def request_password_reset(payload: PasswordResetRequest, request: Request, db: Session = Depends(get_db)):
    """Step 1: Request a password reset. Always returns success (no email enumeration)."""
    key = payload.email_or_phone.lower().strip()
    user = db.query(User).filter((User.email == key) | (User.phone == key)).first()

    if user and user.is_active:
        raw_token, token_hash = generate_password_reset_token()
        reset = PasswordResetToken(
            user_id=user.id,
            token_hash=token_hash,
            expires_at=datetime.utcnow() + timedelta(hours=PASSWORD_RESET_EXPIRE_HOURS),
        )
        db.add(reset)
        _log_audit(db, user.id, "password_reset_request", request)
        db.commit()
        # In production: send via email/SMS. In dev: return token for testing.
        if os.getenv("ENVIRONMENT", "development") != "production":
            return {"detail": "If the account exists, a reset link has been sent.", "token": raw_token}
        # TODO: integrate email/SMS service to send reset link
        return {"detail": "If the account exists, a reset link has been sent."}

    # Always return success to prevent enumeration
    time.sleep(0.3)
    return {"detail": "If the account exists, a reset link has been sent."}


@router.post("/password-reset/confirm")
def confirm_password_reset(payload: PasswordResetConfirm, request: Request, db: Session = Depends(get_db)):
    """Step 2: Confirm password reset with token + new password."""
    token_hash = hash_token(payload.token)
    reset = (
        db.query(PasswordResetToken)
        .filter(PasswordResetToken.token_hash == token_hash, PasswordResetToken.used == False)
        .first()
    )

    if not reset:
        raise HTTPException(status_code=400, detail="Invalid reset token")

    if reset.expires_at < datetime.utcnow():
        reset.used = True
        db.commit()
        raise HTTPException(status_code=400, detail="Reset token expired")

    user = db.query(User).filter(User.id == reset.user_id).first()
    if not user or not user.is_active:
        raise HTTPException(status_code=400, detail="Account not found")

    is_valid, err_msg = validate_password_strength(payload.new_password)
    if not is_valid:
        raise HTTPException(status_code=400, detail=err_msg)

    user.hashed_password = get_password_hash(payload.new_password)
    user.password_changed_at = datetime.utcnow()
    reset.used = True

    # Revoke all existing sessions (force re-login everywhere)
    db.query(SessionModel).filter(
        SessionModel.user_id == user.id, SessionModel.revoked == False
    ).update({"revoked": True})

    _log_audit(db, user.id, "password_reset", request)
    db.commit()

    return {"detail": "Password reset successfully. Please log in again."}


# ── CHANGE PASSWORD (while logged in) ──

@router.post("/change-password")
def change_password(payload: ChangePasswordRequest, request: Request,
                     current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Change password while authenticated. Requires current password."""
    if not verify_password(payload.current_password, current_user.hashed_password):
        raise HTTPException(status_code=400, detail="Current password is incorrect")

    is_valid, err_msg = validate_password_strength(payload.new_password)
    if not is_valid:
        raise HTTPException(status_code=400, detail=err_msg)

    if payload.current_password == payload.new_password:
        raise HTTPException(status_code=400, detail="New password must be different from current password")

    current_user.hashed_password = get_password_hash(payload.new_password)
    current_user.password_changed_at = datetime.utcnow()

    _log_audit(db, current_user.id, "password_change", request)
    db.commit()

    return {"detail": "Password changed successfully"}


# ── PHONE / NIN / BVN / BUSINESS VERIFICATION (existing) ──

@router.post("/verify-phone")
def verify_phone(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    current_user.phone_verified = True
    db.commit()
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


# ── LIST ACTIVE SESSIONS ──

@router.get("/sessions")
def list_sessions(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """List all active sessions for the current user (for security overview)."""
    sessions = (
        db.query(SessionModel)
        .filter(SessionModel.user_id == current_user.id, SessionModel.revoked == False)
        .order_by(SessionModel.created_at.desc())
        .all()
    )
    return {
        "sessions": [
            {
                "id": s.id,
                "ip_address": s.ip_address,
                "user_agent": s.user_agent[:100] if s.user_agent else None,
                "created_at": s.created_at.isoformat() if s.created_at else None,
                "expires_at": s.expires_at.isoformat() if s.expires_at else None,
            }
            for s in sessions
        ]
    }
