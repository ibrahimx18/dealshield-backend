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
from decimal import Decimal, InvalidOperation
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
import requests
from app.dependencies import get_db
from app.schemas.schemas import InitializePayment, VerifyPayment, PaymentResponse
from app.models.models import User, WalletTx, PaymentReference
from app.routers.auth import get_current_user
from app.core.wallet import credit_wallet
from app.core.money import money_out, to_decimal

router = APIRouter()

PAYSTACK_SECRET = os.getenv("PAYSTACK_SECRET_KEY", "")
FLUTTERWAVE_SECRET = os.getenv("FLUTTERWAVE_SECRET_KEY", "")
PAYSTACK_BASE = "https://api.paystack.co"
FLUTTERWAVE_BASE = "https://api.flutterwave.com/v3"


@router.post("/initialize", response_model=PaymentResponse)
def initialize_payment(pay_in: InitializePayment, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Initialize a payment — returns authorization URL for the user to pay."""

    # Amount in kobo for Paystack, or naira for Flutterwave
    reference = f"SFP_{secrets.token_urlsafe(8)}"

    # Audit C3: persist the reference up-front so /verify can only ever
    # consume a reference that this user actually initialized, exactly once.
    db.add(PaymentReference(
        reference=reference,
        user_id=current_user.id,
        amount=pay_in.amount,
        provider=pay_in.provider,
        status="pending",
    ))
    db.commit()

    if pay_in.provider == "paystack":
        if not PAYSTACK_SECRET:
            raise HTTPException(status_code=503, detail="Paystack not configured. Set PAYSTACK_SECRET_KEY env var.")
        headers = {
            "Authorization": f"Bearer {PAYSTACK_SECRET}",
            "Content-Type": "application/json",
        }
        payload = {
            "email": pay_in.email,
            "amount": int(pay_in.amount * 100),  # kobo, exact (Decimal)
            "currency": "NGN",
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
        except Exception:
            raise HTTPException(status_code=502, detail="Paystack init failed")

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
        except Exception:
            raise HTTPException(status_code=502, detail="Flutterwave init failed")

    else:
        raise HTTPException(status_code=400, detail="Provider must be 'paystack' or 'flutterwave'")


def _claim_payment_reference(db: Session, reference: str, current_user: User,
                             provider: str, payment: dict) -> PaymentReference:
    """Audit C3: enforce that a payment reference belongs to the calling user,
    is still 'pending', and atomically flip it to 'consumed' before any wallet
    credit happens. Returns the claimed PaymentReference row.

    Raises HTTPException(404) if the reference doesn't belong to this user,
    or HTTPException(409) if it has already been consumed (double-verify /
    replay attempt) or was claimed concurrently.
    """
    pay_ref = db.query(PaymentReference).filter(
        PaymentReference.reference == reference,
        PaymentReference.user_id == current_user.id,
    ).first()
    if not pay_ref:
        raise HTTPException(status_code=404, detail="Payment reference not found for this user")
    if pay_ref.status != "pending":
        raise HTTPException(status_code=409, detail="Payment reference already consumed")
    if pay_ref.escrow_tx_id is not None:
        raise HTTPException(status_code=400, detail="This reference funds an escrow and is settled by webhook only")

    # B10: the provider response must match the stored payment intent exactly.
    reference_field = "reference" if provider == "paystack" else "tx_ref"
    if pay_ref.provider != provider or str(payment.get(reference_field) or "") != reference:
        raise HTTPException(status_code=400, detail="Payment provider/reference does not match payment intent")
    if str(payment.get("currency") or "").upper() != "NGN":
        raise HTTPException(status_code=400, detail="Payment currency does not match payment intent")
    try:
        amount = Decimal(str(payment.get("amount")))
        if provider == "paystack":
            amount = amount / 100
        valid = amount.is_finite() and amount > 0 and amount == to_decimal(pay_ref.amount)
    except (InvalidOperation, ValueError, TypeError):
        valid = False
    if not valid:
        raise HTTPException(status_code=400, detail="Payment amount does not match payment intent")

    # Atomic conditional update — only one concurrent /verify call can win.
    rows = db.query(PaymentReference).filter(
        PaymentReference.id == pay_ref.id,
        PaymentReference.status == "pending",
    ).update({"status": "consumed"}, synchronize_session=False)
    db.flush()
    if rows != 1:
        db.rollback()
        raise HTTPException(status_code=409, detail="Payment reference already consumed")
    db.refresh(pay_ref)
    return pay_ref


@router.post("/verify", response_model=dict)
def verify_payment(verify_in: VerifyPayment, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Verify a completed payment and credit wallet if successful.

    Audit C3: the reference must belong to the calling user and be 'pending';
    it is atomically marked 'consumed' BEFORE crediting the wallet, so a
    replayed/duplicate verify call cannot credit the wallet twice.
    """
    if verify_in.provider == "paystack":
        headers = {"Authorization": f"Bearer {PAYSTACK_SECRET}"}
        try:
            resp = requests.get(f"{PAYSTACK_BASE}/transaction/verify/{verify_in.reference}", headers=headers, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            if data["data"]["status"] == "success":
                pay_ref = _claim_payment_reference(db, verify_in.reference, current_user, "paystack", data["data"])
                amount = to_decimal(pay_ref.amount)
                new_balance = credit_wallet(db, current_user.id, amount)
                db.add(WalletTx(
                    user_id=current_user.id,
                    amount=amount,
                    type="deposit",
                    description=f"Wallet funding via Paystack ({verify_in.reference})",
                ))
                db.commit()
                return {"status": "success", "amount": money_out(amount), "new_balance": money_out(new_balance)}
            else:
                return {"status": "failed", "detail": data["data"]["status"]}
        except HTTPException:
            raise
        except Exception:
            db.rollback()
            raise HTTPException(status_code=502, detail="Paystack verify failed")

    elif verify_in.provider == "flutterwave":
        headers = {"Authorization": f"Bearer {FLUTTERWAVE_SECRET}"}
        try:
            resp = requests.get(f"{FLUTTERWAVE_BASE}/transactions/verify_by_reference",
                                params={"tx_ref": verify_in.reference}, headers=headers, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            if data["data"]["status"] == "successful":
                pay_ref = _claim_payment_reference(db, verify_in.reference, current_user, "flutterwave", data["data"])
                amount = to_decimal(pay_ref.amount)
                new_balance = credit_wallet(db, current_user.id, amount)
                db.add(WalletTx(
                    user_id=current_user.id,
                    amount=amount,
                    type="deposit",
                    description=f"Wallet funding via Flutterwave ({verify_in.reference})",
                ))
                db.commit()
                return {"status": "success", "amount": money_out(amount), "new_balance": money_out(new_balance)}
            else:
                return {"status": "failed", "detail": data["data"]["status"]}
        except HTTPException:
            raise
        except Exception:
            db.rollback()
            raise HTTPException(status_code=502, detail="Flutterwave verify failed")

    else:
        raise HTTPException(status_code=400, detail="Provider must be 'paystack' or 'flutterwave'")


@router.get("/providers")
def available_providers():
    """Check which payment providers are configured."""
    return {
        "paystack": bool(PAYSTACK_SECRET),
        "flutterwave": bool(FLUTTERWAVE_SECRET),
        "monnify": False,
        "korapay": False,
        "bank_transfer": False,  # B03: no verified corporate account / reconciliation
    }

@router.get("/bank-transfer-details")
def get_bank_transfer_details(current_user: User = Depends(get_current_user)):
    """B03: the previous hardcoded account number was not a real DealShield account.
    Disabled until verified corporate details and reconciliation exist."""
    raise HTTPException(status_code=503, detail="Bank transfer is unavailable: no verified DealShield account is configured.")
