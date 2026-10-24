import pytest

from backend.core.approx_engine import prepare_approx, run_approx
from backend.core.federation import plan_join, push_whole_query
from backend.db import connections as reg


@pytest.fixture
def fake_mysql():
    # push_whole_query only needs the registry's metadata, not a live server.
    reg._connections["shop"] = reg.Connection(
        "shop", "mysql", "", "attached", [], query_alias="shop", default_schema="Shop"
    )
    reg._connections["crm"] = reg.Connection(
        "crm", "postgres", "", "attached", [], query_alias="crm", default_schema="public"
    )
    yield
    reg._connections.pop("shop")
    reg._connections.pop("crm")


def test_whole_query_goes_to_its_single_source_in_its_dialect(fake_mysql):
    sql = push_whole_query(
        "SELECT o.region, COUNT(*) FROM shop.orders o JOIN shop.items i ON i.oid = o.id "
        "WHERE o.note ILIKE '%gift%' GROUP BY o.region"
    )
    assert sql.startswith("SELECT * FROM mysql_query('shop', ")
    assert "`Shop`.`orders` AS o" in sql and "`Shop`.`items` AS i" in sql
    assert "LOWER(o.note) LIKE LOWER(" in sql  # ILIKE translated for MySQL


def test_queries_spanning_sources_or_local_tables_stay_in_duckdb(fake_mysql):
    assert push_whole_query("SELECT COUNT(*) FROM shop.orders o JOIN crm.people p ON p.id = o.pid") is None
    assert push_whole_query("SELECT COUNT(*) FROM shop.orders o JOIN sales s ON s.id = o.id") is None
    assert push_whole_query("SELECT COUNT(*) FROM sales") is None


@pytest.mark.parametrize(
    "sql,message",
    [
        ("SELECT COUNT(*) FROM a LEFT JOIN b ON a.x = b.x", "inner joins"),
        ("SELECT MAX(a.v) FROM a JOIN b ON a.x = b.x", "COUNT, SUM and AVG"),
        ("SELECT COUNT(DISTINCT a.v) FROM a JOIN b ON a.x = b.x", "DISTINCT"),
        ("SELECT a.g, COUNT(*) FROM a JOIN b ON a.x = b.x GROUP BY a.g HAVING COUNT(*) > 1", "HAVING"),
        ("SELECT a.g, COUNT(*) FROM a JOIN b ON a.x = b.x", "GROUP BY"),
    ],
)
def test_join_planner_rejects_what_it_cannot_estimate(sql, message):
    with pytest.raises(ValueError, match=message):
        plan_join(sql)


def test_join_plan_reads_groups_aggregates_and_ordering():
    plan = plan_join(
        "SELECT g.tier, COUNT(*) AS n, AVG(s.amount) FROM sales s JOIN customers c ON c.cust = s.cust "
        "JOIN segments g ON g.seg = c.seg GROUP BY g.tier ORDER BY n DESC LIMIT 2"
    )
    assert plan.group_by == ["g.tier"]
    assert [(a.func, a.alias) for a in plan.aggregates] == [("count", "n"), ("avg", "avg_s_amount")]
    assert plan.order_by[0].key == "n" and plan.order_by[0].descending
    assert plan.limit == 2


def test_join_estimate_samples_the_largest_table(db):
    sql = (
        "SELECT g.tier, COUNT(*), SUM(s.amount) FROM sales s JOIN customers c ON c.cust = s.cust "
        "JOIN segments g ON g.seg = c.seg GROUP BY g.tier ORDER BY g.tier"
    )
    prepared = prepare_approx(sql)
    assert prepared.group_count == 1
    result = run_approx(sql, mode="accurate")
    assert result["sampled_table"] == "sales"
    exact = db.execute(sql).fetchall()
    for (tier, count, total), estimate, widths in zip(exact, result["rows"], result["ci_rows"]):
        assert estimate[0] == tier
        # Within four interval half-widths: a loose bound that should never flake.
        assert abs(estimate[1] - count) <= 4 * widths[0]
        assert abs(estimate[2] - float(total)) <= 4 * widths[1]


def test_remote_table_without_stored_sample_is_explained(fake_mysql):
    with pytest.raises(ValueError, match="stored"):
        prepare_approx("SELECT COUNT(*) FROM shop.orders")
    with pytest.raises(ValueError, match="Build a stored sample"):
        prepare_approx("SELECT COUNT(*) FROM shop.orders o JOIN sales s ON s.id = o.id")


def test_tables_are_probed_not_listed(db):
    # Catalog listings fail for everyone once a locked-down Postgres server is
    # attached, so table/view/missing is decided by probing the one relation.
    from backend.db.duckdb import get_connection, relation_kind

    get_connection().execute("CREATE OR REPLACE VIEW tiny_view AS SELECT * FROM tiny")
    assert relation_kind("tiny") == "table"
    assert relation_kind("tiny_view") == "view"
    assert relation_kind("no_such_table") is None


def test_multiline_join_gets_the_join_error_message(fake_mysql):
    with pytest.raises(ValueError, match="Build a stored sample"):
        prepare_approx("SELECT COUNT(*)\nFROM shop.orders o\nJOIN sales s ON s.id = o.id")
