from pydantic import BaseModel, EmailStr
from typing import Optional, List, Any
from datetime import datetime

# === AUTH ===
class UserCreate(BaseModel):
    name: str
    phone: str
    email: EmailStr
    password: str

class UserLogin(BaseModel):
    email_or_phone: str
    password: str

class UserProfile(BaseModel):
    id: int
    name: str
    phone: str
    email: str
    wallet_balance: float
    nin_verified: bool
    phone_verified: bool
    email_verified: bool = False  # NEW
    id_verified: bool
    bvn_verified: bool = False
    business_verified: bool = False
    business_name: str = ""
    badge_tier: str = "none"
    total_deals: int
    rating: float

class AuthResponse(BaseModel):
    access_token: str = ""
    refresh_token: str = ""  # NEW: refresh token alongside access token
    token_type: str = "bearer"
    user: Optional[UserProfile] = None
    requires_2fa: bool = False  # NEW: if true, client must complete 2FA via /auth/login/2fa
    temp_token: str = ""  # NEW: temporary token for 2FA login completion


class RefreshTokenRequest(BaseModel):
    refresh_token: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class EmailVerifyRequest(BaseModel):
    token: str


class PasswordResetRequest(BaseModel):
    """Step 1: Request password reset (user provides email/phone)."""
    email_or_phone: str


class PasswordResetConfirm(BaseModel):
    """Step 2: Confirm password reset with token + new password."""
    token: str
    new_password: str


class ChangePasswordRequest(BaseModel):
    """Change password while logged in (requires current password)."""
    current_password: str
    new_password: str


class LogoutRequest(BaseModel):
    refresh_token: str


class Enable2FAResponse(BaseModel):
    """Response when enabling 2FA — contains secret, URI for QR, and backup codes."""
    secret: str
    uri: str
    backup_codes: List[str]


class Verify2FARequest(BaseModel):
    """Verify a TOTP token to complete 2FA enablement."""
    token: str


class Disable2FARequest(BaseModel):
    """Disable 2FA — requires current TOTP token."""
    token: str


class Login2FARequest(BaseModel):
    """Complete login with 2FA — temp token + TOTP code (or backup code)."""
    temp_token: str
    totp_code: str

# === LISTING ===
class ListingCreate(BaseModel):
    category: str
    title: str
    description: str = ""
    price: float
    location: str = ""
    insured: bool = False

class ListingOut(BaseModel):
    id: Any
    category: str
    title: str
    description: str
    price: float
    location: str
    seller_name: str
    seller_rating: str
    verified: bool
    image_path: Optional[str] = None
    insured: bool
    posted_date: datetime

class ListingListResponse(BaseModel):
    listings: List[ListingOut]

# === ESCROW ===
class EscrowCreate(BaseModel):
    listing_id: Any
    insured: bool = False
    bag_count: Optional[int] = None  # For cement category (600, 900 bags etc.)

class EscrowShip(BaseModel):
    logistics_provider: str = ""
    tracking_number: str = ""

class EscrowDispute(BaseModel):
    reason: str
    evidence: Optional[str] = None  # Links, doc references, etc.

class EscrowFulfill(BaseModel):
    """Seller marks fulfilment — shipping, digital delivery, document handover, etc."""
    logistics_provider: str = ""
    tracking_number: str = ""
    notes: str = ""

class EscrowOut(BaseModel):
    id: Any
    listing_id: Any
    listing_title: str
    category: str
    amount: float
    commission: float
    status: str
    buyer_id: Any
    seller_id: Any
    buyer_name: Optional[str] = None
    seller_name: Optional[str] = None
    created_at: datetime
    completed_at: Optional[datetime] = None
    insured: bool
    logistics_provider: Optional[str] = None
    tracking_number: Optional[str] = None
    insurance_fee: float = 0
    # New flow fields
    accepted_at: Optional[datetime] = None
    funded_at: Optional[datetime] = None
    fulfilment_started_at: Optional[datetime] = None
    buyer_review_started_at: Optional[datetime] = None
    buyer_review_deadline: Optional[datetime] = None
    dispute_reason: Optional[str] = None
    dispute_initiated_by: Optional[str] = None
    admin_resolution: Optional[str] = None
    admin_reason: Optional[str] = None
    resolved_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    accept_deadline: Optional[datetime] = None
    payment_deadline: Optional[datetime] = None
    # Facilitator fields
    is_facilitated: bool = False
    facilitator_id: Optional[int] = None
    facilitator_name: Optional[str] = None
    facilitator_fee: float = 0.0
    dealshield_cut: float = 0.0
    facilitator_payout: float = 0.0
    buyer_accepted_terms: bool = False
    seller_accepted_terms: bool = False
    gateway_fee: float = 0.0
    buyer_gateway_share: float = 0.0
    seller_gateway_share: float = 0.0

class EscrowListResponse(BaseModel):
    transactions: List[EscrowOut]


# === FACILITATOR ===
class FacilitatedDealCreate(BaseModel):
    """Facilitator creates a deal between a buyer and seller.
    Facilitator sets the deal amount and their own facilitation fee.
    On release: seller gets deal amount, facilitator gets 90% of their fee,
    DealShield keeps 10% of the facilitator's fee.
    """
    title: str
    category: str  # cars, gold, dollars, land, crypto, etc.
    deal_amount: float         # amount agreed between buyer and seller for the goods
    facilitator_fee: float      # fee the facilitator charges for brokering (agreed with buyer/seller)
    buyer_phone: str            # buyer's phone (must be registered)
    seller_phone: str           # seller's phone (must be registered)
    description: str = ""
    insured: bool = False


class FacilitatorAcceptTerms(BaseModel):
    """Buyer or seller accepts the facilitator's deal terms."""
    role: str  # "buyer" or "seller"

# === WALLET ===
class WalletDeposit(BaseModel):
    amount: float

class WalletBalanceResponse(BaseModel):
    balance: float

class WalletTxOut(BaseModel):
    id: int
    amount: float
    type: str
    description: str
    timestamp: datetime

class WalletTxListResponse(BaseModel):
    transactions: List[WalletTxOut]

# === MARKET ===
class MarketPriceOut(BaseModel):
    id: int
    item: str
    category: str
    price_usd: float
    price_ngn: float
    unit: str
    change: float
    trending: bool
    updated_at: Optional[datetime] = None

class MarketPriceListResponse(BaseModel):
    prices: List[MarketPriceOut]

# === REVIEWS ===
class ReviewCreate(BaseModel):
    escrow_id: int
    rating: int  # 1-5
    comment: str = ""

class ReviewOut(BaseModel):
    id: int
    escrow_id: int
    reviewer_id: int
    reviewee_id: int
    reviewer_name: Optional[str] = None
    rating: int
    comment: str
    created_at: datetime

class ReviewListResponse(BaseModel):
    reviews: List[ReviewOut]

# === PAYMENT LINKS ===
class PaymentLinkCreate(BaseModel):
    title: str
    description: str = ""
    amount: float
    category: str = "general"

class PaymentLinkOut(BaseModel):
    id: int
    link_code: str
    seller_id: int
    seller_name: Optional[str] = None
    title: str
    description: str
    amount: float
    category: str
    status: str
    created_at: datetime
    paid_at: Optional[datetime] = None

class PaymentLinkListResponse(BaseModel):
    links: List[PaymentLinkOut]

# === PAYMENT (Paystack/Flutterwave) ===
class InitializePayment(BaseModel):
    amount: float
    email: str
    provider: str = "paystack"  # paystack or flutterwave

class VerifyPayment(BaseModel):
    reference: str
    provider: str = "paystack"

class PaymentResponse(BaseModel):
    authorization_url: Optional[str] = None
    reference: str
    status: str

# === VIRTUAL ACCOUNTS ===
class VirtualAccountOut(BaseModel):
    id: int
    escrow_tx_id: int
    account_number: str
    bank_name: str
    bank_code: str
    account_name: str
    provider: str
    status: str
    expected_amount: float
    expires_at: Optional[datetime] = None
    created_at: datetime
    updated_at: Optional[datetime] = None


# === VERIFICATION ===
class VerifyBVN(BaseModel):
    bvn: str

class VerifyBusiness(BaseModel):
    business_name: str
    rc_number: str

# === DISPATCH RIDER ===
class DispatchRider(BaseModel):
    rider_name: str
    rider_phone: str
    logistics_provider: str = ""

class PickupConfirm(BaseModel):
    otp: str
    rider_phone: str
