-- Migration 014 (N02): per-user 2FA failure counter + temporary lockout.
-- PostgreSQL. Idempotent. Five failed 2FA codes lock 2FA login for that user for 15 minutes;
-- a successful 2FA login resets the counter. Existing users start unlocked.
BEGIN;
ALTER TABLE users ADD COLUMN IF NOT EXISTS totp_failed_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS totp_locked_until TIMESTAMP;
COMMIT;
