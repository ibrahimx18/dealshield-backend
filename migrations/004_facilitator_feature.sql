-- DealShield Facilitator Feature Migration v2
-- Redesigned: facilitator sets their own fee amount (not percentage).
-- DealShield takes 10% of the facilitator fee, facilitator gets 90%.
-- No escrow commission on facilitated deals.

-- 1. Add new facilitator columns
ALTER TABLE escrow_transactions ADD COLUMN IF NOT EXISTS dealshield_cut FLOAT DEFAULT 0.0;
ALTER TABLE escrow_transactions ADD COLUMN IF NOT EXISTS facilitator_payout FLOAT DEFAULT 0.0;

-- 2. Remove old facilitator_fee_pct column (if it exists from v1)
ALTER TABLE escrow_transactions DROP COLUMN IF EXISTS facilitator_fee_pct;

-- 3. Existing facilitator columns from v1 migration (kept):
-- facilitator_id, facilitator_name, facilitator_fee, buyer_accepted_terms,
-- seller_accepted_terms, buyer_accepted_at, seller_accepted_at, is_facilitated
