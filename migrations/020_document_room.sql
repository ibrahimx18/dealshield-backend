-- DealShield migration: Document Room (land/property deals)
-- Adds docs deal columns to escrow_transactions and the deal_documents table.

ALTER TABLE escrow_transactions ADD COLUMN IF NOT EXISTS docs_deadline TIMESTAMP;
ALTER TABLE escrow_transactions ADD COLUMN IF NOT EXISTS docs_uploaded_at TIMESTAMP;
ALTER TABLE escrow_transactions ADD COLUMN IF NOT EXISTS walkaway_fee DOUBLE PRECISION DEFAULT 0.0;

CREATE TABLE IF NOT EXISTS deal_documents (
    id SERIAL PRIMARY KEY,
    tx_id INTEGER NOT NULL REFERENCES escrow_transactions(id),
    uploader_id INTEGER NOT NULL REFERENCES users(id),
    filename VARCHAR DEFAULT '',
    storage_path VARCHAR NOT NULL,
    file_type VARCHAR DEFAULT 'pdf',
    page_count INTEGER DEFAULT 1,
    uploaded_at TIMESTAMP DEFAULT NOW(),
    note VARCHAR DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_deal_documents_tx_id ON deal_documents (tx_id);
