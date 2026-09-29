-- 010: B01/B19 - money as NUMERIC(18,2) + non-negative / positive CHECK constraints (PostgreSQL).
-- Run inside one transaction. PRE-FLIGHT (must return 0 rows each, fix data first otherwise):
--   SELECT id, wallet_balance FROM users WHERE wallet_balance < 0;
--   SELECT id, price FROM listings WHERE price <= 0;
--   SELECT id, amount FROM escrow_transactions WHERE amount <= 0 OR commission < 0;
--   SELECT id, amount FROM payment_references WHERE amount <= 0;
--   SELECT id, amount FROM payment_links WHERE amount <= 0;
-- Negative/zero rows are evidence of the B01 exploit: investigate before "fixing".

BEGIN;

ALTER TABLE users               ALTER COLUMN wallet_balance       TYPE NUMERIC(18,2) USING round(wallet_balance::numeric, 2);
ALTER TABLE listings            ALTER COLUMN price                TYPE NUMERIC(18,2) USING round(price::numeric, 2);
ALTER TABLE escrow_transactions ALTER COLUMN amount               TYPE NUMERIC(18,2) USING round(amount::numeric, 2);
ALTER TABLE escrow_transactions ALTER COLUMN commission           TYPE NUMERIC(18,2) USING round(commission::numeric, 2);
ALTER TABLE escrow_transactions ALTER COLUMN insurance_fee        TYPE NUMERIC(18,2) USING round(coalesce(insurance_fee,0)::numeric, 2);
ALTER TABLE escrow_transactions ALTER COLUMN facilitator_fee      TYPE NUMERIC(18,2) USING round(coalesce(facilitator_fee,0)::numeric, 2);
ALTER TABLE escrow_transactions ALTER COLUMN dealshield_cut       TYPE NUMERIC(18,2) USING round(coalesce(dealshield_cut,0)::numeric, 2);
ALTER TABLE escrow_transactions ALTER COLUMN facilitator_payout   TYPE NUMERIC(18,2) USING round(coalesce(facilitator_payout,0)::numeric, 2);
ALTER TABLE escrow_transactions ALTER COLUMN cancellation_fee     TYPE NUMERIC(18,2) USING round(coalesce(cancellation_fee,0)::numeric, 2);
ALTER TABLE escrow_transactions ALTER COLUMN gateway_fee          TYPE NUMERIC(18,2) USING round(coalesce(gateway_fee,0)::numeric, 2);
ALTER TABLE escrow_transactions ALTER COLUMN buyer_gateway_share  TYPE NUMERIC(18,2) USING round(coalesce(buyer_gateway_share,0)::numeric, 2);
ALTER TABLE escrow_transactions ALTER COLUMN seller_gateway_share TYPE NUMERIC(18,2) USING round(coalesce(seller_gateway_share,0)::numeric, 2);
ALTER TABLE wallet_transactions ALTER COLUMN amount               TYPE NUMERIC(18,2) USING round(amount::numeric, 2);
ALTER TABLE payment_references  ALTER COLUMN amount               TYPE NUMERIC(18,2) USING round(amount::numeric, 2);
ALTER TABLE payment_links       ALTER COLUMN amount               TYPE NUMERIC(18,2) USING round(amount::numeric, 2);
ALTER TABLE processed_payments  ALTER COLUMN amount               TYPE NUMERIC(18,2) USING round(amount::numeric, 2);
ALTER TABLE virtual_accounts    ALTER COLUMN expected_amount      TYPE NUMERIC(18,2) USING round(expected_amount::numeric, 2);

ALTER TABLE users               ADD CONSTRAINT ck_users_wallet_balance_nonneg CHECK (wallet_balance >= 0);
ALTER TABLE listings            ADD CONSTRAINT ck_listings_price_pos          CHECK (price > 0);
ALTER TABLE escrow_transactions ADD CONSTRAINT ck_escrow_amount_pos           CHECK (amount > 0);
ALTER TABLE escrow_transactions ADD CONSTRAINT ck_escrow_commission_nonneg    CHECK (commission >= 0);
ALTER TABLE escrow_transactions ADD CONSTRAINT ck_escrow_fac_fee_nonneg       CHECK (coalesce(facilitator_fee, 0) >= 0);
ALTER TABLE escrow_transactions ADD CONSTRAINT ck_escrow_insurance_nonneg     CHECK (coalesce(insurance_fee, 0) >= 0);
ALTER TABLE payment_references  ADD CONSTRAINT ck_payref_amount_pos           CHECK (amount > 0);
ALTER TABLE payment_links       ADD CONSTRAINT ck_paylink_amount_pos          CHECK (amount > 0);

COMMIT;
