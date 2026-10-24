"""Suggest queries for a table by looking at its data.

A sample of up to 2,000 rows profiles each column: categories (a handful of
distinct values), measures (numeric, not identifiers), dates, and keys. The
suggestions are the questions those shapes invite: counts and averages by
category, overall totals, the top of a distribution, a trend over time, and
joins with tables in any connected source that share a key column.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from backend.db import duckdb as duckdb_db
from backend.db import mongo, samples
from backend.db.connections import list_connections, resolve_remote
from backend.db.duckdb import quote_ident, split_relation

PROFILE_ROWS = 2000
NUMERIC = re.compile(r"INT|DOUBLE|DECIMAL|FLOAT|REAL|NUMERIC|HUGEINT", re.I)
TEMPORAL = re.compile(r"DATE|TIMESTAMP", re.I)
ID_NAME = re.compile(r"(^|_)(id|key|acc|code|uuid|urs)$|^id$|key$|^index$", re.I)
CATEGORY_HINT = re.compile(
    r"type|status|category|region|country|segment|mode|method|kind|class|group|flag|payment|zone|borough|source|genre|rated",
    re.I,
)
MEASURE_HINT = re.compile(
    r"price|amount|total|fare|revenue|cost|sales|quantity|qty|score|value|size|length|distance|duration|weight|count|rating|^num_|_num$",
    re.I,
)
CATALOG_TTL = 600.0


@dataclass
class Profile:
    name: str
    type: str
    distinct: int
    nulls: float
    numeric: bool
    temporal: bool
    p90: float | None = None
    top: list[Any] = field(default_factory=list)
    rows: int = 0

    @property
    def key(self) -> bool:
        if ID_NAME.search(self.name):
            return True
        # An integer column whose sampled values are nearly all distinct is an
        # identifier whatever it is called (customers.cust, orders.number).
        integer = bool(re.search(r"INT", self.type, re.I))
        return integer and self.rows > 50 and self.distinct >= 0.95 * self.rows

    @property
    def category(self) -> bool:
        return not self.key and not self.temporal and 2 <= self.distinct <= 50 and self.nulls < 0.5

    @property
    def measure(self) -> bool:
        return self.numeric and not self.key and self.distinct > 10


def _sample_frame(relation: str) -> pd.DataFrame:
    """Up to PROFILE_ROWS rows of the table, from wherever is cheapest."""
    stored = samples.lookup(relation)
    conn = duckdb_db.get_connection()
    if stored is not None:
        return conn.execute(f"SELECT * FROM {stored.sample_table} USING SAMPLE {PROFILE_ROWS} ROWS").df()
    entry = mongo.lookup(relation)
    if entry is not None:
        total = entry.estimated_count()
        return mongo.fetch(entry, None, sample_size=PROFILE_ROWS if total > PROFILE_ROWS else None)
    remote = resolve_remote(relation)
    if remote is not None:
        # The first rows, not a random sample, but fetching a random one
        # would scan the table on the source.
        return conn.execute(remote.query_sql(f"SELECT * FROM {remote.source_name} LIMIT {PROFILE_ROWS}")).df()
    return conn.execute(f"SELECT * FROM {relation} USING SAMPLE {PROFILE_ROWS} ROWS").df()


def profile(relation: str, columns: list[dict[str, str]]) -> list[Profile]:
    frame = _sample_frame(relation)
    profiles = []
    for column in columns:
        name, type_ = column["name"], column["type"]
        if name not in frame.columns:
            continue
        values = frame[name]
        numeric = bool(NUMERIC.search(type_)) or pd.api.types.is_numeric_dtype(values)
        if pd.api.types.is_bool_dtype(values):
            numeric = False
        temporal = bool(TEMPORAL.search(type_)) or pd.api.types.is_datetime64_any_dtype(values)
        present = values.dropna()
        p90 = None
        if numeric and len(present):
            try:
                p90 = float(present.astype(float).quantile(0.9))
            except (TypeError, ValueError):
                numeric = False
        profiles.append(Profile(
            name=name,
            type=type_,
            distinct=int(present.astype(str).nunique()),
            nulls=1 - len(present) / max(len(values), 1),
            numeric=numeric,
            temporal=temporal,
            p90=p90,
            top=list(present.astype(str).value_counts().index[:3]),
            rows=len(present),
        ))
    return profiles


# ---------------------------------------------------------------------------
# column catalogs, for finding join partners
# ---------------------------------------------------------------------------

_catalogs: dict[str, tuple[float, dict[str, list[str]]]] = {}
_lock = threading.Lock()


def _source_catalog(connection) -> dict[str, list[str]]:
    """relation -> column names for every table of one connection (cached)."""
    with _lock:
        hit = _catalogs.get(connection.name)
        if hit and time.time() - hit[0] < CATALOG_TTL:
            return hit[1]
    catalog: dict[str, list[str]] = {}
    if connection.kind == "mongodb":
        for relation in connection.tables:
            entry = mongo.lookup(relation)
            if entry is not None:
                catalog[relation] = list(entry.paths)
    else:
        sample_relation = connection.tables[0] if connection.tables else None
        remote = resolve_remote(sample_relation) if sample_relation else None
        if remote is not None:
            function = "mysql_query" if remote.kind == "mysql" else "postgres_query"
            sql = (
                "SELECT table_name, column_name FROM information_schema.columns "
                f"WHERE table_schema = '{remote.schema}' ORDER BY table_name, ordinal_position"
            )
            rows = duckdb_db.get_connection().execute(
                f"SELECT * FROM {function}({duckdb_db.sql_literal(remote.alias)}, {duckdb_db.sql_literal(sql)})"
            ).fetchall()
            known = {split_relation(t)[-1].lower(): t for t in connection.tables}
            for table, column in rows:
                table, column = (v.decode() if isinstance(v, bytes) else v for v in (table, column))
                relation = known.get(table.lower())
                if relation:
                    catalog.setdefault(relation, []).append(column)
    with _lock:
        _catalogs[connection.name] = (time.time(), catalog)
    return catalog


def _all_catalogs() -> dict[str, list[str]]:
    catalog: dict[str, list[str]] = {}
    for table in duckdb_db.list_local_tables():
        try:
            catalog[table["name"]] = [c["name"] for c in duckdb_db.describe_relation(quote_ident(table["name"]))]
        except Exception:
            continue
    for connection in list_connections():
        try:
            catalog.update(_source_catalog(connection))
        except Exception:
            continue
    return catalog


def _source_of(relation: str) -> str:
    """The connection a table belongs to, or "local"."""
    parts = split_relation(relation)
    names = {c.name for c in list_connections()}
    return parts[0].lower() if len(parts) >= 2 and parts[0].lower() in names else "local"


def _key_stem(column: str) -> str:
    """l_orderkey and o_orderkey both become "orderkey"."""
    lowered = column.lower()
    return re.sub(r"^[a-z]{1,2}_", "", lowered)


# ---------------------------------------------------------------------------
# suggestions
# ---------------------------------------------------------------------------


def _label(name: str) -> str:
    return re.sub(r"^[a-z]{1,2}_", "", name).replace("_", " ")


def suggest(relation: str, columns: list[dict[str, str]], limit: int = 10) -> list[dict[str, str]]:
    profiles = profile(relation, columns)
    q = quote_ident
    categories = sorted(
        (p for p in profiles if p.category),
        key=lambda p: (not CATEGORY_HINT.search(p.name), abs(p.distinct - 8)),
    )
    # Prefer columns named like amounts, then the most continuous ones.
    measures = sorted((p for p in profiles if p.measure), key=lambda p: (not MEASURE_HINT.search(p.name), -p.distinct))
    dates = [p for p in profiles if p.temporal]
    out: list[dict[str, str]] = []

    def add(title: str, sql: str, why: str) -> None:
        if all(s["sql"] != sql for s in out):
            out.append({"title": title, "sql": sql, "why": why})

    if categories:
        c = categories[0]
        add(
            f"Rows by {_label(c.name)}",
            f"SELECT {q(c.name)}, COUNT(*)\nFROM {relation}\nGROUP BY {q(c.name)}\nORDER BY COUNT(*) DESC",
            f"{c.name} has {c.distinct} values in a sample (e.g. {', '.join(c.top[:3])}).",
        )
    if categories and measures:
        c, m = categories[0], measures[0]
        add(
            f"Average {_label(m.name)} by {_label(c.name)}",
            f"SELECT {q(c.name)}, COUNT(*), AVG({q(m.name)})\nFROM {relation}\nGROUP BY {q(c.name)}\nORDER BY AVG({q(m.name)}) DESC",
            f"Compares {m.name} across the {c.distinct} groups of {c.name}.",
        )
    if measures:
        m = measures[0]
        add(
            f"Total and average {_label(m.name)}",
            f"SELECT COUNT(*), SUM({q(m.name)}), AVG({q(m.name)})\nFROM {relation}",
            f"{m.name} is numeric with many distinct values.",
        )
        if m.p90 is not None:
            other = measures[1] if len(measures) > 1 else m
            add(
                f"The top 10% by {_label(m.name)}",
                f"SELECT COUNT(*), AVG({q(other.name)})\nFROM {relation}\nWHERE {q(m.name)} > {round(m.p90, 4)}",
                f"About 10% of sampled rows have {m.name} above {round(m.p90, 4)}.",
            )
    if len(categories) > 1 and measures:
        c1, c2, m = categories[0], categories[1], measures[0]
        add(
            f"{_label(m.name).capitalize()} by {_label(c1.name)} and {_label(c2.name)}",
            f"SELECT {q(c1.name)}, {q(c2.name)}, SUM({q(m.name)})\nFROM {relation}\nGROUP BY {q(c1.name)}, {q(c2.name)}",
            "Two categories together show where the totals concentrate.",
        )
    if dates:
        d = dates[0]
        bucket = f"date_trunc('month', {q(d.name)})"
        add(
            f"Rows per month of {_label(d.name)}",
            f"SELECT {bucket}, COUNT(*)\nFROM {relation}\nGROUP BY {bucket}\nORDER BY {bucket}",
            f"{d.name} is a date, so the data has a timeline.",
        )

    # Joins: tables anywhere that share a key column with this one.
    # Only columns named like keys: a numeric column that merely looks unique
    # in a sample (ecmli_cal_hits) is not a join key.
    keys = {(_key_stem(p.name), p.name) for p in profiles if ID_NAME.search(p.name) and p.distinct > 1}
    if keys:
        catalog = _all_catalogs()
        this = relation.lower()
        found = 0

        def partner_rank(item: tuple[str, list[str]]) -> tuple:
            # Real tables before staging ones (_annotated_file), tables with
            # something to group by first, then the shortest (most central) name.
            name, cols = item
            table = split_relation(name)[-1]
            has_category = any(CATEGORY_HINT.search(c) and not ID_NAME.search(c) for c in cols)
            return (table.startswith("_"), not has_category, len(table), name)

        for other, other_columns in sorted(catalog.items(), key=partner_rank):
            if other.lower() == this or found >= 2:
                continue
            for stem, mine in keys:
                partner = next((c for c in other_columns if _key_stem(c) == stem), None)
                if partner is None:
                    continue
                label = next(
                    (c for c in other_columns if CATEGORY_HINT.search(c) and not ID_NAME.search(c)),
                    None,
                )
                select = f"o.{q(label)}" if label else f"o.{q(partner)}"
                where = "another source" if _source_of(other) != _source_of(relation) else "the same source"
                add(
                    f"Join with {other} on {mine}",
                    # No LIMIT: an estimate's top 10 can differ from the true
                    # top 10, and every group should be comparable.
                    f"SELECT {select}, COUNT(*)\nFROM {relation} t\nJOIN {other} o ON o.{q(partner)} = t.{q(mine)}\n"
                    f"GROUP BY {select}\nORDER BY COUNT(*) DESC",
                    f"{other} ({where}) also has a {partner} column.",
                )
                found += 1
                break
    return out[:limit]
