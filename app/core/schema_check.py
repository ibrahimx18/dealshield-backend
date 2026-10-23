"""N09: compare the live database schema with the SQLAlchemy models (read-only)."""
from sqlalchemy import inspect


def find_missing_columns(engine, metadata) -> dict[str, list[str]]:
    """Return {table: [missing columns]}; a missing table maps to ['<table missing>']."""
    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())
    missing: dict[str, list[str]] = {}
    for table in metadata.sorted_tables:
        if table.name not in existing_tables:
            missing[table.name] = ["<table missing>"]
            continue
        have = {c["name"] for c in insp.get_columns(table.name)}
        gaps = [c.name for c in table.columns if c.name not in have]
        if gaps:
            missing[table.name] = gaps
    return missing
