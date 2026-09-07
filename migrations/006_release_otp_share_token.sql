-- Migration 006: Release OTP + Share Token features
-- Adds release_otp, release_otp_expiry, share_token to escrow_transactions

ALTER TABLE escrow_transactions ADD COLUMN IF NOT EXISTS release_otp VARCHAR DEFAULT '';
ALTER TABLE escrow_transactions ADD COLUMN IF NOT EXISTS release_otp_expiry TIMESTAMP;
ALTER TABLE escrow_transactions ADD COLUMN IF NOT EXISTS share_token VARCHAR DEFAULT '';
