"""Live MongoDB collections, queried through DuckDB.

Nothing is copied when a connection is made. Each collection is registered
with a schema inferred from a small random sample, and an empty stub table of
that shape in an in-memory catalog named after the connection, so DuckDB can
bind queries against it. Then, per query:

* SQL over one collection that MongoDB can express (filters, GROUP BY,
  COUNT/SUM/AVG/MIN/MAX, ORDER BY, LIMIT) is translated to an aggregation
  pipeline and runs inside MongoDB (backend.core.mongo_query);
* anything else, such as a join, fetches only the fields the query uses, with
  the filters MongoDB can apply, into the stub table just before the query
  runs (backend.core.federation);
* approximate queries fetch a random $sample, which MongoDB serves with a
  random cursor instead of a scan when it is under 5% of the collection.

Nested fields are flattened with "_" (customer.city -> customer_city) and
arrays become JSON text.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import pandas as pd

from backend.db.duckdb import get_connection, quote_ident, split_relation

SCHEMA_SAMPLE = 1000


@dataclass
class MongoCollection:
    connection: str
    database: str
    name: str
    # Flattened column name -> dotted document path, in column order.
    paths: dict[str, str]
    # Flattened column name -> DuckDB type.
    types: dict[str, str]

    @property
    def relation(self) -> str:
        return f"{self.connection}.{quote_ident(self.name)}"

    @property
    def stub(self) -> str:
        return f"{self.connection}.main.{quote_ident(self.name)}"

    def collection(self) -> Any:
        return _clients[self.connection][self.database][self.name]

    def estimated_count(self) -> int:
        return int(self.collection().estimated_document_count())

    def column(self, name: str) -> str | None:
        """The column a SQL identifier refers to (case-insensitive fallback)."""
        if name in self.paths:
            return name
        lowered = {column.lower(): column for column in self.paths}
        return lowered.get(name.lower())


_clients: dict[str, Any] = {}
_collections: dict[str, dict[str, MongoCollection]] = {}
_lock = threading.RLock()


# ---------------------------------------------------------------------------
# documents -> rows
# ---------------------------------------------------------------------------


def plain(value: Any) -> Any:
    """A BSON value as something a DataFrame column can hold."""
    if isinstance(value, list):
        return json.dumps(value, default=str)
    if isinstance(value, dict):
        return json.dumps(value, default=str)
    if value is None or isinstance(value, (str, int, float, bool, datetime, date)):
        return value
    if type(value).__name__ == "Decimal128":
        return float(value.to_decimal())
    return str(value)  # ObjectId, Binary, ...


def flatten_document(document: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """A row of flattened columns, and each column's dotted path."""
    row: dict[str, Any] = {}
    paths: dict[str, str] = {}

    def walk(node: dict[str, Any], prefix: tuple[str, ...]) -> None:
        for key, value in node.items():
            path = (*prefix, str(key))
            if isinstance(value, dict) and value:
                walk(value, path)
            else:
                column = "_".join(path)
                row[column] = plain(value)
                paths.setdefault(column, ".".join(path))

    walk(document, ())
    return row, paths


def frame_from_documents(documents: list[dict[str, Any]], columns: list[str] | None = None) -> pd.DataFrame:
    """Rows of flattened documents. A field whose type varies between
    documents becomes text, since a column needs one type."""
    rows = [flatten_document(doc)[0] for doc in documents]
    frame = pd.DataFrame.from_records(rows, columns=columns) if rows else pd.DataFrame(columns=columns or [])
    for column in frame.columns:
        values = frame[column].dropna()
        if len({type(value) for value in values}) > 1:
            frame[column] = frame[column].map(lambda v: None if pd.isna(v) else str(v))
    return frame


def flatten_documents(documents: list[dict[str, Any]]) -> pd.DataFrame:
    return frame_from_documents(documents)


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


def connect(name: str, client: Any, database_name: str) -> tuple[list[str], str]:
    """Register every collection of ``database_name``; returns (tables, note)."""
    database = client[database_name]
    conn = get_connection()
    conn.execute(f"ATTACH ':memory:' AS {name}")
    registered: dict[str, MongoCollection] = {}
    try:
        for collection_name in sorted(database.list_collection_names()):
            if collection_name.startswith("system."):
                continue
            collection = database[collection_name]
            if collection.estimated_document_count() > SCHEMA_SAMPLE:
                documents = list(collection.aggregate([{"$sample": {"size": SCHEMA_SAMPLE}}]))
            else:
                documents = list(collection.find({}))
            if not documents:
                continue
            paths: dict[str, str] = {}
            for document in documents:
                for column, path in flatten_document(document)[1].items():
                    paths.setdefault(column, path)
            frame = frame_from_documents(documents, list(paths))
            entry = MongoCollection(name, database_name, collection_name, paths, {})
            _write(conn, entry.stub, frame, list(paths), None, temporary=False)
            entry.types = {
                row[0]: row[1] for row in conn.execute(f"DESCRIBE {entry.stub}").fetchall()
            }
            # Keep the stub empty: data arrives per query.
            conn.execute(f"DELETE FROM {entry.stub}")
            registered[collection_name] = entry
    except Exception:
        conn.execute(f"DETACH DATABASE IF EXISTS {name}")
        raise
    with _lock:
        _clients[name] = client
        _collections[name] = registered
    note = (
        f"Queried live in MongoDB database '{database_name}': aggregates over one collection run "
        "inside MongoDB, other queries fetch only the fields they use. Column types were "
        f"inferred from up to {SCHEMA_SAMPLE:,} sampled documents per collection."
    )
    return [entry.relation for entry in registered.values()], note


def disconnect(name: str) -> None:
    with _lock:
        _collections.pop(name, None)
        client = _clients.pop(name, None)
    if client is not None:
        client.close()


def connection_names() -> list[str]:
    return list(_collections)


def lookup(relation: str) -> MongoCollection | None:
    parts = split_relation(relation)
    if len(parts) not in (2, 3):
        return None
    collections = _collections.get(parts[0].lower())
    if not collections:
        return None
    name = parts[-1]
    if name in collections:
        return collections[name]
    return next((c for key, c in collections.items() if key.lower() == name.lower()), None)


# ---------------------------------------------------------------------------
# fetching
# ---------------------------------------------------------------------------


def fetch(
    entry: MongoCollection,
    columns: list[str] | None,
    match: dict[str, Any] | None = None,
    sample_size: int | None = None,
) -> pd.DataFrame:
    """Documents as rows of ``columns`` (all when None).

    With ``sample_size``, a random $sample of that many documents is taken
    first and ``match`` applied to it.
    """
    columns = columns or list(entry.paths)
    projection: dict[str, Any] = {entry.paths[c]: 1 for c in columns}
    if "_id" not in columns:
        projection["_id"] = 0
    pipeline: list[dict[str, Any]] = []
    if sample_size is not None:
        pipeline.append({"$sample": {"size": int(sample_size)}})
    if match:
        pipeline.append({"$match": match})
    pipeline.append({"$project": projection})
    documents = list(entry.collection().aggregate(pipeline, allowDiskUse=True))
    return frame_from_documents(documents, columns)


def _write(conn: Any, target: str, frame: pd.DataFrame, columns: list[str], types: dict[str, str] | None, temporary: bool) -> None:
    view = "__aq_mongo_frame"
    conn.register(view, frame)
    try:
        if types:
            # Keep the registered types even when a fetch sees only NULLs.
            select = ", ".join(
                f"TRY_CAST({quote_ident(c)} AS {types.get(c, 'VARCHAR')}) AS {quote_ident(c)}" for c in columns
            )
        else:
            select = "*"
        kind = "TEMP TABLE" if temporary else "TABLE"
        conn.execute(f"CREATE OR REPLACE {kind} {target} AS SELECT {select} FROM {view}")
    finally:
        conn.unregister(view)


def load(entry: MongoCollection, frame: pd.DataFrame, columns: list[str], target: str | None = None, temporary: bool = False) -> str:
    """Write fetched rows into DuckDB (the collection's stub by default)."""
    target = target or entry.stub
    _write(get_connection(), target, frame, columns, entry.types, temporary)
    return target
