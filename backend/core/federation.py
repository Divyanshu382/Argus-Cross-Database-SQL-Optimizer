"""Query planning across connected databases, using sqlglot's SQL parser.

Two rewrites live here:

* ``push_whole_query`` sends a query whose tables all live in one connected
  database to that database, translated to its SQL dialect, so only result
  rows cross the network.
* ``plan_join_estimate`` estimates an aggregate over a join by sampling one
  table (the largest) and joining the sample with the other tables whole.
  Every joined row comes from exactly one row of the sampled table, so
  scaling COUNT and SUM by 1/f stays unbiased for inner joins of any shape.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

import sqlglot
from sqlglot import exp

from backend.core.estimators import EstimateQuery, fetch_estimate
from backend.core.parser import AggregateSpec, OrderBySpec
from backend.core.runtime_sampling import run_ladder, sampled_table_source
from backend.db import duckdb as duckdb_db
from backend.db import samples
from backend.core import mongo_query
from backend.db import mongo
from backend.db.connections import resolve_remote
from backend.db.duckdb import register_before_query

_AGGREGATES = {exp.Count: "count", exp.Sum: "sum", exp.Avg: "avg"}
_FACT_PLACEHOLDER = "__aq_sampled_table__"
WHOLE_JOIN_MAX_ROWS = 2_000_000


def _parse(query: str) -> exp.Expression:
    try:
        return sqlglot.parse_one(query.strip().rstrip(";"), read="duckdb")
    except sqlglot.errors.ParseError as exc:
        raise ValueError(f"Could not parse the query: {exc}") from exc


def _relation(table: exp.Table) -> str:
    """The table's name as DuckDB resolves it, without the alias."""
    return ".".join(part.sql(dialect="duckdb") for part in table.parts)


def _is_connected(relation: str) -> bool:
    return resolve_remote(relation) is not None or mongo.lookup(relation) is not None


def references_connected_database(query: str) -> bool:
    try:
        tree = _parse(query)
    except ValueError:
        return False
    return any(_is_connected(_relation(table)) for table in tree.find_all(exp.Table))


# ---------------------------------------------------------------------------
# MongoDB tables in DuckDB queries
# ---------------------------------------------------------------------------


def _conjuncts(node: exp.Expression | None) -> list[exp.Expression]:
    if node is None:
        return []
    if isinstance(node, exp.And):
        return [*_conjuncts(node.this), *_conjuncts(node.expression)]
    return [node.this if isinstance(node, exp.Paren) and not isinstance(node.this, exp.Or) else node]


def _direct_tables(select: exp.Select) -> list[exp.Table]:
    """The tables a SELECT reads in its own FROM and JOINs, not in subqueries."""
    from_ = select.args.get("from_") or select.args.get("from")
    sources = [from_.this] if from_ else []
    sources += [join.this for join in select.args.get("joins") or []]
    return [source for source in sources if isinstance(source, exp.Table)]


def _load_mongo_tables(sql: str) -> None:
    """Before DuckDB runs ``sql``, fetch the MongoDB collections it reads.

    Only the fields the query uses are fetched, and WHERE conditions on a
    single collection that MongoDB can evaluate are applied there; DuckDB
    re-applies them, so a condition that is not pushed is still enforced.
    """
    names = mongo.connection_names()
    if not names or not any(name in sql.lower() for name in names):
        return
    try:
        tree = _parse(sql)
    except ValueError:
        return
    tables = [(t, mongo.lookup(_relation(t))) for t in tree.find_all(exp.Table)]
    tables = [(t, entry) for t, entry in tables if entry is not None]
    if not tables:
        return
    all_tables = list(tree.find_all(exp.Table))
    occurrences: dict[str, int] = {}
    for _, entry in tables:
        occurrences[entry.relation] = occurrences.get(entry.relation, 0) + 1

    for table, entry in tables:
        alias = table.alias_or_name
        # SELECT * over this table, or alias.*, in a select list (not the *
        # of COUNT(*)). A bare * in a subquery over another table, such as
        # the sample a join estimate reads, does not need this one's fields.
        star = any(
            (isinstance(item, exp.Star) and any(source is table for source in _direct_tables(select)))
            or (isinstance(item, exp.Column) and isinstance(item.this, exp.Star) and item.table == alias)
            for select in tree.find_all(exp.Select)
            for item in select.expressions
        )
        if star or occurrences[entry.relation] > 1:
            columns = list(entry.paths)
        else:
            columns = []
            for column in tree.find_all(exp.Column):
                if column.table and column.table != alias:
                    continue
                if not column.table and len(all_tables) > 1 and entry.column(column.name) is None:
                    continue
                name = entry.column(column.name)
                if name and name not in columns:
                    columns.append(name)
            if not columns:
                # COUNT(*) and the like still need one row per document.
                columns = [next(iter(entry.paths))]

        match: dict[str, Any] = {}
        if occurrences[entry.relation] == 1 and tree.args.get("where"):
            translator = mongo_query._Translator(entry, alias)
            pushed = []
            for condition in _conjuncts(tree.args["where"].this):
                referenced = list(condition.find_all(exp.Column))
                if len(all_tables) > 1 and any(c.table != alias for c in referenced):
                    continue
                try:
                    pushed.append(translator.match(condition))
                except mongo_query.Untranslatable:
                    continue
            if pushed:
                match = pushed[0] if len(pushed) == 1 else {"$and": pushed}
        mongo.load(entry, mongo.fetch(entry, columns, match or None), columns)


register_before_query(_load_mongo_tables)


# ---------------------------------------------------------------------------
# whole-query pushdown
# ---------------------------------------------------------------------------


def push_whole_query(query: str) -> str | None:
    """DuckDB SQL running ``query`` on the one connected database holding all
    its tables, or None when the tables are local or spread over several."""
    try:
        tree = _parse(query)
    except ValueError:
        return None
    tables = list(tree.find_all(exp.Table))
    if not tables:
        return None
    remotes = [resolve_remote(_relation(table)) for table in tables]
    if any(remote is None for remote in remotes) or len({r.connection for r in remotes}) != 1:
        return None

    for table, remote in zip(tables, remotes):
        # Keep column references like full_region.bit_score working when
        # the table had no alias.
        alias = table.alias or remote.table
        table.replace(exp.table_(remote.table, db=remote.schema, alias=alias, quoted=True))
    try:
        translated = tree.sql(dialect=remotes[0].kind)
    except sqlglot.errors.UnsupportedError:
        return None
    return remotes[0].query_sql(translated)


# ---------------------------------------------------------------------------
# join estimates
# ---------------------------------------------------------------------------


@dataclass
class JoinPlan:
    tree: exp.Select
    tables: list[exp.Table]
    group_by: list[str]
    aggregates: list[AggregateSpec]
    where: str | None
    order_by: list[OrderBySpec] | None
    limit: int | None


def _alias_for(func: str, expression: str) -> str:
    if expression == "*":
        return "count_all"
    slug = re.sub(r"[^a-z0-9]+", "_", expression.lower()).strip("_")
    return f"{func}_{slug}"[:60]


def plan_join(query: str) -> JoinPlan:
    """Check the query is an aggregate over inner joins and pull it apart."""
    tree = _parse(query)
    if not isinstance(tree, exp.Select):
        raise ValueError("Only a single SELECT can be estimated.")
    for clause, label in (("with", "WITH"), ("having", "HAVING"), ("distinct", "DISTINCT")):
        if tree.args.get(clause):
            raise ValueError(f"{label} is not supported in approximate joins.")
    from_ = tree.args.get("from_") or tree.args.get("from")
    if from_ is None or not isinstance(from_.this, exp.Table):
        raise ValueError("The FROM clause must name tables, not subqueries.")
    for join in tree.args.get("joins") or []:
        if join.args.get("side") or (join.args.get("kind") or "").upper() not in ("", "INNER"):
            raise ValueError("Only inner joins can be estimated; outer joins run exactly.")
        if not isinstance(join.this, exp.Table):
            raise ValueError("Joins must name tables, not subqueries.")
    if tree.find(exp.Window):
        raise ValueError("Window functions are not supported in approximate joins.")

    group_by = [e.sql(dialect="duckdb") for e in (tree.args["group"].expressions if tree.args.get("group") else [])]
    aggregates: list[AggregateSpec] = []
    signatures: dict[str, str] = {}
    for item in tree.expressions:
        inner = item.unalias()
        func = next((name for kind, name in _AGGREGATES.items() if isinstance(inner, kind)), None)
        if func is None:
            if inner.find(exp.AggFunc):
                raise ValueError(
                    f"Only plain COUNT, SUM and AVG can be estimated, not {item.sql(dialect='duckdb')}."
                )
            if inner.sql(dialect="duckdb") not in group_by:
                raise ValueError(f"{item.sql(dialect='duckdb')} must be in GROUP BY.")
            continue
        argument = inner.this
        if isinstance(argument, exp.Distinct):
            raise ValueError("COUNT(DISTINCT ...) cannot be estimated from a sample.")
        expression = "*" if isinstance(argument, exp.Star) or argument is None else argument.sql(dialect="duckdb")
        alias = item.alias if isinstance(item, exp.Alias) else _alias_for(func, expression)
        aggregates.append(AggregateSpec(func=func, expression=expression, alias=alias))
        signatures[inner.sql(dialect="duckdb")] = alias
    if not aggregates:
        raise ValueError("Approximate mode needs at least one COUNT, SUM or AVG.")
    plain = [e.unalias().sql(dialect="duckdb") for e in tree.expressions if not isinstance(e.unalias(), tuple(_AGGREGATES))]
    if plain != group_by:
        raise ValueError("Non-aggregate SELECT columns must match GROUP BY columns in order.")

    order_by = None
    if tree.args.get("order"):
        order_by = []
        for ordered in tree.args["order"].expressions:
            key = ordered.this.sql(dialect="duckdb")
            order_by.append(OrderBySpec(key=signatures.get(key, key), descending=bool(ordered.args.get("desc"))))
    limit = None
    if tree.args.get("limit"):
        limit = int(tree.args["limit"].expression.this)

    where = tree.args["where"].this.sql(dialect="duckdb") if tree.args.get("where") else None
    return JoinPlan(tree, list(tree.find_all(exp.Table)), group_by, aggregates, where, order_by, limit)


def _is_base_table(relation: str) -> bool:
    return duckdb_db.relation_kind(relation) == "table"


def _local_rows(relation: str) -> int:
    return duckdb_db.row_count(relation) if _is_base_table(relation) else 0


def _choose_sampled_table(
    plan: JoinPlan,
) -> tuple[exp.Table, samples.StoredSample | None, dict[int, samples.StoredSample]]:
    """Pick the table to sample, and the local copies to use for the others.

    The largest table is sampled: it dominates the cost, and the others are
    joined whole. A stored partial sample is preferred, since sampling a
    connected database over the network saves nothing. Tables with a full
    local copy (fraction 1) are read from that copy instead of the network.
    """
    partial: list[tuple[float, exp.Table, samples.StoredSample]] = []
    copies: dict[int, samples.StoredSample] = {}
    for table in plan.tables:
        stored = samples.lookup(_relation(table))
        if stored is None:
            continue
        if stored.fraction >= 1.0:
            copies[id(table)] = stored
        else:
            partial.append((stored.sample_rows / stored.fraction, table, stored))
    if partial:
        _, table, stored = max(partial, key=lambda item: item[0])
        return table, stored, copies

    uncovered = [t for t in plan.tables if id(t) not in copies and _is_connected(_relation(t))]
    if uncovered:
        names = ", ".join(sorted({_relation(t) for t in uncovered}))
        raise ValueError(
            "Approximate joins sample one table and join the rest whole. Build a stored sample "
            f"of the largest table in this join (one of: {names}) in step 1, then run it again."
        )

    def local_rows(table: exp.Table) -> int:
        copy = copies.get(id(table))
        return copy.sample_rows if copy else _local_rows(_relation(table))

    return max(plan.tables, key=local_rows), None, copies


def prepare_join_estimate(query: str) -> tuple[JoinPlan, Callable[..., dict[str, Any]], Callable, bool]:
    plan = plan_join(query)
    sampled, stored, copies = _choose_sampled_table(plan)
    relation = _relation(sampled)
    alias = sampled.alias or sampled.name
    # When every table is local or has a full local copy and the data is
    # small, join it whole: exact against the copies, in milliseconds, with no
    # sampling error.
    whole = (
        stored is None
        and bool(copies)
        and not any(_is_connected(_relation(t)) and id(t) not in copies for t in plan.tables)
        and sum(copies[id(t)].sample_rows if id(t) in copies else _local_rows(_relation(t)) for t in plan.tables)
        <= WHOLE_JOIN_MAX_ROWS
    )
    if id(sampled) in copies:
        # Sample the local copy rather than the remote table.
        relation = copies[id(sampled)].sample_table

    # Swap the sampled table for a placeholder and the copied tables for
    # their copies, render the FROM clause, then put the sampling subquery
    # where the placeholder was. copy() keeps table order, so the original
    # and copied tables pair up by position.
    tree = plan.tree.copy()
    for original, table in zip(plan.tables, list(tree.find_all(exp.Table))):
        table_alias = table.alias or table.name
        if original is sampled:
            table.replace(exp.table_(_FACT_PLACEHOLDER, alias=table_alias))
        elif id(original) in copies:
            schema, name = copies[id(original)].sample_table.split(".", 1)
            table.replace(exp.table_(name.strip('"'), db=schema, alias=table_alias, quoted=True))
    from_ = tree.args.get("from_") or tree.args.get("from")
    from_template = " ".join(
        [from_.this.sql(dialect="duckdb"), *(j.sql(dialect="duckdb") for j in tree.args.get("joins") or [])]
    )
    unit_column = "__aq_unit"

    def rung(sample_fraction: float):
        if whole:
            item, unit, fraction = relation, None, 1.0
        elif stored is not None:
            item = f"(SELECT *, rowid AS {unit_column} FROM {stored.sample_table})"
            unit = f"{alias}.{unit_column}"
            fraction = stored.fraction
        else:
            item, unit = sampled_table_source(relation, "*", sample_fraction, alias, unit_column)
            # sampled_table_source aliases the item itself; the template
            # already carries the alias.
            item = item.rsplit(" AS ", 1)[0]
            if unit is None:
                # Joined rows from one sampled row are correlated; make the
                # row the unit when it has a row id (base tables do).
                if _is_base_table(relation):
                    item = item.replace("SELECT *", f"SELECT *, rowid AS {unit_column}", 1)
                    unit = f"{alias}.{unit_column}"
            fraction = sample_fraction
        from_sql = from_template.replace(_FACT_PLACEHOLDER, item, 1)
        estimate = EstimateQuery(
            from_sql=from_sql,
            group_by=plan.group_by,
            aggregates=plan.aggregates,
            where=plan.where,
            order_by=plan.order_by,
            limit=plan.limit,
            unit=unit,
        )
        payload, query_time, sql = fetch_estimate(estimate, fraction)
        return payload, query_time, sql, None, fraction

    def run(mode: str, accuracy_target: float | None, progress_callback=None) -> dict[str, Any]:
        result = run_ladder(
            rung,
            source="duckdb",
            mode=mode,
            accuracy_target=accuracy_target,
            progress_callback=progress_callback,
            stored=stored,
            single_pass="local_copies" if whole else None,
        )
        result["sampled_table"] = None if whole else _relation(sampled)
        result["local_copies"] = sorted(_relation(t) for t in plan.tables if id(t) in copies)
        return result

    return plan, run, rung, stored is not None
