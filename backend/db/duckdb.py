import os
import re
import threading
import uuid
from pathlib import Path
from typing import Any, Callable

import duckdb


_BASE_DIR = Path(__file__).resolve().parents[2]
_DB_PATH = os.getenv("AETHERQUERY_DUCKDB_PATH", str(_BASE_DIR / "datasets" / "aetherquery.duckdb"))
_CONNECTION = duckdb.connect(database=_DB_PATH, read_only=False)


# Called with each statement before it runs, so other modules can prepare
# objects the statement refers to (see backend.db.connections).
_before_query: list[Callable[[str], None]] = []


_SIMPLE_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


# Words DuckDB will not take as a bare column name (e.g. a MongoDB field
# called "cast"), so they are quoted too.
_KEYWORDS = {
    row[0]
    for row in duckdb.sql("SELECT keyword_name FROM duckdb_keywords() WHERE keyword_category <> 'unreserved'").fetchall()
}


def quote_ident(name: str) -> str:
    if _SIMPLE_IDENT.match(name) and name.lower() not in _KEYWORDS:
        return name
    return '"' + name.replace('"', '""') + '"'


def split_relation(relation: str) -> list[str]:
    """rfam."Family" -> ["rfam", "Family"]."""
    parts = re.findall(r'"((?:[^"]|"")+)"|([^."]+)', relation.strip())
    return [quoted.replace('""', '"') if quoted else plain.strip() for quoted, plain in parts]


_thread = threading.local()


def get_connection() -> duckdb.DuckDBPyConnection:
    """This thread's connection to the shared database.

    A DuckDB connection object must not be used by two threads at once: their
    results get mixed up. FastAPI runs requests on a thread pool, so each
    thread gets its own cursor (a separate connection to the same database
    instance; attached databases and loaded extensions are shared).
    """
    cursor = getattr(_thread, "cursor", None)
    if cursor is None:
        cursor = _CONNECTION.cursor()
        _thread.cursor = cursor
    return cursor


def register_before_query(hook: Callable[[str], None]) -> None:
    _before_query.append(hook)


def _prepare(sql: str) -> None:
    for hook in _before_query:
        hook(sql)


def _safe_identifier(name: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9_]", "_", name)
    if not clean:
        clean = f"table_{uuid.uuid4().hex[:8]}"
    if clean[0].isdigit():
        clean = f"t_{clean}"
    return clean.lower()


def create_table_from_csv(csv_path: str, table_name: str | None = None) -> str:
    conn = get_connection()
    generated_name = table_name or f"table_{uuid.uuid4().hex}"
    safe_name = _safe_identifier(generated_name)
    escaped_path = csv_path.replace("'", "''")
    # A materialized table, not a view: a view re-parses the CSV on every query
    # and has no storage blocks, so the sampler cannot skip anything.
    conn.execute(
        f"CREATE OR REPLACE TABLE {safe_name} AS SELECT * FROM read_csv_auto('{escaped_path}');"
    )
    note_local_table(safe_name)
    return safe_name


# Catalog listings (duckdb_tables(), information_schema) read every attached
# database, and a Postgres server that denies catalog access makes them fail
# for everyone once attached (see connections._connect_passthrough). So
# objects are probed one at a time instead of listed.


def relation_kind(relation: str) -> str | None:
    """"table" for a base table, "view" for anything without row ids, None if missing."""
    try:
        get_connection().execute(f"SELECT rowid FROM {relation} LIMIT 0")
        return "table"
    except duckdb.CatalogException:
        return None
    except duckdb.BinderException:
        return "view"


def row_count(relation: str) -> int:
    return int(get_connection().execute(f"SELECT COUNT(*) FROM {relation}").fetchone()[0])


# Last successful listing of local tables. Listing fails while a locked-down
# Postgres server is attached, so tables created since are added here directly.
_local_tables: dict[str, dict[str, Any]] = {}
_local_tables_listed = False


def _sql_database_attached() -> bool:
    # duckdb_databases() lists attachments without reading their catalogs.
    count = get_connection().execute(
        "SELECT COUNT(*) FROM duckdb_databases() WHERE type IN ('postgres', 'mysql')"
    ).fetchone()[0]
    return count > 0


def list_local_tables() -> list[dict[str, Any]]:
    """Tables and views in the local database's main schema."""
    global _local_tables_listed
    # duckdb_tables() reads the catalog of every attached database, which for
    # a large remote PostgreSQL takes minutes. Once remote databases are
    # attached, answer from the list kept by note_local_table instead.
    if _local_tables_listed and _sql_database_attached():
        return sorted(_local_tables.values(), key=lambda t: t["name"])
    try:
        rows = get_connection().execute(
            """
            SELECT table_name, 'table', estimated_size FROM duckdb_tables()
            WHERE database_name = current_database() AND schema_name = 'main'
            UNION ALL
            SELECT view_name, 'view', NULL FROM duckdb_views()
            WHERE database_name = current_database() AND schema_name = 'main' AND NOT internal
            ORDER BY 1
            """
        ).fetchall()
        _local_tables.clear()
        _local_tables.update({name: {"name": name, "kind": kind, "rows": size} for name, kind, size in rows})
        _local_tables_listed = True
    except duckdb.Error:
        # Keep only entries that still exist.
        for name in [n for n in _local_tables if relation_kind(quote_ident(n)) is None]:
            del _local_tables[name]
    return sorted(_local_tables.values(), key=lambda t: t["name"])


def note_local_table(name: str) -> None:
    kind = relation_kind(quote_ident(name))
    if kind is None:
        _local_tables.pop(name, None)
        return
    rows = row_count(quote_ident(name)) if kind == "table" else None
    _local_tables[name] = {"name": name, "kind": kind, "rows": rows}


def table_exists(table_name: str) -> bool:
    return relation_kind(_safe_identifier(table_name)) is not None


def _remote_reader(url: str) -> str:
    """Pick the DuckDB table function for a remote file from its extension."""
    path = url.split("?", 1)[0].lower()
    escaped = url.replace("'", "''")
    if path.endswith(".parquet"):
        return f"read_parquet('{escaped}')"
    if path.endswith((".json", ".jsonl", ".ndjson", ".json.gz")):
        return f"read_json_auto('{escaped}')"
    return f"read_csv_auto('{escaped}')"


def create_table_from_url(
    url: str,
    table_name: str,
    materialize: bool = False,
    row_limit: int | None = None,
) -> str:
    """Register a remote http(s) file as a DuckDB view or table.

    A view stores nothing locally but streams from the network on every query,
    and gives the block sampler nothing to skip. A table copies the rows (up to
    ``row_limit``) into the local DuckDB file so sampling behaves as for uploads.
    """
    conn = get_connection()
    safe_name = _safe_identifier(table_name)
    # httpfs is autoloaded by recent DuckDB builds; loading it explicitly gives a
    # clear error up front when it is missing.
    conn.execute("INSTALL httpfs; LOAD httpfs;")
    select = f"SELECT * FROM {_remote_reader(url)}"
    if row_limit is not None:
        select = f"{select} LIMIT {int(row_limit)}"
    kind = "TABLE" if materialize else "VIEW"
    # Drop whichever kind already holds the name: CREATE OR REPLACE VIEW cannot
    # replace a table (and vice versa), and DROP VIEW errors on a table.
    existing = relation_kind(safe_name)
    if existing:
        conn.execute(f"DROP {existing.upper()} {safe_name}")
    conn.execute(f"CREATE {kind} {safe_name} AS {select}")
    note_local_table(safe_name)
    return safe_name


def describe_table(table_name: str) -> list[dict[str, str]]:
    rows = get_connection().execute(f"DESCRIBE {_safe_identifier(table_name)}").fetchall()
    return [{"name": row[0], "type": row[1]} for row in rows]


# One to three dot-separated parts (table, schema.table, catalog.schema.table),
# each a plain identifier or a double-quoted one.
_RELATION_NAME = re.compile(
    r'^(?:[A-Za-z_][A-Za-z0-9_$]*|"(?:[^"]|"")+")(?:\.(?:[A-Za-z_][A-Za-z0-9_$]*|"(?:[^"]|"")+")){0,2}$'
)


def describe_relation(name: str) -> list[dict[str, str]]:
    """Columns of a possibly qualified table name, such as rfam.family."""
    if not _RELATION_NAME.match(name):
        raise ValueError(f"Not a table name: {name}")
    _prepare(name)
    rows = get_connection().execute(f"DESCRIBE {name}").fetchall()
    return [{"name": row[0], "type": row[1]} for row in rows]


# Block ("system") sampling keeps or drops whole 2048-row vectors. That is
# cheap on big local tables, but on a table of only a few vectors a 5% sample
# comes back either empty or as the whole table. Row-level ("bernoulli")
# sampling has no such cliff, and remote tables are fetched in full anyway.
_SYSTEM_SAMPLE_MIN_ROWS = 100 * 2048


def sample_method(relation: str) -> str:
    if "." in relation or relation_kind(relation) != "table":
        return "bernoulli"
    return "system" if row_count(relation) >= _SYSTEM_SAMPLE_MIN_ROWS else "bernoulli"


def execute_query(query: str) -> dict[str, Any]:
    conn = get_connection()
    _prepare(query)
    rows = conn.execute(query).fetchall()
    columns = [d[0] for d in conn.description]
    return {"columns": columns, "rows": rows}


def explain_query(query: str, analyze: bool = True) -> Any:
    conn = get_connection()
    explain_prefix = "EXPLAIN (ANALYZE, FORMAT JSON)" if analyze else "EXPLAIN (FORMAT JSON)"
    if analyze:
        # Only ANALYZE runs the query, so only it needs the data prepared.
        _prepare(query)
    return conn.execute(f"{explain_prefix} {query}").fetchall()


# Read the local tables now, before any remote database is attached.
list_local_tables()
