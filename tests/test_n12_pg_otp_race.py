"""N12: PostgreSQL-only race test for the release-OTP counter (with_for_update is a
no-op on SQLite, so the main suite cannot prove it).

Skipped unless TEST_PG_URL points at a DISPOSABLE PostgreSQL database, e.g.
    TEST_PG_URL=postgresql+psycopg2://user:pass@127.0.0.1:5432/dealshield_test
The test creates the tables there (create_all) and adds a few rows. Never point
it at production or the rehearsal copy.
"""
import os
import secrets
import threading
from datetime import datetime, timedelta

import pytest

PG_URL = os.getenv("TEST_PG_URL", "")
pytestmark = pytest.mark.skipif(not PG_URL.startswith("postgresql"),
                                reason="TEST_PG_URL not set (PostgreSQL-only race test)")


def test_ten_parallel_wrong_guesses_counted_exactly():
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from conftest import app
    from app.core.database import Base
    from app.core.security import create_access_token, get_password_hash
    from app.dependencies import get_db
    from app.models.models import EscrowTransaction, User, Session as SessionModel

    engine = create_engine(PG_URL, pool_size=15, max_overflow=5)
    Base.metadata.create_all(bind=engine)
    PgSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    def pg_db():
        db = PgSession()
        try:
            yield db
        finally:
            db.close()

    db = PgSession()
    sfx = secrets.token_hex(4)
    buyer = User(name="PG Buyer", phone="0811" + sfx, email=f"pg-b-{sfx}@example.test",
                 hashed_password=get_password_hash("TestPass123!"), is_active=True)
    seller = User(name="PG Seller", phone="0812" + sfx, email=f"pg-s-{sfx}@example.test",
                  hashed_password=get_password_hash("TestPass123!"), is_active=True)
    db.add_all([buyer, seller])
    db.flush()
    tx = EscrowTransaction(listing_title="PG race", category="cars", amount=1000, commission=0,
                           status="buyer_review", buyer_id=buyer.id, seller_id=seller.id,
                           release_otp="123456", release_otp_attempts=0,
                           release_otp_expiry=datetime.utcnow() + timedelta(hours=1))
    sid = secrets.token_hex(16)
    db.add(tx)
    db.add(SessionModel(user_id=buyer.id, sid=sid, token_hash=secrets.token_hex(32),
                        expires_at=datetime.utcnow() + timedelta(days=1)))
    db.commit()
    tx_id, buyer_id = tx.id, buyer.id
    db.close()
    token = create_access_token({"sub": str(buyer_id), "sid": sid})

    # Raise the per-user lock threshold above 10 so every guess is counted, not refused.
    import app.routers.escrow as escrow_mod
    old_max = escrow_mod.RELEASE_OTP_MAX_ATTEMPTS
    escrow_mod.RELEASE_OTP_MAX_ATTEMPTS = 10
    app.dependency_overrides[get_db] = pg_db
    statuses, barrier = [], threading.Barrier(10)
    try:
        def guess(i):
            c = TestClient(app, client=(f"192.0.2.{i + 10}", 4000))
            barrier.wait()
            r = c.post(f"/escrow/{tx_id}/release-otp", json={"otp": "000000"},
                       headers={"Authorization": f"Bearer {token}"})
            statuses.append(r.status_code)

        threads = [threading.Thread(target=guess, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        app.dependency_overrides.pop(get_db, None)
        escrow_mod.RELEASE_OTP_MAX_ATTEMPTS = old_max

    db = PgSession()
    try:
        row = db.query(EscrowTransaction).filter_by(id=tx_id).one()
        assert row.release_otp_attempts == 10, statuses
        assert row.release_otp_locked_until is not None
        assert row.status == "buyer_review"
    finally:
        db.close()
    assert sorted(statuses) == [400] * 9 + [429]
