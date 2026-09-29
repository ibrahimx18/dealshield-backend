-- 012: B05 session-bound tokens + B02 fail-closed KYC status (PostgreSQL).
-- EFFECT: every existing session is revoked -> ALL USERS MUST LOG IN AGAIN.
-- Old access tokens have no "sid" claim and are rejected by the new code anyway.
BEGIN;

ALTER TABLE sessions ADD COLUMN IF NOT EXISTS sid VARCHAR(64);
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS revoked_at TIMESTAMP;
CREATE UNIQUE INDEX IF NOT EXISTS ix_sessions_sid ON sessions(sid);
UPDATE sessions SET revoked = TRUE, revoked_at = (now() AT TIME ZONE 'utc') WHERE revoked = FALSE;

ALTER TABLE users ADD COLUMN IF NOT EXISTS kyc_status VARCHAR(32) NOT NULL DEFAULT 'none';
-- B02: nobody was really verified (format check only). Previous "verified" users
-- become pending_verification and must be reviewed via POST /admin/kyc/{id}/decision.
UPDATE users SET kyc_status = 'pending_verification'
 WHERE kyc_verified = TRUE OR nin_encrypted IS NOT NULL OR bvn_encrypted IS NOT NULL;
UPDATE users SET kyc_verified = FALSE, nin_verified = FALSE, bvn_verified = FALSE,
                 id_verified = FALSE, business_verified = FALSE, phone_verified = FALSE;
ALTER TABLE users ADD CONSTRAINT ck_users_kyc_status
  CHECK (kyc_status IN ('none','pending_verification','verified','rejected'));

-- B02: listings were auto-verified.
UPDATE listings SET verified = FALSE;

-- Outstanding reset tokens may have been returned in API responses (B07): burn them.
UPDATE password_reset_tokens SET used = TRUE WHERE used = FALSE;

COMMIT;
