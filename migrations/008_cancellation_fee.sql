-- Migration 008: Add cancellation_fee column to escrow_transactions
-- DealShield charges ₦5,000 flat fee on cancellation/dispute refund/split, credited to DealShield revenue.

ALTER TABLE escrow_transactions
ADD COLUMN IF NOT EXISTS cancellation_fee FLOAT DEFAULT 0.0;

COMMENT ON COLUMN escrow_transactions.cancellation_fee IS '₦5,000 flat fee deducted from buyer refund on cancellation or dispute refund/split, credited to DealShield';
