#!/usr/bin/env python3
"""N09: read-only check that every model column exists in the database.

Usage (from the backend root, venv active):
    DATABASE_URL=postgresql://.../dealshield_rehearsal python scripts/check_schema_drift.py
Prints "OK" and exits 0, or lists missing tables/columns and exits 1.
It never writes to the database and never prints the connection string.
Run it BEFORE importing app.main (which would create_all missing tables).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if not os.getenv("DATABASE_URL"):
    print("DATABASE_URL is not set", file=sys.stderr)
    sys.exit(2)

from sqlalchemy import create_engine  # noqa: E402

from app.core.database import Base  # noqa: E402
import app.models.models  # noqa: E402,F401
from app.core.schema_check import find_missing_columns  # noqa: E402

engine = create_engine(os.environ["DATABASE_URL"])
missing = find_missing_columns(engine, Base.metadata)
if not missing:
    print("OK: every model column exists")
    sys.exit(0)
for table, cols in missing.items():
    print(f"MISSING {table}: {', '.join(cols)}")
sys.exit(1)
