from backend.core import suggest as suggester
from backend.core.exact_engine import run_exact
from backend.db.duckdb import describe_relation


def test_suggestions_come_from_the_data(db):
    db.execute(
        "CREATE OR REPLACE TABLE trips AS SELECT i AS trip_id, ['card', 'cash', 'app'][i % 3 + 1] AS payment_type, "
        "(i % 97) * 1.5 AS fare, DATE '2024-01-01' + (i % 300)::INT AS day, i % 50 AS cust_id FROM range(20000) t(i)"
    )
    db.execute("CREATE OR REPLACE TABLE cust AS SELECT i AS cust_id, ['gold', 'basic'][i % 2 + 1] AS segment FROM range(50) t(i)")
    found = suggester.suggest("trips", describe_relation("trips"))
    titles = [s["title"] for s in found]
    assert "Rows by payment type" in titles
    assert "Average fare by payment type" in titles
    assert any("per month" in t for t in titles)
    join = next(s for s in found if s["title"].startswith("Join with cust"))
    assert "LIMIT" not in join["sql"]  # every group must be comparable
    assert "o.segment" in join["sql"] and "cust_id" in join["sql"]
    for item in found:  # every suggestion must actually run
        assert run_exact(item["sql"], "duckdb")["result"] is not None


def test_identifiers_are_not_treated_as_measures(db):
    found = suggester.suggest("customers", describe_relation("customers"))
    assert all("AVG(cust)" not in s["sql"] and "SUM(cust)" not in s["sql"] for s in found)


def test_endpoint_offers_only_comparable_queries(db):
    from backend.api.suggest import _suggestions
    from backend.core.approx_engine import prepare_approx

    result = _suggestions("trips")
    assert result["suggestions"] and result["note"] is None
    for item in result["suggestions"]:
        prepare_approx(item["sql"])  # raises if it could not be compared
