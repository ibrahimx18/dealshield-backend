from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.dependencies import get_db
from app.schemas.schemas import WalletDeposit, WalletBalanceResponse, WalletTxListResponse, WalletTxOut
from app.models.models import User, WalletTx
from app.routers.auth import get_current_user

router = APIRouter()

@router.get("/balance", response_model=WalletBalanceResponse)
def get_balance(current_user: User = Depends(get_current_user)):
    return {"balance": current_user.wallet_balance}

@router.post("/deposit", response_model=WalletBalanceResponse)
def deposit(data: WalletDeposit, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if data.amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive")
    current_user.wallet_balance += data.amount
    db.add(WalletTx(user_id=current_user.id, amount=data.amount, type="deposit", description="Wallet top-up"))
    db.commit()
    db.refresh(current_user)
    return {"balance": current_user.wallet_balance}

@router.post("/withdraw", response_model=WalletBalanceResponse)
def withdraw(data: WalletDeposit, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
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
