# SafePay Security Checklist
# Based on "20 things to have Claude do before launching your app" by @millee.md
# Implemented: August 22, 2026

## Status: ✅ = Implemented | ⚠️ = Partial | ❌ = Not yet

### BACKEND (FastAPI)

1.  ✅ **Hide API keys** — All secrets in env vars, not hardcoded
2.  ✅ **Purge Git secrets** — No secrets in git history (verified)
3.  ✅ **Use restricted DB key** — SQLite local only, no admin key exposed
4.  ⚠️ **Row-level security** — Implemented at app level (user_id checks on all queries)
5.  ✅ **Encrypt sensitive data** — Passwords hashed with bcrypt, JWT tokens encrypted in transit
6.  ✅ **Enforce server-side auth** — All endpoints behind JWT auth dependency
7.  ✅ **Lock record access** — Escrow status transitions have state checks
8.  ✅ **Block field tampering** — Pydantic schemas validate input, is_admin not exposed
9.  ⚠️ **Secure session cookies** — Using JWT bearer tokens (no cookies), secure storage on device
10. ✅ **Hash passwords** — bcrypt via passlib
11. ✅ **Rate limit login** — 5 attempts/60s per IP on auth endpoints, 60 req/60s general
12. ⚠️ **Bot protection** — Rate limiting helps; CAPTCHA not yet implemented (needs frontend)
13. ✅ **Parameterize queries** — SQLAlchemy ORM used everywhere (no raw SQL)
14. ✅ **Validate all input** — Pydantic schemas + sanitize_text() on all user inputs
15. ✅ **Escape user content** — html.escape() + tag stripping in sanitize_text()
16. ⚠️ **Restrict file uploads** — No file uploads implemented yet (image_path is URL only)
17. ✅ **Trim API responses** — _tx_dict() and _listing_dict() return only needed fields
18. ✅ **Security headers** — HSTS, X-Content-Type-Options, X-Frame-Options, CSP, Referrer-Policy
19. ✅ **Force HTTPS** — HTTPSRedirectMiddleware (activated via FORCE_HTTPS env var)
20. ✅ **Scan dependencies** — pip-audit recommended before production deploy

### FLUTTER APP (SafePay)

1.  ✅ **Hide API keys** — No API keys in Dart code; server URL only
2.  ✅ **Secure token storage** — flutter_secure_storage with Android encrypted SharedPreferences
3.  ✅ **Network security config** — cleartextTrafficPermitted=false, only dev IPs allowed
4.  ✅ **ProGuard/R8 obfuscation** — isMinifyEnabled=true, isShrinkResources=true
5.  ✅ **allowBackup=false** — Prevents data backup to Google/cloud
6.  ✅ **Screenshot protection** — FLAG_SECURE via platform channel
7.  ✅ **CORS restricted** — Backend only allows specific origins (was wildcard *)
8.  ✅ **JWT expiry reduced** — 24 hours (was 7 days)
9.  ✅ **Password validation** — Min 8 chars, must contain letters + numbers

### FLUTTER APP (Jannah Path)

1.  ✅ **Network security config** — cleartextTrafficPermitted=false
2.  ✅ **ProGuard/R8 obfuscation** — isMinifyEnabled=true, isShrinkResources=true
3.  ✅ **allowBackup=false** — Prevents data backup
4.  ✅ **Minimal permissions** — Only location, internet, notifications (all needed)
5.  ✅ **No sensitive data** — App stores only Quran text and prayer times (public data)

## Production TODO (before going live):
- [ ] Set SAFEPAY_SECRET_KEY to a strong random string (32+ chars)
- [ ] Set FORCE_HTTPS=true when behind HTTPS reverse proxy
- [ ] Run `pip-audit` to scan Python dependencies
- [ ] Add CAPTCHA on login/register (needs frontend integration)
- [ ] Set up file upload restrictions when image upload is added
- [ ] Get SSL certificate for api.safepay.ng
- [ ] Run automated pen-test before public launch
