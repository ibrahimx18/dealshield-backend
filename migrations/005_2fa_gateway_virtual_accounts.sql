-- DealShield 2FA + Gateway Fees + Virtual Accounts Migration
-- Adds: TOTP 2FA columns on users, gateway fee columns on escrow_transactions,
-- and virtual_accounts table.

-- 1. 2FA columns on users
ALTER TABLE users ADD COLUMN IF NOT EXISTS totp_secret VARCHAR(255);
ALTER TABLE users ADD COLUMN IF NOT EXISTS totp_enabled BOOLEAN DEFAULT FALSE;

-- 2. Gateway fee columns on escrow_transactions
ALTER TABLE escrow_transactions ADD COLUMN IF NOT EXISTS gateway_fee FLOAT DEFAULT 0.0;
ALTER TABLE escrow_transactions ADD COLUMN IF NOT EXISTS buyer_gateway_share FLOAT DEFAULT 0.0;
ALTER TABLE escrow_transactions ADD COLUMN IF NOT EXISTS seller_gateway_share FLOAT DEFAULT 0.0;

-- 3. Virtual accounts table
CREATE TABLE IF NOT EXISTS virtual_accounts (
    id SERIAL PRIMARY KEY,
    escrow_tx_id INTEGER NOT NULL REFERENCES escrow_transactions(id),
    account_number VARCHAR(20) UNIQUE NOT NULL,
    bank_name VARCHAR(100) DEFAULT 'DealShield Trust Bank',
    bank_code VARCHAR(10) DEFAULT '000000',
    account_name VARCHAR(255) NOT NULL,
    provider VARCHAR(50) DEFAULT 'dealshield',
    status VARCHAR(20) DEFAULT 'active',
    expected_amount FLOAT NOT NULL DEFAULT 0.0,
    expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Index for fast account number lookups (webhook callbacks)
CREATE INDEX IF NOT EXISTS idx_virtual_accounts_account_number ON virtual_accounts(account_number);
CREATE INDEX IF NOT EXISTS idx_virtual_accounts_escrow_tx ON virtual_accounts(escrow_tx_id);
