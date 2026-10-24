"""Benchmark exact vs estimated queries on every kind of source Argus supports.

    .venv/bin/python -m benchmarks.benchmark_sources            # everything (~25 min, needs network)
    .venv/bin/python -m benchmarks.benchmark_sources --local    # local sources only

Sources: a local DuckDB table (TPC-H), an imported CSV, a raw CSV queried in
place, a Parquet file copied locally, the same file read over HTTP, MySQL
(Rfam), PostgreSQL (RNAcentral), MySQL x PostgreSQL joins, and MongoDB (an
in-memory stand-in, so only correctness is reported for it). Each query also
records the cost model's speedup prediction, made before running it.

Everything runs in an in-memory DuckDB; results go to
benchmarks/SOURCES_RESULTS.md and benchmarks/sources_results.json.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

os.environ["AETHERQUERY_DUCKDB_PATH"] = ":memory:"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.core.approx_engine import prepare_approx  # noqa: E402
from backend.core.benchmark import _ci_coverage, _mean_relative_error, _normalize_exact_result  # noqa: E402
from backend.core.exact_engine import run_exact  # noqa: E402
from backend.db import connections, samples  # noqa: E402
from backend.db.duckdb import create_table_from_url, get_connection  # noqa: E402
from backend.ml.model import advise  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "benchmarks"
TAXI = "https://d37ci6vzurychx.cloudfront.net/trip-data/yellow_tripdata_2024-01.parquet"
ORDERS_CSV = ROOT / "datasets" / "4df3314fa21b4b02a8b269140262ff77.csv"
TARGET = 92  # balanced: stop when every 95% interval is within ±8%


def where_exact_ran(payload: dict) -> str:
    sql = payload.get("rewritten_query") or ""
    if sql.startswith("db."):
        return "inside MongoDB"
    if "mysql_query(" in sql:
        return "inside MySQL"
    if "postgres_query(" in sql:
        return "inside PostgreSQL"
    return "DuckDB"


def how_estimated(result: dict) -> str:
    if result.get("stop_reason") == "local_copies":
        return "whole, from local copies"
    if result.get("stop_reason") == "stored_sample":
        stored = result.get("stored_sample") or {}
        fraction = stored.get("fraction", result.get("sample_rate") or 0)
        return "local copy" if fraction >= 1 else f"stored {fraction:.0%} sample"
    sql = result.get("rewritten_query") or ""
    method = "block" if "(system)" in sql else "row" if "(bernoulli)" in sql else "live $sample" if "__aq_mongo" in sql else "sample"
    return f"{method} sample, {result.get('sample_rate', 0):.1%}"


def measure(source: str, label: str, sql: str, runs: int, exact_runs: int) -> dict:
    prepared = prepare_approx(sql)
    predicted = advise(sql, "duckdb", TARGET).get("predicted_speedup")
    exact_times, approx_times, errors, coverages = [], [], [], []
    exact = approx = None
    for i in range(max(runs, exact_runs)):
        if i < exact_runs:
            exact = run_exact(sql, "duckdb")
            exact_times.append(exact["time"])
        if i < runs:
            start = time.perf_counter()
            approx = prepared.run("balanced", TARGET, None)
            approx_times.append(time.perf_counter() - start)
            truth = _normalize_exact_result(prepared.group_count, prepared.aliases, exact)
            errors.append((_mean_relative_error(truth, approx["result_map"]) or 0) * 100)
            coverage = _ci_coverage(truth, approx["result_map"], approx.get("ci_map"))
            if coverage is not None:
                coverages.append(coverage)
    exact_s, approx_s = statistics.median(exact_times), statistics.median(approx_times)
    row = {
        "source": source,
        "query": label,
        "sql": sql,
        "exact_s": exact_s,
        "exact_where": where_exact_ran(exact),
        "approx_s": approx_s,
        "approx_how": how_estimated(approx),
        "speedup": exact_s / approx_s if approx_s else None,
        "predicted_speedup": predicted,
        "error_pct": statistics.mean(errors),
        "coverage": statistics.mean(coverages) if coverages else None,
        "runs": runs,
        "exact_runs": exact_runs,
    }
    print(
        f"  {source:24s} {label:34s} exact {exact_s:9.3f}s ({row['exact_where']:17s}) "
        f"est {approx_s:8.4f}s ({row['approx_how']:22s}) x{row['speedup']:9.1f}  "
        f"pred x{predicted if predicted is None else round(predicted, 1)}  err {row['error_pct']:.2f}%  "
        f"cov {row['coverage'] if row['coverage'] is None else round(row['coverage'], 2)}",
        flush=True,
    )
    return row


def local_suite(runs: int) -> list[dict]:
    conn = get_connection()
    results = []
    print("Local DuckDB table: TPC-H SF 0.5", flush=True)
    conn.execute("INSTALL tpch; LOAD tpch; CALL dbgen(sf = 0.5)")
    for label, sql in [
        ("group by, 3M rows", "SELECT l_returnflag, l_linestatus, SUM(l_quantity), AVG(l_extendedprice), COUNT(*) "
         "FROM lineitem GROUP BY l_returnflag, l_linestatus"),
        ("filtered sum, 3M rows", "SELECT SUM(l_extendedprice * l_discount) FROM lineitem "
         "WHERE l_discount BETWEEN 0.05 AND 0.07 AND l_quantity < 24"),
        ("4-table join", "SELECT n.n_name, COUNT(*), SUM(l.l_extendedprice) FROM lineitem l "
         "JOIN orders o ON o.o_orderkey = l.l_orderkey JOIN customer c ON c.c_custkey = o.o_custkey "
         "JOIN nation n ON n.n_nationkey = c.c_nationkey GROUP BY n.n_name"),
    ]:
        results.append(measure("Local table (DuckDB)", label, sql, runs, 3))

    csv_queries = [
        ("group by region", "SELECT customer_region, COUNT(*), AVG(total_amount) FROM {t} GROUP BY customer_region"),
        ("filtered average", "SELECT AVG(order_quantity) FROM {t} WHERE is_returned = 1"),
    ]
    if ORDERS_CSV.exists():
        path = str(ORDERS_CSV).replace("'", "''")
        print("CSV, imported (126 MB, ~1M rows)", flush=True)
        conn.execute(f"CREATE TABLE orders_csv AS SELECT * FROM read_csv_auto('{path}')")
        for label, sql in csv_queries:
            results.append(measure("CSV, imported", label, sql.format(t="orders_csv"), runs, 3))
        print("CSV, queried in place (re-read every query)", flush=True)
        conn.execute(f"CREATE VIEW orders_raw AS SELECT * FROM read_csv_auto('{path}')")
        for label, sql in csv_queries:
            results.append(measure("CSV, raw file", label, sql.format(t="orders_raw"), min(runs, 3), 2))

    taxi_queries = [
        ("group by payment type", "SELECT payment_type, COUNT(*), AVG(fare_amount) FROM {t} GROUP BY payment_type"),
        ("filtered average", "SELECT AVG(tip_amount) FROM {t} WHERE trip_distance > 5"),
    ]
    print("Parquet, copied locally (NYC taxi, 3M rows)", flush=True)
    create_table_from_url(TAXI, "taxi_local", materialize=True)
    for label, sql in taxi_queries:
        results.append(measure("Parquet, local copy", label, sql.format(t="taxi_local"), runs, 3))
    print("Parquet over HTTP (no copy)", flush=True)
    create_table_from_url(TAXI, "taxi_http", materialize=False)
    for label, sql in taxi_queries:
        results.append(measure("Parquet over HTTP", label, sql.format(t="taxi_http"), 2, 2))
    return results


def mongo_suite() -> list[dict]:
    """Correctness only: an in-memory stand-in has no realistic timings."""
    import mongomock

    random.seed(3)
    client = mongomock.MongoClient()
    start = datetime(2024, 1, 1)
    client.shop.orders.insert_many([
        {"customer": {"region": random.choice(["N", "S", "E", "W"])}, "total": random.uniform(5, 500),
         "items": random.randint(1, 9), "at": start + timedelta(minutes=i)}
        for i in range(60_000)
    ])
    connections.connect("shop", connections.ConnectionParams(kind="mongodb", uri="mongodb://mock/shop", database="shop"),
                        client_factory=lambda *a, **k: client)
    print("MongoDB (in-memory stand-in: correctness only)", flush=True)
    rows = []
    for label, sql in [
        ("group by region", "SELECT customer_region, COUNT(*), AVG(total) FROM shop.orders GROUP BY customer_region"),
        ("filtered sum", "SELECT SUM(items) FROM shop.orders WHERE total > 250"),
    ]:
        row = measure("MongoDB (mock)", label, sql, 3, 1)
        row["timings_meaningful"] = False
        rows.append(row)
    connections.disconnect("shop")
    return rows


def remote_suite() -> list[dict]:
    results = []
    for preset in connections.PUBLIC_DATABASES:
        fields = {k: v for k, v in preset.items() if k not in ("name", "label")}
        connections.connect(preset["name"], connections.ConnectionParams(**fields))
    print("connected to Rfam (MySQL) and RNAcentral (PostgreSQL)", flush=True)

    print("building stored samples and local copies", flush=True)
    for table, fraction in [("rfam.full_region", 0.01), ("rfam.family", 1.0),
                            ("rnacentral.cpat_results", 0.01), ("rnacentral.rfam_models", 1.0)]:
        built = samples.build(table, fraction)
        print(f"  {table}: {built.sample_rows:,} rows in {built.build_seconds:.0f} s", flush=True)
        results.append({"source": "sample-build", "table": table, "fraction": fraction,
                        "rows": built.sample_rows, "build_s": built.build_seconds})

    print("MySQL (Rfam)", flush=True)
    for label, sql, exact_runs in [
        ("group by, 10.6M rows", "SELECT is_significant, COUNT(*), AVG(bit_score) FROM rfam.full_region GROUP BY is_significant", 2),
        ("filtered group by", "SELECT type, COUNT(*), AVG(evalue_score) FROM rfam.full_region WHERE bit_score > 50 GROUP BY type", 2),
        ("join with family", "SELECT f.type, COUNT(*) AS hits FROM rfam.full_region r JOIN rfam.family f "
         "ON f.rfam_acc = r.rfam_acc GROUP BY f.type ORDER BY hits DESC LIMIT 10", 2),
    ]:
        results.append(measure("MySQL", label, sql, 3, exact_runs))

    print("PostgreSQL (RNAcentral)", flush=True)
    for label, sql in [
        ("group by, 3.4M rows", "SELECT is_protein_coding, COUNT(*), AVG(coding_probability) FROM rnacentral.cpat_results GROUP BY is_protein_coding"),
        ("filtered average", "SELECT AVG(fickett_score) FROM rnacentral.cpat_results WHERE hexamer_score > 0"),
    ]:
        results.append(measure("PostgreSQL", label, sql, 3, 2))

    print("MySQL x PostgreSQL", flush=True)
    for label, sql, exact_runs in [
        ("4k x 4k rows", "SELECT f.type, COUNT(*), SUM(m.full_count) FROM rfam.family f JOIN rnacentral.rfam_models m "
         "ON m.rfam_model_id = f.rfam_acc GROUP BY f.type ORDER BY 2 DESC LIMIT 10", 2),
        ("10.6M x 4k rows", "SELECT m.rna_type, COUNT(*) AS hits FROM rfam.full_region r JOIN rnacentral.rfam_models m "
         "ON m.rfam_model_id = r.rfam_acc GROUP BY m.rna_type ORDER BY hits DESC LIMIT 10", 1),
    ]:
        results.append(measure("MySQL x PostgreSQL", label, sql, 3, exact_runs))
    return results


def fmt(seconds: float) -> str:
    if seconds < 1:
        return f"{seconds * 1000:.1f} ms"
    return f"{seconds:.1f} s" if seconds < 120 else f"{seconds / 60:.1f} min"


def speed(x: float | None) -> str:
    if x is None:
        return "–"
    if x < 1:
        return f"{1 / x:.1f}× slower"
    return f"{x:,.0f}×" if x >= 10 else f"{x:.1f}×"


def write(results: list[dict]) -> None:
    (OUT / "sources_results.json").write_text(json.dumps(results, indent=2, default=str))
    lines = [
        "# Exact vs estimate on every kind of source",
        "",
        f"Generated by `benchmarks/benchmark_sources.py` on {time.strftime('%Y-%m-%d %H:%M')}. "
        f"Estimates use the Balanced setting (stop when every 95% interval is within ±8%). Times are medians; "
        "remote exact queries were run once or twice because each takes up to minutes. *Predicted* is the "
        "learned cost model's speedup, made before running. *Coverage* is the share of exact values inside "
        "their estimate's 95% interval.",
        "",
        "| Source | Query | Exact | Exact ran | Estimate | Estimated from | Speedup | Predicted | Error | Coverage |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        if r["source"] == "sample-build":
            continue
        meaningful = r.get("timings_meaningful", True)
        lines.append(
            f"| {r['source']} | {r['query']} | {fmt(r['exact_s']) if meaningful else 'n/a'} | {r['exact_where']} | "
            f"{fmt(r['approx_s']) if meaningful else 'n/a'} | {r['approx_how']} | "
            f"{speed(r['speedup']) if meaningful else 'n/a'} | {speed(r['predicted_speedup']) if meaningful else 'n/a'} | "
            f"{r['error_pct']:.2f}% | {'–' if r['coverage'] is None else f'{r['coverage']:.0%}'} |"
        )
    builds = [r for r in results if r["source"] == "sample-build"]
    if builds:
        lines += ["", "One-time stored sample builds:", ""]
        lines += [f"- `{b['table']}` at {b['fraction']:.0%}: {b['rows']:,} rows in {b['build_s']:.0f} s" for b in builds]
    (OUT / "SOURCES_RESULTS.md").write_text("\n".join(lines) + "\n")
    print(f"wrote {OUT / 'SOURCES_RESULTS.md'}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--local", action="store_true")
    parser.add_argument("--runs", type=int, default=10)
    args = parser.parse_args()
    results = local_suite(args.runs) + mongo_suite()
    if not args.local:
        results += remote_suite()
    write(results)


if __name__ == "__main__":
    main()
