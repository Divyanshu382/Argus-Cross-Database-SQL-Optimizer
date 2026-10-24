"""Translate SQL over one MongoDB collection into an aggregation pipeline.

Covered: WHERE with comparisons, BETWEEN, IN, IS [NOT] NULL, [I]LIKE, AND/OR;
GROUP BY columns; COUNT(*), COUNT(x), COUNT(DISTINCT x), SUM, AVG, MIN, MAX
over columns and + - * / arithmetic; ORDER BY; LIMIT; and plain column
SELECTs. Anything else raises Untranslatable, and the query runs in DuckDB
over fields fetched from MongoDB instead.

SQL's NULL rules are kept where MongoDB's differ: ``x <> 5`` must not match
documents where x is missing or null, so it becomes ``$nin: [5, null]``, and
COUNT(x) counts only values above null in BSON order.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

import sqlglot
from sqlglot import exp

from backend.db import mongo


class Untranslatable(Exception):
    pass


_COMPARISONS = {exp.EQ: "$eq", exp.NEQ: "$ne", exp.GT: "$gt", exp.GTE: "$gte", exp.LT: "$lt", exp.LTE: "$lte"}
_FLIPPED = {"$gt": "$lt", "$gte": "$lte", "$lt": "$gt", "$lte": "$gte", "$eq": "$eq", "$ne": "$ne"}
_ARITHMETIC = {exp.Add: "$add", exp.Sub: "$subtract", exp.Mul: "$multiply"}
_ACCUMULATORS = {exp.Sum: "$sum", exp.Avg: "$avg", exp.Min: "$min", exp.Max: "$max"}


class _Translator:
    def __init__(self, entry: mongo.MongoCollection, alias: str):
        self.entry = entry
        self.alias = alias

    # -- columns and values -------------------------------------------------

    def column(self, node: exp.Expression) -> str:
        """The flattened column a Column node names."""
        if not isinstance(node, exp.Column):
            raise Untranslatable(node.sql())
        if node.table and node.table not in (self.alias, self.entry.name):
            raise Untranslatable(f"column of another table: {node.sql()}")
        column = self.entry.column(node.name)
        if column is None:
            raise Untranslatable(f"unknown column {node.name}")
        return column

    def path(self, node: exp.Expression) -> str:
        return self.entry.paths[self.column(node)]

    def literal(self, node: exp.Expression, column: str | None = None) -> Any:
        if isinstance(node, exp.Paren):
            return self.literal(node.this, column)
        if isinstance(node, exp.Neg):
            return -self.literal(node.this, column)
        if isinstance(node, exp.Null):
            return None
        if isinstance(node, exp.Boolean):
            return node.this
        if isinstance(node, exp.Cast) and isinstance(node.this, exp.Literal):
            return self._typed(node.this.this, node.to.sql().upper())
        if isinstance(node, exp.Literal):
            if node.is_string:
                column_type = self.entry.types.get(column or "", "")
                return self._typed(node.this, column_type) if "TIMESTAMP" in column_type or column_type == "DATE" else node.this
            number = node.this
            return float(number) if any(ch in number for ch in ".eE") else int(number)
        raise Untranslatable(f"not a constant: {node.sql()}")

    @staticmethod
    def _typed(text: str, type_name: str) -> Any:
        if "TIMESTAMP" in type_name or type_name in ("DATE", "DATETIME"):
            try:
                return datetime.fromisoformat(text)
            except ValueError as exc:
                raise Untranslatable(f"not a date: {text}") from exc
        return text

    def value_expression(self, node: exp.Expression) -> Any:
        """An aggregation-expression for column arithmetic."""
        if isinstance(node, exp.Paren):
            return self.value_expression(node.this)
        if isinstance(node, exp.Column):
            return "$" + self.path(node)
        if isinstance(node, (exp.Literal, exp.Neg, exp.Null, exp.Boolean)):
            return self.literal(node)
        for kind, operator in _ARITHMETIC.items():
            if isinstance(node, kind):
                return {operator: [self.value_expression(node.this), self.value_expression(node.expression)]}
        if isinstance(node, exp.Div):
            numerator = self.value_expression(node.this)
            denominator = self.value_expression(node.expression)
            # SQL division by zero is NULL; MongoDB's $divide raises.
            return {"$cond": [{"$eq": [denominator, 0]}, None, {"$divide": [numerator, denominator]}]}
        raise Untranslatable(f"expression {node.sql()}")

    # -- WHERE --------------------------------------------------------------

    def match(self, node: exp.Expression) -> dict[str, Any]:
        if isinstance(node, exp.Paren):
            return self.match(node.this)
        if isinstance(node, exp.And):
            return {"$and": [self.match(node.this), self.match(node.expression)]}
        if isinstance(node, exp.Or):
            return {"$or": [self.match(node.this), self.match(node.expression)]}
        for kind, operator in _COMPARISONS.items():
            if isinstance(node, kind):
                return self._comparison(node.this, node.expression, operator)
        if isinstance(node, exp.Between):
            column = self.column(node.this)
            return {self.entry.paths[column]: {
                "$gte": self.literal(node.args["low"], column),
                "$lte": self.literal(node.args["high"], column),
            }}
        if isinstance(node, exp.In) and not node.args.get("query"):
            column = self.column(node.this)
            return {self.entry.paths[column]: {"$in": [self.literal(v, column) for v in node.expressions]}}
        if isinstance(node, exp.Is) and isinstance(node.expression, exp.Null):
            return {self.path(node.this): None}  # null or missing, as SQL sees it
        if isinstance(node, (exp.Like, exp.ILike)):
            return {self.path(node.this): self._regex(node)}
        if isinstance(node, exp.Not):
            inner = node.this.this if isinstance(node.this, exp.Paren) else node.this
            if isinstance(inner, exp.Is) and isinstance(inner.expression, exp.Null):
                return {self.path(inner.this): {"$ne": None}}
            if isinstance(inner, exp.In) and not inner.args.get("query"):
                column = self.column(inner.this)
                return {self.entry.paths[column]: {"$nin": [*(self.literal(v, column) for v in inner.expressions), None]}}
        raise Untranslatable(f"condition {node.sql()}")

    def _comparison(self, left: exp.Expression, right: exp.Expression, operator: str) -> dict[str, Any]:
        if isinstance(right, exp.Column) and not isinstance(left, exp.Column):
            left, right, operator = right, left, _FLIPPED[operator]
        if isinstance(left, exp.Column) and not isinstance(right, exp.Column):
            column = self.column(left)
            value = self.literal(right, column)
            if value is None:
                raise Untranslatable("comparison with NULL")  # always unknown in SQL
            path = self.entry.paths[column]
            if operator == "$eq":
                return {path: value}
            if operator == "$ne":
                return {path: {"$nin": [value, None]}}
            return {path: {operator: value}}
        # Column against column: compare inside $expr, excluding NULLs as SQL does.
        a, b = self.value_expression(left), self.value_expression(right)
        return {"$expr": {"$and": [{operator: [a, b]}, {"$gt": [a, None]}, {"$gt": [b, None]}]}}

    def _regex(self, node: exp.Expression) -> dict[str, Any]:
        pattern = node.expression
        if not (isinstance(pattern, exp.Literal) and pattern.is_string) or node.args.get("escape"):
            raise Untranslatable(f"pattern {node.sql()}")
        regex = "".join(".*" if ch == "%" else "." if ch == "_" else re.escape(ch) for ch in pattern.this)
        result: dict[str, Any] = {"$regex": f"^{regex}$"}
        if isinstance(node, exp.ILike):
            result["$options"] = "i"
        return result


def _aggregate_accumulator(translator: _Translator, node: exp.Expression) -> tuple[dict[str, Any], bool]:
    """(accumulator, needs_distinct_size) for one aggregate call."""
    if isinstance(node, exp.Count):
        argument = node.this
        if isinstance(argument, exp.Star) or argument is None:
            return {"$sum": 1}, False
        if isinstance(argument, exp.Distinct):
            if len(argument.expressions) != 1:
                raise Untranslatable("COUNT(DISTINCT) of several columns")
            return {"$addToSet": translator.value_expression(argument.expressions[0])}, True
        value = translator.value_expression(argument)
        return {"$sum": {"$cond": [{"$gt": [value, None]}, 1, 0]}}, False
    for kind, operator in _ACCUMULATORS.items():
        if isinstance(node, kind):
            return {operator: translator.value_expression(node.this)}, False
    raise Untranslatable(f"aggregate {node.sql()}")


def translate(query: str) -> tuple[mongo.MongoCollection, list[dict[str, Any]], list[str]] | None:
    """(collection, pipeline, output column names), or None when the query
    is not over a single MongoDB collection. Raises Untranslatable."""
    try:
        tree = sqlglot.parse_one(query.strip().rstrip(";"), read="duckdb")
    except sqlglot.errors.ParseError:
        return None
    if not isinstance(tree, exp.Select):
        return None
    from_ = tree.args.get("from_") or tree.args.get("from")
    if from_ is None or not isinstance(from_.this, exp.Table):
        return None
    table = from_.this
    entry = mongo.lookup(".".join(part.name for part in table.parts))
    if entry is None:
        return None
    if tree.args.get("joins") or any(tree.args.get(k) for k in ("with", "having", "distinct")):
        raise Untranslatable("joins, WITH, HAVING and DISTINCT run in DuckDB")
    if tree.find(exp.Window) or tree.find(exp.Subquery):
        raise Untranslatable("window functions and subqueries run in DuckDB")

    translator = _Translator(entry, table.alias_or_name)
    pipeline: list[dict[str, Any]] = []
    if tree.args.get("where"):
        pipeline.append({"$match": translator.match(tree.args["where"].this)})

    items = tree.expressions
    names = [item.alias_or_name if isinstance(item, (exp.Alias, exp.Column)) else item.sql(dialect="duckdb") for item in items]
    fields = [f"f{i}" for i in range(len(items))]
    group_nodes = tree.args["group"].expressions if tree.args.get("group") else []
    is_aggregate = bool(group_nodes) or any(item.find(exp.AggFunc) for item in items)

    if is_aggregate:
        group_sql = [g.sql(dialect="duckdb") for g in group_nodes]
        group_id = {f"g{i}": translator.value_expression(g) for i, g in enumerate(group_nodes)} or None
        accumulators: dict[str, Any] = {}
        project: dict[str, Any] = {"_id": 0}
        for field, item in zip(fields, items):
            inner = item.unalias()
            if inner.sql(dialect="duckdb") in group_sql:
                project[field] = f"$_id.g{group_sql.index(inner.sql(dialect='duckdb'))}"
                continue
            if not isinstance(inner, exp.AggFunc):
                raise Untranslatable(f"{item.sql()} is neither grouped nor a plain aggregate")
            accumulator, distinct = _aggregate_accumulator(translator, inner)
            accumulators[field] = accumulator
            if distinct:
                # COUNT(DISTINCT x) ignores NULL, which $addToSet keeps.
                project[field] = {"$size": {"$filter": {"input": f"${field}", "cond": {"$ne": ["$$this", None]}}}}
            else:
                project[field] = f"${field}"
        pipeline.append({"$group": {"_id": group_id, **accumulators}})
        pipeline.append({"$project": project})
    else:
        if any(isinstance(item, exp.Star) for item in items):
            names = list(entry.paths)
            items = [exp.column(name) for name in names]
            fields = [f"f{i}" for i in range(len(names))]
            pipeline.append({"$project": {"_id": 0, **{f: "$" + entry.paths[n] for f, n in zip(fields, names)}}})
        else:
            project = {"_id": 0}
            for field, item in zip(fields, items):
                project[field] = translator.value_expression(item.unalias())
            pipeline.append({"$project": project})

    if tree.args.get("order"):
        sort: dict[str, int] = {}
        for ordered in tree.args["order"].expressions:
            key = ordered.this.sql(dialect="duckdb")
            match = next(
                (f for f, item, n in zip(fields, items, names) if key in (item.unalias().sql(dialect="duckdb"), n)),
                None,
            )
            if match is None:
                raise Untranslatable(f"ORDER BY {key} is not a selected column")
            sort[match] = -1 if ordered.args.get("desc") else 1
        pipeline.append({"$sort": sort})
    if tree.args.get("limit"):
        pipeline.append({"$limit": int(tree.args["limit"].expression.this)})
    return entry, pipeline, names


def describe(entry: mongo.MongoCollection, pipeline: list[dict[str, Any]]) -> str:
    """The pipeline as MongoDB shell code, for showing what ran."""
    return f"db.{entry.name}.aggregate({json.dumps(pipeline, default=str)})"


def run(query: str) -> dict[str, Any] | None:
    """Run ``query`` inside MongoDB when it is over one collection and
    translatable; None otherwise (the caller runs it in DuckDB)."""
    try:
        translated = translate(query)
    except Untranslatable:
        return None
    if translated is None:
        return None
    entry, pipeline, names = translated
    fields = [f"f{i}" for i in range(len(names))]
    documents = entry.collection().aggregate(pipeline, allowDiskUse=True)
    rows = [tuple(mongo.plain(doc.get(field)) for field in fields) for doc in documents]
    return {"rows": rows, "columns": names, "pipeline": describe(entry, pipeline)}
