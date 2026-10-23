"""N04: one money-consistent rule for admin dispute settlement.

Money HELD for a funded escrow (what the buyer paid in):
    H = amount + insurance_fee + facilitator_fee (facilitated only) + buyer_gateway_share

Every naira of H is assigned to exactly one of:
    buyer_refund, seller_payout, facilitator_payout, or a DealShield fee line
so   buyer_refund + seller_payout + facilitator_payout + dealshield_retained == H.

Principle (DECISION FOR IBRAHIM - see FIX-REPORT.md):
  * The part of the deal that goes to the seller (seller share s = 1 - p) keeps the
    same deductions as a normal release (_release_funds): commission (normal deals
    only), the seller's gateway share, and the facilitator fee split 90/10.
  * The part refunded to the buyer (buyer share p) returns the buyer's money for
    that part: principal, facilitator fee, insurance and buyer gateway share.
  * Whenever the buyer receives anything, the flat cancellation fee (NGN 5,000,
    capped at what the buyer would receive) is deducted and kept by DealShield -
    same as _refund_buyer for cancelled funded deals.
  * release_to_seller == split with p = 0; refund_to_buyer == split with p = 100.
  * The seller's gateway share is taken out of the seller's side (it is part of
    `amount`), prorated by s. Any gateway cost not covered is absorbed by DealShield.
"""
from dataclasses import dataclass, field
from decimal import Decimal

from app.core.money import q, to_decimal, ZERO

FACILITATOR_SHARE = Decimal("0.90")


@dataclass
class Settlement:
    held: Decimal
    buyer_refund: Decimal
    seller_payout: Decimal
    facilitator_payout: Decimal
    fees: dict = field(default_factory=dict)  # DealShield fee lines (name -> Decimal)

    @property
    def dealshield_retained(self) -> Decimal:
        return q(sum(self.fees.values(), ZERO))

    @property
    def assigned(self) -> Decimal:
        return q(self.buyer_refund + self.seller_payout + self.facilitator_payout + self.dealshield_retained)

    def as_dict(self) -> dict:
        return {"held": str(self.held), "buyer_refund": str(self.buyer_refund),
                "seller_payout": str(self.seller_payout),
                "facilitator_payout": str(self.facilitator_payout),
                "dealshield_retained": str(self.dealshield_retained),
                "fees": {k: str(v) for k, v in self.fees.items()}}


def held_total(tx) -> Decimal:
    total = to_decimal(tx.amount) + to_decimal(tx.insurance_fee) + to_decimal(tx.buyer_gateway_share)
    if tx.is_facilitated:
        total += to_decimal(tx.facilitator_fee)
    return q(total)


def compute_settlement(tx, buyer_percent, cancellation_fee: Decimal,
                       facilitator_exists: bool = True) -> Settlement:
    """buyer_percent: 0..100 (Decimal-compatible). Pure: does not touch the DB."""
    p = to_decimal(buyer_percent) / Decimal(100)
    if p < 0 or p > 1:
        raise ValueError("buyer_percent must be between 0 and 100")
    s = Decimal(1) - p

    A = to_decimal(tx.amount)
    I = to_decimal(tx.insurance_fee)
    F = to_decimal(tx.facilitator_fee) if tx.is_facilitated else ZERO
    Gb = to_decimal(tx.buyer_gateway_share)
    Gs = to_decimal(tx.seller_gateway_share)
    C = ZERO if tx.is_facilitated else to_decimal(tx.commission)

    # Seller side (portion s of each component)
    A_s, F_s, I_s, Gb_s = q(A * s), q(F * s), q(I * s), q(Gb * s)
    C_s, Gs_s = q(C * s), q(Gs * s)
    seller_payout = q(A_s - C_s - Gs_s)
    if seller_payout < 0:
        raise ValueError("Seller deductions exceed the seller share")
    fac_payout = q(F_s * FACILITATOR_SHARE) if facilitator_exists else ZERO
    fac_cut = q(F_s - fac_payout)

    # Buyer side (exact remainder - no rounding leak)
    buyer_gross = q((A - A_s) + (F - F_s) + (I - I_s) + (Gb - Gb_s))
    cancel = min(to_decimal(cancellation_fee), buyer_gross) if buyer_gross > 0 else ZERO
    buyer_refund = q(buyer_gross - cancel)

    fees = {
        "commission": C_s,
        "seller_gateway_share": Gs_s,
        "buyer_gateway_share": Gb_s,
        "insurance": I_s,
        "facilitator_cut" if facilitator_exists else "facilitator_fee_unpaid": fac_cut,
        "cancellation_fee": q(cancel),
    }
    st = Settlement(held=held_total(tx), buyer_refund=buyer_refund, seller_payout=seller_payout,
                    facilitator_payout=fac_payout, fees={k: q(v) for k, v in fees.items()})
    if st.assigned != st.held:  # invariant - must never fire
        raise AssertionError(f"settlement does not balance: {st.assigned} != {st.held}")
    return st
