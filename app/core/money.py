"""Money helpers (B01/B19).

All money is handled as Decimal with exactly 2 decimal places (kobo precision).
Floats are never used for balances, prices or fees.

- Schema layer: use the `Money` / `NonNegMoney` annotated types.
- Service layer: call `require_amount()` again before any balance movement,
  because values can also come from the database (legacy rows) or arithmetic.
"""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Annotated, Any

from fastapi import HTTPException
from pydantic import Field

CENT = Decimal("0.01")
ZERO = Decimal("0.00")
# Upper bound for any single amount: NGN 10 billion. Anything above is rejected.
MAX_AMOUNT = Decimal("10000000000.00")

Money = Annotated[Decimal, Field(gt=0, le=MAX_AMOUNT, max_digits=14, decimal_places=2)]
NonNegMoney = Annotated[Decimal, Field(ge=0, le=MAX_AMOUNT, max_digits=14, decimal_places=2)]


def to_decimal(value: Any) -> Decimal:
    """Convert DB/legacy values to Decimal without float artefacts (via str)."""
    if value is None:
        return ZERO
    if isinstance(value, Decimal):
        d = value
    else:
        try:
            d = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError):
            raise HTTPException(status_code=400, detail="Invalid monetary value")
    if not d.is_finite():
        raise HTTPException(status_code=400, detail="Invalid monetary value")
    return d


def q(value: Any) -> Decimal:
    """Quantize to 2dp (ROUND_HALF_UP)."""
    return to_decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def require_amount(value: Any, *, allow_zero: bool = False, what: str = "Amount") -> Decimal:
    """Service-layer guard: finite, 2dp, >0 (or >=0), <= MAX_AMOUNT."""
    d = to_decimal(value)
    if d != d.quantize(CENT):
        raise HTTPException(status_code=400, detail=f"{what} must have at most 2 decimal places")
    if allow_zero:
        if d < 0:
            raise HTTPException(status_code=400, detail=f"{what} must not be negative")
    elif d <= 0:
        raise HTTPException(status_code=400, detail=f"{what} must be greater than zero")
    if d > MAX_AMOUNT:
        raise HTTPException(status_code=400, detail=f"{what} exceeds the maximum allowed")
    return d.quantize(CENT)


def money_out(value: Any) -> str:
    """Serialise for API responses: always a 2dp string, e.g. "15000.00".
    (A str survives FastAPI's jsonable_encoder, which would turn Decimal into float.)"""
    return f"{q(value or 0):.2f}"
