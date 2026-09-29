"""Atomic wallet balance movements (B01/B08).

Every balance change is a single conditional UPDATE executed by the database:

    UPDATE users SET wallet_balance = wallet_balance - :amt
     WHERE id = :uid AND wallet_balance >= :amt [RETURNING wallet_balance]

PostgreSQL: the UPDATE takes a row lock and re-evaluates the WHERE clause after
acquiring it (READ COMMITTED), so two concurrent debits cannot both pass.
SQLite: writes are serialised by the database write lock; the same conditional
UPDATE is re-checked inside the writer's transaction (rowcount check).
There is also a DB CHECK (wallet_balance >= 0) as a last line of defence.
Never do `user.wallet_balance -= x` in Python.
"""
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import update, select
from sqlalchemy.orm import Session

from app.core.money import require_amount
from app.models.models import User


class InsufficientFunds(HTTPException):
    def __init__(self):
        super().__init__(status_code=400, detail="Insufficient wallet balance. Please deposit funds first.")


def _supports_returning(db: Session) -> bool:
    return bool(getattr(db.get_bind().dialect, "update_returning", False))


def _apply(db: Session, user_id: int, stmt) -> Decimal | None:
    if _supports_returning(db):
        row = db.execute(stmt.returning(User.wallet_balance)).first()
        new_balance = None if row is None else row[0]
    else:
        res = db.execute(stmt)
        if res.rowcount != 1:
            new_balance = None
        else:
            new_balance = db.execute(select(User.wallet_balance).where(User.id == user_id)).scalar_one()
    # Keep any loaded ORM instance in sync without a stale read-modify-write.
    for obj in list(db.identity_map.values()):
        if isinstance(obj, User) and obj.id == user_id:
            db.expire(obj, ["wallet_balance"])
    return new_balance


def debit_wallet(db: Session, user_id: int, amount) -> Decimal:
    amt = require_amount(amount, what="Debit amount")
    stmt = (
        update(User)
        .where(User.id == user_id, User.wallet_balance >= amt)
        .values(wallet_balance=User.wallet_balance - amt)
        .execution_options(synchronize_session=False)
    )
    new_balance = _apply(db, user_id, stmt)
    if new_balance is None:
        raise InsufficientFunds()
    return new_balance


def credit_wallet(db: Session, user_id: int, amount) -> Decimal:
    amt = require_amount(amount, allow_zero=True, what="Credit amount")
    if amt == 0:
        return db.execute(select(User.wallet_balance).where(User.id == user_id)).scalar_one()
    stmt = (
        update(User)
        .where(User.id == user_id)
        .values(wallet_balance=User.wallet_balance + amt)
        .execution_options(synchronize_session=False)
    )
    new_balance = _apply(db, user_id, stmt)
    if new_balance is None:
        raise HTTPException(status_code=404, detail="Wallet owner not found")
    return new_balance
