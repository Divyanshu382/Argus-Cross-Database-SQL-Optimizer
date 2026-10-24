export type Engine = "duckdb" | "postgres" | "mysql";
export type SourceKind = "local" | "postgres" | "mysql" | "mongodb";

export interface Column {
  name: string;
  type: string;
}

/** The table picked in the sources panel. */
export interface SelectedTable {
  relation: string;
  label: string;
  kind: SourceKind;
}

export interface QueryResult {
  query: string;
  rows: unknown[][];
  columns: string[];
  time: number;
  cached?: boolean;
  sampleRate?: number | null;
  stopReason?: string | null;
  neededFraction?: number | null;
  sql?: string | null;
  // 95% confidence half-widths, one per aggregate column of each row.
  ci?: (number | null)[][] | null;
  maxRelativeCi?: number | null;
  storedSample?: boolean;
  sampledTable?: string | null;
  // Set when the estimate should not be trusted as precise (e.g. a stored
  // sample too small for heavily skewed values).
  warning?: string | null;
  // Each sample the estimate took before it stopped.
  iterations?: Iteration[];
  profile?: string | null;
  accuracyTarget?: number | null;
  ranAt?: number;
}

export interface Comparison {
  speedup: number | null;
  // What the cost model predicted before the run, to set against the result.
  predictedSpeedup?: number | null;
  errorPercent: number | null;
  coverage: number | null;
}

export interface ExecutionProgress {
  status?: string;
  phase?: string;
  message?: string;
}

// Loose shape of /api/sql/execute; benchmark responses nest both runs.
export interface ExecutePayload {
  detail?: string;
  rows?: unknown[][];
  result?: unknown;
  columns?: string[];
  time?: number;
  cached?: boolean;
  sample_rate?: number | null;
  stop_reason?: string | null;
  needed_fraction?: number | null;
  rewritten_query?: string | null;
  iterations?: Array<Iteration & { sample_query?: string }>;
  mode_profile?: string | null;
  approx_mode?: string | null;
  accuracy_target?: number | null;
  ci_rows?: (number | null)[][] | null;
  max_relative_ci?: number | null;
  stored_sample?: unknown;
  sampled_table?: string | null;
  warning?: string | null;
  exact?: ExecutePayload;
  approx?: ExecutePayload;
  speedup?: number | null;
  error_percent?: number | null;
  ci_coverage?: number | null;
  // Auto mode: what the cost model chose before running.
  auto_choice?: {
    chosen: "exact" | "approx";
    recommendation?: string;
    method?: string;
    reason?: string;
    predicted_exact_seconds?: number | null;
    predicted_estimate_seconds?: number | null;
    predicted_speedup?: number | null;
    threshold?: number | null;
    decision_seconds?: number;
  } | null;
}

/** /api/advise: the cost model's prediction, made before running. */
export interface Advice {
  recommendation: "exact" | "estimate";
  method: "model" | "rule";
  reason: string;
  approximable: boolean;
  predicted_exact_seconds?: number;
  predicted_estimate_seconds?: number;
  predicted_speedup?: number;
  detail?: string;
  threshold?: number;
  // Median factor the model is off by on datasets it never saw.
  typical_factor?: { exact: number | null; estimate: number | null; speedup: number | null };
  // What the model read off the query, before running it.
  features?: Record<string, number>;
}

export interface Iteration {
  sample_fraction: number;
  rows_sampled?: number | null;
  query_time: number;
  elapsed_time: number;
  relative_ci?: number | null;
  convergence_error?: number | null;
}
