import os
import re
import time
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import OAuth2PasswordBearer
import hmac
from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from app.dependencies import get_db
from app.schemas.schemas import (
    UserCreate, UserLogin, AuthResponse, UserProfile,
    VerifyBVN, VerifyBusiness,
    KYCSubmit, KYCStatus,
    RefreshTokenRequest, TokenResponse,
    EmailVerifyRequest,
    PasswordResetRequest, PasswordResetConfirm,
    ChangePasswordRequest, LogoutRequest,
    Enable2FAResponse, Verify2FARequest, Disable2FARequest, Login2FARequest,
)
from app.core.security import (
    get_password_hash, verify_password,
    create_access_token, decode_access_token,
    create_refresh_token, decode_refresh_token,
    create_email_verification_token, decode_email_verification_token,
    hash_token, generate_password_reset_token,
    generate_totp_secret, generate_totp_uri, verify_totp,
    generate_backup_codes, create_2fa_temp_token, decode_2fa_temp_token,
    new_session_id, credential_version,
)
from app.core.security_middleware import sanitize_text, validate_password_strength
from app.core.kyc_crypto import encrypt_field, decrypt_field, mask_field, verify_id_number
from app.core.kyc_provider import get_kyc_provider, KYC_PENDING, KYC_VERIFIED, KYC_NONE
from app.models.models import User, Session as SessionModel, PasswordResetToken, AuditLog
from app.core.notifications import notify_new_user, notify_kyc_submitted, notification_service
from decimal import Decimal
from app.core.money import money_out

router = APIRouter()
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")
optional_oauth2 = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)

# Test mode — welcome bonus wallet balance (demo only)
SAFEPAY_TEST_MODE = os.getenv("SAFEPAY_TEST_MODE", "false").strip().lower() in ("1", "true", "yes")

# Refresh token TTL
REFRESH_TOKEN_EXPIRE_DAYS = 30
# Password reset token TTL
PASSWORD_RESET_EXPIRE_HOURS = 1
# Session cleanup: max active sessions per user
MAX_ACTIVE_SESSIONS = 5


def _live_session(db: Session, claims: dict) -> SessionModel | None:
    """B05: the server-side session named by the token's sid must exist, belong to
    the subject, be unrevoked and unexpired. Checked on EVERY authenticated request."""
    return db.query(SessionModel).filter(
        SessionModel.sid == claims["sid"],
        SessionModel.user_id == int(claims["sub"]),
        SessionModel.revoked == False,
        SessionModel.expires_at > datetime.utcnow(),
    ).first()


def get_current_session(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> SessionModel:
    payload = decode_access_token(token)
    if payload is None:
        raise HTTPException(status_code=401, detail="Invalid, expired or legacy token. Please log in again.")
    session = _live_session(db, payload)
    if session is None:
        raise HTTPException(status_code=401, detail="Session expired or revoked. Please log in again.")
    return session


def get_current_user(session: SessionModel = Depends(get_current_session), db: Session = Depends(get_db)) -> User:
    user = db.query(User).filter(User.id == session.user_id).first()
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account suspended")
    return user


def _revoke_sessions(db: Session, user_id: int, except_sid: str | None = None):
    q = db.query(SessionModel).filter(SessionModel.user_id == user_id, SessionModel.revoked == False)
    if except_sid:
        q = q.filter(SessionModel.sid != except_sid)
    q.update({"revoked": True, "revoked_at": datetime.utcnow()}, synchronize_session=False)


def _revoke_user_credentials(db: Session, user_id: int):
    """Password change/reset: kill every session and every outstanding reset token."""
    _revoke_sessions(db, user_id)
    db.query(PasswordResetToken).filter(
        PasswordResetToken.user_id == user_id, PasswordResetToken.used == False,
    ).update({"used": True}, synchronize_session=False)


def _profile_dict(user: User) -> dict:
    return {
        "id": user.id,
        "name": user.name,
        "phone": user.phone,
        "email": user.email,
        "wallet_balance": money_out(user.wallet_balance),
        "nin_verified": user.nin_verified,
        "phone_verified": user.phone_verified,
        "email_verified": user.email_verified,
        "totp_enabled": bool(user.totp_enabled),
        "kyc_status": user.kyc_status or KYC_NONE,
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


def _create_session_record(db: Session, user_id: int, request: Request) -> tuple[str, str]:
    """Create a server-side session and return (access_token, refresh_token) bound to it."""
    now = datetime.utcnow()
    active = (
        db.query(SessionModel.id)
        .filter(SessionModel.user_id == user_id, SessionModel.revoked == False, SessionModel.expires_at > now)
        .order_by(SessionModel.created_at.asc(), SessionModel.id.asc())
        .all()
    )
    if len(active) >= MAX_ACTIVE_SESSIONS:
        oldest = [row[0] for row in active[:len(active) - MAX_ACTIVE_SESSIONS + 1]]
        db.query(SessionModel).filter(SessionModel.id.in_(oldest)).update(
            {"revoked": True, "revoked_at": now}, synchronize_session=False)

    sid = new_session_id()
    claims = {"sub": str(user_id), "sid": sid}
    access_token = create_access_token(claims)
    refresh_token = create_refresh_token(claims)
    db.add(SessionModel(
        user_id=user_id,
        sid=sid,
        token_hash=hash_token(refresh_token),
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent", "")[:500],
        expires_at=now + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS),
    ))
    return access_token, refresh_token


# ── REGISTER ──

@router.post("/register", response_model=AuthResponse)
def register(user_in: UserCreate, request: Request, db: Session = Depends(get_db)):
    is_valid, err_msg = validate_password_strength(user_in.password)
    if not is_valid:
        raise HTTPException(status_code=400, detail=err_msg)

    safe_name = sanitize_text(user_in.name, max_length=100)

    normalized_email = user_in.email.lower().strip()
    existing = db.query(User).filter((User.email == normalized_email) | (User.phone == user_in.phone)).first()
    if existing:
        raise HTTPException(status_code=400, detail="Email or phone already registered")

    user = User(
        name=safe_name,
        phone=user_in.phone,
        email=normalized_email,
        hashed_password=get_password_hash(user_in.password),
        wallet_balance=Decimal("500000.00") if SAFEPAY_TEST_MODE else Decimal("0.00"),
        phone_verified=False,  # B02: never assumed; needs an OTP provider
        email_verified=False,
        kyc_status=KYC_NONE,
        password_changed_at=datetime.utcnow(),
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="Email or phone already registered")
    db.refresh(user)

    access_token, refresh_token = _create_session_record(db, user.id, request)
    _log_audit(db, user.id, "register", request)
    db.commit()

    # Generate email verification token (for dev: return it; prod: send via email)
    email_token = create_email_verification_token(user.email)

    notify_new_user({"id": user.id, "name": user.name, "phone": user.phone, "email": user.email})

    # Send email verification (via notification service — logs if SMTP not configured)
    notification_service.notify_email_verification(user.email, email_token, user.name)

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

    # ── 2FA check: if enabled, return temp token instead of full access ──
    if user.totp_enabled and user.totp_secret:
        temp_token = create_2fa_temp_token(user.id, credential_version(user.hashed_password, user.totp_secret))
        _log_audit(db, user.id, "login_2fa_challenge", request)
        db.commit()
        return {
            "access_token": "",
            "refresh_token": "",
            "token_type": "bearer",
            "user": None,
            "requires_2fa": True,
            "temp_token": temp_token,
        }

    access_token, refresh_token = _create_session_record(db, user.id, request)
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
    """B06: single-use rotation via one conditional UPDATE. Presenting an already-
    rotated refresh token (reuse) revokes the whole session."""
    token_data = decode_refresh_token(payload.refresh_token)
    if token_data is None:
        raise HTTPException(status_code=401, detail="Invalid, expired or legacy refresh token. Please log in again.")
    user_id, sid = int(token_data["sub"]), token_data["sid"]
    user = db.query(User).filter(User.id == user_id).first()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or suspended")

    old_hash = hash_token(payload.refresh_token)
    claims = {"sub": str(user_id), "sid": sid}
    new_access = create_access_token(claims)
    new_refresh = create_refresh_token(claims)
    now = datetime.utcnow()
    rows = db.query(SessionModel).filter(
        SessionModel.sid == sid, SessionModel.user_id == user_id,
        SessionModel.token_hash == old_hash, SessionModel.revoked == False,
        SessionModel.expires_at > now,
    ).update({"token_hash": hash_token(new_refresh), "updated_at": now}, synchronize_session=False)
    if rows != 1:
        # Reuse / race loser: revoke the session family so a stolen token dies too.
        db.query(SessionModel).filter(SessionModel.sid == sid, SessionModel.user_id == user_id).update(
            {"revoked": True, "revoked_at": now}, synchronize_session=False)
        db.commit()
        raise HTTPException(status_code=401, detail="Refresh token already used or session revoked")
    _log_audit(db, user.id, "token_refresh", request)
    db.commit()
    return {"access_token": new_access, "refresh_token": new_refresh, "token_type": "bearer"}


# ── LOGOUT / REVOKE ──

@router.post("/logout")
def logout(request: Request, payload: LogoutRequest | None = None,
           token: str | None = Depends(optional_oauth2), db: Session = Depends(get_db)):
    """B05: revoke the server-side session named by the bearer access token (and/or
    the refresh token). After this, the access token is rejected on every route."""
    revoked = 0
    now = datetime.utcnow()
    for claims in (decode_access_token(token) if token else None,
                   decode_refresh_token(payload.refresh_token) if payload and payload.refresh_token else None):
        if claims:
            revoked += db.query(SessionModel).filter(
                SessionModel.sid == claims["sid"], SessionModel.user_id == int(claims["sub"]),
                SessionModel.revoked == False,
            ).update({"revoked": True, "revoked_at": now}, synchronize_session=False)
    db.commit()
    return {"detail": "Logged out successfully", "revoked": bool(revoked)}


# ── LOGOUT ALL DEVICES ──

@router.post("/logout-all")
def logout_all(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Revoke all sessions for the current user (logout from all devices)."""
    _revoke_sessions(db, current_user.id)
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
    notification_service.notify_email_verification(current_user.email, token, current_user.name)
    return {"detail": "Verification email sent"}  # B07: token is never returned


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
        # B07/B13: token goes only to the user's email; never in the response,
        # never via the SMS fallback (which logs message text), in ANY environment.
        notification_service.notify_password_reset(user.email, raw_token, user.name)
        return {"detail": "If the account exists, a reset link has been sent."}

    # Always return success to prevent enumeration
    time.sleep(0.3)
    return {"detail": "If the account exists, a reset link has been sent."}


@router.post("/password-reset/confirm")
def confirm_password_reset(payload: PasswordResetConfirm, request: Request, db: Session = Depends(get_db)):
    """B06: the reset token is claimed with one conditional UPDATE (used=False -> True),
    so two concurrent requests cannot both succeed; a reused token is rejected."""
    is_valid, err_msg = validate_password_strength(payload.new_password)
    if not is_valid:
        raise HTTPException(status_code=400, detail=err_msg)

    token_hash = hash_token(payload.token)
    now = datetime.utcnow()
    reset = db.query(PasswordResetToken).filter(PasswordResetToken.token_hash == token_hash).first()
    if reset is None:
        raise HTTPException(status_code=400, detail="Invalid reset token")
    claimed = db.query(PasswordResetToken).filter(
        PasswordResetToken.id == reset.id, PasswordResetToken.used == False,
        PasswordResetToken.expires_at > now,
    ).update({"used": True}, synchronize_session=False)
    if claimed != 1:
        db.commit()
        raise HTTPException(status_code=400, detail="Reset token already used or expired")

    user = db.query(User).filter(User.id == reset.user_id).first()
    if not user or not user.is_active:
        db.rollback()
        raise HTTPException(status_code=400, detail="Account not found")

    user.hashed_password = get_password_hash(payload.new_password)
    user.password_changed_at = now
    _revoke_user_credentials(db, user.id)  # all sessions + any other reset tokens
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
    _revoke_user_credentials(db, current_user.id)  # B05: every session incl. this one
    _log_audit(db, current_user.id, "password_change", request)
    db.commit()
    return {"detail": "Password changed successfully. Please log in again on all devices."}


# ── MANDATORY KYC: submit NIN or BVN (stored encrypted) ──

@router.post("/kyc/submit", response_model=KYCStatus)
def kyc_submit(payload: KYCSubmit, request: Request,
               current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Submit NIN or BVN for identity verification.
    The ID number is validated, then stored AES-256-GCM encrypted — plaintext
    never touches the database, logs, or audit tables.
    """
    id_type = (payload.id_type or "").strip().lower()
    if id_type not in ("nin", "bvn"):
        raise HTTPException(status_code=400, detail="id_type must be 'nin' or 'bvn'")

    id_number = (payload.id_number or "").strip()
    if not verify_id_number(id_number, id_type):
        raise HTTPException(status_code=400, detail=f"{id_type.upper()} must be exactly 11 digits")

    phone = (payload.phone or "").strip()
    if not re.match(r"^\+?\d{7,15}$", phone):
        raise HTTPException(status_code=400, detail="A valid phone number is required")

    if current_user.kyc_status == KYC_VERIFIED:
        raise HTTPException(status_code=400, detail="Identity already verified")

    encrypted = encrypt_field(id_number)

    if id_type == "nin":
        current_user.nin_encrypted = encrypted
    else:
        current_user.bvn_encrypted = encrypted

    current_user.kyc_id_type = id_type
    current_user.kyc_phone_provided = phone
    current_user.kyc_submitted_at = datetime.utcnow()
    # B02: format checks are NOT verification. Fail closed: pending until a
    # provider or a manual reviewer (POST /admin/kyc/{user_id}/decision) decides.
    status = get_kyc_provider().submit_for_verification(
        user_id=current_user.id, id_type=id_type, encrypted_id=encrypted, phone=phone)
    if status == KYC_VERIFIED:  # defence in depth: a stub may never self-verify
        status = KYC_PENDING
    current_user.kyc_status = status
    current_user.kyc_verified = False

    _log_audit(db, current_user.id, "kyc_submitted", request)  # no ID number in details
    db.commit()

    return {
        "kyc_verified": False,
        "kyc_status": current_user.kyc_status,
        "id_type": id_type,
        "id_masked": mask_field(encrypted),
        "phone_provided": phone,
        "submitted_at": current_user.kyc_submitted_at,
    }


@router.get("/kyc/status", response_model=KYCStatus)
def kyc_status(current_user: User = Depends(get_current_user)):
    enc = current_user.nin_encrypted or current_user.bvn_encrypted or ""
    return {
        "kyc_verified": bool(current_user.kyc_verified) and current_user.kyc_status == KYC_VERIFIED,
        "kyc_status": current_user.kyc_status or KYC_NONE,
        "id_type": current_user.kyc_id_type,
        "id_masked": mask_field(enc) if enc else None,
        "phone_provided": current_user.kyc_phone_provided,
        "submitted_at": current_user.kyc_submitted_at,
    }


# ── PHONE / NIN / BVN / BUSINESS VERIFICATION (existing) ──

_NO_VERIFIER = "Verification is unavailable: no verification provider is integrated. Use /auth/kyc/submit (reviewed manually)."


@router.post("/verify-phone")
def verify_phone(current_user: User = Depends(get_current_user)):
    raise HTTPException(status_code=503, detail=_NO_VERIFIER)  # B02: no self-verification


@router.post("/verify-nin")
def verify_nin(current_user: User = Depends(get_current_user)):
    raise HTTPException(status_code=503, detail=_NO_VERIFIER)


@router.post("/verify-bvn")
def verify_bvn(bvn_in: VerifyBVN, current_user: User = Depends(get_current_user)):
    raise HTTPException(status_code=503, detail=_NO_VERIFIER)


@router.post("/verify-business")
def verify_business(biz_in: VerifyBusiness, current_user: User = Depends(get_current_user)):
    raise HTTPException(status_code=503, detail=_NO_VERIFIER)


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


# ── 2FA / TOTP ──

@router.post("/2fa/enable", response_model=Enable2FAResponse)
def enable_2fa(request: Request, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Step 1: Generate a TOTP secret, return URI + backup codes.
    The secret is stored but 2FA is NOT enabled until verified via /auth/2fa/verify.
    """
    if current_user.totp_enabled:
        raise HTTPException(status_code=400, detail="2FA is already enabled")

    secret = generate_totp_secret()
    uri = generate_totp_uri(secret, current_user.email)
    backup_codes = generate_backup_codes()

    # Store the secret (not enabled yet) and backup codes (as JSON in totp_secret field
    # as a pipe-separated value: secret|backup_codes — to keep schema simple)
    # B12: backup codes are stored only as SHA-256 hashes
    current_user.totp_secret = "|".join([secret] + [hash_token(c) for c in backup_codes])

    _log_audit(db, current_user.id, "2fa_enable_initiated", request)
    db.commit()

    return {
        "secret": secret,
        "uri": uri,
        "backup_codes": backup_codes,
    }


@router.post("/2fa/verify")
def verify_2fa(payload: Verify2FARequest, request: Request, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Step 2: Verify a TOTP token to complete 2FA enablement."""
    if current_user.totp_enabled:
        raise HTTPException(status_code=400, detail="2FA is already enabled")

    if not current_user.totp_secret:
        raise HTTPException(status_code=400, detail="2FA has not been initiated. Call /auth/2fa/enable first.")

    # Extract the actual TOTP secret (strip backup codes)
    parts = current_user.totp_secret.split("|")
    secret = parts[0]

    if not verify_totp(secret, payload.token):
        raise HTTPException(status_code=400, detail="Invalid TOTP token")

    # Enable 2FA; keep the hashed backup codes (B12: they are now usable, once each)
    current_user.totp_enabled = True

    # B05: 2FA change revokes every session (including this one): log in again with 2FA
    _revoke_sessions(db, current_user.id)

    _log_audit(db, current_user.id, "2fa_enabled", request)
    db.commit()

    return {"detail": "2FA enabled successfully. Other sessions have been logged out."}


@router.post("/2fa/disable")
def disable_2fa(payload: Disable2FARequest, request: Request, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Disable 2FA — requires a valid TOTP token."""
    if not current_user.totp_enabled:
        raise HTTPException(status_code=400, detail="2FA is not enabled")

    if not current_user.totp_secret or not verify_totp(current_user.totp_secret.split("|")[0], payload.token):
        raise HTTPException(status_code=400, detail="Invalid TOTP token")

    current_user.totp_enabled = False
    current_user.totp_secret = None

    _revoke_sessions(db, current_user.id)  # B05

    _log_audit(db, current_user.id, "2fa_disabled", request)
    db.commit()

    return {"detail": "2FA disabled successfully. All sessions have been logged out."}


@router.post("/login/2fa", response_model=AuthResponse)
def login_2fa(payload: Login2FARequest, request: Request, db: Session = Depends(get_db)):
    """Complete login for a 2FA-enabled user.
    Accepts the temp token from /auth/login + a TOTP code (or backup code).
    Returns full access + refresh tokens on success.
    """
    temp_data = decode_2fa_temp_token(payload.temp_token)
    if temp_data is None:
        raise HTTPException(status_code=401, detail="Invalid or expired 2FA token")

    user_id = int(temp_data.get("sub", 0))
    user = db.query(User).filter(User.id == user_id).first()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or suspended")

    if not user.totp_enabled or not user.totp_secret:
        raise HTTPException(status_code=400, detail="2FA is not enabled for this account")

    # B12: challenge is bound to the password + 2FA secret at issue time
    if not hmac.compare_digest(str(temp_data.get("cv", "")),
                               credential_version(user.hashed_password, user.totp_secret)):
        raise HTTPException(status_code=401, detail="Credentials changed. Please start login again.")

    totp_code = payload.totp_code.strip()
    stored = user.totp_secret
    parts = stored.split("|")
    if totp_code.isdigit() and len(totp_code) == 6:
        if not verify_totp(parts[0], totp_code):
            raise HTTPException(status_code=401, detail="Invalid TOTP code")
    elif len(totp_code) == 8 and all(c in "0123456789abcdefABCDEF" for c in totp_code):
        code_hash = hash_token(totp_code.upper())
        matched = next((c for c in parts[1:] if hmac.compare_digest(c, code_hash)), None)
        if matched is None:
            raise HTTPException(status_code=401, detail="Invalid backup code")
        remaining = [c for c in parts[1:] if c != matched]
        rows = db.query(User).filter(User.id == user.id, User.totp_secret == stored).update(
            {"totp_secret": "|".join([parts[0]] + remaining)}, synchronize_session=False)
        if rows != 1:
            db.rollback()
            raise HTTPException(status_code=401, detail="Backup code already used")
    else:
        raise HTTPException(status_code=401, detail="Invalid TOTP code format")

    access_token, refresh_token = _create_session_record(db, user.id, request)
    _log_audit(db, user.id, "login_2fa_success", request)
    db.commit()

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "user": _profile_dict(user),
    }
