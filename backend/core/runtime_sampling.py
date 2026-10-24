from __future__ import annotations

import logging
import math
import time
from typing import Any, Callable

from backend.core.executor import (
    fetch_pushdown_aggregate,
    fetch_sample_frame,
    supports_pushdown,
)
from backend.core.estimators import EstimateQuery, fetch_estimate
from backend.core.groupby_engine import aggregate_sample
from backend.core.parser import ParsedQuery
from backend.db import duckdb as duckdb_db
from backend.db import mongo, samples
from backend.db.connections import resolve_remote

logger = logging.getLogger(__name__)


# Each rung is an independent sample of the base table, so the work a mode does
# is the *sum* of its progression, not its final fraction. A ladder summing past
# ~1.0 costs more than the exact query it is meant to replace, so the ladders
# below are kept short enough that every mode stays cheaper than a full scan.
MODE_CONFIGS: dict[str, dict[str, Any]] = {
    "fast": {
        "progression": [0.01, 0.05],
        "convergence_threshold": 0.08,
        "time_budget_seconds": 0.75,
    },
    "balanced": {
        "progression": [0.01, 0.05, 0.15],
        "convergence_threshold": 0.04,
        "time_budget_seconds": 1.5,
    },
    "accurate": {
        "progression": [0.02, 0.08, 0.25],
        "convergence_threshold": 0.02,
        "time_budget_seconds": 3.0,
    },
}

BASE_PROGRESSIONS = [0.01, 0.05, 0.15, 0.25, 0.35]

# Total fraction a run is allowed to sample across all rungs. Past this point a
# full scan is the cheaper way to get the answer, so we stop climbing.
SAMPLING_BUDGET = 0.45


def _trim_to_budget(progression: list[float]) -> list[float]:
    """Drop rungs once the cumulative sample would cost more than a full scan."""
    kept: list[float] = []
    spent = 0.0
    for fraction in progression:
        if kept and spent + fraction > SAMPLING_BUDGET:
            break
        kept.append(fraction)
        spent += fraction
    return kept or progression[:1]


def _derive_accuracy_config(mode: str, accuracy_target: float | None) -> dict[str, Any]:
    mode_key = mode if mode in MODE_CONFIGS else "balanced"
    config = dict(MODE_CONFIGS[mode_key])
    if accuracy_target is None:
        config["progression"] = _trim_to_budget(list(config["progression"]))
        return config

    target = max(50.0, min(99.9, float(accuracy_target)))
    error_budget = max(0.005, min(0.20, 1.0 - (target / 100.0)))
    if target >= 98.0:
        max_fraction = 0.35
    elif target >= 95.0:
        max_fraction = 0.25
    elif target >= 90.0:
        max_fraction = 0.15
    elif target >= 85.0:
        max_fraction = 0.10
    else:
        max_fraction = 0.05

    progression = [fraction for fraction in BASE_PROGRESSIONS if fraction <= max_fraction]
    if not progression or progression[-1] != max_fraction:
        progression.append(max_fraction)
    progression = _trim_to_budget(progression)

    time_budget = max(0.5, min(5.0, 0.6 + ((target - 50.0) / 49.9) * 4.0))
    config["progression"] = progression
    config["convergence_threshold"] = error_budget
    config["time_budget_seconds"] = time_budget
    config["accuracy_target"] = target
    return config


def _safe_relative_error(previous: float | int | None, current: float | int | None) -> float:
    if previous is None or current is None:
        return math.inf
    if previous == 0:
        return 0.0 if current == 0 else math.inf
    return abs(float(current) - float(previous)) / abs(float(previous))


def _max_convergence_delta(previous: Any, current: Any) -> float:
    if previous is None:
        return math.inf

    if isinstance(previous, dict) and isinstance(current, dict):
        keys = set(previous) | set(current)
        if not keys:
            return 0.0

        deltas: list[float] = []
        for key in keys:
            prev_value = previous.get(key)
            curr_value = current.get(key)
            if isinstance(prev_value, dict) and isinstance(curr_value, dict):
                nested_keys = set(prev_value) | set(curr_value)
                if not nested_keys:
                    deltas.append(0.0)
                else:
                    deltas.extend(
                        _safe_relative_error(prev_value.get(nested_key), curr_value.get(nested_key))
                        for nested_key in nested_keys
                    )
            else:
                deltas.append(_safe_relative_error(prev_value, curr_value))
        return max(deltas) if deltas else 0.0

    return _safe_relative_error(previous, current)


def _sample_has_rows(payload: dict[str, Any], rows_sampled: int | None) -> bool:
    if rows_sampled is not None:
        return rows_sampled > 0
    result_map = payload["result_map"]
    if not result_map:
        return False
    if any(isinstance(value, dict) for value in result_map.values()):
        return True  # grouped: a group only appears if the sample had its rows
    # Ungrouped aggregates over an empty sample are COUNT = 0 and SUM/AVG = NULL.
    return any(value not in (None, 0, 0.0) for value in result_map.values())


def required_stored_sample(table: str, source: str) -> samples.StoredSample | None:
    """The stored sample to estimate ``table`` from, if any.

    Tables in connected databases are estimated from a sample stored locally
    (see backend.db.samples), never sampled over the network, so one without a
    stored sample cannot be estimated.
    """
    if source != "duckdb":
        return None
    stored = samples.lookup(table)
    if stored is None and resolve_remote(table) is not None:
        raise ValueError(
            f"Approximate queries on {table} are answered from a sample stored on this "
            "computer, and none has been built yet. Build one in step 1 (a one-time download); "
            "the exact query still runs on the source database."
        )
    return stored


# Adaptive sizing (see run_ladder): never sample more than this in one rung.
# Past about half the data an exact query costs about the same.
MAX_RUNG_FRACTION = 0.5
MAX_RUNGS = 4


def fraction_for_width(fraction: float, width: float, target: float) -> float | None:
    """Sample fraction expected to shrink a relative interval width ``width``
    (observed at ``fraction``) to ``target``.

    Under Poisson sampling the variance scales with (1 - f) / f, so the width
    scales with its square root.
    """
    if width <= 0 or target <= 0 or not 0 < fraction < 1:
        return None
    odds = (target / width) ** 2 * (1.0 - fraction) / fraction
    return 1.0 / (1.0 + odds)


# A rung samples ``fraction`` of the data and returns
# (payload, query_time, sql, rows_sampled, effective_fraction).
Rung = Callable[[float], tuple[dict[str, Any], float, str, "int | None", float]]


def sampled_table_source(table: str, columns: str, fraction: float, alias: str, unit_column: str) -> tuple[str, str | None]:
    """A FROM item sampling ``fraction`` of a local table, and its unit column.

    Big tables use block sampling (whole 2048-row chunks, so the unit is the
    chunk); small tables and views use row sampling (the unit is the row).
    """
    percent = fraction * 100.0
    if duckdb_db.sample_method(table) == "system":
        return (
            f"(SELECT {columns}, rowid // 2048 AS {unit_column} FROM {table} "
            f"USING SAMPLE {percent:.6f}% (system)) AS {alias}",
            f"{alias}.{unit_column}",
        )
    return f"(SELECT {columns} FROM {table} USING SAMPLE {percent:.6f}% (bernoulli)) AS {alias}", None


def _mongo_sample_source(entry: mongo.MongoCollection, parsed: ParsedQuery, sample_fraction: float) -> tuple[str, float]:
    """Fetch a random $sample of a live MongoDB collection into a temp table.

    Unlike MySQL's RAND() or a Postgres TABLESAMPLE on a busy server, MongoDB
    serves a $sample under 5% of a collection with a random cursor rather
    than a scan, so sampling at the source does save work here.
    """
    total = entry.estimated_count()
    if total == 0:
        raise ValueError(f"{entry.relation} is empty.")
    size = max(1, min(total, round(sample_fraction * total)))
    columns = [c for c in (entry.column(n) for n in parsed.projection_columns) if c] or [next(iter(entry.paths))]
    frame = mongo.fetch(entry, columns, sample_size=size if size < total else None)
    target = mongo.load(entry, frame, columns, target=f"__aq_mongo_{entry.connection}", temporary=True)
    return f"{target} AS aqp_sample", size / total


def single_table_rung(parsed: ParsedQuery, stored: samples.StoredSample | None) -> Rung:
    live_mongo = mongo.lookup(parsed.table) if stored is None else None

    def rung(sample_fraction: float):
        unit = None
        if live_mongo is not None:
            from_sql, fraction = _mongo_sample_source(live_mongo, parsed, sample_fraction)
        elif stored is not None:
            # The stored sample already is the sample: use all of it and
            # scale by the fraction of the original rows it holds.
            from_sql, fraction = f"{stored.sample_table} AS aqp_sample", stored.fraction
        elif sample_fraction >= 1.0:
            from_sql, fraction = f"{parsed.table} AS aqp_sample", 1.0
        else:
            columns = ", ".join(parsed.projection_columns) if parsed.projection_columns else "*"
            from_sql, unit = sampled_table_source(parsed.table, columns, sample_fraction, "aqp_sample", "__aq_unit")
            fraction = sample_fraction
        query = EstimateQuery(
            from_sql=from_sql,
            group_by=parsed.group_by,
            aggregates=parsed.aggregates,
            where=parsed.where_clause,
            order_by=parsed.order_by,
            limit=parsed.limit,
            unit=unit,
        )
        payload, query_time, sql = fetch_estimate(query, fraction)
        return payload, query_time, sql, None, fraction

    return rung


def _legacy_rung(parsed: ParsedQuery, source: str) -> Rung:
    """Postgres/MySQL configured on the server: sample inside that database."""
    use_pushdown = supports_pushdown(parsed, source)

    def rung(sample_fraction: float):
        nonlocal use_pushdown
        if use_pushdown:
            try:
                payload, query_time, sql = fetch_pushdown_aggregate(parsed, source, sample_fraction)
                return payload, query_time, sql, None, sample_fraction
            except Exception:
                # Anything the engine cannot express falls back to the
                # row-shipping path rather than failing the query.
                logger.warning("pushdown failed, falling back to sampling frame", exc_info=True)
                use_pushdown = False
        frame, query_time, sql = fetch_sample_frame(parsed, source, sample_fraction)
        payload = aggregate_sample(frame, parsed, sample_fraction)
        return payload, query_time, sql, int(len(frame)), sample_fraction

    return rung


def run_runtime_sampling(
    parsed: ParsedQuery,
    source: str,
    mode: str,
    accuracy_target: float | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    stored = required_stored_sample(parsed.table, source)
    rung = single_table_rung(parsed, stored) if source == "duckdb" else _legacy_rung(parsed, source)
    return run_ladder(
        rung,
        source=source,
        mode=mode,
        accuracy_target=accuracy_target,
        progress_callback=progress_callback,
        stored=stored,
    )


def run_ladder(
    rung: Rung,
    *,
    source: str,
    mode: str,
    accuracy_target: float | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    stored: samples.StoredSample | None = None,
    single_pass: str | None = None,
) -> dict[str, Any]:
    """Sample ever larger fractions until the estimate is good enough.

    ``single_pass`` names a reason to run the rung once and stop (for
    example, everything is in local copies, so there is nothing to sample).

    With confidence intervals (DuckDB), "good enough" means every estimate's
    95% interval is within the threshold, relative to the estimate, and after
    each rung the next one is sized to reach it (fraction_for_width) instead
    of following the fixed progression. If that would take more than
    MAX_RUNG_FRACTION of the data, it stops: an exact query is about as cheap.
    Without intervals it falls back to two consecutive rungs agreeing.
    """
    mode_key = mode if mode in MODE_CONFIGS else "balanced"
    config = _derive_accuracy_config(mode_key, accuracy_target)
    one_pass = stored is not None or single_pass is not None
    progression = [1.0] if one_pass else config["progression"]
    threshold = config["convergence_threshold"]

    start = time.time()
    previous_map: Any = None
    final_payload: dict[str, Any] | None = None
    iteration_details: list[dict[str, Any]] = []
    stop_reason = "progression_exhausted"
    final_error: float | None = None
    produced_rows = False
    needed_fraction: float | None = None

    fractions = list(progression)
    position = 0
    while position < len(fractions) and len(iteration_details) < MAX_RUNGS:
        sample_fraction = fractions[position]
        position += 1
        if progress_callback is not None:
            progress_callback(
                {
                    "phase": "sampling",
                    "message": (
                        f"Reading the stored {stored.fraction * 100:g}% sample"
                        if stored is not None
                        else f"Sampling {sample_fraction * 100:.0f}% of rows"
                    ),
                    "current_sample_fraction": sample_fraction,
                    "accuracy_target": config.get("accuracy_target"),
                }
            )
        aggregate_payload, query_time, sample_query, rows_sampled, fraction = rung(sample_fraction)

        relative_ci = aggregate_payload.get("max_relative_ci")
        if relative_ci is not None:
            convergence_error = relative_ci
        else:
            convergence_error = _max_convergence_delta(previous_map, aggregate_payload["result_map"])
        elapsed = time.time() - start

        iteration_detail = {
            "sample_fraction": fraction,
            "rows_sampled": rows_sampled,
            "query_time": query_time,
            "elapsed_time": elapsed,
            "convergence_error": None if math.isinf(convergence_error) else convergence_error,
            "relative_ci": None if relative_ci is None or math.isinf(relative_ci) else relative_ci,
            "sample_query": sample_query,
        }
        iteration_details.append(iteration_detail)
        if progress_callback is not None:
            progress_callback(
                {
                    "phase": "sampling",
                    "message": f"Processed sample {fraction * 100:g}%",
                    "current_sample_fraction": fraction,
                    "accuracy_target": config.get("accuracy_target"),
                    "latest_iteration": iteration_detail,
                }
            )

        final_payload = aggregate_payload
        previous_map = aggregate_payload["result_map"]
        final_error = convergence_error

        # An empty rung tells us nothing, so never let it trigger convergence.
        produced_rows = _sample_has_rows(aggregate_payload, rows_sampled)
        # Two-phase sampling: the first rung is a pilot that sizes the next
        # one and is never reported. Accepting it whenever its own interval
        # looked narrow would favour samples that missed the outliers, which
        # are exactly the ones whose estimate is off.
        pilot = relative_ci is not None and not one_pass and len(iteration_details) == 1
        skew_fraction = aggregate_payload.get("skew_fraction") or 0.0
        within = produced_rows and not math.isinf(convergence_error) and convergence_error < threshold
        if within and not pilot and fraction >= skew_fraction * 0.95:
            stop_reason = "ci_reached" if relative_ci is not None else "converged"
            break
        if elapsed >= config["time_budget_seconds"]:
            stop_reason = "time_budget_exceeded"
            break
        if relative_ci is not None and produced_rows and stored is None:
            needed = fraction_for_width(fraction, relative_ci, threshold) if not math.isinf(relative_ci) else None
            if skew_fraction:
                needed = max(needed or 0.0, skew_fraction)
            if needed is not None:
                if needed > MAX_RUNG_FRACTION:
                    # Out of reach without nearly an exact scan. Return the
                    # best estimate affordable rather than this rung's.
                    needed_fraction = needed
                    if fraction >= MAX_RUNG_FRACTION * 0.99:
                        break
                    next_fraction = MAX_RUNG_FRACTION
                else:
                    # A little over the estimate, since the width itself is
                    # noisy, and always larger than the pilot.
                    next_fraction = min(MAX_RUNG_FRACTION, max(needed * 1.15, fraction * 1.5))
                fractions = fractions[:position] + [next_fraction]

    if final_payload is None:
        raise RuntimeError("Runtime sampling failed to produce a result")
    if single_pass is not None:
        stop_reason = single_pass
    elif stored is not None:
        stop_reason = "stored_sample"
    elif needed_fraction is not None and stop_reason != "ci_reached":
        stop_reason = "needs_exact"
    if not produced_rows:
        # Scaling up an empty sample would report 0 as if it were an estimate.
        raise ValueError(
            "The sample contained no matching rows, so there is no estimate to give. "
            "The table may be too small or the WHERE clause too selective; run it exactly."
        )

    total_time = time.time() - start
    if progress_callback is not None:
        progress_callback(
            {
                "phase": "finalizing",
                "message": "Finalizing approximate result",
                "current_sample_fraction": iteration_details[-1]["sample_fraction"],
                "accuracy_target": config.get("accuracy_target"),
            }
        )
    relative_ci = final_payload.get("max_relative_ci")
    warning = None
    skew_needed = final_payload.get("skew_fraction") or 0.0
    if stored is not None and skew_needed > stored.fraction * 1.05:
        # A stored sample cannot grow, so say so rather than present a
        # confident-looking interval the data does not support.
        warning = (
            f"These values are heavily skewed: a reliable interval needs about {min(skew_needed, 1.0):.0%} of the "
            f"rows, and the stored sample holds {stored.fraction:.0%}. Treat this estimate as rough; rebuild a "
            "larger sample or run the normal query."
        )
    return {
        **final_payload,
        "time": total_time,
        "approx": True,
        "source": source,
        "mode_profile": mode_key,
        "accuracy_target": config.get("accuracy_target"),
        "sample_rate": iteration_details[-1]["sample_fraction"],
        "iterations": iteration_details,
        "convergence_error": None if final_error is None or math.isinf(final_error) else final_error,
        "convergence_threshold": threshold,
        "max_relative_ci": None if relative_ci is None or math.isinf(relative_ci) else relative_ci,
        "confidence_level": 0.95 if relative_ci is not None else None,
        "stop_reason": stop_reason,
        "warning": warning,
        "needed_fraction": needed_fraction,
        "stored_sample": stored.public() if stored is not None else None,
        "rewritten_query": iteration_details[-1]["sample_query"],
    }
