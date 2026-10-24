import { useMemo } from "react";
import dagre from "dagre";
import ReactFlow, {
  Background,
  Controls,
  MarkerType,
  Panel,
  Position,
} from "reactflow";
import type { Edge, Node } from "reactflow";
import "reactflow/dist/style.css";

interface PlanNode {
  type: string;
  columns?: string[];
  aggregates?: string[];
  children?: PlanNode[];
  rows?: number;
  cardinality?: number;
  memory?: string;
  [key: string]: unknown;
}

interface GraphStats {
  totalNodes: number;
  maxDepth: number;
  scanNodes: number;
  joinNodes: number;
  aggregateNodes: number;
  estimatedRows: number;
}

const nodeWidth = 260;
const nodeHeight = 124;

// Plan nodes stay neutral: identity colour is reserved for exact/estimate.
// Joins and aggregations, where most of the work happens, get a darker rule.
const getNodeColor = (type: string): { border: string } => {
  const upperType = type.toUpperCase();
  if (upperType.includes("JOIN") || upperType.includes("AGGREGATE") || upperType.includes("GROUP")) {
    return { border: "var(--ink-2)" };
  }
  return { border: "var(--line-strong)" };
};

const formatNumber = (value: number) => new Intl.NumberFormat().format(value);

const readNumericMetric = (node: PlanNode, keys: string[]) => {
  for (const key of keys) {
    const raw = node[key];
    if (typeof raw === "number" && Number.isFinite(raw)) return raw;
    if (typeof raw === "string") {
      const parsed = Number(raw);
      if (Number.isFinite(parsed)) return parsed;
    }
  }
  return null;
};

const collectStats = (plan: PlanNode): GraphStats => {
  const stats: GraphStats = {
    totalNodes: 0,
    maxDepth: 0,
    scanNodes: 0,
    joinNodes: 0,
    aggregateNodes: 0,
    estimatedRows: 0,
  };

  const walk = (node: PlanNode, depth: number) => {
    stats.totalNodes += 1;
    stats.maxDepth = Math.max(stats.maxDepth, depth);

    const type = node.type.toUpperCase();
    if (type.includes("SCAN") || type.includes("READ") || type.includes("FILTER")) {
      stats.scanNodes += 1;
    }
    if (type.includes("JOIN")) {
      stats.joinNodes += 1;
    }
    if (type.includes("AGGREGATE") || type.includes("GROUP")) {
      stats.aggregateNodes += 1;
    }

    const rows = readNumericMetric(node, [
      "rows",
      "plan_rows",
      "actual_rows",
      "cardinality",
    ]);
    if (rows !== null) {
      stats.estimatedRows += rows;
    }

    (node.children ?? []).forEach((child) => walk(child, depth + 1));
  };

  walk(plan, 1);
  return stats;
};

const buildGraph = (plan: PlanNode) => {
  const nodes: Node[] = [];
  const edges: Edge[] = [];
  let idCounter = 1;
  const dagreGraph = new dagre.graphlib.Graph();

  dagreGraph.setDefaultEdgeLabel(() => ({}));
  dagreGraph.setGraph({
    rankdir: "TB",
    nodesep: 42,
    ranksep: 72,
    marginx: 16,
    marginy: 16,
  });

  const walk = (node: PlanNode, parentId: string | null) => {
    const id = `n${idCounter++}`;
    const colors = getNodeColor(node.type);
    const nodeLabel = node.type.replace(/_/g, " ");
    const rows = readNumericMetric(node, [
      "rows",
      "plan_rows",
      "actual_rows",
      "cardinality",
    ]);
    const cost = readNumericMetric(node, ["total_cost", "startup_cost", "cost"]);
    const time = readNumericMetric(node, [
      "actual_total_time",
      "execution_time",
      "time",
    ]);

    const columnPreview =
      node.columns && node.columns.length > 0
        ? node.columns.slice(0, 2).join(", ")
        : null;

    nodes.push({
      id,
      position: { x: 0, y: 0 },
      sourcePosition: Position.Bottom,
      targetPosition: Position.Top,
      data: {
        label: (
          <div style={{ width: "100%" }}>
            <div
              style={{
                fontSize: "11px",
                fontWeight: 700,
                letterSpacing: "0.2px",
                marginBottom: "8px",
                textTransform: "uppercase",
              }}
              title={node.type}
            >
              {nodeLabel}
            </div>
            <div
              style={{
                display: "flex",
                gap: "6px",
                flexWrap: "wrap",
                marginBottom: "6px",
              }}
            >
              {rows !== null && (
                <span
                  title="Estimated/actual rows"
                  style={{
                    fontSize: "10px",
                    background: "var(--sunken)", border: "1px solid var(--line)",
                    borderRadius: "999px",
                    padding: "2px 7px",
                  }}
                >
                  rows {formatNumber(Math.round(rows))}
                </span>
              )}
              {cost !== null && (
                <span
                  title="Estimated cost"
                  style={{
                    fontSize: "10px",
                    background: "var(--sunken)", border: "1px solid var(--line)",
                    borderRadius: "999px",
                    padding: "2px 7px",
                  }}
                >
                  cost {cost.toFixed(1)}
                </span>
              )}
              {time !== null && (
                <span
                  title="Execution time metric"
                  style={{
                    fontSize: "10px",
                    background: "var(--sunken)", border: "1px solid var(--line)",
                    borderRadius: "999px",
                    padding: "2px 7px",
                  }}
                >
                  time {time.toFixed(2)}ms
                </span>
              )}
            </div>
            {(columnPreview || (node.aggregates && node.aggregates.length > 0)) && (
              <div style={{ fontSize: "10px", lineHeight: 1.35, opacity: 0.9 }}>
                {columnPreview && (
                  <div title={node.columns?.join(", ")}>cols: {columnPreview}</div>
                )}
                {node.aggregates && node.aggregates.length > 0 && (
                  <div title={node.aggregates.join(", ")}>
                    agg: {node.aggregates[0]}
                  </div>
                )}
              </div>
            )}
          </div>
        ),
      },
      style: {
        width: nodeWidth,
        minHeight: `${nodeHeight}px`,
        padding: "10px 12px",
        borderRadius: "5px",
        border: "1px solid var(--line)",
        borderLeft: `3px solid ${colors.border}`,
        background: "var(--raised)",
        color: "var(--ink)",
        fontFamily: "var(--sans)",
      },
    });

    dagreGraph.setNode(id, { width: nodeWidth, height: nodeHeight });

    if (parentId) {
      const edgeId = `e-${parentId}-${id}`;
      edges.push({
        id: edgeId,
        source: parentId,
        target: id,
        type: "smoothstep",
        style: {
          stroke: "var(--line-strong)",
          strokeWidth: 1.5,
        },
        markerEnd: {
          type: MarkerType.ArrowClosed,
          color: "var(--line-strong)",
        },
      });
      dagreGraph.setEdge(parentId, id);
    }

    (node.children ?? []).forEach((child) => walk(child, id));
  };

  walk(plan, null);
  dagre.layout(dagreGraph);

  const layoutedNodes = nodes.map((node) => {
    const position = dagreGraph.node(node.id);
    return {
      ...node,
      position: {
        x: position.x - nodeWidth / 2,
        y: position.y - nodeHeight / 2,
      },
    };
  });

  return { nodes: layoutedNodes, edges };
};

function PlanGraph({ plan }: { plan: PlanNode }) {
  const { nodes, edges } = useMemo(() => buildGraph(plan), [plan]);
  const stats = useMemo(() => collectStats(plan), [plan]);

  return (
    <ReactFlow nodes={nodes} edges={edges} fitView proOptions={{ hideAttribution: true }}>
      <Background color="var(--line)" gap={22} />
      <Controls showInteractive={false} />
      <Panel
        position="top-left"
        style={{
          background: "var(--raised)",
          border: "1px solid var(--line)",
          borderRadius: "5px",
          padding: "6px 10px",
          color: "var(--muted)",
          display: "flex",
          gap: "12px",
          flexWrap: "wrap",
          fontSize: "11.5px",
          fontFamily: "var(--sans)",
        }}
      >
        <span>{stats.totalNodes} steps</span>
        <span>{stats.scanNodes} scans</span>
        <span>{stats.joinNodes} joins</span>
        <span>{stats.aggregateNodes} aggregations</span>
        <span>~{formatNumber(Math.round(stats.estimatedRows))} rows handled</span>
      </Panel>
    </ReactFlow>
  );
}

export default PlanGraph;
