-- Migration 009: Fix missing columns from migrations 005-008
-- These columns were defined in models but never added to the DB (ADD COLUMN IF NOT EXISTS silently failed)

-- wallet_transactions
ALTER TABLE wallet_transactions ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT now();

-- escrow_transactions (missing from migrations 005, 006, 008)
ALTER TABLE escrow_transactions ADD COLUMN IF NOT EXISTS otp_expires_at TIMESTAMP;
ALTER TABLE escrow_transactions ADD COLUMN IF NOT EXISTS gateway_fee_buyer FLOAT DEFAULT 0;
ALTER TABLE escrow_transactions ADD COLUMN IF NOT EXISTS gateway_fee_seller FLOAT DEFAULT 0;
ALTER TABLE escrow_transactions ADD COLUMN IF NOT EXISTS accepted_by INTEGER;
ALTER TABLE escrow_transactions ADD COLUMN IF NOT EXISTS cancelled_at TIMESTAMP;
ALTER TABLE escrow_transactions ADD COLUMN IF NOT EXISTS cancelled_by INTEGER;
ALTER TABLE escrow_transactions ADD COLUMN IF NOT EXISTS cancel_reason TEXT DEFAULT '';
ALTER TABLE escrow_transactions ADD COLUMN IF NOT EXISTS auto_release_at TIMESTAMP;
