from typing import Any

from fastapi import APIRouter, HTTPException

from backend.core.federation import references_connected_database
from backend.core.plan_parser import parse_plan
from backend.db import duckdb as duckdb_db
from backend.db import mysql as mysql_db
from backend.db import postgres as postgres_db
from backend.models.query import PlanRequest

router = APIRouter()


def _fetch_plan(query: str, source: str) -> Any:
    source_key = source.lower().strip()
    if source_key == "duckdb":
        # EXPLAIN ANALYZE runs the query; on a connected database that can
        # take minutes, so only estimate those plans.
        if references_connected_database(query):
            return duckdb_db.explain_query(query, analyze=False)
        try:
            return duckdb_db.explain_query(query, analyze=True)
        except Exception:
            return duckdb_db.explain_query(query, analyze=False)
    if source_key == "postgres":
        try:
            return postgres_db.explain_query(query, analyze=True)
        except Exception:
            return postgres_db.explain_query(query, analyze=False)
    if source_key == "mysql":
        try:
            return mysql_db.explain_query(query, analyze=True)
        except Exception:
            return mysql_db.explain_query(query, analyze=False)
    raise ValueError(f"Unsupported source: {source}")


@router.post("/plan")
@router.post("/sql/parse-plan")
async def get_plan(req: PlanRequest) -> dict[str, Any]:
    try:
        raw_plan = _fetch_plan(req.query, req.source)
        parsed = parse_plan(raw_plan)
        return {
            "success": True,
            "source": req.source,
            "raw_plan": raw_plan,
            "parsed_plan": parsed,
            "plan_tree": parsed.get("plan_tree"),
            "explanation": parsed.get("explanation"),
        }
    except Exception as exc:
        msg = str(exc)
        source_key = req.source.lower().strip()
        if source_key != "duckdb" and "table_" in req.query.lower():
            msg = (
                "The query looks like a CSV-loaded DuckDB table query (table_*). "
                "Use source=duckdb for this plan/execute request. "
                f"Original error: {msg}"
            )
        raise HTTPException(status_code=400, detail=msg) from exc
