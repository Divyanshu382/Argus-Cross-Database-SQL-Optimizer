import type { QueryResult } from "../types";
import { isNumeric } from "./format";

export interface MergedRow {
  group: unknown[];
  exact: unknown[] | null;
  estimate: unknown[] | null;
  widths: (number | null)[] | null;
}

export const toNumber = (value: unknown) =>
  value === null || value === undefined || !isNumeric(value) ? null : Number(value);

/** One spelling per group value, so rows from different engines match
 * (MySQL '2013-02-01 00:00:00' vs DuckDB '2013-02-01T00:00:00', numbers as text). */
const canonical = (value: unknown) => {
  if (typeof value === "number") return String(value);
  const text = String(value);
  const iso = text.match(/^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})/);
  if (iso) return `${iso[1]} ${iso[2]}`;
  return /^-?\d+(\.\d+)?$/.test(text) ? String(Number(text)) : text;
};

/** Line exact and estimated rows up by their GROUP BY values. */
export function merge(exact: QueryResult | null, estimate: QueryResult | null) {
  if (!estimate?.ci?.length) return null;
  const aggregateCount = estimate.ci[0].length;
  const groupCount = estimate.columns.length - aggregateCount;
  const key = (row: unknown[]) => JSON.stringify(row.slice(0, groupCount).map(canonical));
  const aligned = exact && exact.columns.length === estimate.columns.length ? exact : null;

  const byKey = new Map<string, MergedRow>();
  const order: string[] = [];
  const add = (row: unknown[]) => {
    const k = key(row);
    if (!byKey.has(k)) {
      byKey.set(k, { group: row.slice(0, groupCount), exact: null, estimate: null, widths: null });
      order.push(k);
    }
    return byKey.get(k)!;
  };
  for (const row of aligned?.rows ?? []) add(row).exact = row.slice(groupCount);
  estimate.rows.forEach((row, i) => {
    const merged = add(row);
    merged.estimate = row.slice(groupCount);
    merged.widths = estimate.ci?.[i] ?? null;
  });
  return { columns: (aligned ?? estimate).columns, groupCount, rows: order.map((k) => byKey.get(k)!) };
}

/** Mean relative error of the estimates, and the share of exact values
 * inside their 95% interval: the same measures the backend reports. */
export function accuracy(exact: QueryResult, estimate: QueryResult) {
  const merged = merge(exact, estimate);
  if (!merged) return { errorPercent: null, coverage: null };
  const errors: number[] = [];
  let inside = 0;
  let judged = 0;
  for (const row of merged.rows) {
    if (!row.exact || !row.estimate) continue;
    row.estimate.forEach((value, i) => {
      const truth = toNumber(row.exact?.[i]);
      const guess = toNumber(value);
      const width = row.widths?.[i] ?? null;
      if (truth === null || guess === null) return;
      if (truth !== 0) errors.push(Math.abs(guess - truth) / Math.abs(truth));
      if (width !== null) {
        judged += 1;
        // Allow for rounding, so an exact (zero-width) answer still counts.
        if (Math.abs(guess - truth) <= width + 1e-4 * Math.max(1, Math.abs(truth))) inside += 1;
      }
    });
  }
  return {
    errorPercent: errors.length ? (errors.reduce((a, b) => a + b, 0) / errors.length) * 100 : null,
    coverage: judged ? inside / judged : null,
  };
}
