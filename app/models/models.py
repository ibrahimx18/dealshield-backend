from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, ForeignKey, Enum as SQLEnum, Text
from sqlalchemy.orm import relationship
from datetime import datetime
from app.core.database import Base
import enum


class CommodityCategory(str, enum.Enum):
    cars = "cars"
    gold = "gold"
    dollars = "dollars"
    oil = "oil"
    land = "land"
    cement = "cement"
    crypto = "crypto"
    giftcards = "giftcards"


class EscrowStatus(str, enum.Enum):
    created = "created"
    seller_accepted = "seller_accepted"
    seller_declined = "seller_declined"
    payment_pending = "payment_pending"
    funded = "funded"
    seller_fulfilling = "seller_fulfilling"
    buyer_review = "buyer_review"
    buyer_approved = "buyer_approved"
    disputed = "disputed"
    under_investigation = "under_investigation"
    released = "released"
    refunded = "refunded"
    split_resolution = "split_resolution"
    cancelled = "cancelled"
    expired = "expired"
    closed = "closed"
    # Legacy statuses (for backward compat with existing transactions)
    pending = "pending"
    funds_deposited = "funds_deposited"
    shipped = "shipped"
    delivered = "delivered"


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    phone = Column(String, nullable=False, index=True)
    email = Column(String, nullable=False, unique=True, index=True)
    hashed_password = Column(String, nullable=False)
    wallet_balance = Column(Float, default=0.0)
    nin_verified = Column(Boolean, default=False)
    phone_verified = Column(Boolean, default=True)
    email_verified = Column(Boolean, default=False, nullable=False)  # NEW: email verification flag
    id_verified = Column(Boolean, default=False)
    bvn_verified = Column(Boolean, default=False)
    business_verified = Column(Boolean, default=False)
    business_name = Column(String, default="")
    badge_tier = Column(String, default="none")  # none, verified, trusted, top_dealer
    total_deals = Column(Integer, default=0)
    rating = Column(Float, default=5.0)
    failed_attempts = Column(Integer, default=0)
    locked_until = Column(DateTime, nullable=True)
    password_changed_at = Column(DateTime, nullable=True)  # NEW: track password changes for token invalidation
    is_active = Column(Boolean, default=True, nullable=False)  # NEW: soft-disable instead of delete
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)  # NEW
    role = Column(String, default="user")  # user, merchant, admin
    totp_secret = Column(String, nullable=True)  # TOTP shared secret for 2FA
    totp_enabled = Column(Boolean, default=False)  # whether 2FA is enabled

    listings = relationship("Listing", backref="seller", foreign_keys="Listing.seller_id")
    reviews_given = relationship("Review", backref="reviewer", foreign_keys="Review.reviewer_id")
    reviews_received = relationship("Review", backref="reviewee", foreign_keys="Review.reviewer_id")
    sessions = relationship("Session", backref="user", cascade="all, delete-orphan")  # NEW


class Listing(Base):
    __tablename__ = "listings"
    id = Column(Integer, primary_key=True, index=True)
    category = Column(String, nullable=False)
    title = Column(String, nullable=False, index=True)
    description = Column(String, default="")
    price = Column(Float, nullable=False)
    location = Column(String, default="")
    seller_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    seller_name = Column(String, default="")
    seller_rating = Column(String, default="5.0")
    verified = Column(Boolean, default=False)
    image_path = Column(String, nullable=True)
    insured = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True, nullable=False)  # NEW: soft-delete listings
    posted_date = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)  # NEW


class EscrowTransaction(Base):
    __tablename__ = "escrow_transactions"
    id = Column(Integer, primary_key=True, index=True)
    listing_id = Column(Integer, ForeignKey("listings.id"), nullable=False)
    listing_title = Column(String, nullable=False)
    category = Column(String, nullable=False)
    amount = Column(Float, nullable=False)
    commission = Column(Float, nullable=False)
    status = Column(String, default="created")
    buyer_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    seller_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    buyer_name = Column(String, default="")
    seller_name = Column(String, default="")
    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)
    insured = Column(Boolean, default=False)
    logistics_provider = Column(String, default="")
    tracking_number = Column(String, default="")
    insurance_fee = Column(Float, default=0)
    pickup_otp = Column(String, default="")  # 6-digit OTP for rider pickup
    rider_phone = Column(String, default="")  # dispatch rider's phone
    rider_name = Column(String, default="")
    pickup_confirmed = Column(Boolean, default=False)
    dispatched_at = Column(DateTime, nullable=True)
    # ── OTP hardening columns ──
    otp_attempts = Column(Integer, default=0, nullable=True)
    otp_locked = Column(Boolean, default=False, nullable=True)
    otp_expiry = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    # ── New escrow flow columns ──
    accepted_at = Column(DateTime, nullable=True)          # seller accepted the deal
    funded_at = Column(DateTime, nullable=True)            # payment confirmed
    fulfilment_started_at = Column(DateTime, nullable=True) # seller began fulfilling
    buyer_review_started_at = Column(DateTime, nullable=True)  # buyer received goods
    buyer_review_deadline = Column(DateTime, nullable=True)    # auto-release after this
    dispute_reason = Column(Text, nullable=True)           # who disputed and why
    dispute_evidence = Column(Text, nullable=True)         # JSON: links, docs, messages
    dispute_initiated_by = Column(String, default="")      # buyer or seller
    admin_resolution = Column(String, nullable=True)       # release_to_seller, refund_to_buyer, split
    admin_reason = Column(Text, nullable=True)             # admin's explanation
    resolved_at = Column(DateTime, nullable=True)          # when admin resolved
    closed_at = Column(DateTime, nullable=True)             # terminal state timestamp
    # ── Expiry deadlines ──
    accept_deadline = Column(DateTime, nullable=True)      # seller must accept within 48h
    payment_deadline = Column(DateTime, nullable=True)     # buyer must pay within 24h
    # ── Facilitator columns ──
    facilitator_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    facilitator_name = Column(String, default="")
    facilitator_fee = Column(Float, default=0.0)           # total fee the facilitator charges buyer/seller for brokering
    dealshield_cut = Column(Float, default=0.0)            # 10% of facilitator fee — DealShield's share
    facilitator_payout = Column(Float, default=0.0)        # 90% of facilitator fee — what facilitator receives
    buyer_accepted_terms = Column(Boolean, default=False)
    seller_accepted_terms = Column(Boolean, default=False)
    buyer_accepted_at = Column(DateTime, nullable=True)
    seller_accepted_at = Column(DateTime, nullable=True)
    is_facilitated = Column(Boolean, default=False)
    # ── Gateway fees (split 50/50 between buyer and seller) ──
    gateway_fee = Column(Float, default=0.0)             # total payment gateway fee
    buyer_gateway_share = Column(Float, default=0.0)     # buyer's portion (paid on funding)
    seller_gateway_share = Column(Float, default=0.0)    # seller's portion (deducted on release)


class WalletTx(Base):
    __tablename__ = "wallet_transactions"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    amount = Column(Float, nullable=False)
    type = Column(String, nullable=False)  # deposit, withdraw, escrow_hold, escrow_release, escrow_refund
    description = Column(String, default="")
    timestamp = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)  # NEW


class PaymentReference(Base):
    """Tracks payment references created at /payments/initialize so
    /payments/verify can only credit a wallet once per reference, and only
    for the user who created it (prevents replay / idempotency abuse).
    """
    __tablename__ = "payment_references"
    id = Column(Integer, primary_key=True, index=True)
    reference = Column(String, unique=True, index=True, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    amount = Column(Float, nullable=False)
    provider = Column(String, nullable=False, default="")
    status = Column(String, nullable=False, default="pending")  # pending -> consumed
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)  # NEW


class MarketPrice(Base):
    __tablename__ = "market_prices"
    id = Column(Integer, primary_key=True, index=True)
    item = Column(String, nullable=False)
    category = Column(String, nullable=False)
    price_usd = Column(Float, default=0.0)
    price_ngn = Column(Float, default=0.0)
    unit = Column(String, default="unit")
    change = Column(Float, default=0.0)
    trending = Column(Boolean, default=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Review(Base):
    __tablename__ = "reviews"
    id = Column(Integer, primary_key=True, index=True)
    escrow_id = Column(Integer, ForeignKey("escrow_transactions.id"), nullable=False)
    reviewer_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    reviewee_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    rating = Column(Integer, nullable=False)  # 1-5
    comment = Column(String, default="")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)  # NEW


class PaymentLink(Base):
    __tablename__ = "payment_links"
    id = Column(Integer, primary_key=True, index=True)
    link_code = Column(String, unique=True, index=True, nullable=False)
    seller_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    title = Column(String, nullable=False)
    description = Column(String, default="")
    amount = Column(Float, nullable=False)
    category = Column(String, default="general")
    status = Column(String, default="active")  # active, paid, expired
    created_at = Column(DateTime, default=datetime.utcnow)
    paid_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)  # NEW


class ProcessedPayment(Base):
    """Tracks payment references that have been consumed to prevent replay attacks."""
    __tablename__ = "processed_payments"
    id = Column(Integer, primary_key=True, index=True)
    reference = Column(String, unique=True, index=True, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    provider = Column(String, nullable=False)
    amount = Column(Float, nullable=False)
    processed_at = Column(DateTime, default=datetime.utcnow)


class AdminAuditLog(Base):
    """Immutable audit trail for all admin dispute resolution actions."""
    __tablename__ = "admin_audit_logs"
    id = Column(Integer, primary_key=True, index=True)
    admin_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    action = Column(String, nullable=False)  # release_to_seller, refund_to_buyer
    target_type = Column(String, nullable=False)  # escrow_transaction
    target_id = Column(Integer, nullable=False)
    details = Column(String, default="")
    created_at = Column(DateTime, default=datetime.utcnow)


class Session(Base):
    """Tracks active user sessions for token revocation and audit.
    Stores SHA-256 hash of the refresh token — never the raw token.
    """
    __tablename__ = "sessions"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token_hash = Column(String(64), unique=True, index=True, nullable=False)  # SHA-256 hex
    ip_address = Column(String(45), nullable=True)  # IPv4 or IPv6
    user_agent = Column(Text, nullable=True)
    expires_at = Column(DateTime, nullable=False)
    revoked = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class PasswordResetToken(Base):
    """One-time use password reset tokens with expiry.
    Stores SHA-256 hash of the token — never the raw token.
    """
    __tablename__ = "password_reset_tokens"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token_hash = Column(String(64), unique=True, index=True, nullable=False)  # SHA-256 hex
    expires_at = Column(DateTime, nullable=False)
    used = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class AuditLog(Base):
    """General-purpose audit trail for all user actions (not just admin).
    Records who did what, to what, when, with optional metadata.
    """
    __tablename__ = "audit_logs"
    id = Column(Integer, primary_key=True, index=True)
    actor_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    action = Column(String(100), nullable=False)  # login, register, escrow_create, etc.
    target_type = Column(String(100), nullable=True)  # escrow_transaction, listing, user, etc.
    target_id = Column(Integer, nullable=True)
    ip_address = Column(String(45), nullable=True)
    details = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.utcnow)


class VirtualAccount(Base):
    """Dedicated virtual (NUBAN) account generated for each escrow transaction.
    Buyers transfer directly to this account; a webhook confirms payment.
    """
    __tablename__ = "virtual_accounts"
    id = Column(Integer, primary_key=True, index=True)
    escrow_tx_id = Column(Integer, ForeignKey("escrow_transactions.id"), nullable=False, index=True)
    account_number = Column(String(10), unique=True, nullable=False, index=True)
    bank_name = Column(String, nullable=False, default="DealShield MFB")
    bank_code = Column(String, nullable=False, default="999")
    account_name = Column(String, nullable=False)
    provider = Column(String, nullable=False, default="dealshield")
    status = Column(String, nullable=False, default="active")  # active, expired, paid
    expected_amount = Column(Float, nullable=False, default=0.0)
    expires_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    escrow_tx = relationship("EscrowTransaction", backref="virtual_accounts")
