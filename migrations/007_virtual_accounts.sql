-- DealShield Virtual Accounts Migration
-- Each escrow transaction gets a dedicated virtual (NUBAN) account number
-- so buyers can transfer funds directly instead of wallet-only funding.

CREATE TABLE IF NOT EXISTS virtual_accounts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    escrow_tx_id    INTEGER NOT NULL REFERENCES escrow_transactions(id),
    account_number  VARCHAR(10)  NOT NULL UNIQUE,
    bank_name       VARCHAR(100) NOT NULL DEFAULT 'DealShield MFB',
    bank_code       VARCHAR(10)  NOT NULL DEFAULT '999',
    account_name    VARCHAR(200) NOT NULL,
    provider        VARCHAR(50)  NOT NULL DEFAULT 'dealshield',
    status          VARCHAR(20)  NOT NULL DEFAULT 'active',   -- active, expired, paid
    expected_amount FLOAT        NOT NULL DEFAULT 0.0,
    expires_at      DATETIME,
    created_at      DATETIME     DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME     DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_virtual_accounts_escrow_tx_id ON virtual_accounts(escrow_tx_id);
CREATE INDEX IF NOT EXISTS idx_virtual_accounts_account_number ON virtual_accounts(account_number);
CREATE INDEX IF NOT EXISTS idx_virtual_accounts_status ON virtual_accounts(status);
