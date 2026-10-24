from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable

from backend.core.approx_engine import prepare_approx
from backend.core.exact_engine import run_exact


_ISO_DATETIME = re.compile(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})")


def canonical_key(key: Any) -> Any:
    """One spelling per group value, so rows from different engines match:
    MySQL returns '2013-02-01 00:00:00' where DuckDB returns a timestamp,
    and numbers may arrive as text."""
    if isinstance(key, tuple):
        return tuple(canonical_key(part) for part in key)
    if isinstance(key, (datetime, date)):
        key = key.isoformat(sep=" ") if isinstance(key, datetime) else key.isoformat()
    if isinstance(key, str):
        match = _ISO_DATETIME.match(key)
        if match:
            return f"{match.group(1)} {match.group(2)}"
        try:
            return float(key) if key.strip() and key.strip().lstrip("-").replace(".", "", 1).isdigit() else key
        except ValueError:
            return key
    if isinstance(key, (int, float, Decimal)) and not isinstance(key, bool):
        return float(key)
    return key


def canonical_map(result_map: Any) -> Any:
    if isinstance(result_map, dict) and result_map and all(isinstance(v, dict) for v in result_map.values()):
        return {canonical_key(k): v for k, v in result_map.items()}
    return result_map


def _normalize_exact_result(group_count: int, aliases: list[str], payload: dict[str, Any]) -> Any:
    """Key exact rows the way estimates are keyed in ``result_map``."""
    rows = payload.get("result", [])
    if not group_count:
        if not rows:
            return {}
        return dict(zip(aliases, rows[0]))

    normalized: dict[Any, dict[str, Any]] = {}
    for row in rows:
        key = row[0] if group_count == 1 else tuple(row[:group_count])
        normalized[key] = dict(zip(aliases, row[group_count:]))
    return normalized


def _ci_coverage(exact: Any, approx: Any, ci: Any) -> float | None:
    """Share of exact values inside their estimate's 95% interval."""
    hits: list[bool] = []

    def check(exact_values: dict[str, Any], approx_values: dict[str, Any], widths: dict[str, Any]) -> None:
        for alias, width in widths.items():
            truth, estimate = exact_values.get(alias), approx_values.get(alias)
            if width is None or truth is None or estimate is None:
                continue
            # Allow for rounding: an exact answer from a local copy has a
            # zero-width interval but may differ in the last digits (MySQL
            # decimal, which MySQL rounds AVG to four places, against DuckDB double).
            tolerance = 1e-4 * max(1.0, abs(float(truth)))
            hits.append(abs(float(truth) - float(estimate)) <= float(width) + tolerance)

    if not isinstance(ci, dict) or not ci:
        return None
    if all(isinstance(value, dict) for value in ci.values()):
        for key, widths in ci.items():
            if key in exact and key in approx:
                check(exact[key], approx[key], widths)
    else:
        check(exact, approx, ci)
    return sum(hits) / len(hits) if hits else None


def _mean_relative_error(exact: Any, approx: Any) -> float | None:
    values: list[float] = []

    if isinstance(exact, dict) and isinstance(approx, dict):
        keys = set(exact) & set(approx)
        if not keys:
            return None
        for key in keys:
            exact_value = exact[key]
            approx_value = approx[key]
            if isinstance(exact_value, dict) and isinstance(approx_value, dict):
                nested_keys = set(exact_value) & set(approx_value)
                for nested_key in nested_keys:
                    reference = exact_value[nested_key]
                    candidate = approx_value[nested_key]
                    if candidate is None:
                        continue
                    if reference in (None, 0):
                        if candidate == reference:
                            values.append(0.0)
                        continue
                    values.append(abs(float(candidate) - float(reference)) / abs(float(reference)))
            else:
                if approx_value is None:
                    continue
                if exact_value in (None, 0):
                    if approx_value == exact_value:
                        values.append(0.0)
                    continue
                values.append(abs(float(approx_value) - float(exact_value)) / abs(float(exact_value)))

    if not values:
        return None
    return sum(values) / len(values)


def run_benchmark(
    query: str,
    source: str,
    approx_mode: str = "balanced",
    accuracy_target: float | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    # Fail before the exact run, which can take a minute on a remote table.
    prepared = prepare_approx(query, source)
    if progress_callback is not None:
        progress_callback({"phase": "exact", "message": "Running exact query"})
    exact_payload = run_exact(query, source)
    if progress_callback is not None:
        progress_callback({"phase": "approx", "message": "Running approximate query"})
    approx_payload = prepared.run(approx_mode, accuracy_target, progress_callback)

    exact_result_map = canonical_map(_normalize_exact_result(prepared.group_count, prepared.aliases, exact_payload))
    approx_result_map = canonical_map(approx_payload["result_map"])
    error_ratio = _mean_relative_error(exact_result_map, approx_result_map)
    coverage = _ci_coverage(exact_result_map, approx_result_map, canonical_map(approx_payload.get("ci_map")))
    exact_time = float(exact_payload.get("time", 0.0))
    approx_time = float(approx_payload.get("time", 0.0))

    return {
        "benchmark": True,
        "source": source,
        "approx_mode": approx_mode,
        "accuracy_target": accuracy_target,
        "exact": {
            "result": exact_payload.get("result"),
            "columns": exact_payload.get("columns", []),
            "time": exact_time,
            "rewritten_query": exact_payload.get("rewritten_query"),
        },
        "approx": {
            "result": approx_payload.get("result"),
            "rows": approx_payload.get("rows"),
            "columns": approx_payload.get("columns", []),
            "time": approx_time,
            "sample_rate": approx_payload.get("sample_rate"),
            "mode_profile": approx_payload.get("mode_profile"),
            "accuracy_target": approx_payload.get("accuracy_target"),
            "iterations": approx_payload.get("iterations", []),
            "stop_reason": approx_payload.get("stop_reason"),
            "stored_sample": approx_payload.get("stored_sample"),
            "warning": approx_payload.get("warning"),
            "needed_fraction": approx_payload.get("needed_fraction"),
            "rewritten_query": approx_payload.get("rewritten_query"),
            "ci_rows": approx_payload.get("ci_rows"),
            "max_relative_ci": approx_payload.get("max_relative_ci"),
            "confidence_level": approx_payload.get("confidence_level"),
            "sampled_table": approx_payload.get("sampled_table"),
        },
        "ci_coverage": coverage,
        "speedup": (exact_time / approx_time) if approx_time > 0 else None,
        "error_percent": (error_ratio * 100.0) if error_ratio is not None else None,
    }
