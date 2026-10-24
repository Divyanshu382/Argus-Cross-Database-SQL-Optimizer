"""Live MongoDB: translation to pipelines, fetch fallback, joins and sampling.

Runs against mongomock; every result is checked against DuckDB computing the
same query over all documents."""

import math
import random
from datetime import datetime, timedelta

import mongomock
import pytest

from backend.core import mongo_query
from backend.core.approx_engine import run_approx
from backend.core.exact_engine import run_exact
from backend.db import connections as reg
from backend.db import mongo, samples
from backend.db.duckdb import execute_query, get_connection


@pytest.fixture(scope="module")
def lab(db):
    random.seed(7)
    client = mongomock.MongoClient()
    start = datetime(2024, 1, 1)
    trips = []
    for i in range(5000):
        doc = {
            "pickup": {"zone": f"Z{i % 10}", "at": start + timedelta(minutes=13 * i)},
            "fare": {"total": round(random.uniform(3, 60), 2), "tip": random.choice([None, 0.0, 1.5, 4.0])},
            "tags": ["airport"] if i % 7 == 0 else [],
        }
        if i % 9:  # some documents have no passengers field at all
            doc["passengers"] = i % 4
        trips.append(doc)
    client.lab.trips.insert_many(trips)
    client.lab.zones.insert_many([{"zone": f"Z{i}", "borough": ["Queens", "Bronx", "Brooklyn"][i % 3]} for i in range(10)])
    params = reg.ConnectionParams(kind="mongodb", uri="mongodb://fake/lab", database="lab")
    connection = reg.connect("lab", params, client_factory=lambda *a, **k: client)
    # Ground truth: every document, loaded into plain DuckDB tables.
    for name in ("trips", "zones"):
        entry = mongo.lookup(f"lab.{name}")
        mongo.load(entry, mongo.fetch(entry, None), list(entry.paths), target=f"truth_{name}")
    yield connection
    reg.disconnect("lab")


def truth(sql):
    sql = sql.replace("lab.trips", "truth_trips").replace("lab.zones", "truth_zones")
    return get_connection().execute(sql).fetchall()


def same(a, b):
    assert len(a) == len(b)
    for row_a, row_b in zip(a, b):
        for x, y in zip(row_a, row_b):
            if isinstance(x, float) or isinstance(y, float):
                assert x == pytest.approx(y, rel=1e-9) if y is not None else x in (None, 0)
            else:
                assert x == y


def test_connection_is_live_with_inferred_schema(lab):
    assert lab.mode == "live"
    assert set(lab.tables) == {"lab.trips", "lab.zones"}
    entry = mongo.lookup("lab.trips")
    assert entry.paths["pickup_zone"] == "pickup.zone"
    assert entry.paths["fare_total"] == "fare.total"
    assert "TIMESTAMP" in entry.types["pickup_at"]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT pickup_zone, COUNT(*), SUM(fare_total), AVG(fare_tip), MIN(passengers), MAX(passengers) "
        "FROM lab.trips WHERE fare_total > 10 AND passengers IS NOT NULL GROUP BY pickup_zone ORDER BY pickup_zone",
        "SELECT COUNT(passengers), COUNT(DISTINCT pickup_zone) FROM lab.trips "
        "WHERE pickup_zone IN ('Z1', 'Z2') OR fare_total BETWEEN 5 AND 6",
        "SELECT COUNT(*) FROM lab.trips WHERE passengers <> 2",  # missing passengers must not count
        "SELECT COUNT(*) FROM lab.trips WHERE pickup_at >= '2024-01-15' AND pickup_zone NOT IN ('Z3')",
        "SELECT pickup_zone, fare_total FROM lab.trips WHERE pickup_zone LIKE 'Z1%' ORDER BY fare_total DESC LIMIT 5",
        "SELECT pickup_zone, SUM(fare_total * 2 - fare_tip) AS adjusted FROM lab.trips GROUP BY pickup_zone ORDER BY adjusted DESC LIMIT 3",
    ],
)
def test_pipeline_matches_duckdb(lab, sql):
    result = mongo_query.run(sql)
    assert result is not None, "should have been translated"
    assert result["pipeline"].startswith("db.trips.aggregate(")
    same(result["rows"], truth(sql))


def test_exact_engine_runs_translatable_queries_in_mongodb(lab):
    result = run_exact("SELECT pickup_zone, COUNT(*) FROM lab.trips GROUP BY pickup_zone ORDER BY pickup_zone", "duckdb")
    assert result["rewritten_query"].startswith("db.trips.aggregate(")


def test_untranslatable_query_falls_back_to_duckdb(lab):
    sql = "SELECT EXTRACT(hour FROM pickup_at) AS h, COUNT(*) FROM lab.trips GROUP BY h ORDER BY h"
    assert mongo_query.run(sql) is None
    result = run_exact(sql, "duckdb")
    assert result["rewritten_query"] is None
    same(result["result"], truth(sql))


def test_joins_fetch_only_what_they_need(lab, db):
    db.execute("CREATE OR REPLACE TABLE zone_targets AS SELECT 'Z' || i AS zone, i * 10 AS target FROM range(10) t(i)")
    for sql in [
        "SELECT z.borough, COUNT(*), SUM(t.fare_total) FROM lab.trips t JOIN lab.zones z ON z.zone = t.pickup_zone "
        "WHERE t.fare_total > 20 GROUP BY z.borough ORDER BY z.borough",
        "SELECT g.target, COUNT(*) FROM lab.trips t JOIN zone_targets g ON g.zone = t.pickup_zone "
        "GROUP BY g.target ORDER BY g.target",
    ]:
        same(run_exact(sql, "duckdb")["result"], truth(sql))
    # The trips table in DuckDB now holds only the fields that query used.
    columns = [row[0] for row in db.execute("DESCRIBE lab.trips").fetchall()]
    assert set(columns) == {"pickup_zone"}


def test_star_in_a_subquery_over_another_table_fetches_only_used_fields(lab, db):
    # A join estimate reads its sample as (SELECT *, rowid FROM sample); that
    # * must not pull every field of the MongoDB collection it joins.
    db.execute("CREATE OR REPLACE TABLE zone_picks AS SELECT 'Z' || i AS zone, i AS weight FROM range(10) t(i)")
    sql = (
        "SELECT z.borough, COUNT(*) FROM (SELECT *, rowid AS u FROM zone_picks) p "
        "JOIN lab.zones z ON z.zone = p.zone GROUP BY z.borough ORDER BY z.borough"
    )
    execute_query(sql)
    columns = [row[0] for row in db.execute("DESCRIBE lab.zones").fetchall()]
    assert set(columns) == {"zone", "borough"}


def test_live_sampling_estimates_with_intervals(lab):
    sql = "SELECT pickup_zone, COUNT(*), AVG(fare_total) FROM lab.trips GROUP BY pickup_zone ORDER BY pickup_zone"
    result = run_approx(sql, mode="balanced", accuracy_target=80)
    assert result["stop_reason"] != "stored_sample"
    assert 0 < result["sample_rate"] <= 0.5
    for (zone, count, avg), estimate, widths in zip(truth(sql), result["rows"], result["ci_rows"]):
        assert estimate[0] == zone
        assert abs(estimate[1] - count) <= 4 * widths[0]
        assert abs(estimate[2] - avg) <= 4 * widths[1]


def test_stored_mongo_sample_supports_approximate_joins(lab):
    stored = samples.build("lab.trips", 0.2)
    assert stored.fraction == pytest.approx(0.2)
    assert stored.sample_rows == 1000
    sql = (
        "SELECT z.borough, COUNT(*), SUM(t.fare_total) FROM lab.trips t JOIN lab.zones z ON z.zone = t.pickup_zone "
        "GROUP BY z.borough ORDER BY z.borough"
    )
    result = run_approx(sql)
    assert result["stop_reason"] == "stored_sample"
    assert result["sampled_table"] == "lab.trips"
    for exact, estimate, widths in zip(truth(sql), result["rows"], result["ci_rows"]):
        assert abs(estimate[1] - exact[1]) <= 4 * widths[0]
        assert abs(estimate[2] - float(exact[2])) <= 4 * widths[1]
    samples.delete("lab.trips")
