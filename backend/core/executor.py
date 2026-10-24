from __future__ import annotations

from typing import Any

import pandas as pd

from backend.core.parser import AggregateSpec, ParsedQuery
from backend.db import duckdb as duckdb_db
from backend.db import mysql as mysql_db
from backend.db import postgres as postgres_db

# Sources whose engine can sample by skipping storage blocks rather than by
# scanning every row and discarding most of them.
PUSHDOWN_SOURCES = {"duckdb", "postgres"}


def _sample_clause(source: str, sample_fraction: float) -> str:
    percent = sample_fraction * 100.0
    if source == "postgres":
        return f"TABLESAMPLE SYSTEM ({percent:.4f})"
    return ""


def build_sample_query(parsed: ParsedQuery, source: str, sample_fraction: float) -> str:
    select_list = ", ".join(parsed.projection_columns) if parsed.projection_columns else "1 AS __aqp_count_marker"
    sample_clause = _sample_clause(source, sample_fraction)
    where_parts: list[str] = []
    if parsed.where_clause:
        where_parts.append(f"({parsed.where_clause})")
    if source == "duckdb":
        where_parts.append(f"(random() < {sample_fraction:.8f})")
    elif source == "mysql":
        where_parts.append(f"(RAND() < {sample_fraction:.8f})")

    if sample_clause:
        query = f"SELECT {select_list} FROM (SELECT * FROM {parsed.table} {sample_clause}) sampled_source"
    else:
        query = f"SELECT {select_list} FROM {parsed.table}"

    if where_parts:
        query = f"{query} WHERE {' AND '.join(where_parts)}"
    return query


def _execute_source_query(query: str, source: str) -> dict[str, Any]:
    if source == "duckdb":
        return duckdb_db.execute_query(query)
    if source == "postgres":
        return postgres_db.execute_query(query)
    if source == "mysql":
        return mysql_db.execute_query(query)
    raise ValueError(f"Unsupported source: {source}")


def fetch_sample_frame(parsed: ParsedQuery, source: str, sample_fraction: float) -> tuple[pd.DataFrame, float, str]:
    sql = build_sample_query(parsed, source, sample_fraction)
    payload = _execute_source_query(sql, source)
    frame = pd.DataFrame(payload.get("rows", []), columns=payload.get("columns", []))
    return frame, float(payload.get("time", 0.0)), sql


# ---------------------------------------------------------------------------
# Pushdown path
#
# The sampling loop above pulls raw rows into Python, wraps them in a
# DataFrame and re-aggregates them in a second DuckDB instance. That costs a
# flat overhead per rung that easily dwarfs the scan it is trying to avoid.
#
# When the source engine can both sample and aggregate itself we instead build
# a single statement that samples, filters, groups and rescales in one shot,
# and only the finished aggregate rows come back.
# ---------------------------------------------------------------------------


def _native_sample_source(parsed: ParsedQuery, source: str, sample_fraction: float) -> str:
    """A FROM-able subquery yielding a sample of the base table."""
    columns = ", ".join(parsed.projection_columns) if parsed.projection_columns else "*"
    percent = sample_fraction * 100.0

    if sample_fraction >= 1.0:
        # Sampling everything is strictly worse than not sampling at all.
        return f"(SELECT {columns} FROM {parsed.table})"
    if source == "duckdb":
        method = duckdb_db.sample_method(parsed.table)
        return f"(SELECT {columns} FROM {parsed.table} USING SAMPLE {percent:.6f}% ({method}))"
    if source == "postgres":
        return f"(SELECT {columns} FROM {parsed.table} TABLESAMPLE SYSTEM ({percent:.6f}))"
    raise ValueError(f"No native sampling for source: {source}")


def _scaled_aggregate_sql(aggregate: AggregateSpec, sample_fraction: float) -> str:
    """COUNT/SUM are scaled back up by 1/f; AVG is already scale-free."""
    expression = aggregate.expression.strip()
    inner = "COUNT(*)" if aggregate.is_count_star else f"{aggregate.func.upper()}({expression})"
    if aggregate.func in {"count", "sum"} and sample_fraction < 1.0:
        return f"({inner}) / {sample_fraction!r} AS {aggregate.alias}"
    return f"{inner} AS {aggregate.alias}"


def build_pushdown_query(parsed: ParsedQuery, source: str, sample_fraction: float) -> str:
    sampled_source = _native_sample_source(parsed, source, sample_fraction)

    select_parts: list[str] = list(parsed.group_by)
    select_parts.extend(
        _scaled_aggregate_sql(aggregate, sample_fraction) for aggregate in parsed.aggregates
    )

    sql = f"SELECT {', '.join(select_parts)} FROM {sampled_source} AS aqp_sample"
    # The filter runs after sampling: sampling f of the rows and then filtering
    # estimates the filtered aggregate just as well, and lets the sampler skip
    # blocks instead of being forced to evaluate the predicate over all of them.
    if parsed.where_clause:
        sql = f"{sql} WHERE {parsed.where_clause}"
    if parsed.group_by:
        sql = f"{sql} GROUP BY {', '.join(parsed.group_by)}"
    if parsed.order_by:
        order_parts = [
            f"{spec.key} {'DESC' if spec.descending else 'ASC'}" for spec in parsed.order_by
        ]
        sql = f"{sql} ORDER BY {', '.join(order_parts)}"
    if parsed.limit is not None:
        sql = f"{sql} LIMIT {parsed.limit}"
    return sql


def supports_pushdown(parsed: ParsedQuery, source: str) -> bool:
    return source in PUSHDOWN_SOURCES and bool(parsed.aggregates)


def fetch_pushdown_aggregate(
    parsed: ParsedQuery,
    source: str,
    sample_fraction: float,
) -> tuple[dict[str, Any], float, str]:
    """Run one sampling rung entirely inside the source engine.

    Returns the same payload shape as ``groupby_engine.aggregate_sample`` so the
    convergence loop cannot tell the two paths apart.
    """
    sql = build_pushdown_query(parsed, source, sample_fraction)
    payload = _execute_source_query(sql, source)
    rows = [tuple(row) for row in payload.get("rows", [])]
    columns = list(payload.get("columns", []))
    query_time = float(payload.get("time", 0.0))

    group_count = len(parsed.group_by)
    aliases = [aggregate.alias for aggregate in parsed.aggregates]

    if not parsed.group_by:
        if not rows:
            values: dict[str, Any] = {
                aggregate.alias: (0.0 if aggregate.func in {"count", "sum"} else None)
                for aggregate in parsed.aggregates
            }
            row = tuple(values[alias] for alias in aliases)
            return (
                {
                    "result": row[0] if len(row) == 1 else [row],
                    "rows": [row],
                    "columns": aliases,
                    "result_map": values,
                },
                query_time,
                sql,
            )
        first = rows[0]
        result_map = {alias: first[index] for index, alias in enumerate(aliases)}
        return (
            {
                "result": first[0] if len(first) == 1 else [first],
                "rows": [first],
                "columns": columns or aliases,
                "result_map": result_map,
            },
            query_time,
            sql,
        )

    result_map: dict[Any, dict[str, Any]] = {}
    for row in rows:
        key = row[0] if group_count == 1 else tuple(row[:group_count])
        result_map[key] = {
            alias: row[group_count + index] for index, alias in enumerate(aliases)
        }

    return (
        {
            "result": rows,
            "rows": rows,
            "columns": columns or [*parsed.group_by, *aliases],
            "result_map": result_map,
        },
        query_time,
        sql,
    )
