from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from typing import Optional
import requests as http_requests
from datetime import datetime
from app.dependencies import get_db
from app.schemas.schemas import MarketPriceListResponse
from app.models.models import MarketPrice

router = APIRouter()

# Fallback static rates (updated Aug 20, 2026)
FALLBACK_RATES = {
    "Gold (per oz)": {"price_usd": 4520.20, "price_ngn": 6102858, "unit": "oz", "category": "gold", "change": 1.2, "trending": True},
    "Gold (per gram)": {"price_usd": 145.33, "price_ngn": 196211, "unit": "gram", "category": "gold", "change": 1.2, "trending": True},
    "Gold (per kg)": {"price_usd": 145330.00, "price_ngn": 196211000, "unit": "kg", "category": "gold", "change": 1.2, "trending": True},
    "WTI Crude (per barrel)": {"price_usd": 86.25, "price_ngn": 116449, "unit": "barrel", "category": "oil", "change": -0.5, "trending": False},
    "Brent Crude (per barrel)": {"price_usd": 93.20, "price_ngn": 125832, "unit": "barrel", "category": "oil", "change": 0.8, "trending": True},
    "AGO (per litre)": {"price_usd": 0.85, "price_ngn": 1148, "unit": "litre", "category": "oil", "change": 0.3, "trending": True},
    "US Dollar": {"price_usd": 1.00, "price_ngn": 1350.13, "unit": "USD", "category": "currency", "change": 0.1, "trending": True},
}


def fetch_live_rates():
    """Fetch live commodity and FX rates from free APIs."""
    rates = {}
    headers = {"User-Agent": "Mozilla/5.0"}

    # Gold
    try:
        r = http_requests.get("https://api.gold-api.com/price/XAU", timeout=10)
        if r.status_code == 200:
            gold_usd = r.json()["price"]
            rates["gold_usd"] = gold_usd
        else:
            rates["gold_usd"] = 4520.20
    except Exception:
        rates["gold_usd"] = 4520.20

    # USD to NGN
    try:
        r = http_requests.get("https://api.exchangerate-api.com/v4/latest/USD", timeout=10)
        if r.status_code == 200:
            rates["usd_ngn"] = r.json()["rates"]["NGN"]
        else:
            rates["usd_ngn"] = 1350.13
    except Exception:
        rates["usd_ngn"] = 1350.13

    # WTI Crude
    try:
        r = http_requests.get("https://query1.finance.yahoo.com/v8/finance/chart/CL=F?range=1d&interval=1d", headers=headers, timeout=10)
        if r.status_code == 200:
            rates["wti_usd"] = r.json()["chart"]["result"][0]["meta"]["regularMarketPrice"]
        else:
            rates["wti_usd"] = 86.25
    except Exception:
        rates["wti_usd"] = 86.25

    # Brent Crude
    try:
        r = http_requests.get("https://query1.finance.yahoo.com/v8/finance/chart/BZ=F?range=1d&interval=1d", headers=headers, timeout=10)
        if r.status_code == 200:
            rates["brent_usd"] = r.json()["chart"]["result"][0]["meta"]["regularMarketPrice"]
        else:
            rates["brent_usd"] = 93.20
    except Exception:
        rates["brent_usd"] = 93.20

    return rates


def update_prices_in_db(db: Session):
    """Fetch live rates and update the database."""
    rates = fetch_live_rates()
    gold_usd = rates.get("gold_usd", 4520.20)
    usd_ngn = rates.get("usd_ngn", 1350.13)
    wti_usd = rates.get("wti_usd", 86.25)
    brent_usd = rates.get("brent_usd", 93.20)

    gold_per_gram = gold_usd / 31.1035

    items = {
        "Gold (per oz)": {"price_usd": gold_usd, "price_ngn": gold_usd * usd_ngn, "unit": "oz", "category": "gold"},
        "Gold (per gram)": {"price_usd": round(gold_per_gram, 2), "price_ngn": round(gold_per_gram * usd_ngn), "unit": "gram", "category": "gold"},
        "Gold (per kg)": {"price_usd": round(gold_per_gram * 1000, 2), "price_ngn": round(gold_per_gram * usd_ngn * 1000), "unit": "kg", "category": "gold"},
        "WTI Crude (per barrel)": {"price_usd": wti_usd, "price_ngn": round(wti_usd * usd_ngn), "unit": "barrel", "category": "oil"},
        "Brent Crude (per barrel)": {"price_usd": brent_usd, "price_ngn": round(brent_usd * usd_ngn), "unit": "barrel", "category": "oil"},
        "AGO (per litre)": {"price_usd": round(wti_usd * 0.01, 2), "price_ngn": round(wti_usd * usd_ngn * 0.01), "unit": "litre", "category": "oil"},
        "US Dollar": {"price_usd": 1.00, "price_ngn": usd_ngn, "unit": "USD", "category": "currency"},
    }

    for item_name, data in items.items():
        existing = db.query(MarketPrice).filter(MarketPrice.item == item_name).first()
        if existing:
            existing.price_usd = data["price_usd"]
            existing.price_ngn = data["price_ngn"]
            existing.updated_at = datetime.utcnow()
        else:
            entry = MarketPrice(
                item=item_name,
                category=data["category"],
                price_usd=data["price_usd"],
                price_ngn=data["price_ngn"],
                unit=data["unit"],
                change=0.0,
                trending=True,
                updated_at=datetime.utcnow(),
            )
            db.add(entry)
    db.commit()


@router.get("/prices", response_model=MarketPriceListResponse)
def get_prices(
    category: Optional[str] = Query(None),
    refresh: bool = Query(False),
    db: Session = Depends(get_db),
):
    # Auto-refresh prices if DB is empty or refresh requested
    count = db.query(MarketPrice).count()
    if count == 0 or refresh:
        update_prices_in_db(db)

    q = db.query(MarketPrice)
    if category and category != "undefined":
        q = q.filter(MarketPrice.category == category)
    prices = q.all()
    return {"prices": [
        {
            "id": p.id, "item": p.item, "category": p.category,
            "price_usd": p.price_usd, "price_ngn": p.price_ngn,
            "unit": p.unit, "change": p.change, "trending": p.trending,
            "updated_at": p.updated_at,
        }
        for p in prices
    ]}


@router.post("/refresh")
def refresh_prices(db: Session = Depends(get_db)):
    """Force refresh market prices from live API sources."""
    update_prices_in_db(db)
    prices = db.query(MarketPrice).all()
    return {
        "detail": "Prices updated",
        "count": len(prices),
        "prices": [
            {"item": p.item, "price_usd": p.price_usd, "price_ngn": p.price_ngn, "unit": p.unit}
            for p in prices
        ],
    }
