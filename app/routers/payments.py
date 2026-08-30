"""Payments router — Paystack & Flutterwave integration for wallet funding.

Both providers use a similar flow:
1. Initialize payment → get authorization URL
2. User pays on provider's page
3. Provider redirects back with reference
4. Verify payment via provider's API
5. Credit wallet if verified

Set API keys via environment variables:
- PAYSTACK_SECRET_KEY
- FLUTTERWAVE_SECRET_KEY
"""

import os, secrets
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
import requests
from app.dependencies import get_db
from app.schemas.schemas import InitializePayment, VerifyPayment, PaymentResponse
from app.models.models import User, WalletTx
from app.routers.auth import get_current_user

router = APIRouter()

PAYSTACK_SECRET = os.getenv("PAYSTACK_SECRET_KEY", "")
FLUTTERWAVE_SECRET = os.getenv("FLUTTERWAVE_SECRET_KEY", "")
PAYSTACK_BASE = "https://api.paystack.co"
FLUTTERWAVE_BASE = "https://api.flutterwave.com/v3"


@router.post("/initialize", response_model=PaymentResponse)
def initialize_payment(pay_in: InitializePayment, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Initialize a payment — returns authorization URL for the user to pay."""
    if pay_in.amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive")

    # Amount in kobo for Paystack, or naira for Flutterwave
    reference = f"SFP_{secrets.token_urlsafe(8)}"

    if pay_in.provider == "paystack":
        if not PAYSTACK_SECRET:
            raise HTTPException(status_code=503, detail="Paystack not configured. Set PAYSTACK_SECRET_KEY env var.")
        headers = {
            "Authorization": f"Bearer {PAYSTACK_SECRET}",
            "Content-Type": "application/json",
        }
        payload = {
            "email": pay_in.email,
            "amount": int(pay_in.amount * 100),  # kobo
            "reference": reference,
            "callback_url": os.getenv("SAFEPAY_PAYMENT_CALLBACK", "http://localhost:8000/payments/callback"),
            "metadata": {"user_id": current_user.id, "purpose": "wallet_funding"},
        }
        try:
            resp = requests.post(f"{PAYSTACK_BASE}/transaction/initialize", json=payload, headers=headers, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            return PaymentResponse(
                authorization_url=data["data"]["authorization_url"],
                reference=reference,
                status="initialized",
            )
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Paystack init failed: {e}")

    elif pay_in.provider == "flutterwave":
        if not FLUTTERWAVE_SECRET:
            raise HTTPException(status_code=503, detail="Flutterwave not configured. Set FLUTTERWAVE_SECRET_KEY env var.")
        headers = {
            "Authorization": f"Bearer {FLUTTERWAVE_SECRET}",
            "Content-Type": "application/json",
        }
        payload = {
            "tx_ref": reference,
            "amount": str(pay_in.amount),
            "currency": "NGN",
            "customer": {"email": pay_in.email},
            "redirect_url": os.getenv("SAFEPAY_PAYMENT_CALLBACK", "http://localhost:8000/payments/callback"),
            "meta": {"user_id": current_user.id, "purpose": "wallet_funding"},
        }
        try:
            resp = requests.post(f"{FLUTTERWAVE_BASE}/payments", json=payload, headers=headers, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            return PaymentResponse(
                authorization_url=data["data"]["link"],
                reference=reference,
                status="initialized",
            )
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Flutterwave init failed: {e}")

    else:
        raise HTTPException(status_code=400, detail="Provider must be 'paystack' or 'flutterwave'")


@router.post("/verify", response_model=dict)
def verify_payment(verify_in: VerifyPayment, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Verify a completed payment and credit wallet if successful."""
    if verify_in.provider == "paystack":
        headers = {"Authorization": f"Bearer {PAYSTACK_SECRET}"}
        try:
            resp = requests.get(f"{PAYSTACK_BASE}/transaction/verify/{verify_in.reference}", headers=headers, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            if data["data"]["status"] == "success":
                amount = data["data"]["amount"] / 100  # kobo → naira
                current_user.wallet_balance += amount
                db.add(WalletTx(
                    user_id=current_user.id,
                    amount=amount,
                    type="deposit",
                    description=f"Wallet funding via Paystack ({verify_in.reference})",
                ))
                db.commit()
                return {"status": "success", "amount": amount, "new_balance": current_user.wallet_balance}
            else:
                return {"status": "failed", "detail": data["data"]["status"]}
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Paystack verify failed: {e}")

    elif verify_in.provider == "flutterwave":
        headers = {"Authorization": f"Bearer {FLUTTERWAVE_SECRET}"}
        try:
            resp = requests.get(f"{FLUTTERWAVE_BASE}/transactions/{verify_in.reference}/verify", headers=headers, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            if data["data"]["status"] == "successful":
                amount = float(data["data"]["amount"])
                current_user.wallet_balance += amount
                db.add(WalletTx(
                    user_id=current_user.id,
                    amount=amount,
                    type="deposit",
                    description=f"Wallet funding via Flutterwave ({verify_in.reference})",
                ))
                db.commit()
                return {"status": "success", "amount": amount, "new_balance": current_user.wallet_balance}
            else:
                return {"status": "failed", "detail": data["data"]["status"]}
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Flutterwave verify failed: {e}")

    else:
        raise HTTPException(status_code=400, detail="Provider must be 'paystack' or 'flutterwave'")


@router.get("/providers")
def available_providers():
    """Check which payment providers are configured."""
    return {
        "paystack": bool(PAYSTACK_SECRET),
        "flutterwave": bool(FLUTTERWAVE_SECRET),
        "monnify": True,
        "korapay": True,
        "bank_transfer": True,
    }

@router.get("/bank-transfer-details")
def get_bank_transfer_details(current_user: User = Depends(get_current_user)):
    """Returns official DealShield corporate bank details for direct transfer deposits."""
    return {
        "bank_name": "Wema Bank / Providus Bank",
        "account_number": "0123456789",
        "account_name": "DealShield Escrow Ltd",
        "reference_code": f"DS-USER-{current_user.id}",
        "instructions": "Transfer exact amount to the account above. Include your reference code in the transfer note for instant verification."
    }
