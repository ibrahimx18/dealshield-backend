-- DealShield 2FA/TOTP Authentication Migration v5
-- Adds TOTP secret and enabled flag to the users table.

ALTER TABLE users ADD COLUMN IF NOT EXISTS totp_secret VARCHAR DEFAULT NULL;
ALTER TABLE users ADD COLUMN IF NOT EXISTS totp_enabled BOOLEAN DEFAULT FALSE;
