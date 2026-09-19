# DealShield Data Protection & Security Policy (NDPR-aligned)

**Document owner:** DealShield Technologies (pre-incorporation)
**Data Protection Officer:** Geralt Revia (Director) — contact: Ibrahimx18@gmail.com
**Last updated:** September 19, 2026

## 1. Scope
This document describes how DealShield ("we") collects, stores, protects, and retains personal data of buyers, sellers, and facilitators, in alignment with the Nigeria Data Protection Regulation (NDPR) and the Nigeria Data Protection Act 2023.

## 2. Data we collect
- **Account data:** name, email, phone number, password (bcrypt-hashed — never stored readable)
- **Identity data (KYC):** NIN or BVN number, and a verified phone number
- **Transaction data:** listings, escrow deals, amounts, statuses, proofs of delivery
- **Security telemetry:** session records (IP, user agent, timestamps), audit logs

## 3. How identity data is protected
- NIN/BVN numbers are stored **only** as AES-256-GCM authenticated ciphertext (`enc:v1:` format)
- The encryption key is held **outside the database** (server environment, restricted file permissions)
- A stolen database dump alone **cannot reveal** any NIN/BVN
- Plaintext ID numbers never appear in API responses (masked form `*******8901` only), logs, or audit tables
- Every NIN/BVN submission is validated (11-digit structural check) before encryption

## 4. Access control
- All accounts require password + optional TOTP two-factor authentication
- Access tokens expire in 30 minutes; refresh tokens rotate on every use
- Stolen tokens are invalidated when the user changes their password
- All sessions are revoked on: password change, 2FA enable, 2FA disable
- Maximum 5 concurrent sessions per user
- Rate limiting on all authentication endpoints (5 attempts/60s/IP)
- Account lockout with progressive delays on failed logins

## 5. Mandatory KYC
No user may create listings, enter escrow, or facilitate deals without completing identity verification (NIN or BVN). This protects all transacting parties and reduces fraud.

## 6. Infrastructure security
- Self-owned VPS (Contabo), root login disabled, SSH keys only
- Firewall (ufw): only 22/80/443/8000 exposed
- fail2ban brute-force protection
- Cloudflare edge: DDoS protection, bot filtering, TLS
- Nginx reverse proxy isolates the application
- PostgreSQL bound to localhost only

## 7. Backups
- Nightly encrypted backups (AES-256, PBKDF2 200k iterations), 14-day local retention
- Off-site copy to a second, geographically separate machine, 30-day retention
- Backup encryption key stored separately from the database server
- Backups are verified by checksum after transfer

## 8. Data retention
- Account + KYC data: retained while the account is active; deleted/de-identified 90 days after account closure request, except where retention is required for dispute resolution or law
- Transaction records: retained 6 years (financial record-keeping standard)
- Audit logs: retained 2 years

## 9. Data subject rights
Users may request: access to their data, correction, deletion (subject to legal retention), and a copy of their transaction history. Requests via Ibrahimx18@gmail.com, actioned within 30 days.

## 10. Breach response
- Suspected breaches investigated immediately; affected users and NDPC notified within 72 hours of confirming a breach involving personal data
- Encrypted KYC storage (Section 3) ensures a database breach alone does not expose identity numbers

## 11. What we do NOT do
- We do not sell, rent, or share personal data with third parties for marketing
- We do not store card numbers (payment gateway integration, when live, handles card data under the gateway's PCI-DSS certification)
- We do not transfer personal data outside Nigeria except for infrastructure hosting, disclosed in the privacy policy

## 12. Encryption key management
- KYC encryption key: 32-byte random key, stored in server environment config (0600 permissions), never in the database, never in code, never in backups of the database
- Backup encryption key: separate key, different file, different machine from primary data
- Key rotation procedure: decrypt with old key, re-encrypt with new (documented in runbook)
