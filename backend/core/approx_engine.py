from __future__ import annotations

import re

from dataclasses import dataclass
from typing import Any, Callable

from backend.core.executor import (
    build_pushdown_query,
    build_sample_query,
    supports_pushdown,
)
from backend.core.federation import prepare_join_estimate
from backend.core.parser import parse_analytical_query
from backend.core.runtime_sampling import (
    MODE_CONFIGS,
    Rung,
    single_table_rung,
    required_stored_sample,
    run_runtime_sampling,
)


def _rewrite_agg_query(query: str, source: str, mode: str = "balanced") -> str:
    """The SQL the first sampling rung will actually run."""
    source_key = source.lower().strip()
    mode_key = mode if mode in MODE_CONFIGS else "balanced"
    parsed = parse_analytical_query(query)
    first_fraction = MODE_CONFIGS[mode_key]["progression"][0]
    if supports_pushdown(parsed, source_key):
        return build_pushdown_query(parsed, source_key, first_fraction)
    return build_sample_query(parsed, source_key, first_fraction)


@dataclass
class PreparedApprox:
    """A query checked for approximation, ready to run.

    ``group_count`` and ``aliases`` describe the result rows, so callers can
    line exact rows up with estimated ones.
    """

    group_count: int
    aliases: list[str]
    run: Callable[[str, float | None, Callable[[dict[str, Any]], None] | None], dict[str, Any]]
    # One sampling step at a given fraction (DuckDB only): what the cost
    # model's pilot runs before deciding (see backend.ml.features).
    rung: Rung | None = None
    stored: bool = False


def prepare_approx(query: str, source: str = "duckdb") -> PreparedApprox:
    """Raise ValueError now, before any work, if the query cannot be estimated."""
    source_key = source.lower().strip()
    try:
        parsed = parse_analytical_query(query)
    except ValueError as single_table_error:
        # The single-table parser is strict; joins (and anything else it
        # rejects) go through the SQL-parser-based planner, DuckDB only.
        if source_key != "duckdb":
            raise
        try:
            plan, run, rung, stored = prepare_join_estimate(query)
        except ValueError as join_error:
            if re.search(r"\bjoin\b", query, re.IGNORECASE):
                raise join_error from None
            raise single_table_error from None
        return PreparedApprox(len(plan.group_by), [a.alias for a in plan.aggregates], run, rung, stored)

    stored_sample = required_stored_sample(parsed.table, source_key)
    return PreparedApprox(
        len(parsed.group_by),
        [a.alias for a in parsed.aggregates],
        lambda mode, target, callback: run_runtime_sampling(
            parsed, source_key, mode, accuracy_target=target, progress_callback=callback
        ),
        single_table_rung(parsed, stored_sample) if source_key == "duckdb" else None,
        stored_sample is not None,
    )


def run_approx(
    query: str,
    source: str = "duckdb",
    mode: str = "balanced",
    accuracy_target: float | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    return prepare_approx(query, source).run(mode, accuracy_target, progress_callback)
