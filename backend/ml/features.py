"""Describe a query, before it runs, as numbers a cost model can learn from.

Everything here is cheap: the query is parsed (not run), and table sizes come
from local row counts, the source's own statistics (MySQL table_rows,
Postgres reltuples, MongoDB's estimated count) or stored-sample metadata,
cached per table so a remote lookup happens once.
"""

from __future__ import annotations

import math
import threading
import time
from typing import Any

import sqlglot
from sqlglot import exp

from backend.core import mongo_query
from backend.core.approx_engine import PreparedApprox, prepare_approx
from backend.core.runtime_sampling import MAX_RUNG_FRACTION, _derive_accuracy_config, fraction_for_width
from backend.core.federation import push_whole_query
from backend.db import duckdb as duckdb_db
from backend.db import mongo, samples
from backend.db.connections import resolve_remote

# The order the model sees them in. Add new features at the end and retrain.
FEATURES = [
    "n_tables",
    "n_joins",
    "n_group_by",
    "n_aggregates",
    "n_conditions",
    "has_order_by",
    "has_limit",
    "log_rows_largest",
    "log_rows_total",
    "n_local",
    "n_remote_sql",
    "n_mongo",
    "largest_is_remote",
    "latency_ms",
    "exact_pushdown",
    "stored_sample",
    "stored_fraction",
    "n_local_copies",
    "block_sampled",
    "accuracy_target",
    # A view re-reads its source (a raw CSV, a file over HTTP) on every query.
    "largest_is_view",
    # From a pilot: one cheap sampling step, the same one the sampler starts with.
    "pilot_ok",
    "pilot_log_ms",
    "pilot_ci",
    "pilot_skew_fraction",
    "predicted_fraction",
    "log_formula_estimate_ms",
    "log_pilot_groups",
]

# The features of the first model, before the pilot (kept for the ablation).
SHAPE_FEATURES = FEATURES[: FEATURES.index("pilot_ok")]

_SIZE_TTL = 600.0
_sizes: dict[str, tuple[float, int | None]] = {}
_latency: dict[str, tuple[float, float]] = {}
_lock = threading.Lock()


def _cached(cache: dict, key: str, compute) -> Any:
    with _lock:
        hit = cache.get(key)
        if hit and time.time() - hit[0] < _SIZE_TTL:
            return hit[1]
    value = compute()
    with _lock:
        cache[key] = (time.time(), value)
    return value


def _remote_rows(remote) -> int | None:
    if remote.kind == "mysql":
        sql = (
            "SELECT table_rows FROM information_schema.tables "
            f"WHERE table_schema = '{remote.schema}' AND table_name = '{remote.table}'"
        )
    else:
        sql = f"SELECT reltuples::bigint FROM pg_class WHERE oid = '{remote.source_name}'::regclass"
    try:
        row = duckdb_db.get_connection().execute(remote.query_sql(sql)).fetchone()
        return int(row[0]) if row and row[0] is not None and int(row[0]) >= 0 else None
    except Exception:
        return None


def table_rows(relation: str) -> int | None:
    """Rows in a table, wherever it lives (None when unknown)."""

    def compute() -> int | None:
        stored = samples.lookup(relation)
        if stored is not None:
            return round(stored.sample_rows / stored.fraction)
        entry = mongo.lookup(relation)
        if entry is not None:
            return entry.estimated_count()
        remote = resolve_remote(relation)
        if remote is not None:
            return _remote_rows(remote)
        if duckdb_db.relation_kind(relation) == "table":
            return duckdb_db.row_count(relation)
        return None

    return _cached(_sizes, relation.lower(), compute)


def _view_rows(relation: str) -> int | None:
    def compute() -> int | None:
        try:
            return duckdb_db.row_count(relation)
        except Exception:
            return None

    return _cached(_sizes, "view:" + relation.lower(), compute)


def source_latency_ms(relation: str) -> float:
    """Round-trip time to the database holding ``relation`` (0 when local)."""
    remote = resolve_remote(relation)
    entry = mongo.lookup(relation) if remote is None else None
    if remote is None and entry is None:
        return 0.0
    key = remote.connection if remote else entry.connection

    def compute() -> float:
        start = time.perf_counter()
        try:
            if remote is not None:
                duckdb_db.get_connection().execute(remote.query_sql("SELECT 1")).fetchall()
            else:
                entry.collection().database.command("ping")
        except Exception:
            return 0.0
        return (time.perf_counter() - start) * 1000.0

    return _cached(_latency, key, compute)


def _log(value: int | None) -> float:
    return math.log10(1 + value) if value else 0.0


def _pilot(prepared: PreparedApprox, accuracy_target: float, features: dict[str, Any]) -> None:
    """Run the sampler's first step and predict where it will go from there.

    The fraction the target needs follows from the pilot's interval width
    (it shrinks with sqrt((1 - f) / f)) and from Cochran's skewness rule; the
    formula estimate is the pilot's cost scaled to that fraction, plus the
    pilot itself. The model learns how far to trust it.
    """
    config = _derive_accuracy_config("balanced", accuracy_target)
    start = time.perf_counter()
    payload, _, _, _, fraction = prepared.rung(config["progression"][0])
    seconds = time.perf_counter() - start
    width = payload.get("max_relative_ci")
    skew = payload.get("skew_fraction") or 0.0
    features["pilot_ok"] = 1.0
    features["pilot_log_ms"] = math.log10(1 + seconds * 1000)
    features["pilot_ci"] = 5.0 if width is None or math.isinf(width) else min(width, 5.0)
    features["pilot_skew_fraction"] = min(skew, 1.0)
    features["log_pilot_groups"] = math.log10(1 + len(payload.get("rows") or []))
    if prepared.stored:
        # A stored sample is read whole, once: the pilot is the whole estimate.
        features["predicted_fraction"] = fraction
        features["log_formula_estimate_ms"] = features["pilot_log_ms"]
        return
    if width is None or math.isinf(width):
        needed = MAX_RUNG_FRACTION
    else:
        needed = max(fraction_for_width(fraction, width, config["convergence_threshold"]) or fraction, skew)
    final = min(MAX_RUNG_FRACTION, max(needed * 1.15, fraction * 1.5))
    features["predicted_fraction"] = min(needed, 1.0)
    features["log_formula_estimate_ms"] = math.log10(1 + seconds * 1000 * (1 + final / fraction))


def extract(
    query: str,
    source: str = "duckdb",
    accuracy_target: float | None = None,
    pilot: bool = True,
) -> dict[str, Any]:
    """Features of ``query``, plus ``approximable`` and the table list."""
    features: dict[str, Any] = {name: 0.0 for name in FEATURES}
    features["accuracy_target"] = float(accuracy_target or 92)
    try:
        tree = sqlglot.parse_one(query.strip().rstrip(";"), read="duckdb")
    except sqlglot.errors.ParseError:
        return {"features": features, "approximable": False, "tables": [], "reason": "unparseable"}

    tables = [".".join(part.name for part in t.parts) for t in tree.find_all(exp.Table)]
    features["n_tables"] = len(tables)
    features["n_joins"] = len(tree.args.get("joins") or []) if isinstance(tree, exp.Select) else 0
    features["n_group_by"] = len(tree.args["group"].expressions) if tree.args.get("group") else 0
    features["n_aggregates"] = sum(1 for _ in tree.find_all(exp.AggFunc))
    where = tree.args.get("where")
    features["n_conditions"] = (
        sum(1 for _ in where.find_all(exp.Predicate)) if where is not None else 0
    )
    features["has_order_by"] = float(bool(tree.args.get("order")))
    features["has_limit"] = float(bool(tree.args.get("limit")))

    sizes: list[tuple[str, int | None]] = []
    for relation in tables:
        remote = resolve_remote(relation)
        entry = mongo.lookup(relation)
        if entry is not None:
            features["n_mongo"] += 1
        elif remote is not None:
            features["n_remote_sql"] += 1
        else:
            features["n_local"] += 1
        sizes.append((relation, table_rows(relation)))
        features["latency_ms"] = max(features["latency_ms"], source_latency_ms(relation))
        stored = samples.lookup(relation)
        if stored is not None and stored.fraction >= 1.0:
            features["n_local_copies"] += 1

    # Views have no cheap row count; count once (cached) so size still informs.
    sizes = [(r, n if n is not None or duckdb_db.relation_kind(r) != "view" else _view_rows(r)) for r, n in sizes]
    known = [(r, n) for r, n in sizes if n is not None]
    if known:
        largest, rows = max(known, key=lambda item: item[1])
        features["log_rows_largest"] = _log(rows)
        features["log_rows_total"] = _log(sum(n for _, n in known))
        features["largest_is_remote"] = float(resolve_remote(largest) is not None or mongo.lookup(largest) is not None)
        stored = samples.lookup(largest)
        if stored is not None and stored.fraction < 1.0:
            features["stored_sample"] = 1.0
            features["stored_fraction"] = stored.fraction
        kind = duckdb_db.relation_kind(largest) if "." not in largest else None
        features["block_sampled"] = float(kind == "table" and rows >= 100 * 2048)
        features["largest_is_view"] = float(kind == "view")

    pushed = push_whole_query(query) is not None
    if not pushed:
        try:
            pushed = mongo_query.translate(query) is not None
        except mongo_query.Untranslatable:
            pushed = False
    features["exact_pushdown"] = float(pushed)

    try:
        prepared = prepare_approx(query, source)
        approximable, reason = True, None
    except ValueError as exc:
        prepared, approximable, reason = None, False, str(exc)
    # The pilot must stay cheap: a remote table with neither a stored sample
    # nor a local copy would be read whole over the network.
    uncovered = [t for t in tables if (resolve_remote(t) is not None or mongo.lookup(t) is not None) and samples.lookup(t) is None]
    live_mongo_only = all(mongo.lookup(t) is not None for t in uncovered) and len(tables) == 1
    if pilot and prepared is not None and prepared.rung is not None and (not uncovered or live_mongo_only):
        try:
            _pilot(prepared, features["accuracy_target"], features)
        except Exception:
            features["pilot_ok"] = 0.0  # the model then leans on the shape features
    return {"features": features, "approximable": approximable, "tables": tables, "reason": reason}
