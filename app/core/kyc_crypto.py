"""Field-level encryption for sensitive KYC data (NIN/BVN).

Design:
- AES-256-GCM authenticated encryption. Any tampering with ciphertext fails
  decryption loudly instead of returning garbage.
- Key comes from DEALSHIELD_KYC_KEY (32-byte urlsafe base64) or .env KYC_KEY.
  The key NEVER lives in the database — a stolen DB dump alone is useless.
- Format stored in DB: "enc:v1:<base64(nonce)>:<base64(ciphertext)>"
- Anonymous: only "enc:v1:..." strings are stored; the plaintext NIN/BVN never
  touches a log, audit table, or API response.
"""
import base64
import os
import re

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_V1_RE = re.compile(r"^enc:v1:([A-Za-z0-9+/=]+):([A-Za-z0-9+/=]+)$")


def _load_key() -> bytes:
    raw = os.environ.get("DEALSHIELD_KYC_KEY") or os.environ.get("KYC_KEY", "")
    if not raw:
        # fall back to .env via settings lazy import (avoids circular import)
        try:
            from dotenv import dotenv_values
            vals = dotenv_values(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))
            raw = vals.get("DEALSHIELD_KYC_KEY") or vals.get("KYC_KEY", "")
        except Exception:
            raw = ""
    if not raw:
        raise RuntimeError(
            "KYC encryption key missing. Set DEALSHIELD_KYC_KEY (32-byte urlsafe base64) in the environment or .env"
        )
    try:
        key = base64.urlsafe_b64decode(raw)
        if len(key) != 32:
            raise ValueError("bad length")
        return key
    except Exception:
        raise RuntimeError("DEALSHIELD_KYC_KEY must be 32 bytes encoded as urlsafe base64")


def generate_key() -> str:
    """Generate a new key string suitable for .env (run manually, print once)."""
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


def encrypt_field(plaintext: str) -> str:
    """Encrypt an NIN/BVN string. Returns 'enc:v1:<nonce>:<ct>'."""
    if not plaintext:
        return ""
    if plaintext.startswith("enc:v1:"):
        return plaintext  # already encrypted — idempotent
    key = _load_key()
    aes = AESGCM(key)
    nonce = os.urandom(12)
    ct = aes.encrypt(nonce, plaintext.encode(), b"dealshield-kyc")
    return f"enc:v1:{base64.b64encode(nonce).decode()}:{base64.b64encode(ct).decode()}"


def decrypt_field(value: str) -> str:
    """Decrypt an encrypted field. Raises if tampered. Returns '' for empty."""
    if not value:
        return ""
    m = _V1_RE.match(value)
    if not m:
        raise ValueError("Field is not encrypted with enc:v1 format")
    key = _load_key()
    aes = AESGCM(key)
    nonce = base64.b64decode(m.group(1))
    ct = base64.b64decode(m.group(2))
    return aes.decrypt(nonce, ct, b"dealshield-kyc").decode()


def mask_field(value: str) -> str:
    """Return a safe display form: last 4 chars visible, rest masked."""
    if not value:
        return ""
    try:
        plain = decrypt_field(value)
    except Exception:
        plain = value
    if len(plain) <= 4:
        return "*" * len(plain)
    return "*" * (len(plain) - 4) + plain[-4:]


def verify_id_number(number: str, id_type: str) -> bool:
    """Structural validation before anything is stored."""
    number = (number or "").strip()
    if id_type == "nin":
        return len(number) == 11 and number.isdigit()
    if id_type == "bvn":
        return len(number) == 11 and number.isdigit()
    return False
