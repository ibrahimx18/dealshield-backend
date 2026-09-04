import hashlib
import secrets
from passlib.context import CryptContext
from datetime import datetime, timedelta
from typing import Optional, Tuple
from jwt import PyJWTError as JWTError, encode as jwt_encode, decode as jwt_decode
from app.core.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Token type constants for JWT "type" claim
TOKEN_TYPE_ACCESS = "access"
TOKEN_TYPE_REFRESH = "refresh"
TOKEN_TYPE_EMAIL_VERIFY = "email_verify"


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


# ── Access Token (short-lived JWT) ──

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire, "type": TOKEN_TYPE_ACCESS})
    return jwt_encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def decode_access_token(token: str) -> Optional[dict]:
    """Decode and validate an access token. Returns payload or None."""
    try:
        payload = jwt_decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        if payload.get("type") != TOKEN_TYPE_ACCESS:
            return None
        return payload
    except JWTError:
        return None


# ── Refresh Token (longer-lived JWT) ──

def create_refresh_token(data: dict) -> str:
    """Create a longer-lived refresh token (30 days).
    Includes a random JTI to ensure uniqueness even if generated in the same second.
    """
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(days=30)
    to_encode.update({"exp": expire, "type": TOKEN_TYPE_REFRESH, "jti": secrets.token_hex(16)})
    return jwt_encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def decode_refresh_token(token: str) -> Optional[dict]:
    """Decode and validate a refresh token. Returns payload or None."""
    try:
        payload = jwt_decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        if payload.get("type") != TOKEN_TYPE_REFRESH:
            return None
        return payload
    except JWTError:
        return None


# ── Email Verification Token (short-lived JWT) ──

def create_email_verification_token(email: str) -> str:
    """Create a 24-hour email verification token."""
    expire = datetime.utcnow() + timedelta(hours=24)
    to_encode = {"sub": email, "exp": expire, "type": TOKEN_TYPE_EMAIL_VERIFY}
    return jwt_encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def decode_email_verification_token(token: str) -> Optional[str]:
    """Decode and validate an email verification token. Returns email or None."""
    try:
        payload = jwt_decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        if payload.get("type") != TOKEN_TYPE_EMAIL_VERIFY:
            return None
        return payload.get("sub")
    except JWTError:
        return None


# ── Token Hashing (for storage) ──

def hash_token(token: str) -> str:
    """SHA-256 hash a token for safe storage. Never store raw tokens."""
    return hashlib.sha256(token.encode()).hexdigest()


# ── Password Reset Token (random, not JWT) ──

def generate_password_reset_token() -> Tuple[str, str]:
    """Generate a random password reset token.
    Returns (raw_token, token_hash) — store the hash, return the raw token to user.
    """
    raw_token = secrets.token_urlsafe(32)
    token_hash = hash_token(raw_token)
    return raw_token, token_hash


# ── TOTP / 2FA ──

TOKEN_TYPE_2FA_TEMP = "2fa_temp"


def generate_totp_secret() -> str:
    """Generate a random base32 TOTP secret."""
    import pyotp
    return pyotp.random_base32()


def generate_totp_uri(secret: str, email: str, issuer: str = "DealShield") -> str:
    """Generate an otpauth:// URI for QR code provisioning."""
    import pyotp
    totp = pyotp.TOTP(secret)
    return totp.provisioning_uri(name=email, issuer_name=issuer)


def verify_totp(secret: str, token: str) -> bool:
    """Verify a TOTP token against the secret. Returns True if valid."""
    import pyotp
    totp = pyotp.TOTP(secret)
    return totp.verify(token, valid_window=1)


def generate_backup_codes(count: int = 10) -> list[str]:
    """Generate one-time backup codes (8-char alphanumeric)."""
    return [secrets.token_hex(4).upper() for _ in range(count)]


def create_2fa_temp_token(user_id: int) -> str:
    """Create a short-lived temporary JWT for the 2FA login flow (5 minutes)."""
    expire = datetime.utcnow() + timedelta(minutes=5)
    to_encode = {"sub": str(user_id), "exp": expire, "type": TOKEN_TYPE_2FA_TEMP}
    return jwt_encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def decode_2fa_temp_token(token: str) -> Optional[dict]:
    """Decode and validate a 2FA temp token. Returns payload or None."""
    try:
        payload = jwt_decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        if payload.get("type") != TOKEN_TYPE_2FA_TEMP:
            return None
        return payload
    except JWTError:
        return None
