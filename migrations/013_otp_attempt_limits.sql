-- Migration 013: persist failed escrow release OTP attempts and temporary lockout.
-- Run on PostgreSQL. Existing pending buyer-review transactions start unlocked.
ALTER TABLE escrow_transactions ADD COLUMN IF NOT EXISTS release_otp_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE escrow_transactions ADD COLUMN IF NOT EXISTS release_otp_locked_until TIMESTAMPTZ;
