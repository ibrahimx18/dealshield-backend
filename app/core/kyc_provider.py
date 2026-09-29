"""Identity-verification provider interface (B02).

Format checks (11 digits) are NOT verification. Submitting an ID only moves a
user to `pending_verification`. Only a real provider callback or a manual
reviewer decision (admin endpoint) may set `verified`.

No provider is integrated yet: `submit_for_verification()` records nothing
externally and returns "pending_verification".
"""
KYC_NONE = "none"
KYC_PENDING = "pending_verification"
KYC_VERIFIED = "verified"
KYC_REJECTED = "rejected"
KYC_STATES = (KYC_NONE, KYC_PENDING, KYC_VERIFIED, KYC_REJECTED)


class KYCProvider:
    name = "none"

    def submit_for_verification(self, *, user_id: int, id_type: str, encrypted_id: str, phone: str) -> str:
        """Queue the identity for verification. Must never return 'verified' synchronously
        from format checks. Stub: always pending (manual review)."""
        return KYC_PENDING


def get_kyc_provider() -> KYCProvider:
    return KYCProvider()
