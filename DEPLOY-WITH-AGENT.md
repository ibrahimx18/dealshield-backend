# Deploying backend branch `kiko/otp-throttle-on-main-20261023` (+ website `kiko/review-fixes-20261023`) with a coding agent

Paste the block below into your coding agent (Claude Code, Codex, Cursor, Aider, etc.)
**on the server that runs the DealShield backend**, or on a machine with SSH access to it.
The agent will do the work step by step and stop at every point where a human must decide.

Estimated time: 30-45 minutes. Users will be logged out once at the end.

---

## Prompt to paste

```
You are deploying backend branch `kiko/otp-throttle-on-main-20261023` of the DealShield
backend (FastAPI + PostgreSQL) and website branch `kiko/review-fixes-20261023` (static, served by nginx/Vercel). Work carefully, verify
every step, and STOP and ask me whenever a step says "ASK".

Context files in the backend repo: FIX-REPORT.md is not in the repo; read
`migrations/MIGRATION_ORDER.md`, `requirements.txt`, and `app/main.py` startup checks.

STEP 0 - Discover (read-only)
- Find how the backend runs today (systemd unit / pm2 / docker / uvicorn) and its .env location.
- Find the live DATABASE_URL host/db name (never print the password).
- Find the nginx server blocks for the website and the API (`nginx -T | grep -A20 server_name`).
- Report: current deployed commit (`git -C <backend> rev-parse --short HEAD`), Python version,
  Postgres version, service name. ASK me to confirm before continuing.

STEP 1 - Backup
- `pg_dump --format=custom --no-owner` the live database to /var/backups/dealshield/pre-fix-<date>.dump
- Verify with `pg_restore --list` and print the file size + sha256. Do not continue if this fails.

STEP 2 - Rehearse migrations on a COPY
- `createdb dealshield_rehearsal` and `pg_restore` the dump into it.
- Check out the backend branch in a temporary clone.
- N09 DRIFT CHECK FIRST (read-only): `DATABASE_URL=<rehearsal url> python scripts/check_schema_drift.py`
  and save the output. Missing columns are expected before 013-016; anything else missing
  after migration 015 means STOP and ASK.
- Run the migrations against dealshield_rehearsal ONLY, in the exact order of
  `migrations/MIGRATION_ORDER.md`. Skip any file the file marks as SQLite-only (007).
  Skip 002-009 if `\dt`/schema shows they are already applied live (compare tables/columns first).
- BEFORE migration 010, run its pre-flight SELECTs. If any negative balance or amount rows exist,
  STOP and ASK me - a human must correct them.
- Run 010 -> 011 -> 012 -> 013 -> 014 -> 015 -> 016 (see MIGRATION_ORDER.md).
- After they succeed on the copy, print the new tables (payment_webhook_events,
  webhook_quarantine, sessions, kyc changes) and row counts, and the new columns:
  escrow_transactions.release_otp_attempts / release_otp_locked_until (013),
  users.totp_failed_attempts / totp_locked_until (014),
  escrow_transactions.buyer_review_reminder_sent_at (016).
- Re-run `scripts/check_schema_drift.py` against the copy: it MUST print `OK`. If not, STOP and ASK.
  (Without 013 every escrow endpoint returns 500; without 014 2FA login returns 500.) Drop the rehearsal DB only
  after I say so.

STEP 3 - Prepare the release (no traffic change yet)
- Fetch and check out `kiko/otp-throttle-on-main-20261023` in the backend deploy directory
  (or a new release dir if the service uses one).
- Create/refresh the venv and `pip install -r requirements.txt` (pinned).
- Update .env (keep a .bak): ENVIRONMENT=production; remove DEALSHIELD_SEED_DEMO and
  SAFEPAY_TEST_MODE or set them false; ensure PAYSTACK_SECRET_KEY and a non-empty
  SAFEPAY_SECRET_KEY exist.
  Proxy / client IP (N02): if the API is reachable ONLY through nginx/Cloudflare (port 8000
  firewalled from the internet - verify with `ss -ltnp` and the firewall), set TRUSTED_PROXY=1
  and FORWARDED_ALLOW_IPS=<nginx address, usually 127.0.0.1>. If port 8000 is public, leave
  TRUSTED_PROXY unset (headers would be spoofable) and ASK me. Optional: TRUSTED_PROXY_HOPS
  (default 1). nginx must send `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;`.
  Cookies (N16): keep AUTH_COOKIE_SECURE unset/true; keep AUTH_REFRESH_BODY_COMPAT=true until
  the Android app also uses the cookie. Start the service via start.sh (it passes
  --proxy-headers --forwarded-allow-ips). Print only the KEY NAMES you changed, never values.
- Dry-start the app on a spare port (e.g. 8099) with the same .env against the REHEARSAL db
  and hit /health. It must start (the app refuses to boot with test/seed flags in production).

STEP 4 - Deploy backend (brief downtime, ~1 minute)
- Put up a maintenance notice if the site has one.
- Run migrations 010 -> 011 -> 012 -> 013 -> 014 -> 015 -> 016 against the LIVE database
  (same order, same files; skip any already applied), then `scripts/check_schema_drift.py` must print OK.
- Restart the service. Confirm /health is 200 and the log shows no startup refusal.
- Smoke: unauthenticated GET on a protected route returns 401; login works and returns a token
  that contains `sid` and sets an HttpOnly `ds_refresh` cookie; a request with a pre-deploy token
  returns 401 (expected - sessions revoked). `/docs` and `/openapi.json` return 404.
  Log in twice from two networks and confirm `audit_logs.ip_address` shows two different real
  client IPs (not the proxy address). If they are identical, TRUSTED_PROXY is wrong - ASK me.
- Auto-release + reminders (N05): make sure the cron that POSTs /escrow/process-expired-reviews
  runs at least every 15 minutes (it now also sends the pre-release reminder).
- If anything fails: stop the service, `pg_restore --clean` the STEP 1 dump, check out the
  previous commit, restart, and report. ASK me before any rollback.

STEP 5 - Website
- Check out `kiko/review-fixes-20261023` in the website root (or deploy it on Vercel - vercel.json
  carries the same headers).
- Merge the headers from `docs/nginx-security-headers.conf` and the `location = /listings`
  rewrite from `docs/SECURITY-HEADERS.md` into the site's nginx server block (keep a .bak).
- `nginx -t` then reload. Confirm with curl -I that Content-Security-Policy, X-Frame-Options and
  X-Robots-Tag headers are present on / and on /dashboard.html.
- Open the site in a browser: login -> dashboard -> create listing -> logout must work and logout
  must call POST /auth/logout.

STEP 6 - Tell me
- Print: deployed commits (backend + website), migration versions applied, backup path + sha256,
  header check output, and the exact list of things users will notice (everyone logs in again;
  previously verified KYC is now "pending - admin review"; bank-account generation shows
  "unavailable" until a real provider is connected).

Hard rules: never print secrets; never run destructive SQL outside the rehearsal database
without my explicit yes; never force-push; never edit git history.
```

---

## Manual decisions Ibrahim still owns (the agent will ask)
1. Any negative balance/amount rows found by the 010 pre-flight.
2. Legal sign-off of the updated terms copy (24-hour auto-release, NGN 5,000 cancellation fee) - N05.
2b. Dispute settlement rule in app/core/settlement.py - N04.
3. Facilitator roles, public share-link fields, full ledger (B14/B16/B19) - product decisions.
4. Confirm domains hard-coded in the branch: site `https://dealshield.com.ng`, API `https://api.dealshield.ng`.
5. Rate limiting: confirm nginx sets `X-Forwarded-For` and port 8000 is not public (TRUSTED_PROXY).
   The in-app limiter is in-memory per worker; the 2FA lock is in the database (N02).
6. History purge + key rotation: see HISTORY-PURGE.md (N14).
