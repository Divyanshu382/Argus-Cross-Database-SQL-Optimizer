"""Stored random samples of tables in connected databases.

Sampling on a remote server rarely saves time: MySQL has no sampling, so
RAND() < f still reads every row, and Postgres TABLESAMPLE SYSTEM does random
page reads that can be slower than a full scan on a busy server. Instead a
sample is pulled once and kept in the local DuckDB file, so approximate queries
are answered locally, without contacting the source (the idea behind BlinkDB).

Samples persist across restarts. They are snapshots: rebuild one to pick up
changes made on the source since it was built. A "sample" of fraction 1 is a
full local copy, meant for small tables that big ones are joined with.
"""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from backend.db import mongo
from backend.db.connections import resolve_remote, split_relation
from backend.db.duckdb import get_connection

SCHEMA = "aq_samples"


@dataclass
class StoredSample:
    source_table: str
    sample_table: str
    fraction: float
    sample_rows: int
    built_at: datetime
    build_seconds: float

    def public(self) -> dict[str, Any]:
        data = asdict(self)
        data["built_at"] = self.built_at.isoformat()
        return data


def _key(relation: str) -> str:
    """One spelling per table, so rfam.Family and rfam."family" match."""
    return ".".join(part.lower() for part in split_relation(relation))


def _ensure_catalog() -> None:
    conn = get_connection()
    conn.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA}.catalog (
            source_table VARCHAR PRIMARY KEY,
            sample_table VARCHAR NOT NULL,
            fraction DOUBLE NOT NULL,
            sample_rows BIGINT NOT NULL,
            built_at TIMESTAMP NOT NULL,
            build_seconds DOUBLE NOT NULL
        )
        """
    )


def build(relation: str, fraction: float) -> StoredSample:
    if not 0 < fraction <= 1.0:
        raise ValueError("The sample fraction must be above 0 and at most 1 (a full local copy).")
    key = _key(relation)
    sample_table = f'{SCHEMA}."{re.sub(r"[^a-z0-9_]", "_", key.replace(".", "__"))}"'
    entry = mongo.lookup(relation)
    if entry is not None:
        return _build_from_mongo(entry, key, sample_table, fraction)
    remote = resolve_remote(relation)
    if remote is None:
        raise ValueError(
            f"{relation} is not a table in a connected database. Local tables are sampled on "
            "the fly and need no stored sample."
        )

    if fraction >= 1.0:
        # A full local copy: worth it for small tables that queries join
        # against, which otherwise cross the network on every query.
        pull = f"SELECT * FROM {remote.source_name}"
    elif remote.kind == "postgres":
        # BERNOULLI picks rows independently; SYSTEM picks whole pages, which
        # is biased when similar rows are stored together.
        pull = f"SELECT * FROM {remote.source_name} TABLESAMPLE BERNOULLI ({fraction * 100!r})"
    else:
        pull = f"SELECT * FROM {remote.source_name} WHERE RAND() < {fraction!r}"

    _ensure_catalog()
    # A cursor is a separate connection to the same database, so a long pull
    # does not hold up queries running on the shared connection.
    cursor = get_connection().cursor()
    try:
        start = time.time()
        cursor.execute(f"CREATE OR REPLACE TABLE {sample_table} AS {remote.query_sql(pull)}")
        build_seconds = time.time() - start
        sample_rows = cursor.execute(f"SELECT COUNT(*) FROM {sample_table}").fetchone()[0]
        _record(cursor, key, sample_table, fraction, sample_rows, build_seconds)
    finally:
        cursor.close()
    sample = lookup(relation)
    assert sample is not None
    return sample


def _record(cursor: Any, key: str, sample_table: str, fraction: float, rows: int, seconds: float) -> None:
    cursor.execute(f"DELETE FROM {SCHEMA}.catalog WHERE source_table = ?", [key])
    cursor.execute(
        f"INSERT INTO {SCHEMA}.catalog VALUES (?, ?, ?, ?, now()::TIMESTAMP, ?)",
        [key, sample_table, fraction, rows, seconds],
    )


def _build_from_mongo(entry: mongo.MongoCollection, key: str, sample_table: str, fraction: float) -> StoredSample:
    """A $sample of the collection (or all of it), with every field."""
    _ensure_catalog()
    start = time.time()
    total = entry.estimated_count()
    size = max(1, min(total, round(fraction * total)))
    frame = mongo.fetch(entry, None, sample_size=size if size < total else None)
    mongo.load(entry, frame, list(entry.paths), target=sample_table)
    # The fraction actually held, so estimates scale by the right amount.
    held = 1.0 if size >= total else size / total
    _record(get_connection(), key, sample_table, held, len(frame), time.time() - start)
    sample = lookup(key)
    assert sample is not None
    return sample


def lookup(relation: str) -> StoredSample | None:
    _ensure_catalog()
    row = get_connection().execute(
        f"SELECT * FROM {SCHEMA}.catalog WHERE source_table = ?", [_key(relation)]
    ).fetchone()
    return StoredSample(*row) if row else None


def list_samples() -> list[StoredSample]:
    _ensure_catalog()
    rows = get_connection().execute(f"SELECT * FROM {SCHEMA}.catalog ORDER BY source_table").fetchall()
    return [StoredSample(*row) for row in rows]


def delete(relation: str) -> None:
    sample = lookup(relation)
    if sample is None:
        raise KeyError(relation)
    conn = get_connection()
    conn.execute(f"DROP TABLE IF EXISTS {sample.sample_table}")
    conn.execute(f"DELETE FROM {SCHEMA}.catalog WHERE source_table = ?", [sample.source_table])
