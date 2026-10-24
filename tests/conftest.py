import os
import sys
import tempfile
from pathlib import Path

# The DuckDB connection opens at import time, so point it at a throwaway file
# before any backend module is imported.
os.environ["AETHERQUERY_DUCKDB_PATH"] = str(Path(tempfile.mkdtemp()) / "test.duckdb")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from backend.db.duckdb import get_connection  # noqa: E402


@pytest.fixture(scope="session")
def db():
    conn = get_connection()
    # 1M rows: block-sampled. 50k and 20 rows: row-sampled.
    conn.execute(
        "CREATE OR REPLACE TABLE sales AS SELECT i AS id, i % 50000 AS cust, "
        "(i * 7919) % 1000 / 10.0 AS amount, i % 4 AS region FROM range(1000000) t(i)"
    )
    conn.execute(
        "CREATE OR REPLACE TABLE customers AS SELECT i AS cust, i % 20 AS seg, i % 97 AS score "
        "FROM range(50000) t(i)"
    )
    conn.execute("CREATE OR REPLACE TABLE segments AS SELECT i AS seg, i % 3 AS tier FROM range(20) t(i)")
    conn.execute("CREATE OR REPLACE TABLE tiny AS SELECT i FROM range(3000) t(i)")
    conn.execute("CHECKPOINT")
    return conn


@pytest.fixture(scope="session")
def heavy(db):
    # Heavy-tailed values: median ~4, a few in the thousands (like real order quantities).
    db.execute("SELECT setseed(0.7)")
    db.execute(
        "CREATE OR REPLACE TABLE heavy AS SELECT i AS id, (random() < 0.15) AS flagged, "
        "CASE WHEN random() < 0.01 THEN 200 + random() * 1200 ELSE 1 + random() * 7 END AS qty "
        "FROM range(1000000) t(i)"
    )
    db.execute("CHECKPOINT")
    return db
