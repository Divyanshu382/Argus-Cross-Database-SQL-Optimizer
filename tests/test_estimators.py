import math

import pytest

from backend.core.approx_engine import run_approx
from backend.core.estimators import Z_95, EstimateQuery, _half_width, _output_name, build_estimate_sql
from backend.core.parser import AggregateSpec, OrderBySpec
from backend.db.duckdb import sample_method

COUNT = AggregateSpec("count", "*", "count_all")
SUM = AggregateSpec("sum", "x", "sum_x")
AVG = AggregateSpec("avg", "x", "avg_x")


def test_count_and_sum_variance_follow_horvitz_thompson():
    # f = 0.5 -> (1 - f) / f^2 = 2
    row = {"__sumn0": 10, "__nn0": 10, "__ss0": 50.0}
    assert _half_width(COUNT, 0, row, 0.5) == pytest.approx(Z_95 * math.sqrt(20))
    assert _half_width(SUM, 0, row, 0.5) == pytest.approx(Z_95 * math.sqrt(100))


def test_avg_variance_uses_ratio_linearisation():
    # Units (n, s): (1, 2), (1, 4) -> R = 3, residuals -1 and 1.
    row = {"__sumn0": 2, "__sums0": 6.0, "__nn0": 2.0, "__ss0": 20.0, "__sn0": 6.0}
    assert _half_width(AVG, 0, row, 0.5) == pytest.approx(Z_95 * math.sqrt(0.5 * 2 / 4))


def test_full_scan_has_zero_width():
    assert _half_width(SUM, 0, {"__ss0": 9.0}, 1.0) == 0.0


def test_order_by_maps_group_expressions_and_names():
    query = EstimateQuery(
        from_sql="t",
        group_by=["f.type"],
        aggregates=[COUNT],
        order_by=[OrderBySpec("f.type", False), OrderBySpec("count_all", True)],
        limit=3,
    )
    sql = build_estimate_sql(query, 0.1)
    assert "ORDER BY __g0 ASC, count_all DESC LIMIT 3" in sql
    assert _output_name("f.type") == "type"
    assert _output_name("lower(name)") == "lower(name)"


def test_sampling_method_depends_on_table_size(db):
    assert sample_method("sales") == "system"
    assert sample_method("customers") == "bernoulli"
    assert sample_method("rfam.family") == "bernoulli"


def test_small_table_estimate_is_never_zero_or_whole_table_scaled(db):
    # Regression: block sampling on a 3000-row table returned 0 or 1/f times
    # the true count. Row sampling keeps estimates near the truth.
    for _ in range(10):
        estimate = run_approx("SELECT COUNT(*) FROM tiny", mode="accurate")["rows"][0][0]
        assert 1000 < estimate < 6000


def test_empty_sample_is_an_error_not_zero(db):
    with pytest.raises(ValueError, match="no matching rows"):
        run_approx("SELECT COUNT(*) FROM tiny WHERE i < 0")


@pytest.mark.parametrize(
    "sql,truth_sql",
    [
        ("SELECT AVG(score) FROM customers", "SELECT AVG(score) FROM customers"),
        ("SELECT SUM(amount) FROM sales", "SELECT SUM(amount) FROM sales"),
    ],
)
def test_confidence_intervals_cover_about_95_percent(db, sql, truth_sql):
    truth = float(db.execute(truth_sql).fetchone()[0])
    runs = 120
    hits = 0
    for _ in range(runs):
        result = run_approx(sql, mode="fast")
        estimate, width = result["rows"][0][0], result["ci_rows"][0][0]
        hits += abs(estimate - truth) <= width
    # Nominal 95%; the normal approximation runs slightly low on small samples.
    # Below 85% in 120 runs would be very unlikely if the intervals were right.
    assert hits / runs >= 0.85


def test_sample_size_is_chosen_to_reach_the_target_width():
    from backend.core.runtime_sampling import fraction_for_width

    # Width scales with sqrt((1 - f) / f): halving it from f = 0.2 needs
    # (1 - f') / f' = 4 / 4 = 1, so f' = 0.5.
    assert fraction_for_width(0.2, 0.10, 0.05) == pytest.approx(0.5)
    assert fraction_for_width(0.2, 0.04, 0.05) < 0.2  # already narrow enough


def test_accurate_preset_reaches_its_target_or_says_exact_is_needed(db):
    result = run_approx("SELECT region, SUM(amount) FROM sales GROUP BY region", mode="accurate", accuracy_target=98)
    if result["stop_reason"] == "needs_exact":
        assert result["needed_fraction"] > 0.5
    else:
        assert result["max_relative_ci"] <= 0.02


def test_pilot_sample_is_never_the_reported_one(db):
    # Even when the first small sample already looks precise enough, the
    # answer comes from a second, independent sample sized by it.
    result = run_approx("SELECT AVG(score) FROM customers", mode="fast", accuracy_target=80)
    assert len(result["iterations"]) >= 2
    assert result["sample_rate"] > result["iterations"][0]["sample_fraction"]


def test_skewed_data_raises_the_minimum_sample(heavy):
    from backend.core.estimators import EstimateQuery, fetch_estimate
    from backend.core.parser import AggregateSpec

    query = EstimateQuery(
        from_sql="(SELECT * FROM heavy USING SAMPLE 1% (bernoulli)) AS s",
        group_by=[],
        aggregates=[AggregateSpec("avg", "qty", "avg_qty")],
        where="flagged",
    )
    payload, _, _ = fetch_estimate(query, 0.01)
    assert payload["skew_fraction"] > 0.01  # 1% of the flagged rows is too few for these tails


def test_heavy_tailed_filtered_average_keeps_its_coverage(heavy):
    truth = float(heavy.execute("SELECT AVG(qty) FROM heavy WHERE flagged").fetchone()[0])
    runs, hits = 80, 0
    for _ in range(runs):
        result = run_approx("SELECT AVG(qty) FROM heavy WHERE flagged", mode="balanced", accuracy_target=92)
        hits += abs(result["rows"][0][0] - truth) <= result["ci_rows"][0][0]
    # Before two-phase sampling and the skewness rule this sat near 60%.
    assert hits / runs >= 0.85


def test_group_keys_from_different_engines_match():
    from datetime import datetime

    from backend.core.benchmark import _ci_coverage, canonical_key, canonical_map

    # MySQL text vs DuckDB timestamp for the same month, and numbers as text.
    assert canonical_key("2013-02-01 00:00:00") == canonical_key("2013-02-01T00:00:00") == canonical_key(datetime(2013, 2, 1))
    assert canonical_key("4") == canonical_key(4) == canonical_key(4.0)
    exact = canonical_map({"2013-02-01 00:00:00": {"n": 91}})
    approx = canonical_map({datetime(2013, 2, 1): {"n": 91.0000000001}})
    # A zero-width interval (exact answer from a local copy) still counts, allowing for rounding.
    assert _ci_coverage(exact, approx, canonical_map({datetime(2013, 2, 1): {"n": 0.0}})) == 1.0
