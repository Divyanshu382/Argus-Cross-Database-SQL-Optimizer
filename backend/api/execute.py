import time
import hashlib
import uuid
from typing import Any
from collections import deque

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool

from backend.core.cache import query_cache
from backend.core.progress import execution_progress
from backend.ml import model, run_log
from backend.core.router import route_query
from backend.core.sql_syntax import auto_correct_query, suggest_functions
from backend.models.query import ExecuteRequest

router = APIRouter()

# ✅ Query history storage (thread-safe, max 50 entries)
query_history: deque[dict] = deque(maxlen=50)

# ✅ Helper to append to history
def append_history(entry: dict):
    query_history.append(entry)

async def _choose_mode(query: str, req: ExecuteRequest) -> dict[str, Any]:
    """Auto mode: the cost model predicts both times and picks the faster way."""
    start = time.time()
    try:
        advice = await run_in_threadpool(model.advise, query, req.source.lower().strip(), req.accuracy_target)
    except Exception as exc:
        # Without a prediction the exact query is the safe choice.
        advice = {"recommendation": "exact", "method": "rule", "reason": f"Cost model unavailable ({exc})."}
    return {
        "chosen": "approx" if advice.get("recommendation") == "estimate" else "exact",
        "recommendation": advice.get("recommendation"),
        "method": advice.get("method"),
        "reason": advice.get("reason"),
        "predicted_exact_seconds": advice.get("predicted_exact_seconds"),
        "predicted_estimate_seconds": advice.get("predicted_estimate_seconds"),
        "predicted_speedup": advice.get("predicted_speedup"),
        "threshold": advice.get("threshold"),
        "decision_seconds": time.time() - start,
    }


@router.post("/execute")
@router.post("/sql/execute")
async def execute(req: ExecuteRequest) -> dict[str, Any]:
    request_id = req.request_id or uuid.uuid4().hex
    corrected_query = auto_correct_query(req.query)
    auto_choice = None
    if req.mode.lower().strip() == "auto":
        auto_choice = await _choose_mode(corrected_query, req)
        req = req.model_copy(update={"mode": auto_choice["chosen"]})
    function_suggestions = suggest_functions(corrected_query)
    cache_lookup_start = time.time()
    cache_key = hashlib.sha256(
        # The accuracy target changes the approximate answer, so it is part of the key.
        f"{req.source}|{req.mode}|{req.profile}|{req.accuracy_target}|{corrected_query}".encode("utf-8")
    ).hexdigest()

    cached = query_cache.get(cache_key) if req.use_cache else None

    # ✅ Handle cached response
    if cached is not None:
        cache_load_time = time.time() - cache_lookup_start
        cached_response = {
            **cached,
            "original_query": req.query,
            "corrected_query": corrected_query,
            "function_suggestions": function_suggestions,
            "old_time": cached.get("time"),
            "cache_load_time": cache_load_time,
            "time": cache_load_time,
            "cached": True,
        }
        execution_progress.start(
            request_id,
            query=req.query,
            source=req.source,
            mode=req.mode,
        )
        execution_progress.finish(request_id, result=cached_response)

        append_history({
            "query": req.query,
            "corrected_query": corrected_query,
            "mode": req.mode,
            "source": req.source,
            "result": cached_response.get("result"),
            "time": cached_response.get("time"),
            "timestamp": time.time(),
            "cached": True,
            "cache_key": cache_key
        })

        return {**cached_response, "request_id": request_id, "auto_choice": auto_choice}

    # ✅ Execute fresh query
    execution_progress.start(
        request_id,
        query=req.query,
        source=req.source,
        mode=req.mode,
    )

    def publish_progress(update: dict[str, Any]) -> None:
        latest_iteration = update.pop("latest_iteration", None)
        execution_progress.update(request_id, **update)
        if latest_iteration is not None:
            execution_progress.append_iteration(request_id, latest_iteration)

    try:
        payload = await run_in_threadpool(
            route_query,
            corrected_query,
            req.mode,
            req.source,
            req.accuracy_target,
            publish_progress,
            req.profile,
        )
    except Exception as exc:
        execution_progress.fail(request_id, str(exc))
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Every fresh run becomes training data for the cost model.
    mode_key = req.mode.lower().strip()
    if req.source.lower().strip() == "duckdb":
        run_log.record_in_background(
            corrected_query,
            req.source,
            req.accuracy_target,
            "benchmark" if payload.get("benchmark") else ("exact" if mode_key == "exact" else "approx"),
            payload,
        )

    if payload.get("benchmark"):
        benchmark_response = {
            **payload,
            "original_query": req.query,
            "corrected_query": corrected_query,
            "function_suggestions": function_suggestions,
            "cached": False,
        }
        query_cache.set(cache_key, benchmark_response)
        execution_progress.finish(request_id, result=payload.get("approx"))
        append_history({
            "query": req.query,
            "corrected_query": corrected_query,
            "mode": req.mode,
            "source": req.source,
            "result": benchmark_response.get("approx", {}).get("result"),
            "time": benchmark_response.get("approx", {}).get("time"),
            "timestamp": time.time(),
            "cached": False,
            "cache_key": cache_key
        })
        return {**benchmark_response, "request_id": request_id}

    response = {
        "result": payload.get("result"),
        "rows": payload.get("rows", payload.get("result")),
        "columns": payload.get("columns", []),
        "time": payload.get("time", 0.0),
        "approx": payload.get("approx", False),
        "sample_rate": payload.get("sample_rate"),
        "source": payload.get("source", req.source),
        "rewritten_query": payload.get("rewritten_query"),
        "original_query": req.query,
        "corrected_query": corrected_query,
        "function_suggestions": function_suggestions,
        "iterations": payload.get("iterations", []),
        "mode_profile": payload.get("mode_profile"),
        "accuracy_target": payload.get("accuracy_target", req.accuracy_target),
        "convergence_error": payload.get("convergence_error"),
        "convergence_threshold": payload.get("convergence_threshold"),
        "stop_reason": payload.get("stop_reason"),
        "stored_sample": payload.get("stored_sample"),
        "warning": payload.get("warning"),
        "needed_fraction": payload.get("needed_fraction"),
        "ci_rows": payload.get("ci_rows"),
        "max_relative_ci": payload.get("max_relative_ci"),
        "confidence_level": payload.get("confidence_level"),
        "sampled_table": payload.get("sampled_table"),
        "request_id": request_id,
        "cached": False,
    }

    query_cache.set(cache_key, response)
    response = {**response, "auto_choice": auto_choice}
    execution_progress.finish(request_id, result=payload)

    append_history({
        "query": req.query,
        "corrected_query": corrected_query,
        "mode": req.mode,
        "source": req.source,
        "result": response["result"],
        "time": response["time"],
        "timestamp": time.time(),
        "cached": False,
        "cache_key": cache_key
    })

    return response


@router.get("/execute/progress/{request_id}")
@router.get("/sql/execute/progress/{request_id}")
def get_execute_progress(request_id: str) -> dict[str, Any]:
    progress = execution_progress.get(request_id)
    if progress is None:
        # The client starts polling as it sends the query, which can arrive
        # before the query itself is registered.
        return {"status": "pending"}
    return progress

# ✅ API to fetch history (latest first)
@router.get("/history")
def get_history():
    return list(query_history)[::-1]


@router.post("/cache/clear")
def clear_cache(clear_history: bool = True) -> dict[str, Any]:
    history_items_before = len(query_history)
    query_cache.clear()
    if clear_history:
        query_history.clear()

    return {
        "ok": True,
        "cache_cleared": True,
        "history_cleared": clear_history,
        "history_items_before": history_items_before,
        "history_items_after": len(query_history),
    }
