import time

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool

from backend.core import suggest as suggester
from backend.core.approx_engine import prepare_approx
from backend.db import samples
from backend.db.connections import PUBLIC_DATABASES, list_connections, resolve_remote
from backend.db.duckdb import list_local_tables, split_relation
from backend.db.samples import SCHEMA as SAMPLE_SCHEMA
from backend.db import mongo
from backend.db.duckdb import describe_relation
from backend.ml import model

router = APIRouter()

PREDICTION_BUDGET_SECONDS = 10.0
# The editor asks again as the user types; profiling a remote table takes
# seconds, so answers are kept for a few minutes.
CACHE_SECONDS = 300.0
_cache: dict[str, tuple[float, dict]] = {}


SHOWN = 7


def _suggestions(table: str) -> dict:
    """Queries that can be run both ways, so exact and estimate can be compared."""
    entry = mongo.lookup(table)
    columns = (
        [{"name": n, "type": entry.types.get(n, "VARCHAR")} for n in entry.paths]
        if entry is not None
        else describe_relation(table)
    )
    comparable: list[dict] = []
    rejected: list[str] = []
    for item in suggester.suggest(table, columns):
        # The same check "Run both and compare" makes; cheap (no execution).
        try:
            prepare_approx(item["sql"])
        except ValueError as exc:
            rejected.append(str(exc))
            continue
        comparable.append(item)
    comparable = comparable[:SHOWN]

    # Attach the cost model's prediction while the time budget lasts; the
    # list itself never waits on more than that.
    start = time.time()
    for item in comparable:
        if time.time() - start > PREDICTION_BUDGET_SECONDS:
            break
        try:
            advice = model.advise(item["sql"])
        except Exception:
            continue
        item["prediction"] = {
            "recommendation": advice["recommendation"],
            "approximable": advice["approximable"],
            "speedup": advice.get("predicted_speedup"),
            "exact_seconds": advice.get("predicted_exact_seconds"),
            "estimate_seconds": advice.get("predicted_estimate_seconds"),
            # Backed by a pilot run, not just table shapes. Without one (a
            # remote table in the query has no stored sample or local copy,
            # so the estimate would read it whole) the prediction is a guess.
            "measured": bool((advice.get("features") or {}).get("pilot_ok")),
        }
    # Measured wins first, fastest first; unmeasured predictions after.
    comparable.sort(
        key=lambda item: (
            not (item.get("prediction") or {}).get("measured"),
            -((item.get("prediction") or {}).get("speedup") or 0),
        )
    )

    note = None
    if not comparable:
        note = (
            f"Comparing on {table} needs a stored sample (it lives in another database): build one in the "
            "table's panel on the left, then suggestions appear here."
            if any("stored" in reason for reason in rejected)
            else f"No query on {table} can be estimated yet: approximation needs COUNT, SUM or AVG over "
            "numeric columns."
        )
    return {"table": table, "suggestions": comparable, "note": note}


def _cached(table: str) -> dict:
    key = table.strip().lower()
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    found = _suggestions(table)
    _cache[key] = (time.time(), found)
    return found


@router.get("/suggest/best")
async def best(limit: int = 8):
    return await run_in_threadpool(_best, limit)


def _best(limit: int) -> dict:
    """Queries, across every source, where the estimate is predicted to beat
    the normal query: connected tables with a stored sample or local copy,
    MongoDB collections, and the largest local tables."""
    candidates: list[str] = []
    disconnected: set[str] = set()
    for stored in samples.list_samples():
        if resolve_remote(stored.source_table) or mongo.lookup(stored.source_table):
            candidates.append(stored.source_table)
        elif "." in stored.source_table and not stored.source_table.startswith(SAMPLE_SCHEMA):
            disconnected.add(split_relation(stored.source_table)[0])
    for connection in list_connections():
        if connection.kind == "mongodb":
            candidates.extend(connection.tables)
    big_local = sorted(
        (t for t in list_local_tables() if t["kind"] == "table" and (t["rows"] or 0) >= 1_000_000),
        key=lambda t: -(t["rows"] or 0),
    )
    candidates.extend(t["name"] for t in big_local[:2])

    wins: list[dict] = []
    for table in dict.fromkeys(candidates):
        try:
            found = _cached(table)
        except Exception:
            continue
        for item in found["suggestions"]:
            prediction = item.get("prediction") or {}
            if prediction.get("recommendation") == "estimate" and prediction.get("speedup") and prediction.get("measured"):
                wins.append({**item, "table": table})
    wins.sort(key=lambda item: -item["prediction"]["speedup"])
    # At most three per table, so one table does not fill the list.
    picked, per_table = [], {}
    for item in wins:
        if per_table.get(item["table"], 0) < 3:
            picked.append(item)
            per_table[item["table"]] = per_table.get(item["table"], 0) + 1
    presets = {db["name"] for db in PUBLIC_DATABASES}
    return {
        "queries": picked[:limit],
        # Stored samples whose database is not connected right now: reconnect
        # it and its queries can be estimated again.
        "reconnect": [{"name": n, "public": n in presets} for n in sorted(disconnected)],
    }


@router.get("/suggest")
async def suggest(table: str):
    try:
        return await run_in_threadpool(_cached, table)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not profile {table}: {exc}") from exc
