import os
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.core.database import Base, engine, SessionLocal
from app.core.security_middleware import (
    SecurityHeadersMiddleware,
    HTTPSRedirectMiddleware,
    RateLimitMiddleware,
)
from app.routers import auth, listings, escrow, wallet, market, health, integrations, ai, reviews, payment_links, payments, dispatch

Base.metadata.create_all(bind=engine)

# ── Seed demo data ──
from app.models.models import User, Listing, MarketPrice
from app.core.security import get_password_hash

def seed():
    db = SessionLocal()
    try:
        if db.query(User).first() is None:
            seller = User(
                name="Ibrahim Seller", phone="08012345678", email="seller@safepay.ng",
                hashed_password=get_password_hash("demo1234"),
                wallet_balance=500000, nin_verified=True, phone_verified=True, id_verified=True,
                total_deals=12, rating=4.8,
            )
            buyer = User(
                name="Geralt Buyer", phone="08098765432", email="geralt@safepay.ng",
                hashed_password=get_password_hash("demo1234"),
                wallet_balance=500000, nin_verified=True, phone_verified=True, id_verified=True,
                total_deals=3, rating=5.0,
            )
            db.add_all([seller, buyer])
            db.flush()

            listings = [
                Listing(category="cars", title="Toyota Corolla 2015", description="Well maintained, 50,000km, AC working perfectly. Lagos registered.", price=4500000, location="Lekki, Lagos", seller_id=seller.id, seller_name=seller.name, seller_rating="4.8", verified=True),
                Listing(category="gold", title="24K Gold Bar — 100g", description="Pure 24K gold bar, certified. Direct from refinery.", price=8500000, location="Kano", seller_id=seller.id, seller_name=seller.name, seller_rating="4.8", verified=True),
                Listing(category="dollars", title="$5,000 USD", description="Transfer at bank rate. Clean funds.", price=4000000, location="Abuja", seller_id=seller.id, seller_name=seller.name, seller_rating="4.8", verified=True),
                Listing(category="land", title="500sqm Land — Lekki Scheme", description="C of O, dry land, fenced, ready to build.", price=15000000, location="Lekki, Lagos", seller_id=seller.id, seller_name=seller.name, seller_rating="4.8", verified=True),
                Listing(category="oil", title="10,000 Litres AGO (Diesel)", description="Bulk diesel at depot price. Quality tested.", price=4200000, location="Apapa, Lagos", seller_id=seller.id, seller_name=seller.name, seller_rating="4.8", verified=True),
            ]
            db.add_all(listings)

            prices = [
                MarketPrice(item="Gold (per oz)", category="gold", price_usd=4520.20, price_ngn=6102858, unit="oz", change=1.2, trending=True),
                MarketPrice(item="Gold (per gram)", category="gold", price_usd=145.33, price_ngn=196211, unit="gram", change=1.2, trending=True),
                MarketPrice(item="Gold (per kg)", category="gold", price_usd=145330.00, price_ngn=196211000, unit="kg", change=1.2, trending=True),
                MarketPrice(item="WTI Crude (per barrel)", category="oil", price_usd=86.25, price_ngn=116449, unit="barrel", change=-0.5, trending=False),
                MarketPrice(item="Brent Crude (per barrel)", category="oil", price_usd=93.20, price_ngn=125832, unit="barrel", change=0.8, trending=True),
                MarketPrice(item="AGO (per litre)", category="oil", price_usd=0.85, price_ngn=1148, unit="litre", change=0.3, trending=True),
                MarketPrice(item="US Dollar", category="currency", price_usd=1.00, price_ngn=1350.13, unit="USD", change=0.1, trending=True),
            ]
            db.add_all(prices)
            db.commit()
            print("✓ Seeded demo data: 2 users, 5 listings, 10 market prices")
    finally:
        db.close()

seed()

app = FastAPI(title="SafePay API", version="1.1.0")

# ── Security middleware (checklist items #11, #18, #19) ──
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(HTTPSRedirectMiddleware)
app.add_middleware(RateLimitMiddleware)

# ── CORS — restricted origins only (was: allow_origins=["*"]) ──
ALLOWED_ORIGINS = os.getenv("SAFEPAY_CORS_ORIGINS", "http://localhost:8080,http://15.204.248.160:8000").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Authorization", "Content-Type", "X-SafePay-Secret"],
)

app.include_router(health.router, tags=["health"])
app.include_router(auth.router, prefix="/auth", tags=["auth"])
app.include_router(listings.router, prefix="/listings", tags=["listings"])
app.include_router(escrow.router, prefix="/escrow", tags=["escrow"])
app.include_router(wallet.router, prefix="/wallet", tags=["wallet"])
app.include_router(market.router, prefix="/market", tags=["market"])
app.include_router(integrations.router, prefix="/api", tags=["integrations"])
app.include_router(ai.router, prefix="/ai", tags=["ai"])
app.include_router(reviews.router, prefix="/reviews", tags=["reviews"])
app.include_router(payment_links.router, prefix="/pay-links", tags=["payment-links"])
app.include_router(payments.router, prefix="/payments", tags=["payments"])
app.include_router(dispatch.router, prefix="/dispatch", tags=["dispatch"])
if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=False)
