-- DealShield Escrow Flow Upgrade Migration
-- Adds new columns for the full transaction lifecycle:
-- CREATED → SELLER_ACCEPTED → FUNDED → SELLER_FULFILLING → BUYER_REVIEW →
-- BUYER_APPROVED → RELEASED → CLOSED
-- With dispute path: DISPUTED → UNDER_INVESTIGATION → RELEASED/REFUNDED/SPLIT → CLOSED

-- 1. New escrow flow columns
ALTER TABLE escrow_transactions ADD COLUMN IF NOT EXISTS accepted_at TIMESTAMPTZ;
ALTER TABLE escrow_transactions ADD COLUMN IF NOT EXISTS funded_at TIMESTAMPTZ;
ALTER TABLE escrow_transactions ADD COLUMN IF NOT EXISTS fulfilment_started_at TIMESTAMPTZ;
ALTER TABLE escrow_transactions ADD COLUMN IF NOT EXISTS buyer_review_started_at TIMESTAMPTZ;
ALTER TABLE escrow_transactions ADD COLUMN IF NOT EXISTS buyer_review_deadline TIMESTAMPTZ;
ALTER TABLE escrow_transactions ADD COLUMN IF NOT EXISTS dispute_reason TEXT;
ALTER TABLE escrow_transactions ADD COLUMN IF NOT EXISTS dispute_evidence TEXT;
ALTER TABLE escrow_transactions ADD COLUMN IF NOT EXISTS dispute_initiated_by VARCHAR(20) DEFAULT '';
ALTER TABLE escrow_transactions ADD COLUMN IF NOT EXISTS admin_resolution VARCHAR(50);
ALTER TABLE escrow_transactions ADD COLUMN IF NOT EXISTS admin_reason TEXT;
ALTER TABLE escrow_transactions ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMPTZ;
ALTER TABLE escrow_transactions ADD COLUMN IF NOT EXISTS closed_at TIMESTAMPTZ;
ALTER TABLE escrow_transactions ADD COLUMN IF NOT EXISTS accept_deadline TIMESTAMPTZ;
ALTER TABLE escrow_transactions ADD COLUMN IF NOT EXISTS payment_deadline TIMESTAMPTZ;

-- 2. Add is_active to listings (if not already added by auth migration)
ALTER TABLE listings ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE;

-- 3. Migrate existing transactions to new status mapping
-- Old: funds_deposited → New: funded
UPDATE escrow_transactions SET status = 'funded', funded_at = created_at
WHERE status = 'funds_deposited';

-- Old: shipped → New: seller_fulfilling
UPDATE escrow_transactions SET status = 'seller_fulfilling', fulfilment_started_at = COALESCE(dispatched_at, updated_at)
WHERE status = 'shipped';

-- Old: delivered → New: closed (already completed, treat as released + closed)
UPDATE escrow_transactions SET status = 'closed', closed_at = COALESCE(completed_at, updated_at)
WHERE status = 'delivered';

-- Old: cancelled stays cancelled
-- Old: disputed stays disputed (will be handled by new admin flow)

-- 4. Update default status for new transactions
ALTER TABLE escrow_transactions ALTER COLUMN status SET DEFAULT 'created';

-- 5. Add index on status for faster queries
CREATE INDEX IF NOT EXISTS idx_escrow_status ON escrow_transactions(status);
CREATE INDEX IF NOT EXISTS idx_escrow_buyer ON escrow_transactions(buyer_id);
CREATE INDEX IF NOT EXISTS idx_escrow_seller ON escrow_transactions(seller_id);
CREATE INDEX IF NOT EXISTS idx_escrow_buyer_review_deadline ON escrow_transactions(buyer_review_deadline)
    WHERE status = 'buyer_review';
