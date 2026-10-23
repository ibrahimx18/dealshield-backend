-- Migration 016 (N05): remember when the "funds auto-release soon" reminder was sent,
-- so the auto-release pass sends it once per buyer-review window. PostgreSQL. Idempotent.
BEGIN;
ALTER TABLE escrow_transactions ADD COLUMN IF NOT EXISTS buyer_review_reminder_sent_at TIMESTAMP;
COMMIT;
