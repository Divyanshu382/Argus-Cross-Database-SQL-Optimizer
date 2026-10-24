import json
from typing import Any


def clean_explain_output(text: str) -> list[str]:
    cleaned = []
    for line in text.split("\n"):
        for ch in ["┌", "┐", "└", "┘", "│", "─", "┬", "┴"]:
            line = line.replace(ch, "")
        line = line.strip()
        if line:
            cleaned.append(line)
    return cleaned


def _make_node(op_type: str) -> dict[str, Any]:
    return {
        "type": op_type,
        "columns": [],
        "aggregates": [],
        "rows": None,
        "children": [],
    }


def _assemble_tree(nodes: list[dict[str, Any]]) -> dict[str, Any] | None:
    root = None
    last_projection = None
    for node in nodes:
        if node["type"] == "UNGROUPED_AGGREGATE":
            root = node
        elif node["type"] == "PROJECTION":
            if root:
                root["children"].append(node)
            last_projection = node
        elif node["type"] in ("SEQ_SCAN", "READ_CSV_AUTO"):
            if last_projection:
                last_projection["children"].append(node)
    return root


def _build_operator_tree(lines: list[str]) -> dict[str, Any] | None:
    ops = []
    current = None

    for line in lines:
        if "UNGROUPED_AGGREGATE" in line:
            current = _make_node("UNGROUPED_AGGREGATE")
            ops.append(current)
        elif "PROJECTION" in line:
            current = _make_node("PROJECTION")
            ops.append(current)
        elif "SEQ_SCAN" in line:
            current = _make_node("SEQ_SCAN")
            ops.append(current)
        elif "READ_CSV_AUTO" in line:
            current = _make_node("READ_CSV_AUTO")
            ops.append(current)

        if current is None:
            continue

        if "Aggregates:" in line:
            current["aggregates"] = [line.replace("Aggregates:", "").strip()]

        if "rows" in line and "~" in line:
            try:
                num = line.split("~")[1].split("rows")[0].strip()
                current["rows"] = int(num)
            except Exception:
                pass

    return _assemble_tree(ops)


def explain_tree(node: dict[str, Any] | None) -> str:
    if not node:
        return "Could not parse query plan."

    t = node["type"]
    if t == "UNGROUPED_AGGREGATE":
        return f"Computes aggregate: {', '.join(node['aggregates'])}."
    if t == "PROJECTION":
        return f"Selects columns: {', '.join(node['columns'])}."
    if t == "SEQ_SCAN":
        return f"Sequential scan over table (~{node['rows']} rows)."
    return f"Executes operator: {t}."


def _from_postgres_node(node: dict[str, Any]) -> dict[str, Any]:
    node_type = str(node.get("Node Type", "UNKNOWN")).upper().replace(" ", "_")
    children = [_from_postgres_node(child) for child in node.get("Plans", [])]

    columns: list[str] = []
    output = node.get("Output")
    if isinstance(output, list):
        columns = [str(v) for v in output]

    aggregates: list[str] = []
    if "Group Key" in node and isinstance(node["Group Key"], list):
        aggregates = ["GROUP BY " + ", ".join(str(v) for v in node["Group Key"])]

    rows = node.get("Actual Rows", node.get("Plan Rows"))
    try:
        rows = int(rows) if rows is not None else None
    except Exception:
        rows = None

    return {
        "type": node_type,
        "columns": columns,
        "aggregates": aggregates,
        "rows": rows,
        "children": children,
    }


def _as_list(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    return [str(v) for v in value] if isinstance(value, list) else [str(value)]


def _from_duckdb_node(node: dict[str, Any]) -> dict[str, Any] | None:
    """Convert a DuckDB EXPLAIN (FORMAT JSON) node, with or without ANALYZE."""
    name = node.get("operator_name") or node.get("operator_type") or node.get("name")
    children = node.get("children") or []
    if not name or name == "EXPLAIN_ANALYZE":
        # The ANALYZE wrapper objects carry no operator of their own.
        return _from_duckdb_node(children[0]) if children else None

    extra = node.get("extra_info") or {}
    columns: list[str] = []
    aggregates: list[str] = []
    if "Table" in extra:
        columns.append(str(extra["Table"]).split(".")[-1])
    if "Join Type" in extra:
        columns.append(f"{extra['Join Type']} ON {', '.join(_as_list(extra.get('Conditions')))}")
    for key in ("Expression", "Filters", "Groups"):
        columns.extend(_as_list(extra.get(key)))
    columns.extend(
        # DuckDB's internal compression helpers are noise to a reader.
        p for p in _as_list(extra.get("Projections")) if not p.startswith("__internal")
    )
    aggregates.extend(_as_list(extra.get("Aggregates")))

    rows = node.get("operator_cardinality")
    if rows is None:
        try:
            rows = int(extra.get("Estimated Cardinality"))
        except (TypeError, ValueError):
            rows = None
    timing = node.get("operator_timing")

    converted: dict[str, Any] = {
        "type": str(name).upper().replace(" ", "_"),
        "columns": columns,
        "aggregates": aggregates,
        "rows": rows,
        "children": [c for c in (_from_duckdb_node(child) for child in children) if c is not None],
    }
    if timing is not None:
        converted["time"] = float(timing) * 1000.0
    return converted


def explain_duckdb_tree(node: dict[str, Any] | None) -> str:
    if not node:
        return "Could not parse query plan."
    operators: list[str] = []

    def walk(n: dict[str, Any]) -> None:
        for child in n.get("children", []):
            walk(child)
        operators.append(n["type"].replace("_", " ").lower())

    walk(node)
    return "Runs bottom-up: " + " → ".join(operators) + "."


def parse_plan(raw_plan: Any) -> dict[str, Any]:
    if (
        isinstance(raw_plan, list)
        and raw_plan
        and isinstance(raw_plan[0], tuple)
        and raw_plan[0][0] in ("physical_plan", "analyzed_plan")
        and str(raw_plan[0][1]).lstrip().startswith(("[", "{"))
    ):
        document = json.loads(raw_plan[0][1])
        root = document[0] if isinstance(document, list) else document
        tree = _from_duckdb_node(root)
        return {"format": "duckdb_json", "plan_tree": tree, "explanation": explain_duckdb_tree(tree)}

    if isinstance(raw_plan, list):
        if raw_plan and isinstance(raw_plan[0], tuple) and len(raw_plan[0]) > 1:
            lines = [str(r[1]) for r in raw_plan]
            cleaned = clean_explain_output("\n".join(lines))
            tree = _build_operator_tree(cleaned)
            return {"format": "duckdb_text", "plan_tree": tree, "explanation": explain_tree(tree)}

        if raw_plan and isinstance(raw_plan[0], dict):
            pg_root = raw_plan[0].get("Plan", raw_plan[0])
            tree = _from_postgres_node(pg_root)
            return {"format": "json", "plan": raw_plan, "plan_tree": tree, "explanation": explain_tree(tree)}

        return {"format": "json", "plan": raw_plan}

    if isinstance(raw_plan, dict):
        pg_root = raw_plan.get("Plan", raw_plan)
        if isinstance(pg_root, dict):
            tree = _from_postgres_node(pg_root)
            return {"format": "json", "plan": raw_plan, "plan_tree": tree, "explanation": explain_tree(tree)}
        return {"format": "json", "plan": raw_plan}

    if isinstance(raw_plan, str):
        text = raw_plan.strip()
        if text.startswith("{") or text.startswith("["):
            try:
                return {"format": "json", "plan": json.loads(text)}
            except json.JSONDecodeError:
                pass
        cleaned = clean_explain_output(text)
        tree = _build_operator_tree(cleaned)
        return {"format": "text", "plan_tree": tree, "explanation": explain_tree(tree)}

    return {"format": "unknown", "plan": raw_plan}
