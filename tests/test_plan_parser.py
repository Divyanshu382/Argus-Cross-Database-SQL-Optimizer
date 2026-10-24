from backend.core.plan_parser import parse_plan


def test_duckdb_json_plans_become_trees(db):
    sql = "SELECT g.tier, COUNT(*) FROM customers c JOIN segments g ON g.seg = c.seg GROUP BY g.tier"
    for prefix in ("EXPLAIN (FORMAT JSON)", "EXPLAIN (ANALYZE, FORMAT JSON)"):
        parsed = parse_plan(db.execute(f"{prefix} {sql}").fetchall())
        assert parsed["format"] == "duckdb_json"
        types = []

        def walk(node):
            types.append(node["type"])
            for child in node["children"]:
                walk(child)

        walk(parsed["plan_tree"])
        assert "HASH_JOIN" in types
        assert any("SCAN" in t for t in types)
        assert parsed["explanation"].startswith("Runs bottom-up")
