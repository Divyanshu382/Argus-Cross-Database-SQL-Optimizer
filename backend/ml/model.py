"""Learned cost model: predict exact and estimate times before running.

Two gradient-boosted regressors predict log10(seconds) for the exact query
and for the estimate from the features in backend.ml.features. Their ratio is
the predicted speedup; Argus recommends the estimate only when that clears
SPEEDUP_THRESHOLD, since an estimate also carries error.

Without a trained model, a hand-written rule decides (and it is also the
baseline the model is evaluated against, see backend.ml.train).
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from backend.ml import features as feature_module
from backend.ml.features import FEATURES

MODEL_PATH = Path(__file__).resolve().parent / "data" / "cost_model.joblib"
REPORT_PATH = MODEL_PATH.parent / "model_report.json"
SPEEDUP_THRESHOLD = 1.5


def vector(features: dict[str, float], names: list[str] = FEATURES) -> np.ndarray:
    return np.array([[float(features.get(name, 0.0)) for name in names]])


@dataclass
class CostModel:
    exact: Any
    estimate: Any
    trained_on: int
    # Features each regressor reads. The pilot sample informs the estimate's
    # time but says nothing about the exact query's, so they can differ.
    features: list[str]
    estimate_features: list[str] | None = None

    def predict(self, features: dict[str, float]) -> tuple[float, float]:
        exact = self.exact.predict(vector(features, self.features))[0]
        estimate = self.estimate.predict(vector(features, self.estimate_features or self.features))[0]
        return 10 ** float(exact), 10 ** float(estimate)


def rule_recommends_estimate(features: dict[str, float]) -> bool:
    """The hand-written policy: estimate when the data is big or remote and
    a cheap sample (or a full local copy) is at hand, otherwise run exactly."""
    remote_with_sample = features["largest_is_remote"] and (
        features["stored_sample"] or features["n_mongo"] or features["n_local_copies"]
    )
    huge_local = not features["largest_is_remote"] and features["log_rows_largest"] >= 7  # 10M rows
    return bool(remote_with_sample or huge_local)


_loaded: tuple[float, CostModel] | None = None
_lock = threading.Lock()


def load(path: Path | None = None) -> CostModel | None:
    """The trained model, reloaded when the file changes; None if untrained."""
    global _loaded
    path = path or MODEL_PATH
    if not path.exists():
        return None
    mtime = path.stat().st_mtime
    with _lock:
        if _loaded is None or _loaded[0] != mtime:
            import joblib

            _loaded = (mtime, joblib.load(path))
        return _loaded[1]


_report: tuple[float, dict] | None = None


def report() -> dict | None:
    """The evaluation saved by backend.ml.train; None before the first training."""
    global _report
    if not REPORT_PATH.exists():
        return None
    mtime = REPORT_PATH.stat().st_mtime
    with _lock:
        if _report is None or _report[0] != mtime:
            import json

            _report = (mtime, json.loads(REPORT_PATH.read_text()))
        return _report[1]


def advise(query: str, source: str = "duckdb", accuracy_target: float | None = None) -> dict[str, Any]:
    extracted = feature_module.extract(query, source, accuracy_target)
    features = extracted["features"]
    result: dict[str, Any] = {"features": features, "approximable": extracted["approximable"]}
    if not extracted["approximable"]:
        return {
            **result,
            "recommendation": "exact",
            "method": "rule",
            "reason": "This query can't be estimated, so only the exact query applies.",
            "detail": extracted["reason"],
        }

    model = load()
    if model is None:
        estimate = rule_recommends_estimate(features)
        return {
            **result,
            "recommendation": "estimate" if estimate else "exact",
            "method": "rule",
            "reason": (
                "No trained model yet; by rule, the data is large or remote with a sample at hand."
                if estimate
                else "No trained model yet; by rule, the data is small and local, so exact is likely as fast."
            ),
        }

    exact_s, estimate_s = model.predict(features)
    speedup = exact_s / estimate_s if estimate_s > 0 else math.inf
    estimate = speedup >= SPEEDUP_THRESHOLD
    unseen = (report() or {}).get("unseen", {})
    return {
        **result,
        "threshold": SPEEDUP_THRESHOLD,
        # Median factor the predictions were off by on datasets held out of training.
        "typical_factor": {
            "exact": unseen.get("exact_factor"),
            "estimate": unseen.get("estimate_factor"),
            "speedup": unseen.get("speedup_factor"),
        },
        "recommendation": "estimate" if estimate else "exact",
        "method": "model",
        "predicted_exact_seconds": exact_s,
        "predicted_estimate_seconds": estimate_s,
        "predicted_speedup": speedup,
        "reason": (
            f"Predicted {speedup:,.0f}× faster as an estimate." if speedup >= 10 else
            f"Predicted {speedup:.1f}× faster as an estimate." if estimate else
            "Predicted to gain too little from sampling; the exact query is the better choice."
        ),
    }
