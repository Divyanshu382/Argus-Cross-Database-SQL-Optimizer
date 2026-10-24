import logging
import time
from typing import Any

from backend.core import mongo_query
from backend.core.federation import push_whole_query
from backend.db import duckdb as duckdb_db
from backend.db import mysql as mysql_db
from backend.db import postgres as postgres_db

logger = logging.getLogger(__name__)


def run_exact(query: str, source: str) -> dict[str, Any]:
    source_key = source.lower().strip()
    start = time.time()
    rewritten: str | None = None

    if source_key == "duckdb":
        # Queries whose tables all live in one connected database run there,
        # so only the result crosses the network.
        rewritten = push_whole_query(query)
        payload = None
        try:
            # A query over one MongoDB collection runs as an aggregation pipeline.
            mongo_payload = mongo_query.run(query)
        except Exception as exc:
            logger.warning("MongoDB pushdown failed, running in DuckDB: %s", str(exc).splitlines()[0])
            mongo_payload = None
        if mongo_payload is not None:
            payload, rewritten = mongo_payload, mongo_payload["pipeline"]
        elif rewritten is not None:
            try:
                payload = duckdb_db.execute_query(rewritten)
            except Exception as exc:
                # Usually DuckDB-only syntax the source does not understand;
                # pulling the rows into DuckDB still gives the right answer.
                logger.warning("source pushdown failed, running in DuckDB: %s", str(exc).splitlines()[0])
                rewritten = None
                start = time.time()
        if payload is None:
            payload = duckdb_db.execute_query(query)
    elif source_key == "postgres":
        payload = postgres_db.execute_query(query)
    elif source_key == "mysql":
        payload = mysql_db.execute_query(query)
    else:
        raise ValueError(f"Unsupported source: {source}")

    total = time.time() - start
    logger.info("run_exact source=%s duration=%.6f", source_key, total)

    return {
        "result": payload.get("rows", []),
        "columns": payload.get("columns", []),
        "time": payload.get("time", total),
        "approx": False,
        "source": source_key,
        "rewritten_query": rewritten,
    }
