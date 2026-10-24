"""Generate cost-model training data by running varied queries both ways.

    .venv/bin/python -m benchmarks.generate_training_data             # local only (~5 min)
    .venv/bin/python -m benchmarks.generate_training_data --remote    # + Rfam MySQL (~10 more min)

Each run records the query's features (taken before it runs) and the measured
exact and estimate times, into backend/ml/data/generated_runs.jsonl. Local
data lives in an in-memory DuckDB, so nothing is written to the project's
database file.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

os.environ["AETHERQUERY_DUCKDB_PATH"] = ":memory:"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.core.router import route_query  # noqa: E402
from backend.db import connections, samples  # noqa: E402
from backend.db.duckdb import get_connection  # noqa: E402
from backend.ml import features as feature_module  # noqa: E402
from backend.ml import run_log  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "backend" / "ml" / "data" / "generated_runs.jsonl"
SIZES = [20_000, 60_000, 200_000, 600_000, 2_000_000, 6_000_000]
TARGETS = [80, 92, 98]

LOCAL_TEMPLATES = [
    "SELECT COUNT(*) FROM {t}",
    "SELECT SUM(amount), AVG(qty) FROM {t}",
    "SELECT g4, COUNT(*), AVG(amount) FROM {t} GROUP BY g4",
    "SELECT g100, SUM(amount) FROM {t} GROUP BY g100",
    "SELECT g5000, COUNT(*) FROM {t} GROUP BY g5000",
    "SELECT COUNT(*) FROM {t} WHERE amount < 1",
    "SELECT AVG(amount) FROM {t} WHERE qty < 50",
    "SELECT g100, COUNT(*) FROM {t} GROUP BY g100 ORDER BY COUNT(*) DESC LIMIT 5",
    "SELECT d.cat, COUNT(*), SUM(f.amount) FROM {t} f JOIN dim d ON d.k = f.g100 GROUP BY d.cat",
    "SELECT d.cat, AVG(f.qty) FROM {t} f JOIN dim d ON d.k = f.g100 WHERE f.amount > 50 GROUP BY d.cat",
]

TPCH_QUERIES = [
    "SELECT l_returnflag, l_linestatus, SUM(l_quantity), AVG(l_extendedprice), COUNT(*) FROM lineitem "
    "GROUP BY l_returnflag, l_linestatus ORDER BY l_returnflag, l_linestatus",
    "SELECT SUM(l_extendedprice * l_discount) FROM lineitem WHERE l_discount BETWEEN 0.05 AND 0.07 AND l_quantity < 24",
    "SELECT n.n_name, COUNT(*), SUM(l.l_extendedprice) FROM lineitem l JOIN orders o ON o.o_orderkey = l.l_orderkey "
    "JOIN customer c ON c.c_custkey = o.o_custkey JOIN nation n ON n.n_nationkey = c.c_nationkey GROUP BY n.n_name",
]

REMOTE_QUERIES = [
    ("rfam-full_region", "SELECT is_significant, COUNT(*), AVG(bit_score) FROM rfam.full_region GROUP BY is_significant"),
    ("rfam-full_region", "SELECT COUNT(*), SUM(seq_end - seq_start) FROM rfam.full_region WHERE bit_score > 100"),
    ("rfam-join", "SELECT f.type, COUNT(*) AS hits FROM rfam.full_region r JOIN rfam.family f ON f.rfam_acc = r.rfam_acc "
     "GROUP BY f.type ORDER BY hits DESC LIMIT 10"),
    ("rfam-family", "SELECT type, COUNT(*), AVG(num_seed) FROM rfam.family GROUP BY type"),
    ("rfam-family", "SELECT COUNT(*) FROM rfam.family WHERE num_full > 1000"),
    ("rfam-full_region-filtered", "SELECT type, COUNT(*), AVG(evalue_score) FROM rfam.full_region WHERE bit_score > 50 GROUP BY type"),
    ("rfam-full_region-filtered", "SELECT COUNT(*), AVG(bit_score) FROM rfam.full_region WHERE truncated = '0' AND is_significant = 1"),
    ("rfam-full_region-filtered", "SELECT rfam_acc, COUNT(*) AS n FROM rfam.full_region GROUP BY rfam_acc ORDER BY n DESC LIMIT 20"),
    ("rfam-join-clan", "SELECT c.clan_acc, COUNT(*) AS hits FROM rfam.full_region r JOIN rfam.clan_membership c "
     "ON c.rfam_acc = r.rfam_acc GROUP BY c.clan_acc ORDER BY hits DESC LIMIT 10"),
    ("rfam-family", "SELECT seed_source, AVG(num_full), SUM(num_seed) FROM rfam.family GROUP BY seed_source"),
]


def run(dataset: str, sql: str, target: float) -> None:
    extracted = feature_module.extract(sql, "duckdb", target)
    if not extracted["approximable"]:
        return
    start = time.time()
    try:
        payload = route_query(sql, "benchmark", "duckdb", target)
    except Exception as exc:  # a query the estimator rejects at run time
        print(f"  skipped ({str(exc)[:70]})", flush=True)
        return
    record = {
        "timestamp": time.time(),
        "origin": "generated",
        "dataset": dataset,
        "query": sql,
        "mode": "benchmark",
        "approximable": True,
        "features": extracted["features"],
        **run_log.outcome("benchmark", payload),
    }
    run_log.write(record, OUT)
    print(
        f"  {dataset:18s} t={target:.0f}  exact {record['exact_time']:8.4f}s  est {record['approx_time']:8.4f}s  "
        f"({time.time() - start:5.1f}s)  {sql[:60]}",
        flush=True,
    )


REAL_LOCAL = [
    ("orders-csv", "orders_csv", [
        "SELECT customer_region, COUNT(*), AVG(total_amount) FROM orders_csv GROUP BY customer_region",
        "SELECT AVG(order_quantity) FROM orders_csv WHERE is_returned = 1",
        "SELECT product_category, SUM(total_amount) FROM orders_csv GROUP BY product_category",
        "SELECT payment_method, COUNT(*) FROM orders_csv WHERE customer_age > 40 GROUP BY payment_method",
    ]),
    ("taxi-parquet", "taxi_local", [
        "SELECT payment_type, COUNT(*), AVG(fare_amount) FROM taxi_local GROUP BY payment_type",
        "SELECT AVG(tip_amount) FROM taxi_local WHERE trip_distance > 5",
        "SELECT PULocationID, COUNT(*) FROM taxi_local GROUP BY PULocationID ORDER BY COUNT(*) DESC LIMIT 10",
        "SELECT SUM(total_amount) FROM taxi_local WHERE passenger_count >= 2",
    ]),
]


def generate_local(repeats: int) -> None:
    conn = get_connection()
    conn.execute("SELECT setseed(0.42)")
    conn.execute("CREATE TABLE dim AS SELECT i AS k, 'cat' || (i % 7) AS cat FROM range(100) t(i)")
    for size in SIZES:
        name = f"f_{size}"
        conn.execute(
            f"""CREATE TABLE {name} AS SELECT i AS id, (random() * 4)::INT AS g4,
                    (random() * 100)::INT AS g100, (random() * 5000)::INT AS g5000,
                    random() * 100 AS amount, (random() * 100)::INT AS qty
                FROM range({size}) t(i)"""
        )
        conn.execute("CHECKPOINT")
        print(f"table {name}", flush=True)
        for _ in range(repeats):
            for template in LOCAL_TEMPLATES:
                for target in TARGETS:
                    run(f"synthetic-{size}", template.format(t=name), target)
        conn.execute(f"DROP TABLE {name}")

    # Harder shapes at 1M rows: heavy-tailed values (a few huge amounts), and
    # rows stored sorted by group, so block samples see few groups each.
    for variant, amount, order in [
        ("skewed", "exp(random() * 9)", ""),
        ("clustered", "random() * 100", "ORDER BY g100"),
    ]:
        name = f"f_{variant}"
        conn.execute(
            f"""CREATE TABLE {name} AS SELECT * FROM (SELECT i AS id, (random() * 4)::INT AS g4,
                    (random() * 100)::INT AS g100, (random() * 5000)::INT AS g5000,
                    {amount} AS amount, (random() * 100)::INT AS qty FROM range(1000000) t(i)) {order}"""
        )
        conn.execute("CHECKPOINT")
        print(f"table {name}", flush=True)
        for _ in range(repeats):
            for template in LOCAL_TEMPLATES:
                for target in TARGETS:
                    run(f"{variant}-1000000", template.format(t=name), target)
        conn.execute(f"DROP TABLE {name}")

    from backend.db.duckdb import create_table_from_url

    orders = Path(__file__).resolve().parents[1] / "datasets" / "4df3314fa21b4b02a8b269140262ff77.csv"
    if orders.exists():
        conn.execute(f"CREATE TABLE orders_csv AS SELECT * FROM read_csv_auto('{orders}')")
    create_table_from_url(
        "https://d37ci6vzurychx.cloudfront.net/trip-data/yellow_tripdata_2024-01.parquet", "taxi_local", materialize=True
    )
    for dataset, table, queries in REAL_LOCAL:
        if table == "orders_csv" and not orders.exists():
            continue
        print(f"table {table}", flush=True)
        for _ in range(repeats):
            for sql in queries:
                for target in TARGETS:
                    run(dataset, sql, target)

    conn.execute("INSTALL tpch; LOAD tpch; CALL dbgen(sf = 0.3)")
    print("TPC-H sf 0.3", flush=True)
    for _ in range(repeats):
        for sql in TPCH_QUERIES:
            for target in TARGETS:
                run("tpch-0.3", sql, target)


POSTGRES_QUERIES = [
    ("rnacentral-cpat", "SELECT is_protein_coding, COUNT(*), AVG(coding_probability) FROM rnacentral.cpat_results GROUP BY is_protein_coding"),
    ("rnacentral-cpat", "SELECT AVG(fickett_score) FROM rnacentral.cpat_results WHERE hexamer_score > 0"),
    ("rnacentral-cpat", "SELECT COUNT(*), SUM(coding_probability) FROM rnacentral.cpat_results WHERE fickett_score > 0.5"),
]

CROSS_QUERIES = [
    ("cross-small", "SELECT f.type, COUNT(*), SUM(m.full_count) FROM rfam.family f JOIN rnacentral.rfam_models m "
     "ON m.rfam_model_id = f.rfam_acc GROUP BY f.type ORDER BY 2 DESC LIMIT 10"),
    ("cross-big", "SELECT m.rna_type, COUNT(*) AS hits FROM rfam.full_region r JOIN rnacentral.rfam_models m "
     "ON m.rfam_model_id = r.rfam_acc GROUP BY m.rna_type ORDER BY hits DESC LIMIT 10"),
]


def generate_views(repeats: int) -> None:
    """Views that re-read their source on every query: a raw CSV and Parquet over HTTP."""
    from backend.db.duckdb import create_table_from_url

    conn = get_connection()
    orders = Path(__file__).resolve().parents[1] / "datasets" / "4df3314fa21b4b02a8b269140262ff77.csv"
    views = []
    if orders.exists():
        conn.execute(f"CREATE OR REPLACE VIEW orders_csv AS SELECT * FROM read_csv_auto('{orders}')")
        views.append(("csv-raw", REAL_LOCAL[0][2]))
    create_table_from_url(
        "https://d37ci6vzurychx.cloudfront.net/trip-data/yellow_tripdata_2024-01.parquet", "taxi_local", materialize=False
    )
    views.append(("parquet-http", REAL_LOCAL[1][2]))
    for dataset, queries in views:
        print(f"view {dataset}", flush=True)
        for _ in range(repeats):
            for sql in queries:
                for target in (80, 92):
                    run(dataset, sql, target)


def generate_remote() -> None:
    for preset in connections.PUBLIC_DATABASES:
        connections.connect(preset["name"], connections.ConnectionParams(
            **{k: v for k, v in preset.items() if k not in ("name", "label")}
        ))
    print("connected to Rfam and RNAcentral", flush=True)
    samples.build("rfam.full_region", 0.01)
    # First with the small tables read over the network, then with local copies.
    for with_copy in (False, True):
        if with_copy:
            samples.build("rfam.family", 1.0)
            samples.build("rfam.clan_membership", 1.0)
        for dataset, sql in REMOTE_QUERIES:
            if dataset == "rfam-family" and not with_copy:
                continue  # no sample of family yet: these can't be estimated
            if dataset.startswith("rfam-full_region") and with_copy:
                continue  # unaffected by the copies; measured once
            run(dataset + ("-copy" if with_copy else ""), sql, 92)

    samples.build("rnacentral.cpat_results", 0.01)
    for dataset, sql in POSTGRES_QUERIES:
        for target in (80, 92):
            run(dataset, sql, target)
    samples.build("rnacentral.rfam_models", 1.0)
    for dataset, sql in CROSS_QUERIES:
        run(dataset, sql, 92)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--remote", action="store_true", help="also run against the public Rfam MySQL database")
    parser.add_argument("--remote-only", action="store_true")
    parser.add_argument("--views-only", action="store_true", help="add runs on views over files (appends)")
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if args.views_only:
        generate_views(args.repeats)
        print(f"wrote {OUT}")
        return
    if not args.remote_only:
        # Local runs are regenerated from scratch; remote ones are kept.
        if OUT.exists():
            remote = ('"dataset": "rfam', '"dataset": "rnacentral', '"dataset": "cross')
            keep = [line for line in OUT.read_text().splitlines() if any(tag in line for tag in remote)]
            OUT.write_text("".join(line + "\n" for line in keep))
        generate_local(args.repeats)
    if args.remote or args.remote_only:
        generate_remote()
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
