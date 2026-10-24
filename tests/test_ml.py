import math
import random

import pytest

from backend.ml import features as feature_module
from backend.ml import model as model_module
from backend.ml import run_log
from backend.ml.train import evaluate, fit


def test_features_describe_the_query_before_it_runs(db):
    extracted = feature_module.extract(
        "SELECT g.tier, COUNT(*) FROM sales s JOIN customers c ON c.cust = s.cust "
        "JOIN segments g ON g.seg = c.seg WHERE s.amount > 5 GROUP BY g.tier",
        accuracy_target=98,
    )
    f = extracted["features"]
    assert extracted["approximable"]
    assert (f["n_tables"], f["n_joins"], f["n_group_by"], f["n_conditions"]) == (3, 2, 1, 1)
    assert f["log_rows_largest"] == pytest.approx(math.log10(1 + 1_000_000))
    assert f["block_sampled"] == 1.0  # sales is big enough for block sampling
    assert f["n_local"] == 3 and f["largest_is_remote"] == 0.0
    assert f["accuracy_target"] == 98


def test_unestimable_query_is_flagged(db):
    extracted = feature_module.extract("SELECT MAX(amount) FROM sales")
    assert not extracted["approximable"]


def test_rule_baseline():
    base = {name: 0.0 for name in feature_module.FEATURES}
    assert not model_module.rule_recommends_estimate({**base, "log_rows_largest": 5})
    assert model_module.rule_recommends_estimate({**base, "log_rows_largest": 7.5})
    assert model_module.rule_recommends_estimate({**base, "largest_is_remote": 1, "stored_sample": 1})


def _synthetic_runs(n=200):
    """Exact time grows with table size and latency; estimates stay cheap."""
    random.seed(1)
    runs = []
    for i in range(n):
        rows = random.uniform(3, 8)
        latency = random.choice([0.0, 150.0])
        f = {name: 0.0 for name in feature_module.FEATURES}
        f.update(log_rows_largest=rows, latency_ms=latency, largest_is_remote=float(latency > 0), stored_sample=float(latency > 0))
        exact = 10 ** (rows - 7.5) + latency / 1000 * (1 + rows)
        approx = 10 ** (rows - 8.5) * (0.2 if latency else 1) + 0.002
        runs.append({"dataset": f"d{i % 6}", "features": f, "exact_time": exact, "approx_time": approx, "approximable": True})
    return runs


def test_model_learns_and_beats_always_estimate():
    runs = _synthetic_runs()
    model = fit(runs)
    small = {name: 0.0 for name in feature_module.FEATURES} | {"log_rows_largest": 3.5}
    remote = small | {"log_rows_largest": 7.5, "latency_ms": 150.0, "largest_is_remote": 1.0, "stored_sample": 1.0}
    assert model.predict(remote)[0] / model.predict(remote)[1] > model.predict(small)[0] / model.predict(small)[1]
    predictions = [{"record": r, "exact": e, "estimate": a} for r in runs for e, a in [model.predict(r["features"])]]
    result = evaluate(predictions)
    assert result["policies"]["model"]["total_seconds"] <= result["policies"]["always estimate"]["total_seconds"]
    assert result["policies"]["model"]["agreement"] >= 0.9


def test_advise_falls_back_to_the_rule_without_a_model(db, tmp_path, monkeypatch):
    monkeypatch.setattr(model_module, "MODEL_PATH", tmp_path / "missing.joblib")
    advice = model_module.advise("SELECT COUNT(*) FROM customers")
    assert advice["method"] == "rule" and advice["recommendation"] == "exact"


def test_advise_uses_a_trained_model(db, tmp_path, monkeypatch):
    import joblib

    path = tmp_path / "model.joblib"
    joblib.dump(fit(_synthetic_runs()), path)
    monkeypatch.setattr(model_module, "MODEL_PATH", path)
    advice = model_module.advise("SELECT COUNT(*) FROM sales")
    assert advice["method"] == "model"
    assert advice["predicted_exact_seconds"] > 0 and advice["predicted_estimate_seconds"] > 0


def test_run_log_round_trip(tmp_path):
    path = tmp_path / "runs.jsonl"
    run_log.write({"mode": "exact", "exact_time": 1.5}, path)
    run_log.write({"mode": "approx", "approx_time": 0.1}, path)
    assert [r["mode"] for r in run_log.read([path])] == ["exact", "approx"]
    assert run_log.outcome("benchmark", {"exact": {"time": 2.0}, "approx": {"time": 0.5}, "error_percent": 1.0})["approx_time"] == 0.5


def test_pilot_features_predict_the_needed_fraction(db):
    f = feature_module.extract("SELECT region, SUM(amount) FROM sales GROUP BY region", accuracy_target=92)["features"]
    assert f["pilot_ok"] == 1.0
    assert 0 < f["predicted_fraction"] <= 1.0
    assert f["log_formula_estimate_ms"] > 0
    assert f["log_pilot_groups"] == pytest.approx(math.log10(1 + 4))


@pytest.mark.parametrize("recommendation, ran_approx", [("exact", False), ("estimate", True)])
def test_auto_mode_runs_only_what_the_model_chose(db, monkeypatch, recommendation, ran_approx):
    import asyncio

    from backend.api import execute as execute_api
    from backend.models.query import ExecuteRequest

    monkeypatch.setattr(
        model_module, "advise",
        lambda *a, **k: {"recommendation": recommendation, "method": "model", "reason": "test", "predicted_speedup": 3.0},
    )
    req = ExecuteRequest(query="SELECT region, COUNT(*) FROM sales GROUP BY region", mode="auto", use_cache=False)
    response = asyncio.run(execute_api.execute(req))
    assert response["auto_choice"]["chosen"] == ("approx" if ran_approx else "exact")
    assert bool(response["approx"]) is ran_approx
