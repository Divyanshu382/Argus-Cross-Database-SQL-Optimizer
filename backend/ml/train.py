"""Train the cost model, evaluate it honestly, and save it.

    .venv/bin/python -m backend.ml.train

Evaluation holds out one whole dataset at a time (leave-one-dataset-out), so
every prediction is for tables the model never saw, as it will be for a new
connection. It is compared with the hand-written rule and with "always
exact" / "always estimate", on how often each picks the faster option and how
much total time its choices cost. Results go to benchmarks/ML_RESULTS.md.
"""

from __future__ import annotations

import json
import math
import statistics
import sys
import time
from pathlib import Path

import numpy as np
from sklearn.ensemble import GradientBoostingRegressor
from sklearn.model_selection import KFold, LeaveOneGroupOut

from backend.ml import run_log
from backend.ml.features import FEATURES, SHAPE_FEATURES
from backend.ml.model import MODEL_PATH, SPEEDUP_THRESHOLD, CostModel, rule_recommends_estimate

ROOT = Path(__file__).resolve().parents[2]
SOURCES = [Path(__file__).resolve().parent / "data" / "generated_runs.jsonl", run_log.LOG_PATH]
REPORT = ROOT / "benchmarks" / "ML_RESULTS.md"
# The same evaluation as JSON, for the app's model page.
SUMMARY = MODEL_PATH.parent / "model_report.json"
FLOOR = 1e-5  # seconds; times below this are timer noise


def regressor() -> GradientBoostingRegressor:
    return GradientBoostingRegressor(
        n_estimators=400, max_depth=3, learning_rate=0.05, subsample=0.8, random_state=0
    )


def matrix(records: list[dict], names: list[str] = FEATURES) -> np.ndarray:
    return np.array([[float(r["features"].get(name, 0.0)) for name in names] for r in records])


def aggregate(records: list[dict]) -> list[dict]:
    """One record per (dataset, query, target, mode): the median of repeated
    runs. Sub-millisecond timings are noisy, and repeats are not independent
    examples."""
    groups: dict[tuple, list[dict]] = {}
    for r in records:
        key = (r["dataset"], r["query"], r["features"].get("accuracy_target"), r.get("mode"))
        groups.setdefault(key, []).append(r)
    merged = []
    for runs in groups.values():
        record = dict(runs[0])
        record["features"] = {
            name: statistics.median(float(run["features"].get(name, 0.0)) for run in runs)
            for name in runs[0]["features"]
        }
        for field in ("exact_time", "approx_time", "error_percent"):
            values = [run[field] for run in runs if run.get(field) is not None]
            record[field] = statistics.median(values) if values else None
        merged.append(record)
    return merged


def log_time(seconds: float) -> float:
    return math.log10(max(seconds, FLOOR))


def fit(records: list[dict], names: tuple[list[str], list[str]] = (FEATURES, FEATURES)) -> CostModel:
    """``names`` is (exact features, estimate features)."""
    exact_names, estimate_names = names
    exact = [r for r in records if r.get("exact_time") is not None]
    estimate = [r for r in records if r.get("approx_time") is not None]
    exact_model = regressor().fit(matrix(exact, exact_names), [log_time(r["exact_time"]) for r in exact])
    estimate_model = regressor().fit(matrix(estimate, estimate_names), [log_time(r["approx_time"]) for r in estimate])
    return CostModel(exact_model, estimate_model, len(records), list(exact_names), list(estimate_names))


def cross_validate(records: list[dict], paired: list[dict], splitter, groups=None, names=(FEATURES, FEATURES)) -> list[dict]:
    """Held-out predictions for every paired record."""
    predictions: list[dict] = []
    index = {id(r): i for i, r in enumerate(paired)}
    for train_idx, test_idx in splitter.split(paired, groups=groups):
        held_out = {id(paired[i]) for i in test_idx}
        held_datasets = {paired[i]["dataset"] for i in test_idx} if groups is not None else set()
        train = [
            r for r in records
            if id(r) not in held_out and (groups is None or r["dataset"] not in held_datasets)
        ]
        model = fit(train, names)
        for i in test_idx:
            exact_s, estimate_s = model.predict(paired[i]["features"])
            predictions.append({"record": paired[index[id(paired[i])]], "exact": exact_s, "estimate": estimate_s})
    return predictions


def evaluate(predictions: list[dict]) -> dict:
    exact_err = [abs(log_time(p["exact"]) - log_time(p["record"]["exact_time"])) for p in predictions]
    estimate_err = [abs(log_time(p["estimate"]) - log_time(p["record"]["approx_time"])) for p in predictions]
    speedup_err = [
        abs(math.log10(p["exact"] / p["estimate"]) - (log_time(p["record"]["exact_time"]) - log_time(p["record"]["approx_time"])))
        for p in predictions
    ]

    def choose(policy: str, p: dict) -> bool:
        r = p["record"]
        actual = max(r["exact_time"], FLOOR) / max(r["approx_time"], FLOOR)
        return {
            "always exact": False,
            "always estimate": True,
            "rule": rule_recommends_estimate(r["features"]),
            "model": p["exact"] / p["estimate"] >= SPEEDUP_THRESHOLD,
            "oracle": actual >= SPEEDUP_THRESHOLD,
        }[policy]

    policies = {}
    oracle = [choose("oracle", p) for p in predictions]
    for policy in ("always exact", "always estimate", "rule", "model", "oracle"):
        picks = [choose(policy, p) for p in predictions]
        chosen = [max(p["record"]["approx_time"] if pick else p["record"]["exact_time"], FLOOR) for p, pick in zip(predictions, picks)]
        best = [max(p["record"]["approx_time"] if o else p["record"]["exact_time"], FLOOR) for p, o in zip(predictions, oracle)]
        policies[policy] = {
            "agreement": sum(a == b for a, b in zip(picks, oracle)) / len(picks),
            "total_seconds": sum(chosen),
            # Geometric mean of chosen / oracle time: how much slower, per query.
            "slowdown": 10 ** statistics.mean(math.log10(c / b) for c, b in zip(chosen, best)),
            "estimates": sum(picks),
        }
    return {
        "n": len(predictions),
        "exact_factor": 10 ** statistics.median(exact_err),
        "estimate_factor": 10 ** statistics.median(estimate_err),
        "speedup_factor": 10 ** statistics.median(speedup_err),
        "speedup_within_2x": sum(e <= math.log10(2) for e in speedup_err) / len(speedup_err),
        "policies": policies,
    }


def report(records: list[dict], paired: list[dict], unseen: dict, seen: dict, model: CostModel) -> str:
    datasets = sorted({r["dataset"] for r in paired})
    lines = [
        "# Learned cost model: results",
        "",
        f"Generated by `backend/ml/train.py` on {time.strftime('%Y-%m-%d %H:%M')}. "
        f"{unseen.get('raw_runs', len(records))} logged runs, merged into {len(records)} distinct queries "
        f"(repeats replaced by their median), {len(paired)} with both exact and estimate times, "
        f"across {len(datasets)} datasets.",
        "",
        "The model predicts the exact and the estimate time of a query from features measured "
        "before it runs (table sizes and locations, joins, groups, stored samples, network latency, "
        f"pushdown, accuracy target). It recommends the estimate when the predicted speedup is at least "
        f"{SPEEDUP_THRESHOLD}×.",
        "",
        "## Prediction accuracy",
        "",
        "*Typical factor* is the median ratio between predicted and actual (1.0 would be perfect).",
        "",
        "| Evaluation | Exact time | Estimate time | Speedup | Speedup within 2× |",
        "|---|---|---|---|---|",
    ]
    for label, result in (("Unseen datasets (leave one dataset out)", unseen), ("Seen datasets (5-fold)", seen)):
        lines.append(
            f"| {label} | ×{result['exact_factor']:.2f} | ×{result['estimate_factor']:.2f} | "
            f"×{result['speedup_factor']:.2f} | {result['speedup_within_2x']:.0%} |"
        )
    lines += [
        "",
        "## Ablation (unseen datasets)",
        "",
        "Same evaluation, different feature sets. *Formula only* predicts the estimate time from the pilot "
        "with no learning: its cost scaled to the sample fraction the target needs.",
        "",
        "| Features | Exact time | Estimate time | Speedup | Speedup within 2× | Picks faster | Slowdown per query |",
        "|---|---|---|---|---|---|---|",
    ]
    for label, result in unseen["ablation"].items():
        model_policy = result["policies"]["model"]
        lines.append(
            f"| {label}{' (deployed)' if label == unseen['chosen'] else ''} | ×{result['exact_factor']:.2f} | "
            f"×{result['estimate_factor']:.2f} | ×{result['speedup_factor']:.2f} | {result['speedup_within_2x']:.0%} | "
            f"{model_policy['agreement']:.0%} | {model_policy['slowdown']:.2f}× |"
        )
    if unseen.get("formula_factor"):
        lines.append(f"| formula only (no ML), estimate time | – | ×{unseen['formula_factor']:.2f} | – | – | – | – |")
    lines += [
        "",
        "## Choosing exact or estimate (unseen datasets, deployed model)",
        "",
        "*Agreement* is how often the policy made the same choice as an oracle that knows the real "
        f"times and picks the estimate whenever it is really at least {SPEEDUP_THRESHOLD}× faster (the same bar "
        "the model uses, since an estimate also carries error); *total time* is what running every query "
        "by that policy would have cost.",
        "",
        "*Slowdown* is the geometric mean, per query, of the chosen time over the oracle's (1.00× is perfect); "
        "it weighs a millisecond query made 2× slower as much as a minute-long one.",
        "",
    ]
    for title, result in (("All datasets", unseen), *[(f"{k} only", v) for k, v in unseen["by_setting"].items()]):
        lines += [
            f"**{title}** ({result['n']} queries)",
            "",
            "| Policy | Agreement with oracle | Slowdown per query | Estimates chosen | Total time |",
            "|---|---|---|---|---|",
        ]
        for name, policy in result["policies"].items():
            lines.append(
                f"| {name} | {policy['agreement']:.0%} | {policy['slowdown']:.2f}× | "
                f"{policy['estimates']} of {result['n']} | {policy['total_seconds']:.2f} s |"
            )
        lines.append("")
    def top(regressor, names):
        return sorted(zip(names, regressor.feature_importances_), key=lambda item: -item[1])[:6]

    lines += [
        "",
        "## What the model relies on",
        "",
        "Impurity importance, exact-time model: "
        + ", ".join(f"`{n}` {v:.2f}" for n, v in top(model.exact, model.features)),
        "",
        "Impurity importance, estimate-time model: "
        + ", ".join(f"`{n}` {v:.2f}" for n, v in top(model.estimate, model.estimate_features or model.features)),
        "",
        "## Datasets",
        "",
        ", ".join(f"`{d}`" for d in datasets),
        "",
    ]
    return "\n".join(lines)


def formula_factor(paired: list[dict]) -> float | None:
    """How well the pilot formula alone (no ML) predicts the estimate time."""
    errors = [
        abs(math.log10(max((10 ** r["features"]["log_formula_estimate_ms"] - 1) / 1000, FLOOR)) - log_time(r["approx_time"]))
        for r in paired
        if r["features"].get("pilot_ok")
    ]
    return 10 ** statistics.median(errors) if errors else None


def _metrics(result: dict) -> dict:
    keys = ("n", "exact_factor", "estimate_factor", "speedup_factor", "speedup_within_2x", "policies")
    out = {k: result[k] for k in keys}
    if "by_setting" in result:
        out["by_setting"] = {name: _metrics(r) for name, r in result["by_setting"].items()}
    return out


def summary(raw, records, paired, unseen, seen, ablation, held_out, setting) -> dict:
    """The evaluation, plus every held-out prediction (each made by a model
    that never saw that query's dataset) for a predicted-vs-actual chart."""
    return {
        "trained_at": time.time(),
        "raw_runs": len(raw),
        "queries": len(records),
        "paired": len(paired),
        "datasets": sorted({r["dataset"] for r in paired}),
        "threshold": SPEEDUP_THRESHOLD,
        "chosen": unseen["chosen"],
        "formula_factor": unseen["formula_factor"],
        "unseen": _metrics(unseen),
        "seen": _metrics(seen),
        "ablation": {label: _metrics(result) for label, result in ablation.items()},
        "held_out": [
            {
                "dataset": p["record"]["dataset"],
                "query": p["record"]["query"],
                "setting": setting(p),
                "exact": max(p["record"]["exact_time"], FLOOR),
                "approx": max(p["record"]["approx_time"], FLOOR),
                "predicted_exact": p["exact"],
                "predicted_approx": p["estimate"],
            }
            for p in held_out
        ],
    }


def main() -> None:
    raw = [r for r in run_log.read(SOURCES) if r.get("approximable") and r.get("features")]
    records = aggregate(raw)
    paired = [r for r in records if r.get("exact_time") is not None and r.get("approx_time") is not None]
    if len(paired) < 20:
        sys.exit(f"Need at least 20 runs with both times; found {len(paired)}. Run benchmarks.generate_training_data first.")
    groups = [r["dataset"] for r in paired]
    remote = ("rfam", "rnacentral", "cross", "mongo")
    setting = lambda p: "Remote" if p["record"]["dataset"].startswith(remote) else "Local"  # noqa: E731

    # Ablation: the first model's features against those plus the pilot.
    variants = {
        "size and shape (first model)": (SHAPE_FEATURES, SHAPE_FEATURES),
        "+ pilot sample, both models": (FEATURES, FEATURES),
        "+ pilot sample, estimate model only": (SHAPE_FEATURES, FEATURES),
    }
    ablation = {}
    held_outs = {}
    for label, names in variants.items():
        held_out = held_outs[label] = cross_validate(records, paired, LeaveOneGroupOut(), groups, names)
        result = evaluate(held_out)
        result["by_setting"] = {
            name: evaluate([p for p in held_out if setting(p) == name])
            for name in ("Local", "Remote")
            if any(setting(p) == name for p in held_out)
        }
        ablation[label] = result
    # Deploy the variant that chooses best, then predicts the speedup best.
    best = min(
        ablation,
        key=lambda k: (
            round(ablation[k]["policies"]["model"]["slowdown"], 2),
            -ablation[k]["speedup_within_2x"],
            ablation[k]["speedup_factor"],
        ),
    )
    unseen = ablation[best]
    names = variants[best]
    seen = evaluate(cross_validate(records, paired, KFold(5, shuffle=True, random_state=0), None, names))
    model = fit(records, names)
    unseen["ablation"] = ablation
    unseen["chosen"] = best
    unseen["formula_factor"] = formula_factor(paired)
    unseen["raw_runs"] = len(raw)

    import joblib

    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, MODEL_PATH)
    REPORT.write_text(report(records, paired, unseen, seen, model))
    SUMMARY.write_text(json.dumps(summary(raw, records, paired, unseen, seen, ablation, held_outs[best], setting), indent=1))
    print(REPORT.read_text())
    print(f"saved {MODEL_PATH}")


if __name__ == "__main__":
    main()
