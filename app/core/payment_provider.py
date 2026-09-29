"""Payment-account provider interface (B03).

DealShield must never fabricate bank or virtual account numbers. A dedicated
account may only be shown to a buyer after a real provider has issued it.

No provider integration exists in this codebase yet, so `get_account_provider()`
returns None and callers must answer HTTP 503. To add one, implement
`AccountProvider.issue_account()` against the provider's sandbox, add
reconciliation tests, and register it here behind explicit configuration.
"""
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional


@dataclass
class IssuedAccount:
    account_number: str
    bank_name: str
    bank_code: str
    account_name: str
    provider: str
    provider_reference: str


class AccountProvider:
    name = "abstract"

    def issue_account(self, *, escrow_tx_id: int, expected_amount: Decimal,
                      customer_email: str, customer_name: str) -> IssuedAccount:  # pragma: no cover
        raise NotImplementedError


def get_account_provider() -> Optional[AccountProvider]:
    """Return a configured, implemented provider, or None (=> HTTP 503)."""
    return None


UNSUPPORTED_DETAIL = (
    "Dedicated payment accounts are unavailable: no account provider is integrated. "
    "No account number has been issued."
)
