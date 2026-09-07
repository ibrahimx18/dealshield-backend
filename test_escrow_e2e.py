#!/usr/bin/env python3
"""End-to-end escrow flow test against localhost:8001.

Flow: register 3 users → fund via DB → create listing → create escrow → accept → fund → fulfill → deliver → release with OTP → verify payouts
"""
import requests, time, re, sys, psycopg2

BASE = "http://localhost:8001"
S = requests.Session()
results = []
deal_otp = None

def log(step, ok, detail=""):
    results.append((step, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {step}: {detail}")

# ── 1. Register 3 users ──────────────────────────────────────────────────
ts = int(time.time())
users = {}
for role in ["buyer", "seller", "facilitator"]:
    email = f"test_{role}_{ts}@mail.tm"
    phone_suffix = {"buyer": "01", "seller": "02", "facilitator": "03"}[role]
    r = S.post(f"{BASE}/auth/register", json={
        "email": email,
        "password": "TestPass123!",
        "name": f"Test {role.title()}",
        "phone": f"080{ts % 10000000:07d}{phone_suffix}"
    })
    if r.status_code in (200, 201):
        data = r.json()
        user_obj = data.get("user", {})
        users[role] = {"email": email, "user_id": user_obj.get("id"), "token": data.get("access_token")}
        log(f"Register {role}", True, f"uid={user_obj.get('id')}")
    else:
        log(f"Register {role}", False, f"{r.status_code}: {r.text[:300]}")
        sys.exit(1)
    time.sleep(2)  # avoid rate limiter

# ── 2. Verify tokens (skip login — we already have tokens from registration) ─
def auth(role):
    return {"Authorization": f"Bearer {users[role]['token']}"}

# Rate limiter is 5 auth requests / 60s. Registration gives us valid tokens.
for role in ["buyer", "seller", "facilitator"]:
    r = S.get(f"{BASE}/auth/me", headers=auth(role))
    if r.status_code == 200:
        log(f"Token verify {role}", True)
    else:
        log(f"Token verify {role}", False, f"{r.status_code}: {r.text[:200]}")
        sys.exit(1)

# ── 3. Fund wallets via DB (update users.wallet_balance) ─────────────────
url_re = re.search(r'DATABASE_URL=postgresql://(\w+):(.*)@(\w+):(\d+)/(\w+)', open('.env').read())
db_user, db_pwd, db_host, db_port, db_name = url_re.groups()
conn = psycopg2.connect(user=db_user, password=db_pwd, host=db_host, port=db_port, dbname=db_name)
cur = conn.cursor()

buyer_id = users["buyer"]["user_id"]
seller_id = users["seller"]["user_id"]

cur.execute("UPDATE users SET wallet_balance = 10000000.0 WHERE id = %s", (buyer_id,))
cur.execute("UPDATE users SET wallet_balance = 2000000.0 WHERE id = %s", (seller_id,))
conn.commit()
conn.close()
log("Fund wallets (DB)", True, f"buyer={buyer_id} ₦10M, seller={seller_id} ₦2M")

# ── 4. Verify balances via API ───────────────────────────────────────────
for role in ["buyer", "seller"]:
    r = S.get(f"{BASE}/wallet/balance", headers=auth(role))
    if r.status_code == 200:
        bal = r.json().get("balance", 0)
        users[role]["balance"] = bal
        log(f"Wallet {role}", True, f"balance={bal:,.0f}")
    else:
        log(f"Wallet {role}", False, f"{r.status_code}: {r.text[:200]}")

# ── 5. Seller creates a listing ──────────────────────────────────────────
r = S.post(f"{BASE}/listings", json={
    "category": "cars",
    "title": "Toyota Corolla 2021",
    "description": "Clean Nigerian-used Toyota Corolla 2021, full option",
    "price": 8500000.0,
    "location": "Lagos",
    "insured": False
}, headers=auth("seller"))
if r.status_code in (200, 201):
    listing = r.json()
    listing_id = listing.get("id")
    log("Create listing", True, f"listing_id={listing_id}")
else:
    log("Create listing", False, f"{r.status_code}: {r.text[:300]}")
    sys.exit(1)

# ── 6. Buyer creates escrow from listing ────────────────────────────────
r = S.post(f"{BASE}/escrow/create", json={
    "listing_id": listing_id,
    "insured": False
}, headers=auth("buyer"))
if r.status_code in (200, 201):
    deal = r.json()
    deal_id = deal.get("id")
    log("Create escrow", True, f"deal_id={deal_id}, status={deal.get('status')}, amount={deal.get('amount')}")
else:
    log("Create escrow", False, f"{r.status_code}: {r.text[:300]}")
    sys.exit(1)

# ── 7. Seller accepts ───────────────────────────────────────────────────
r = S.post(f"{BASE}/escrow/{deal_id}/accept", headers=auth("seller"))
log("Seller accepts", r.status_code == 200,
    f"status={r.json().get('status')}" if r.status_code == 200 else f"{r.status_code}: {r.text[:300]}")

# ── 8. Buyer funds ──────────────────────────────────────────────────────
r = S.post(f"{BASE}/escrow/{deal_id}/fund", headers=auth("buyer"))
log("Buyer funds", r.status_code == 200,
    f"status={r.json().get('status')}" if r.status_code == 200 else f"{r.status_code}: {r.text[:300]}")

# ── 9. Seller fulfills ──────────────────────────────────────────────────
r = S.post(f"{BASE}/escrow/{deal_id}/fulfill", json={
    "logistics_provider": "ABC Logistics",
    "tracking_number": "TRK123456"
}, headers=auth("seller"))
log("Seller fulfills", r.status_code == 200,
    f"status={r.json().get('status')}" if r.status_code == 200 else f"{r.status_code}: {r.text[:300]}")

# ── 10. Seller marks delivered ─────────────────────────────────────────
r = S.post(f"{BASE}/escrow/{deal_id}/mark-delivered", headers=auth("seller"))
log("Seller delivers", r.status_code == 200,
    f"status={r.json().get('status')}" if r.status_code == 200 else f"{r.status_code}: {r.text[:300]}")

# ── 11. Get OTP ─────────────────────────────────────────────────────────
r = S.get(f"{BASE}/escrow/{deal_id}/release-otp", headers=auth("buyer"))
if r.status_code == 200:
    deal_otp = r.json().get("otp")
    log("Get OTP", True, f"otp={deal_otp}")
else:
    log("Get OTP", False, f"{r.status_code}: {r.text[:200]}")

# ── 12. Buyer releases with OTP ────────────────────────────────────────
if deal_otp:
    r = S.post(f"{BASE}/escrow/{deal_id}/release-otp", json={"otp": deal_otp}, headers=auth("buyer"))
    log("Release with OTP", r.status_code == 200,
        f"status={r.json().get('status')}" if r.status_code == 200 else f"{r.status_code}: {r.text[:300]}")
else:
    r = S.post(f"{BASE}/escrow/{deal_id}/approve", headers=auth("buyer"))
    log("Buyer approves (no OTP)", r.status_code == 200,
        f"status={r.json().get('status')}" if r.status_code == 200 else f"{r.status_code}: {r.text[:300]}")

# ── 13. Final deal status ───────────────────────────────────────────────
r = S.get(f"{BASE}/escrow/{deal_id}", headers=auth("buyer"))
if r.status_code == 200:
    final = r.json()
    log("Final status", final.get("status") == "released",
        f"status={final.get('status')}")
else:
    log("Final status", False, f"{r.status_code}: {r.text[:200]}")

# ── 14. Seller wallet after payout ──────────────────────────────────────
r = S.get(f"{BASE}/wallet/balance", headers=auth("seller"))
if r.status_code == 200:
    seller_final = r.json().get("balance", 0)
    # Seller started with 2M, should get ~8.5M minus commission minus gateway share
    log("Seller paid", seller_final > 2000000, f"balance={seller_final:,.0f} (was ₦2M)")
else:
    log("Seller paid", False, f"{r.status_code}: {r.text[:200]}")

# ── 15. Buyer wallet after escrow ───────────────────────────────────────
r = S.get(f"{BASE}/wallet/balance", headers=auth("buyer"))
if r.status_code == 200:
    buyer_final = r.json().get("balance", 0)
    # Buyer started with 10M, paid 8.5M + gateway share
    log("Buyer remaining", buyer_final < 10000000, f"balance={buyer_final:,.0f} (was ₦10M)")
else:
    log("Buyer remaining", False, f"{r.status_code}: {r.text[:200]}")

# ── Summary ─────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
passed = sum(1 for _, ok, _ in results if ok)
failed = sum(1 for _, ok, _ in results if not ok)
print(f"RESULTS: {passed}/{len(results)} passed, {failed} failed")
for step, ok, detail in results:
    print(f"  {'✓' if ok else '✗'} {step}: {detail}")
