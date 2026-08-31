from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, ForeignKey, Enum as SQLEnum
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
    pending = "pending"
    funds_deposited = "funds_deposited"
    shipped = "shipped"
    delivered = "delivered"
    disputed = "disputed"
    cancelled = "cancelled"


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
    id_verified = Column(Boolean, default=False)
    bvn_verified = Column(Boolean, default=False)
    business_verified = Column(Boolean, default=False)
    business_name = Column(String, default="")
    badge_tier = Column(String, default="none")  # none, verified, trusted, top_dealer
    total_deals = Column(Integer, default=0)
    rating = Column(Float, default=5.0)
    failed_attempts = Column(Integer, default=0)
    locked_until = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    listings = relationship("Listing", backref="seller", foreign_keys="Listing.seller_id")
    reviews_given = relationship("Review", backref="reviewer", foreign_keys="Review.reviewer_id")
    reviews_received = relationship("Review", backref="reviewee", foreign_keys="Review.reviewee_id")


class Listing(Base):
    __tablename__ = "listings"
    id = Column(Integer, primary_key=True, index=True)
    category = Column(String, nullable=False)  # stored as string key
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
    posted_date = Column(DateTime, default=datetime.utcnow)


class EscrowTransaction(Base):
    __tablename__ = "escrow_transactions"
    id = Column(Integer, primary_key=True, index=True)
    listing_id = Column(Integer, ForeignKey("listings.id"), nullable=False)
    listing_title = Column(String, nullable=False)
    category = Column(String, nullable=False)
    amount = Column(Float, nullable=False)
    commission = Column(Float, nullable=False)
    status = Column(String, default="pending")
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
    pickup_otp = Column(String, default="")  # 6-digit OTP for rider pickup (audit C5: was 4-digit random.randint)
    rider_phone = Column(String, default="")  # dispatch rider's phone
    rider_name = Column(String, default="")
    pickup_confirmed = Column(Boolean, default=False)
    dispatched_at = Column(DateTime, nullable=True)
    # ── Audit C5: OTP hardening columns ──
    # NEW nullable columns added on the existing escrow_transactions table.
    # MIGRATION NOTE (do not run automatically): these are picked up by
    # Base.metadata.create_all() for brand-new SQLite DBs, but existing
    # databases (e.g. Postgres in production) need a manual migration, e.g.:
    #   ALTER TABLE escrow_transactions ADD COLUMN otp_attempts INTEGER DEFAULT 0;
    #   ALTER TABLE escrow_transactions ADD COLUMN otp_locked BOOLEAN DEFAULT FALSE;
    otp_attempts = Column(Integer, default=0, nullable=True)  # wrong-OTP attempt counter
    otp_locked = Column(Boolean, default=False, nullable=True)  # true after 5 wrong attempts


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
