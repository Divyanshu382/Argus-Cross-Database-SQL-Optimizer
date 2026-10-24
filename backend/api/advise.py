import threading
import time

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from backend.ml import model, run_log

router = APIRouter()

# Retraining runs in a background thread; one at a time.
_training: dict = {"running": False, "error": None, "finished_at": None}
_training_lock = threading.Lock()


class AdviseRequest(BaseModel):
    query: str = Field(..., min_length=1)
    source: str = "duckdb"
    accuracy_target: float | None = Field(default=None, ge=50.0, le=99.9)


@router.post("/advise")
async def advise(req: AdviseRequest):
    """Predict exact and estimate times for a query without running it."""
    # Feature extraction may look up a remote table's size once (then cached).
    return await run_in_threadpool(model.advise, req.query, req.source.lower().strip(), req.accuracy_target)


def _importance(regressor, names: list[str]) -> list[dict]:
    pairs = sorted(zip(names, regressor.feature_importances_), key=lambda item: -item[1])
    return [{"feature": name, "importance": float(value)} for name, value in pairs]


def _new_runs(since: float | None) -> int:
    """Runs logged by the app after the model was trained."""
    if not run_log.LOG_PATH.exists():
        return 0
    return sum(1 for r in run_log.read([run_log.LOG_PATH]) if since is None or r.get("timestamp", 0) > since)


@router.get("/model")
def model_info():
    """What the cost model is, what it relies on and how well it predicts."""
    cost_model = model.load()
    report = model.report()
    info = {
        "trained": cost_model is not None,
        "threshold": model.SPEEDUP_THRESHOLD,
        "report": report,
        "new_runs": _new_runs(report.get("trained_at") if report else None),
        "training": dict(_training),
    }
    if cost_model is not None:
        info["trained_on"] = cost_model.trained_on
        info["importance"] = {
            "exact": _importance(cost_model.exact, cost_model.features),
            "estimate": _importance(cost_model.estimate, cost_model.estimate_features or cost_model.features),
        }
        info["regressor"] = {
            "kind": type(cost_model.exact).__name__,
            "n_estimators": cost_model.exact.n_estimators,
            "max_depth": cost_model.exact.max_depth,
            "learning_rate": cost_model.exact.learning_rate,
        }
    return info


def _train() -> None:
    from backend.ml import train

    error = None
    try:
        train.main()
    except SystemExit as exc:  # train.main exits when there are too few runs
        error = str(exc)
    except Exception as exc:  # noqa: BLE001 - reported to the page
        error = str(exc)
    with _training_lock:
        _training.update(running=False, error=error, finished_at=time.time())


@router.post("/model/train")
def retrain():
    """Retrain on every logged run, including the ones made in the app."""
    with _training_lock:
        if not _training["running"]:
            _training.update(running=True, error=None)
            threading.Thread(target=_train, daemon=True).start()
        return dict(_training)
