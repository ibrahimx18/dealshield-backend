from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, ForeignKey, Text
from sqlalchemy.orm import relationship
from datetime import datetime
from app.core.database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    phone_number = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    full_name = Column(String, default="")
    wallet_balance = Column(Float, default=0.0)
    created_at = Column(DateTime, default=datetime.utcnow)
    is_active = Column(Boolean, default=True)
    is_verified = Column(Boolean, default=False)
    verification_tier = Column(Integer, default=1)  # 1: basic phone, 2: NIN/BVN, 3: CAC/business
    role = Column(String, default="user")  # user, merchant, admin

    # Relationships
    escrows_as_buyer = relationship("EscrowTransaction", foreign_keys="EscrowTransaction.buyer_id", back_populates="buyer")
    escrows_as_seller = relationship("EscrowTransaction", foreign_keys="EscrowTransaction.seller_id", back_populates="seller")


class EscrowTransaction(Base):
    __tablename__ = "escrow_transactions"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)
    description = Column(String, default="")
    amount = Column(Float, nullable=False)
    fee = Column(Float, default=0.0)
    status = Column(String, default="created")
    # Statuses: created -> funded -> shipped -> inspect_period -> completed / disputed / cancelled

    buyer_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    seller_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    category = Column(String, default="general")
    # Categories: cars, real_estate, crypto, gold, fx, cement, general

    # Cement specific metadata
    cement_bags = Column(Integer, default=0)
    cement_factory = Column(String, default="")

    # Crypto / FX metadata
    crypto_amount = Column(Float, default=0.0)
    crypto_symbol = Column(String, default="")  # USDT, BTC
    fiat_rate = Column(Float, default=0.0)

    # Giftcard metadata
    giftcard_type = Column(String, default="")  # Amazon, Apple, Steam
    giftcard_code = Column(String, default="")

    inspection_period_hours = Column(Integer, default=24)

    # Relationships
    buyer = relationship("User", foreign_keys=[buyer_id], back_populates="escrows_as_buyer")
    seller = relationship("User", foreign_keys=[seller_id], back_populates="escrows_as_seller")

    buyer_phone = Column(String, default="")
    seller_phone = Column(String, default="")
    buyer_name = Column(String, default="")
    seller_name = Column(String, default="")
    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)
    insured = Column(Boolean, default=False)
    logistics_provider = Column(String, default="")
    tracking_number = Column(String, default="")
    insurance_fee = Column(Float, default=0)
    pickup_otp = Column(String, default="")  # 6-digit OTP for rider pickup (audit C5: was 4-digit random.randint)
    pickup_otp_attempts = Column(Integer, default=0)
    pickup_otp_expires_at = Column(DateTime, nullable=True)
    rider_phone = Column(String, default="")  # dispatch rider's phone
    rider_name = Column(String, default="")
    pickup_confirmed = Column(Boolean, default=False)
    dispatched_at = Column(DateTime, nullable=True)
    
    # ── Audit C5: OTP hardening columns ──
    otp_attempts = Column(Integer, default=0, nullable=True)  # wrong-OTP attempt counter
    otp_locked = Column(Boolean, default=False, nullable=True)  # true after 5 wrong attempts
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class WalletTx(Base):
    __tablename__ = "wallet_transactions"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    amount = Column(Float, nullable=False)
    type = Column(String, nullable=False)  # deposit, withdraw, escrow_hold, escrow_release, escrow_refund
    description = Column(String, default="")
    timestamp = Column(DateTime, default=datetime.utcnow)


class PaymentReference(Base):
    """Audit C3: tracks payment references created at /payments/initialize so
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
    updated_at = Column(DateTime, default=datetime.utcnow)


class Review(Base):
    __tablename__ = "reviews"
    id = Column(Integer, primary_key=True, index=True)
    escrow_id = Column(Integer, ForeignKey("escrow_transactions.id"), nullable=False)
    reviewer_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    reviewee_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    rating = Column(Integer, nullable=False)  # 1-5
    comment = Column(String, default="")
    created_at = Column(DateTime, default=datetime.utcnow)


class PaymentLink(Base):
    __tablename__ = "payment_links"
    id = Column(Integer, primary_key=True, index=True)
    link_code = Column(String, unique=True, index=True, nullable=False)  # short shareable code
    seller_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    title = Column(String, nullable=False)
    description = Column(String, default="")
    amount = Column(Float, nullable=False)
    category = Column(String, default="general")
    status = Column(String, default="active")  # active, paid, expired
    created_at = Column(DateTime, default=datetime.utcnow)
    paid_at = Column(DateTime, nullable=True)


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
