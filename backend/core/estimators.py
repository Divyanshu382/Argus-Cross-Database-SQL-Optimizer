"""Aggregate estimates with 95% confidence intervals, computed from a sample.

Every sample Argus takes is Poisson sampling: each sampling unit is kept
independently with probability f. A unit is either one row (Bernoulli
sampling, stored samples) or one block of 2048 consecutive rows (DuckDB's
block sampling keeps or drops whole row-id-aligned 2048-row chunks). For
per-unit totals N_c (non-null count) and S_c (sum), the Horvitz-Thompson
estimates and their unbiased variance estimates are

    COUNT  = sum(N_c) / f        Var = (1 - f) / f^2 * sum(N_c^2)
    SUM    = sum(S_c) / f        Var = (1 - f) / f^2 * sum(S_c^2)
    AVG    = R = sum(S_c) / sum(N_c)     (a ratio; variance by linearisation)
                                 Var ~ (1 - f) * sum((S_c - R N_c)^2) / sum(N_c)^2

Using blocks (not rows) as the units for a block sample matters: rows in a
block are correlated, and the row formulas would understate the error.

For a join, the unit is a row (or block) of the one sampled table: every
joined row comes from exactly one of its rows, so the same formulas hold, with
that row's joined output counted as its unit total.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from backend.core.parser import AggregateSpec, OrderBySpec
from backend.db import duckdb as duckdb_db

Z_95 = 1.959963984540054
_COLUMN_REF = re.compile(r'^(?:[A-Za-z_][A-Za-z0-9_]*\.)*("?)([A-Za-z_][A-Za-z0-9_ ]*)\1$')


@dataclass(frozen=True)
class EstimateQuery:
    """An aggregate query over a sampled FROM clause."""

    from_sql: str
    group_by: list[str]
    aggregates: list[AggregateSpec]
    where: str | None = None
    order_by: list[OrderBySpec] | None = None
    limit: int | None = None
    # Expression naming the sampling unit each row belongs to; None when each
    # row is its own unit.
    unit: str | None = None


def _output_name(expression: str) -> str:
    """f.type -> type; anything that is not a plain column keeps its text."""
    match = _COLUMN_REF.match(expression.strip())
    return match.group(2) if match else expression.strip()


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _normalise(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()


def build_estimate_sql(query: EstimateQuery, fraction: float) -> str:
    groups = [f"{expr} AS __g{i}" for i, expr in enumerate(query.group_by)]
    group_refs = [f"__g{i}" for i in range(len(query.group_by))]

    # Per-unit (or per-row) count of non-null values and sum, per aggregate.
    unit_columns: list[str] = []
    for i, aggregate in enumerate(query.aggregates):
        expr = aggregate.expression.strip()
        if query.unit is None:
            count = "1" if aggregate.is_count_star else f"CASE WHEN ({expr}) IS NULL THEN 0 ELSE 1 END"
            unit_columns.append(f"{count} AS __n{i}")
            if not aggregate.is_count_star and aggregate.func != "count":
                unit_columns.append(f"CAST({expr} AS DOUBLE) AS __s{i}")
        else:
            unit_columns.append(f"{'COUNT(*)' if aggregate.is_count_star else f'COUNT({expr})'} AS __n{i}")
            if not aggregate.is_count_star and aggregate.func != "count":
                unit_columns.append(f"SUM(CAST({expr} AS DOUBLE)) AS __s{i}")

    inner = f"SELECT {', '.join([*groups, *unit_columns])} FROM {query.from_sql}"
    if query.where:
        inner = f"{inner} WHERE {query.where}"
    if query.unit is not None:
        inner = f"{inner} GROUP BY {', '.join([*query.group_by, query.unit])}"

    # Estimates first (what the user sees), then the moments the confidence
    # intervals need.
    visible = [f"{ref} AS {_quote(_output_name(expr))}" for ref, expr in zip(group_refs, query.group_by)]
    moments: list[str] = []
    for i, aggregate in enumerate(query.aggregates):
        has_sum = not aggregate.is_count_star and aggregate.func != "count"
        if aggregate.func == "avg":
            visible.append(f"SUM(__s{i}) / NULLIF(SUM(__n{i}), 0) AS {aggregate.alias}")
        elif has_sum:
            visible.append(f"SUM(__s{i}) / {fraction!r} AS {aggregate.alias}")
        else:
            visible.append(f"SUM(__n{i}) / {fraction!r} AS {aggregate.alias}")
        moments.append(f"SUM(__n{i}) AS __sumn{i}")
        moments.append(f"SUM(CAST(__n{i} AS DOUBLE) * __n{i}) AS __nn{i}")
        # Third moment of the unit values, for the skewness check (Cochran).
        value = f"__s{i}" if has_sum else f"CAST(__n{i} AS DOUBLE)"
        moments.append(f"SUM({value} * {value} * {value}) AS __v3_{i}")
        if has_sum:
            moments.append(f"SUM(__s{i}) AS __sums{i}")
            moments.append(f"SUM(__s{i} * __s{i}) AS __ss{i}")
            moments.append(f"SUM(__s{i} * __n{i}) AS __sn{i}")

    moments.append("COUNT(*) AS __units")
    sql = f"SELECT {', '.join([*visible, *moments])} FROM ({inner}) AS aq_units"
    if group_refs:
        sql = f"{sql} GROUP BY {', '.join(group_refs)}"
    if query.order_by:
        sql = f"{sql} ORDER BY {', '.join(_order_term(query, spec) for spec in query.order_by)}"
    if query.limit is not None:
        sql = f"{sql} LIMIT {query.limit}"
    return sql


def _order_term(query: EstimateQuery, spec: OrderBySpec) -> str:
    key = _normalise(spec.key)
    target = spec.key
    for i, expr in enumerate(query.group_by):
        if key in (_normalise(expr), _normalise(_output_name(expr))):
            target = f"__g{i}"
    return f"{target} {'DESC' if spec.descending else 'ASC'}"


def _half_width(aggregate: AggregateSpec, i: int, row: dict[str, Any], fraction: float) -> float | None:
    if fraction >= 1.0:
        return 0.0
    k = (1.0 - fraction) / (fraction * fraction)
    sum_n = row.get(f"__sumn{i}") or 0
    if aggregate.func == "avg":
        sum_s = row.get(f"__sums{i}")
        if not sum_n or sum_s is None:
            return None
        ratio = sum_s / sum_n
        residual = row[f"__ss{i}"] - 2 * ratio * row[f"__sn{i}"] + ratio * ratio * row[f"__nn{i}"]
        variance = (1.0 - fraction) * residual / (sum_n * sum_n)
    elif aggregate.func == "sum":
        if row.get(f"__ss{i}") is None:
            return None
        variance = k * row[f"__ss{i}"]
    else:
        variance = k * (row.get(f"__nn{i}") or 0)
    return Z_95 * math.sqrt(max(variance, 0.0))


# Cochran's rule: the normal approximation behind the intervals needs about
# 25 * g1^2 sampled units, where g1 is the skewness of the unit values. Heavy
# tails (a few huge values) make intervals too narrow below that.
COCHRAN_FACTOR = 25.0


def _min_units(aggregate: AggregateSpec, i: int, row: dict[str, Any]) -> float | None:
    units = row.get("__units") or 0
    if units < 3:
        return None
    has_sum = not aggregate.is_count_star and aggregate.func != "count"
    total = row.get(f"__sums{i}") if has_sum else row.get(f"__sumn{i}")
    squares = row.get(f"__ss{i}") if has_sum else row.get(f"__nn{i}")
    cubes = row.get(f"__v3_{i}")
    if total is None or squares is None or cubes is None:
        return None
    mean = total / units
    second = squares / units - mean * mean
    if second <= 1e-12 * max(1.0, mean * mean):
        return None  # (near) constant values: no skew to worry about
    third = cubes / units - 3 * mean * squares / units + 2 * mean**3
    skew = third / second**1.5
    return COCHRAN_FACTOR * skew * skew


def fetch_estimate(query: EstimateQuery, fraction: float) -> tuple[dict[str, Any], float, str]:
    """Run one estimate. The payload matches executor.fetch_pushdown_aggregate,
    plus ``ci_rows`` (95% half-widths aligned with each row's aggregates),
    ``ci_map`` (the same, keyed like ``result_map``) and ``max_relative_ci``."""
    sql = build_estimate_sql(query, fraction)
    payload = duckdb_db.execute_query(sql)
    columns: list[str] = list(payload["columns"])
    group_count = len(query.group_by)
    aliases = [aggregate.alias for aggregate in query.aggregates]
    visible_count = group_count + len(aliases)

    rows: list[tuple[Any, ...]] = []
    ci_rows: list[tuple[float | None, ...]] = []
    relative: list[float] = []
    # Smallest sample fraction at which every group meets Cochran's rule.
    skew_fraction = 0.0
    for raw in payload["rows"]:
        named = dict(zip(columns, raw))
        values = tuple(raw[:visible_count])
        widths = tuple(
            _half_width(aggregate, i, named, fraction) for i, aggregate in enumerate(query.aggregates)
        )
        # An ungrouped query over an empty sample still returns one row of
        # COUNT = 0 / SUM = NULL; that is no estimate at all.
        if not group_count and not any(named.get(f"__sumn{i}") for i in range(len(aliases))):
            continue
        rows.append(values)
        ci_rows.append(widths)
        for i, aggregate in enumerate(query.aggregates):
            needed_units = _min_units(aggregate, i, named)
            if needed_units and needed_units > named["__units"]:
                skew_fraction = max(skew_fraction, fraction * needed_units / named["__units"])
        for value, width in zip(values[group_count:], widths):
            if width is not None and value not in (None, 0):
                relative.append(width / abs(float(value)))

    result_map: dict[Any, Any] = {}
    ci_map: dict[Any, Any] = {}
    if not group_count:
        if rows:
            result_map = dict(zip(aliases, rows[0]))
            ci_map = dict(zip(aliases, ci_rows[0]))
    else:
        for values, widths in zip(rows, ci_rows):
            key = values[0] if group_count == 1 else tuple(values[:group_count])
            result_map[key] = dict(zip(aliases, values[group_count:]))
            ci_map[key] = dict(zip(aliases, widths))

    return (
        {
            "result": rows if group_count or len(rows) != 1 or len(rows[0]) != 1 else rows[0][0],
            "rows": rows,
            "columns": columns[:visible_count],
            "result_map": result_map,
            "ci_rows": ci_rows,
            "ci_map": ci_map,
            "max_relative_ci": max(relative) if relative else math.inf,
            "skew_fraction": skew_fraction if fraction < 1.0 else 0.0,
        },
        float(payload.get("time", 0.0)),
        sql,
    )
