-- DealShield Facilitator Feature Migration
-- Adds facilitator support: a third party (facilitator/middleman) can create
-- deals between buyers and sellers and earn a percentage of the commission.

-- 1. Add facilitator columns to escrow_transactions
ALTER TABLE escrow_transactions ADD COLUMN IF NOT EXISTS facilitator_id INTEGER REFERENCES users(id);
ALTER TABLE escrow_transactions ADD COLUMN IF NOT EXISTS facilitator_name VARCHAR(255) DEFAULT '';
ALTER TABLE escrow_transactions ADD COLUMN IF NOT EXISTS facilitator_fee FLOAT DEFAULT 0.0;
ALTER TABLE escrow_transactions ADD COLUMN IF NOT EXISTS facilitator_fee_pct FLOAT DEFAULT 0.0;
ALTER TABLE escrow_transactions ADD COLUMN IF NOT EXISTS buyer_accepted_terms BOOLEAN DEFAULT FALSE;
ALTER TABLE escrow_transactions ADD COLUMN IF NOT EXISTS seller_accepted_terms BOOLEAN DEFAULT FALSE;
ALTER TABLE escrow_transactions ADD COLUMN IF NOT EXISTS buyer_accepted_at TIMESTAMPTZ;
ALTER TABLE escrow_transactions ADD COLUMN IF NOT EXISTS seller_accepted_at TIMESTAMPTZ;
ALTER TABLE escrow_transactions ADD COLUMN IF NOT EXISTS is_facilitated BOOLEAN DEFAULT FALSE;

-- 2. Add index for facilitator queries
CREATE INDEX IF NOT EXISTS idx_escrow_facilitator ON escrow_transactions(facilitator_id)
    WHERE is_facilitated = TRUE;

-- 3. Allow listing_id = 0 for facilitated deals (no listing required)
-- This is handled at the application level — no DB change needed since
-- listing_id is an integer, not a FK constraint (it references listings.id
-- but 0 won't exist, which is fine for facilitated deals that don't need a listing)
