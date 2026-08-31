import os
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.dependencies import get_db
from app.schemas.schemas import WalletDeposit, WalletBalanceResponse, WalletTxListResponse, WalletTxOut
from app.models.models import User, WalletTx
from app.routers.auth import get_current_user

router = APIRouter()

# Audit L2/H1: /wallet/deposit and /wallet/withdraw credit/debit the wallet
# directly with no real payment gateway involved (real deposits should go
# through /payments/initialize + /payments/verify). These are test/demo-only
# conveniences and must be disabled by default in any real deployment.
SAFEPAY_TEST_MODE = os.getenv("SAFEPAY_TEST_MODE", "false").strip().lower() in ("1", "true", "yes")


def _require_test_mode():
    if not SAFEPAY_TEST_MODE:
        raise HTTPException(
            status_code=403,
            detail="Direct wallet deposit/withdraw is disabled outside SAFEPAY_TEST_MODE. Use /payments/initialize + /payments/verify instead.",
        )

@router.get("/balance", response_model=WalletBalanceResponse)
def get_balance(current_user: User = Depends(get_current_user)):
    return {"balance": current_user.wallet_balance}

@router.post("/deposit", response_model=WalletBalanceResponse)
def deposit(data: WalletDeposit, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require_test_mode()
    if data.amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive")
    current_user.wallet_balance += data.amount
    db.add(WalletTx(user_id=current_user.id, amount=data.amount, type="deposit", description="Wallet top-up"))
    db.commit()
    db.refresh(current_user)
    return {"balance": current_user.wallet_balance}

@router.post("/withdraw", response_model=WalletBalanceResponse)
def withdraw(data: WalletDeposit, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require_test_mode()
    if data.amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive")
    if current_user.wallet_balance < data.amount:
        raise HTTPException(status_code=400, detail="Insufficient balance")
    current_user.wallet_balance -= data.amount
    db.add(WalletTx(user_id=current_user.id, amount=data.amount, type="withdraw", description="Wallet withdrawal"))
    db.commit()
    db.refresh(current_user)
    return {"balance": current_user.wallet_balance}

@router.get("/transactions", response_model=WalletTxListResponse)
def get_transactions(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    txs = db.query(WalletTx).filter(WalletTx.user_id == current_user.id).order_by(WalletTx.timestamp.desc()).all()
    return {"transactions": [
        {"id": t.id, "amount": t.amount, "type": t.type, "description": t.description, "timestamp": t.timestamp.isoformat()}
        for t in txs
    ]}
