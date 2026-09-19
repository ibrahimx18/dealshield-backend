-- 004: KYC — encrypted NIN/BVN storage + mandatory KYC gates
-- Columns store only AES-256-GCM ciphertext ("enc:v1:...").

ALTER TABLE users ADD COLUMN IF NOT EXISTS nin_encrypted TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS bvn_encrypted TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS kyc_verified BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS kyc_submitted_at TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS kyc_id_type TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS kyc_phone_provided TEXT;
