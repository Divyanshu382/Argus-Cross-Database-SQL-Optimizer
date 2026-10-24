"""Benchmark exact vs approximate execution, locally and across the network.

    .venv/bin/python -m benchmarks.run_benchmarks            # local + remote
    .venv/bin/python -m benchmarks.run_benchmarks --local    # local only

Local data is TPC-H generated in memory by DuckDB, so nothing is written to
disk. Remote queries use the public Rfam MySQL database (EMBL-EBI), so they
need network access and take several minutes, mostly for the exact runs.
Results go to benchmarks/results.json and benchmarks/RESULTS.md.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

# In-memory database: nothing is written to the project's DuckDB file.
os.environ["AETHERQUERY_DUCKDB_PATH"] = ":memory:"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.core.approx_engine import prepare_approx  # noqa: E402
from backend.core.benchmark import _ci_coverage, _mean_relative_error, _normalize_exact_result  # noqa: E402
from backend.core.exact_engine import run_exact  # noqa: E402
from backend.db import connections, samples  # noqa: E402
from backend.db.duckdb import get_connection  # noqa: E402

OUT = Path(__file__).resolve().parent

LOCAL_QUERIES = [
    ("Q1 group by, 3M rows",
     "SELECT l_returnflag, l_linestatus, SUM(l_quantity), AVG(l_extendedprice), COUNT(*) "
     "FROM lineitem GROUP BY l_returnflag, l_linestatus ORDER BY l_returnflag, l_linestatus"),
    ("Q6-style filter, 3M rows",
     "SELECT SUM(l_extendedprice * l_discount) FROM lineitem "
     "WHERE l_discount BETWEEN 0.05 AND 0.07 AND l_quantity < 24"),
    ("3-way join, group by nation",
     "SELECT n.n_name, COUNT(*), SUM(l.l_extendedprice) FROM lineitem l "
     "JOIN orders o ON o.o_orderkey = l.l_orderkey JOIN customer c ON c.c_custkey = o.o_custkey "
     "JOIN nation n ON n.n_nationkey = c.c_nationkey GROUP BY n.n_name ORDER BY n.n_name"),
]

REMOTE_QUERIES = [
    ("Rfam full_region (10.6M rows)",
     "SELECT is_significant, COUNT(*), AVG(bit_score) FROM rfam.full_region "
     "GROUP BY is_significant ORDER BY is_significant"),
    ("Rfam full_region JOIN family",
     "SELECT f.type, COUNT(*) AS hits, AVG(r.bit_score) AS avg_score FROM rfam.full_region r "
     "JOIN rfam.family f ON f.rfam_acc = r.rfam_acc GROUP BY f.type ORDER BY hits DESC LIMIT 10"),
]


def measure(label: str, sql: str, runs: int, mode: str = "balanced", target: float | None = 92) -> dict:
    prepared = prepare_approx(sql)
    exact_times, approx_times, errors, max_errors, coverages, rates, stops, widths = ([] for _ in range(8))
    exact_payload = None
    for i in range(runs):
        # The exact answer does not change; time it on the first few runs only.
        if i < min(runs, 3):
            exact_payload = run_exact(sql, "duckdb")
            exact_times.append(exact_payload["time"])
        start = time.perf_counter()
        approx = prepared.run(mode, target, None)
        approx_times.append(time.perf_counter() - start)
        exact_map = _normalize_exact_result(prepared.group_count, prepared.aliases, exact_payload)
        errors.append(_mean_relative_error(exact_map, approx["result_map"]) * 100)
        max_errors.append(_max_relative_error(exact_map, approx["result_map"]) * 100)
        coverage = _ci_coverage(exact_map, approx["result_map"], approx.get("ci_map"))
        if coverage is not None:
            coverages.append(coverage)
        rates.append(approx["sample_rate"])
        stops.append(approx["stop_reason"])
        if approx.get("max_relative_ci") is not None:
            widths.append(approx["max_relative_ci"] * 100)
    exact_s = statistics.median(exact_times)
    approx_s = statistics.median(approx_times)
    row = {
        "query": label,
        "sql": sql,
        "runs": runs,
        "exact_s": exact_s,
        "approx_s": approx_s,
        "speedup": exact_s / approx_s if approx_s else None,
        "mean_error_pct": statistics.mean(errors),
        "worst_error_pct": max(max_errors),
        "ci_coverage": statistics.mean(coverages) if coverages else None,
        "median_ci_pct": statistics.median(widths) if widths else None,
        "sample_rate": statistics.median(rates),
        "stop_reasons": sorted(set(stops)),
        "exact_pushed_to_source": bool(exact_payload.get("rewritten_query")),
    }
    print(
        f"  {label:34s} exact {exact_s:8.3f}s  approx {approx_s:8.4f}s  x{row['speedup']:8.1f}  "
        f"err {row['mean_error_pct']:5.2f}% (worst {row['worst_error_pct']:5.2f}%)  "
        f"coverage {row['ci_coverage'] if row['ci_coverage'] is None else round(row['ci_coverage'], 3)}  "
        f"±{row['median_ci_pct'] or 0:.1f}%  rate {row['sample_rate']}",
        flush=True,
    )
    return row


def _max_relative_error(exact, approx) -> float:
    worst = 0.0

    def compare(e: dict, a: dict) -> None:
        nonlocal worst
        for alias, truth in e.items():
            estimate = a.get(alias)
            if truth not in (None, 0) and estimate is not None:
                worst = max(worst, abs(float(estimate) - float(truth)) / abs(float(truth)))

    if exact and all(isinstance(v, dict) for v in exact.values()):
        for key, values in exact.items():
            if key in approx:
                compare(values, approx[key])
    else:
        compare(exact, approx)
    return worst


def run_local(runs: int) -> list[dict]:
    conn = get_connection()
    print("Generating TPC-H scale factor 0.5 in memory…", flush=True)
    conn.execute("INSTALL tpch; LOAD tpch; CALL dbgen(sf = 0.5)")
    print(f"  lineitem rows: {conn.execute('SELECT COUNT(*) FROM lineitem').fetchone()[0]:,}")
    results = []
    for mode, target in (("fast", 80), ("balanced", 92), ("accurate", 98)):
        print(f"Local, preset {mode} (stop at ±{100 - target}%)", flush=True)
        for label, sql in LOCAL_QUERIES:
            row = measure(label, sql, runs, mode, target)
            row.update(setting="local", preset=mode)
            results.append(row)
    return results


def run_remote(runs: int) -> list[dict]:
    preset = connections.PUBLIC_DATABASES[0]
    fields = {k: v for k, v in preset.items() if k not in ("name", "label")}
    start = time.time()
    connections.connect(preset["name"], connections.ConnectionParams(**fields))
    print(f"Connected to Rfam in {time.time() - start:.1f}s", flush=True)
    sample = samples.build("rfam.full_region", 0.01)
    print(f"  1% sample of full_region: {sample.sample_rows:,} rows in {sample.build_seconds:.1f}s", flush=True)
    results = []
    print("Remote (exact runs on the MySQL server; approximate from the stored sample)", flush=True)
    for label, sql in REMOTE_QUERIES:
        row = measure(label, sql, runs)
        row.update(setting="remote", preset="balanced", local_copies=[])
        results.append(row)
    copy = samples.build("rfam.family", 1.0)
    print(f"  full local copy of family: {copy.sample_rows:,} rows in {copy.build_seconds:.1f}s", flush=True)
    label, sql = REMOTE_QUERIES[1]
    row = measure(label + " (family copied locally)", sql, runs)
    row.update(setting="remote", preset="balanced", local_copies=["rfam.family"])
    results.append(row)
    for build in (sample, copy):
        row_note = {"sample_of": build.source_table, "fraction": build.fraction,
                    "rows": build.sample_rows, "build_s": build.build_seconds}
        results.append({"setting": "remote-build", **row_note})
    return results


def write_markdown(results: list[dict]) -> None:
    lines = [
        "# Benchmark results",
        "",
        f"Generated by `benchmarks/run_benchmarks.py` on {time.strftime('%Y-%m-%d %H:%M')}. "
        "Times are medians. Error is the mean relative error of all estimated values against "
        "the exact answer; *coverage* is the share of exact values that fell inside the "
        "estimate's 95% confidence interval (ideal: about 95%).",
        "",
    ]
    for setting, title in (("local", "Local data (TPC-H SF 0.5, in memory)"),
                           ("remote", "Connected database (Rfam, public MySQL server in the UK)")):
        rows = [r for r in results if r.get("setting") == setting]
        if not rows:
            continue
        lines += [f"## {title}", "",
                  "| Query | Preset | Exact | Approx | Speedup | Mean error | Worst error | Coverage | Median ± | Sample |",
                  "|---|---|---|---|---|---|---|---|---|---|"]
        for r in rows:
            coverage = "–" if r["ci_coverage"] is None else f"{r['ci_coverage']:.0%}"
            ci = "–" if r["median_ci_pct"] is None else f"±{r['median_ci_pct']:.1f}%"
            lines.append(
                f"| {r['query']} | {r['preset']} | {_fmt(r['exact_s'])} | {_fmt(r['approx_s'])} | "
                f"{r['speedup']:.1f}× | {r['mean_error_pct']:.2f}% | {r['worst_error_pct']:.2f}% | "
                f"{coverage} | {ci} | {r['sample_rate']:.2%} |"
            )
        lines.append("")
    builds = [r for r in results if r.get("setting") == "remote-build"]
    if builds:
        lines += ["One-time stored sample builds:", ""]
        lines += [f"- `{b['sample_of']}` at {b['fraction']:.0%}: {b['rows']:,} rows in {b['build_s']:.0f} s" for b in builds]
        lines.append("")
    (OUT / "RESULTS.md").write_text("\n".join(lines))


def _fmt(seconds: float) -> str:
    return f"{seconds * 1000:.1f} ms" if seconds < 1 else f"{seconds:.1f} s"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--local", action="store_true", help="skip the remote (network) benchmarks")
    parser.add_argument("--runs", type=int, default=30, help="approximate runs per local query")
    parser.add_argument("--remote-runs", type=int, default=5, help="approximate runs per remote query")
    args = parser.parse_args()
    results = run_local(args.runs)
    if not args.local:
        results += run_remote(args.remote_runs)
    elif (OUT / "results.json").exists():
        # Keep the slow remote results from the last full run.
        previous = json.loads((OUT / "results.json").read_text())
        results += [r for r in previous if str(r.get("setting", "")).startswith("remote")]
    (OUT / "results.json").write_text(json.dumps(results, indent=2, default=str))
    write_markdown(results)
    print(f"Wrote {OUT / 'RESULTS.md'}")


if __name__ == "__main__":
    main()
