# Migration order (B18)

Historic files are NOT renamed: they may already be applied on the live database,
and renaming them would make an operator re-run or skip them. Numbers 004 and 005
are duplicated; this file is the single authoritative order. All files are
PostgreSQL unless noted, and are idempotent (IF NOT EXISTS) except 010 and 012.

| Step | File | Notes |
|---|---|---|
| 1 | 002_auth_upgrade.sql | |
| 2 | 003_escrow_flow_upgrade.sql | |
| 3 | 004_facilitator_feature.sql | assumes v1 facilitator columns exist |
| 4 | 004_kyc_encrypted_ids.sql | |
| 5 | 005_2fa.sql | superseded by the next file (same columns); harmless |
| 6 | 005_2fa_gateway_virtual_accounts.sql | |
| 7 | 006_release_otp_share_token.sql | |
| 8 | 007_virtual_accounts.sql | **SQLite syntax** - skip on PostgreSQL (step 6 already creates the table) |
| 9 | 008_cancellation_fee.sql | |
| 10 | 009_fix_missing_columns.sql | adds columns the models do not use; harmless |
| 11 | 010_money_numeric_constraints.sql | **new** - run the pre-flight SELECTs first |
| 12 | 011_webhook_idempotency.sql | **new** |
| 13 | 012_sessions_kyc_status.sql | **new** - revokes all sessions (everyone re-logs in), resets KYC to pending review |

New migrations start at 013. Never reuse a number.

Before running 010-012 on production: take a backup, rehearse on a restored copy,
and run each file inside its own transaction (they contain BEGIN/COMMIT).
