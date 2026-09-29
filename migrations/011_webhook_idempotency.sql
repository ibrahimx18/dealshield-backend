-- 011: B04 - webhook idempotency ledger, quarantine table, escrow-bound funding intents (PostgreSQL).
BEGIN;

ALTER TABLE payment_references ADD COLUMN IF NOT EXISTS escrow_tx_id INTEGER REFERENCES escrow_transactions(id);
ALTER TABLE payment_references ADD COLUMN IF NOT EXISTS currency VARCHAR(8) NOT NULL DEFAULT 'NGN';
CREATE INDEX IF NOT EXISTS ix_payment_references_escrow_tx_id ON payment_references(escrow_tx_id);

CREATE TABLE IF NOT EXISTS payment_webhook_events (
    id           SERIAL PRIMARY KEY,
    provider     VARCHAR(32)  NOT NULL,
    event_id     VARCHAR(128) NOT NULL,
    reference    VARCHAR(128) NOT NULL,
    event_type   VARCHAR(64)  NOT NULL,
    escrow_tx_id INTEGER REFERENCES escrow_transactions(id),
    amount       NUMERIC(18,2),
    currency     VARCHAR(8),
    status       VARCHAR(32)  NOT NULL DEFAULT 'received',
    reason       TEXT DEFAULT '',
    received_at  TIMESTAMP DEFAULT (now() AT TIME ZONE 'utc'),
    CONSTRAINT uq_webhook_provider_event     UNIQUE (provider, event_id),
    CONSTRAINT uq_webhook_provider_reference UNIQUE (provider, reference)
);

CREATE TABLE IF NOT EXISTS webhook_quarantine (
    id              SERIAL PRIMARY KEY,
    provider        VARCHAR(32)  NOT NULL,
    event_id        VARCHAR(128),
    reference       VARCHAR(128),
    event_type      VARCHAR(64),
    escrow_tx_id    INTEGER,
    expected_amount NUMERIC(18,2),
    received_amount NUMERIC(18,2),
    currency        VARCHAR(8),
    reason          TEXT NOT NULL,
    payload_sha256  VARCHAR(64) NOT NULL,
    created_at      TIMESTAMP DEFAULT (now() AT TIME ZONE 'utc')
);

-- B18: system audit entries now use actor_id NULL instead of 0.
UPDATE audit_logs SET actor_id = NULL WHERE actor_id = 0;

COMMIT;
