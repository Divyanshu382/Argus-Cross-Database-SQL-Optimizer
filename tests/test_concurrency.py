from concurrent.futures import ThreadPoolExecutor

from backend.db.duckdb import execute_query, list_local_tables


def test_parallel_requests_get_their_own_results(db):
    # Regression: one shared connection mixed up results between threads
    # (e.g. /api/tables receiving another request's rows).
    def work(i):
        for _ in range(30):
            rows = execute_query(f"SELECT {i} AS id, COUNT(*) FROM range({1000 + i})")["rows"]
            assert rows == [(i, 1000 + i)]
            assert {t["name"] for t in list_local_tables()} >= {"sales", "customers"}
        return i

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sorted(pool.map(work, range(8))) == list(range(8))
