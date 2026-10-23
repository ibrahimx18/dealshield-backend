-- Migration 015 (N09): columns the models map that no earlier migration creates.
-- They were probably added by the original SQLAlchemy create_all() on the live DB,
-- but a database built only from migrations would lack them and every query on
-- these tables would fail. PostgreSQL. Idempotent: ADD COLUMN IF NOT EXISTS is a
-- no-op where the column already exists (existing types/defaults are NOT changed).
-- Base columns of the original tables (users.name, listings.title, ...) are not
-- listed: those tables cannot exist without them.
-- Run scripts/check_schema_drift.py afterwards; it must print "OK".
BEGIN;

-- users
ALTER TABLE users ADD COLUMN IF NOT EXISTS role VARCHAR DEFAULT 'user';
ALTER TABLE users ADD COLUMN IF NOT EXISTS failed_attempts INTEGER DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS locked_until TIMESTAMP;
ALTER TABLE users ADD COLUMN IF NOT EXISTS business_name VARCHAR DEFAULT '';
ALTER TABLE users ADD COLUMN IF NOT EXISTS badge_tier VARCHAR DEFAULT 'none';
ALTER TABLE users ADD COLUMN IF NOT EXISTS total_deals INTEGER DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS rating DOUBLE PRECISION DEFAULT 5.0;

-- listings
ALTER TABLE listings ADD COLUMN IF NOT EXISTS image_path VARCHAR;
ALTER TABLE listings ADD COLUMN IF NOT EXISTS insured BOOLEAN DEFAULT FALSE;

-- escrow_transactions: dispatch / legacy pickup-OTP feature
ALTER TABLE escrow_transactions ADD COLUMN IF NOT EXISTS insured BOOLEAN DEFAULT FALSE;
ALTER TABLE escrow_transactions ADD COLUMN IF NOT EXISTS logistics_provider VARCHAR DEFAULT '';
ALTER TABLE escrow_transactions ADD COLUMN IF NOT EXISTS tracking_number VARCHAR DEFAULT '';
ALTER TABLE escrow_transactions ADD COLUMN IF NOT EXISTS pickup_otp VARCHAR DEFAULT '';
ALTER TABLE escrow_transactions ADD COLUMN IF NOT EXISTS rider_phone VARCHAR DEFAULT '';
ALTER TABLE escrow_transactions ADD COLUMN IF NOT EXISTS rider_name VARCHAR DEFAULT '';
ALTER TABLE escrow_transactions ADD COLUMN IF NOT EXISTS pickup_confirmed BOOLEAN DEFAULT FALSE;
ALTER TABLE escrow_transactions ADD COLUMN IF NOT EXISTS dispatched_at TIMESTAMP;
ALTER TABLE escrow_transactions ADD COLUMN IF NOT EXISTS otp_attempts INTEGER DEFAULT 0;
ALTER TABLE escrow_transactions ADD COLUMN IF NOT EXISTS otp_locked BOOLEAN DEFAULT FALSE;
ALTER TABLE escrow_transactions ADD COLUMN IF NOT EXISTS otp_expiry TIMESTAMP;

COMMIT;
