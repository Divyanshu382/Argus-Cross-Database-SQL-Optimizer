/** Query suggestions for the SQL editor: complete queries that can be run
 * both ways (normal and estimate), ranked by what has been typed. */

export interface Suggestion {
  title: string;
  sql: string;
  why: string;
  prediction?: {
    recommendation: "exact" | "estimate";
    approximable: boolean;
    speedup?: number | null;
    exact_seconds?: number | null;
    estimate_seconds?: number | null;
  };
}

/** /api/suggest: comparable queries for a table, or a note on why there are none. */
export interface SuggestResponse {
  table: string;
  suggestions: Suggestion[];
  note: string | null;
}

const TABLE_REF = /\b(?:from|join)\s+("(?:[^"]|"")+"|[A-Za-z_][\w.]*(?:\."(?:[^"]|"")+")?)/gi;

/** Tables the query names after FROM or JOIN, in order. */
export function tablesIn(text: string): string[] {
  const found: string[] = [];
  for (const match of text.matchAll(TABLE_REF)) {
    if (!found.includes(match[1])) found.push(match[1]);
  }
  return found;
}

const normalise = (sql: string) => sql.replace(/\s+/g, " ").trim().toLowerCase();

/**
 * Rank suggested queries against what has been typed: a suggestion that
 * continues the text exactly comes first, then those sharing the most
 * typed words (functions, column names, clauses).
 */
export function rankQueries(suggestions: Suggestion[], text: string, limit = 5): Suggestion[] {
  const typed = normalise(text);
  if (!typed) return suggestions.slice(0, limit);
  const words = typed.match(/[a-z_][a-z0-9_]{2,}/g) ?? [];
  const scored = suggestions
    .map((s) => {
      const sql = normalise(s.sql);
      if (sql === typed) return { s, score: -1 };
      const prefix = sql.startsWith(typed) ? 100 : 0;
      const overlap = words.filter((w) => sql.includes(w)).length;
      // Among equally good matches, queries where Argus wins come first.
      const win = s.prediction?.recommendation === "estimate" ? Math.log10(Math.max(s.prediction.speedup ?? 1, 1)) * 3 : 0;
      return { s, score: prefix + overlap * 10 + win - Math.abs(sql.length - typed.length) / 1000 };
    })
    .filter((x) => x.score >= 0);
  return scored.sort((a, b) => b.score - a.score).slice(0, limit).map((x) => x.s);
}

/**
 * With no FROM yet and no table picked, guess the table from the column
 * names typed so far: the one whose columns match the most of them.
 */
export function guessTable(text: string, columnsByTable: Map<string, string[]>): string | null {
  const typed = new Set((text.toLowerCase().match(/[a-z_][a-z0-9_]*/g) ?? []).filter((w) => w.length > 2));
  let best: string | null = null;
  let bestHits = 0;
  for (const [table, columns] of columnsByTable) {
    const hits = columns.filter((c) => typed.has(c.toLowerCase())).length;
    if (hits > bestHits) {
      best = table;
      bestHits = hits;
    }
  }
  return best;
}

export const predictionTag = (p: NonNullable<Suggestion["prediction"]>) => {
  if (!p.approximable) return "exact only";
  if (p.recommendation === "exact" || !p.speedup) return "normal is faster";
  return `Argus ~${p.speedup >= 10 ? Math.round(p.speedup).toLocaleString() : p.speedup.toFixed(1)}× faster`;
};
