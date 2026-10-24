"""Append one line per executed query: its features and what it cost.

The cost model is trained on these lines (plus generated ones, see
benchmarks/generate_training_data.py), so every query Argus runs makes the
next prediction better informed.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

LOG_PATH = Path(
    os.getenv("AETHERQUERY_ML_LOG", Path(__file__).resolve().parents[2] / "datasets" / "ml_runs.jsonl")
)
_lock = threading.Lock()


def outcome(mode: str, payload: dict[str, Any]) -> dict[str, Any]:
    """The measured part of a run: times, error and how the estimate stopped."""
    if mode == "benchmark":
        exact, approx = payload.get("exact") or {}, payload.get("approx") or {}
        return {
            "exact_time": exact.get("time"),
            "approx_time": approx.get("time"),
            "error_percent": payload.get("error_percent"),
            "max_relative_ci": approx.get("max_relative_ci"),
            "sample_rate": approx.get("sample_rate"),
            "stop_reason": approx.get("stop_reason"),
        }
    if mode == "exact":
        return {"exact_time": payload.get("time")}
    return {
        "approx_time": payload.get("time"),
        "max_relative_ci": payload.get("max_relative_ci"),
        "sample_rate": payload.get("sample_rate"),
        "stop_reason": payload.get("stop_reason"),
    }


def write(record: dict[str, Any], path: Path = LOG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, default=str)
    with _lock, path.open("a") as handle:
        handle.write(line + "\n")


def record_in_background(query: str, source: str, accuracy_target: float | None, mode: str, payload: dict[str, Any]) -> None:
    """Extract features and log the run without delaying the response."""

    def work() -> None:
        from backend.ml import features as feature_module

        try:
            extracted = feature_module.extract(query, source, accuracy_target)
            write(
                {
                    "timestamp": time.time(),
                    "origin": "app",
                    "dataset": ",".join(sorted(extracted["tables"])),
                    "query": query,
                    "mode": mode,
                    "approximable": extracted["approximable"],
                    "features": extracted["features"],
                    **outcome(mode, payload),
                }
            )
        except Exception:
            logger.warning("could not log run for the cost model", exc_info=True)

    threading.Thread(target=work, daemon=True).start()


def read(paths: list[Path]) -> list[dict[str, Any]]:
    records = []
    for path in paths:
        if path.exists():
            with path.open() as handle:
                records.extend(json.loads(line) for line in handle if line.strip())
    return records
