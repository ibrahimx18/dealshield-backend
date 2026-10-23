"""N09: drift checker finds missing columns; test DB (built from models) has none;
migration 015 covers every column the review listed."""
import os
import tempfile
from pathlib import Path

from sqlalchemy import create_engine, text

from conftest import engine
from app.core.database import Base
from app.core.schema_check import find_missing_columns

ROOT = Path(__file__).resolve().parents[1]


def test_models_db_has_no_drift():
    assert find_missing_columns(engine, Base.metadata) == {}


def test_checker_reports_missing_column_and_table():
    path = os.path.join(tempfile.mkdtemp(), "drift.db")
    e = create_engine("sqlite:///" + path.replace("\\", "/"))
    with e.begin() as c:
        c.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, name VARCHAR)"))
    missing = find_missing_columns(e, Base.metadata)
    assert "role" in missing["users"]
    assert missing["escrow_transactions"] == ["<table missing>"]


def test_migration_015_covers_review_columns():
    sql = (ROOT / "migrations" / "015_backfill_model_columns.sql").read_text(encoding="utf-8").lower()
    for col in ("role", "otp_attempts", "otp_locked", "otp_expiry", "pickup_otp",
                "rider_name", "rider_phone", "pickup_confirmed"):
        assert f"add column if not exists {col} " in sql, col
