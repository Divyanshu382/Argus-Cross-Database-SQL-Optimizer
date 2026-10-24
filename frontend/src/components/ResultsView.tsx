import { type ComponentProps, type CSSProperties, type ReactNode, useEffect, useState } from "react";
import type { Advice, Comparison, Engine, QueryResult } from "../types";
import { formatCell, formatPercent, formatSeconds, formatWidth, isNumeric } from "../utils/format";
import { merge, toNumber } from "../utils/compare";
import IntervalStrip from "./IntervalStrip";
import PlanGraph from "./PlanGraph";
import Spinner from "./Spinner";

type Tab = "results" | "details" | "ran" | "plan" | "history";
type PlanTree = ComponentProps<typeof PlanGraph>["plan"];

const MAX_ROWS = 100;

const STOP_NOTES: Record<string, string> = {
  ci_reached: "The sampler stopped as soon as every 95% interval was within the chosen margin.",
  converged: "The sampler stopped once successive samples agreed.",
  time_budget_exceeded: "The sampler stopped at its time limit; intervals may be wider than the chosen margin.",
  progression_exhausted: "The sampler used its largest sample; intervals may be wider than the chosen margin.",
  stored_sample: "Estimated from the stored sample on this computer; the source database was not contacted.",
  local_copies:
    "Every table has a local copy and the data is small, so the join was computed whole from the copies: no sampling error, but as of when the copies were built.",
};

/** Where the exact query ran, read off the SQL that actually ran. */
function ranWhere(sql: string | null | undefined): string {
  if (!sql) return "ran in DuckDB";
  if (sql.startsWith("db.")) return "ran inside MongoDB";
  if (sql.includes("mysql_query(")) return "ran inside MySQL";
  if (sql.includes("postgres_query(")) return "ran inside PostgreSQL";
  return "ran in DuckDB";
}

function PlainTable({ result }: { result: QueryResult }) {
  if (!result.rows.length) return <p className="table-note">The query returned no rows.</p>;
  return (
    <>
      <div className="table-wrap">
        <table className="data-table">
          <thead>
            <tr>
              {result.columns.map((c, i) => (
                <th key={i} className={isNumeric(result.rows[0]?.[i]) ? "num" : undefined}>
                  {c}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {result.rows.slice(0, MAX_ROWS).map((row, r) => (
              <tr key={r}>
                {row.map((value, i) => (
                  <td key={i} className={isNumeric(value) ? "num" : undefined}>
                    {formatCell(value)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {result.rows.length > MAX_ROWS && (
        <p className="table-note">
          Showing {MAX_ROWS} of {result.rows.length.toLocaleString()} rows.
        </p>
      )}
    </>
  );
}

interface Cell {
  truth: number | null;
  guess: number | null;
  width: number | null;
  /** Signed relative error, (estimate - exact) / |exact|. */
  off: number | null;
  inside: boolean | null;
}

function cellOf(exact: unknown, estimate: unknown, width: number | null): Cell {
  const truth = toNumber(exact);
  const guess = toNumber(estimate);
  const off = truth !== null && guess !== null && truth !== 0 ? (guess - truth) / Math.abs(truth) : null;
  const inside =
    truth !== null && guess !== null && width !== null
      ? Math.abs(guess - truth) <= width + 1e-4 * Math.max(1, Math.abs(truth))
      : null;
  return { truth, guess, width, off, inside };
}

/** Signed percentage with enough digits to show small errors: +0.042%, −1.3%, +12%. */
function signedPercent(fraction: number) {
  const percent = Math.abs(fraction) * 100;
  const digits = percent < 0.1 ? 3 : percent < 10 ? 2 : 1;
  return `${fraction >= 0 ? "+" : "−"}${percent.toFixed(digits)}%`;
}

/** Every comparable value of the estimate against the exact answer. */
function errorStats(exact: QueryResult | null, estimate: QueryResult | null) {
  const merged = exact && estimate ? merge(exact, estimate) : null;
  if (!merged) return null;
  const aggregates = merged.columns.slice(merged.groupCount);
  const cells = merged.rows.flatMap((row) =>
    aggregates.map((name, i) => ({
      ...cellOf(row.exact?.[i], row.estimate?.[i], row.widths?.[i] ?? null),
      where: [name, ...row.group.map(formatCell)].join(" · "),
    })),
  );
  const scored = cells.filter((c) => c.off !== null);
  if (!scored.length) return null;
  const abs = scored.map((c) => Math.abs(c.off!)).sort((a, b) => a - b);
  const judged = cells.filter((c) => c.inside !== null);
  return {
    count: scored.length,
    mean: abs.reduce((a, b) => a + b, 0) / abs.length,
    median: abs[Math.floor(abs.length / 2)],
    worst: scored.reduce((a, b) => (Math.abs(b.off!) > Math.abs(a.off!) ? b : a)),
    inside: judged.filter((c) => c.inside).length,
    judged: judged.length,
    missing: merged.rows.filter((r) => r.exact && !r.estimate).length,
  };
}

type Stat = { label: string; value: string; sub?: string; tone?: "good" | "bad" };

/** The numbers worth reading after a run, speed and accuracy, on the results tab itself. */
function RunStats({ exact, estimate }: { exact: QueryResult | null; estimate: QueryResult | null }) {
  const speedStats: Stat[] = [];
  const accuracyStats: Stat[] = [];

  if (exact && estimate) {
    const saved = exact.time - estimate.time;
    speedStats.push({
      label: saved >= 0 ? "Time saved" : "Time lost",
      value: formatSeconds(Math.abs(saved)),
      sub: `${formatPercent(Math.min(1, Math.abs(saved) / Math.max(exact.time, 1e-9)))} of the normal query`,
      tone: saved > 0 ? "good" : "bad",
    });
  }
  if (estimate) {
    const steps = estimate.iterations ?? [];
    const rowsRead = [...steps].reverse().find((s) => typeof s.rows_sampled === "number")?.rows_sampled;
    if (typeof estimate.sampleRate === "number")
      speedStats.push({
        label: estimate.storedSample ? "Stored sample" : "Sample read",
        value: formatPercent(estimate.sampleRate),
        sub: typeof rowsRead === "number" ? `${rowsRead.toLocaleString()} rows` : "of the rows",
      });
    if (steps.length)
      speedStats.push({
        label: "Sampling steps",
        value: String(steps.length),
        sub: steps.length > 1 ? `pilot, then ${steps.length - 1} larger sample${steps.length > 2 ? "s" : ""}` : "one pass",
      });
    if (typeof estimate.maxRelativeCi === "number") {
      const margin = estimate.accuracyTarget ? (100 - estimate.accuracyTarget) / 100 : null;
      accuracyStats.push({
        label: "Widest interval",
        value: `±${formatPercent(estimate.maxRelativeCi)}`,
        sub: margin !== null ? `target ±${formatPercent(margin)}${estimate.maxRelativeCi <= margin ? ", met" : ", not met"}` : undefined,
        tone: margin !== null ? (estimate.maxRelativeCi <= margin ? "good" : "bad") : undefined,
      });
    }
  }
  const rowsOut = (exact ?? estimate)?.rows.length;
  if (typeof rowsOut === "number") speedStats.push({ label: "Rows returned", value: rowsOut.toLocaleString(), sub: exact ? "by the normal query" : "by the estimate" });

  const err = errorStats(exact, estimate);
  if (err) {
    accuracyStats.unshift(
      { label: "Values compared", value: err.count.toLocaleString(), sub: err.missing ? `${err.missing} group(s) missing from the sample` : "estimate vs exact" },
      { label: "Mean error", value: signedPercent(err.mean).slice(1), tone: err.mean < 0.05 ? "good" : "bad" },
      { label: "Median error", value: signedPercent(err.median).slice(1) },
      { label: "Largest error", value: signedPercent(err.worst.off!), sub: err.worst.where },
    );
    if (err.judged)
      accuracyStats.push({
        label: "Inside 95% interval",
        value: `${err.inside} of ${err.judged}`,
        sub: `${Math.round((err.inside / err.judged) * 100)}% (expect about 95%)`,
        tone: err.inside / err.judged >= 0.9 ? "good" : "bad",
      });
  }

  const group = (title: string, stats: Stat[]) =>
    stats.length > 0 && (
      <div className="run-stats-group">
        <h3>{title}</h3>
        <dl>
          {stats.map((s) => (
            <div key={s.label}>
              <dt>{s.label}</dt>
              <dd className={s.tone}>{s.value}</dd>
              {s.sub && <dd className="sub">{s.sub}</dd>}
            </div>
          ))}
        </dl>
      </div>
    );

  if (!speedStats.length && !accuracyStats.length) return null;
  return (
    <div className="run-stats" aria-label="Run statistics">
      {group("Speed", speedStats)}
      {group("Accuracy", accuracyStats)}
    </div>
  );
}

/** Exact and estimate side by side: for each numeric column, the exact
 * value, the estimate with its 95% interval, and the percentage error. */
function CompareTable({ exact, estimate }: { exact: QueryResult | null; estimate: QueryResult }) {
  const merged = merge(exact, estimate);
  if (!merged) return <PlainTable result={estimate} />;
  const { columns, groupCount, rows } = merged;
  const aggregates = columns.slice(groupCount);
  const hasExact = rows.some((r) => r.exact);
  const shown = rows.slice(0, MAX_ROWS);

  const grid = shown.map((row) => aggregates.map((_, i) => cellOf(row.exact?.[i], row.estimate?.[i], row.widths?.[i] ?? null)));
  // A column is compared only when it holds numbers on either side.
  const numeric = aggregates.map((_, i) => grid.some((cells) => cells[i].truth !== null || cells[i].guess !== null));
  const subCols = hasExact ? 3 : 1;

  return (
    <>
      {!hasExact && (
        <p className="table-note">
          Only the estimate ran. Use <strong>Run both and compare</strong>, or tick <em>time the normal query after an estimate</em>,
          to see the exact values and the percentage error beside each estimate.
        </p>
      )}
      <div className="legend" aria-hidden="true">
        <span>
          <i className="swatch exact" /> exact: the normal query's answer
        </span>
        <span>
          <i className="swatch estimate" /> estimate ± 95% interval
        </span>
        {hasExact && <span>error = (estimate − exact) ÷ exact</span>}
      </div>
      <div className="table-wrap">
        <table className="data-table compare-table">
          <thead>
            <tr>
              {columns.slice(0, groupCount).map((c, i) => (
                <th key={`g${i}`} rowSpan={2} className="group-head">
                  {c}
                </th>
              ))}
              {aggregates.map((c, i) => (
                <th key={`a${i}`} colSpan={numeric[i] ? subCols : 1} className="agg-head">
                  {c}
                </th>
              ))}
            </tr>
            <tr>
              {aggregates.map((_, i) =>
                numeric[i] && hasExact ? (
                  [
                    <th key={`e${i}`} className="num sub exact grp">
                      Exact
                    </th>,
                    <th key={`s${i}`} className="num sub estimate">
                      Estimate
                    </th>,
                    <th key={`p${i}`} className="num sub">
                      Error
                    </th>,
                  ]
                ) : (
                  <th key={`s${i}`} className="num sub estimate grp">
                    {numeric[i] ? "Estimate" : ""}
                  </th>
                ),
              )}
            </tr>
          </thead>
          <tbody>
            {shown.map((row, r) => (
              <tr key={r}>
                {row.group.map((value, i) => (
                  <td key={`g${i}`} className="group">
                    {formatCell(value)}
                  </td>
                ))}
                {aggregates.map((_, i) => {
                  const c = grid[r][i];
                  const estimateCell = (
                    <td key={`s${i}`} className={`num${hasExact && numeric[i] ? "" : " grp"}`}>
                      {c.guess !== null ? (
                        <>
                          <span className="est-value">{formatCell(c.guess)}</span>
                          {c.width !== null && c.width > 0 && <span className="ci-line">± {formatWidth(c.width)}</span>}
                          {c.width !== null && c.width > 0 && <IntervalStrip estimate={c.guess} width={c.width} exact={c.truth} />}
                        </>
                      ) : row.estimate ? (
                        formatCell(row.estimate[i])
                      ) : (
                        <span className="ci-line">not in sample</span>
                      )}
                    </td>
                  );
                  if (!numeric[i] || !hasExact) return estimateCell;
                  return [
                    <td key={`e${i}`} className="num grp exact-value">
                      {row.exact ? formatCell(row.exact[i]) : <span className="ci-line">no row</span>}
                    </td>,
                    estimateCell,
                    <td key={`p${i}`} className="num">
                      {c.off !== null ? (
                        <>
                          <span className={`err${Math.abs(c.off) >= 0.05 ? " big" : ""}`}>{signedPercent(c.off)}</span>
                          {c.inside !== null && (
                            <span className={`ci-line${c.inside ? "" : " miss"}`}>{c.inside ? "inside ±" : "outside ±"}</span>
                          )}
                        </>
                      ) : c.truth === 0 && c.guess !== null ? (
                        <span className="ci-line">{c.guess === 0 ? "0% (both 0)" : "exact is 0"}</span>
                      ) : (
                        <span className="ci-line">–</span>
                      )}
                    </td>,
                  ];
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {rows.length > MAX_ROWS && (
        <p className="table-note">
          Showing {MAX_ROWS} of {rows.length.toLocaleString()} groups.
        </p>
      )}
    </>
  );
}

const offBy = (predicted: number, measured: number) =>
  Math.max(predicted, measured) / Math.max(Math.min(predicted, measured), 1e-9);

/** What the cost model predicted before the run, against what was measured,
 * and what following its recommendation was worth. */
function ModelHelped({
  advice,
  exact,
  estimate,
  normalRunning,
  auto = false,
}: {
  advice: Advice | null;
  exact: QueryResult | null;
  estimate: QueryResult | null;
  normalRunning: boolean;
  auto?: boolean;
}) {
  if (!advice || (!exact && !estimate)) return null;
  const threshold = advice.threshold ?? 1.5;
  const recommendsEstimate = advice.recommendation === "estimate";
  // Cached times are lookups, not runs: nothing to judge the model against.
  const exactTime = exact && !exact.cached ? exact.time : null;
  const estimateTime = estimate && !estimate.cached ? estimate.time : null;
  const measuredSpeedup = exactTime !== null && estimateTime !== null ? exactTime / Math.max(estimateTime, 1e-9) : null;

  const rows: Array<{ label: ReactNode; predicted: string; measured: string; off: number | null }> = [];
  if (typeof advice.predicted_exact_seconds === "number")
    rows.push({
      label: (
        <>
          <i className="swatch exact" /> Normal query
        </>
      ),
      predicted: formatSeconds(advice.predicted_exact_seconds),
      measured: exactTime !== null ? formatSeconds(exactTime) : normalRunning ? "running…" : exact?.cached ? "cached" : "not run",
      off: exactTime !== null ? offBy(advice.predicted_exact_seconds, exactTime) : null,
    });
  if (typeof advice.predicted_estimate_seconds === "number")
    rows.push({
      label: (
        <>
          <i className="swatch estimate" /> Estimate
        </>
      ),
      predicted: formatSeconds(advice.predicted_estimate_seconds),
      measured: estimateTime !== null ? formatSeconds(estimateTime) : estimate?.cached ? "cached" : "not run",
      off: estimateTime !== null ? offBy(advice.predicted_estimate_seconds, estimateTime) : null,
    });
  if (typeof advice.predicted_speedup === "number")
    rows.push({
      label: "Speedup",
      predicted: speedWords(advice.predicted_speedup),
      measured: measuredSpeedup !== null ? speedWords(measuredSpeedup) : "–",
      off: measuredSpeedup !== null ? offBy(advice.predicted_speedup, measuredSpeedup) : null,
    });

  let verdict: ReactNode = null;
  let right: boolean | null = null;
  if (measuredSpeedup !== null && exactTime !== null && estimateTime !== null) {
    right = recommendsEstimate === measuredSpeedup >= threshold;
    const saved = exactTime - estimateTime;
    if (recommendsEstimate)
      verdict = right ? (
        <>
          Following it saved <strong>{formatSeconds(saved)}</strong> ({speedWords(measuredSpeedup)}) against running the normal query.
        </>
      ) : (
        <>
          The estimate was only {speedWords(measuredSpeedup)}, under the {threshold}× bar: the normal query would have been the
          better call here.
        </>
      );
    else
      verdict = right ? (
        <>
          The estimate would have {saved > 0 ? <>saved only {formatSeconds(saved)}</> : <>cost {formatSeconds(-saved)} more</>}, not
          worth its error, so running the exact query was the right call.
        </>
      ) : (
        <>
          The estimate turned out {speedWords(measuredSpeedup)}: worth taking, so the model was too cautious here.
        </>
      );
  } else if (recommendsEstimate && estimateTime !== null && typeof advice.predicted_exact_seconds === "number") {
    verdict = (
      <>
        Expected saving: about <strong>{formatSeconds(Math.max(0, advice.predicted_exact_seconds - estimateTime))}</strong> against the
        normal query. Run both to measure it.
      </>
    );
  } else if (
    !recommendsEstimate &&
    exactTime !== null &&
    typeof advice.predicted_estimate_seconds === "number" &&
    typeof advice.predicted_exact_seconds === "number"
  ) {
    verdict = (
      <>
        The estimate was predicted at {formatSeconds(advice.predicted_estimate_seconds)} against{" "}
        {formatSeconds(advice.predicted_exact_seconds)} for the normal query, under the {threshold}× bar, so{" "}
        {auto ? "only the normal query ran" : "the normal query is the better choice"}: no time is lost to sampling.
      </>
    );
  } else if (recommendsEstimate && exactTime !== null) {
    verdict = <>The model expected the estimate to be faster here. Run the estimate (or both) to check.</>;
  }

  if (advice.method !== "model") {
    return (
      <section className="helped" aria-label="Cost model">
        <h3>Cost model</h3>
        <p className="table-note">{advice.reason}</p>
      </section>
    );
  }

  return (
    <section className="helped" aria-label="How the cost model helped">
      <div className="helped-head">
        <h3>How the cost model helped</h3>
        <span className="helped-sub">
          {auto
            ? "Auto mode: the model chose and ran it, no manual choice"
            : "predicted before the query ran · gradient-boosted trees"}
        </span>
      </div>
      <div className="helped-body">
        <div className="helped-call">
          <span className="helped-k">{auto ? "It chose" : "It recommended"}</span>
          <span className="helped-reco">
            <i className={`swatch ${recommendsEstimate ? "estimate" : "exact"}`} />
            {recommendsEstimate ? "the estimate" : "the normal query"}
          </span>
          {right !== null && <span className={`helped-badge ${right ? "good" : "bad"}`}>{right ? "✓ right call" : "✗ wrong call"}</span>}
          {verdict && <p>{verdict}</p>}
        </div>
        <table className="data-table helped-table">
          <thead>
            <tr>
              <th />
              <th className="num">Predicted</th>
              <th className="num">Measured</th>
              <th className="num">Off by</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={i}>
                <td>{r.label}</td>
                <td className="num">{r.predicted}</td>
                <td className="num">{r.measured}</td>
                <td className={`num${r.off !== null && r.off > 2 ? " far" : ""}`}>{r.off !== null ? `×${r.off.toFixed(2)}` : "–"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function Tiles({
  exact,
  estimate,
  comparison,
  normalRunning,
  predictedNormal,
}: {
  exact: QueryResult | null;
  estimate: QueryResult | null;
  comparison: Comparison | null;
  normalRunning: boolean;
  predictedNormal: number | null;
}) {
  const tiles: Array<{ label: ReactNode; value: string; sub: string; good?: boolean }> = [];
  if (!exact && normalRunning) {
    tiles.push({
      label: (
        <>
          <i className="swatch exact" /> Normal query (exact)
        </>
      ),
      value: "running…",
      sub: predictedNormal ? `predicted ~${formatSeconds(predictedNormal)}; timed in the background` : "timed in the background",
    });
  }
  if (exact) {
    tiles.push({
      label: (
        <>
          <i className="swatch exact" /> Normal query (exact)
        </>
      ),
      value: formatSeconds(exact.time),
      sub: exact.cached ? "from cache" : `${ranWhere(exact.sql)} · ${exact.rows.length.toLocaleString()} rows`,
    });
  }
  if (estimate) {
    const how = estimate.stopReason === "local_copies"
      ? "computed whole from local copies"
      : estimate.storedSample
      ? `from the stored ${formatPercent(estimate.sampleRate ?? 0)} sample`
      : typeof estimate.sampleRate === "number"
        ? `read ${formatPercent(estimate.sampleRate)} of the rows`
        : "from a sample";
    tiles.push({
      label: (
        <>
          <i className="swatch estimate" /> Estimate
        </>
      ),
      value: formatSeconds(estimate.time),
      sub: estimate.cached ? "from cache" : how,
    });
  }
  if (comparison?.speedup) {
    const faster = comparison.speedup >= 1;
    const ratio = faster ? comparison.speedup : 1 / comparison.speedup;
    tiles.push({
      label: "Speed",
      value: `${ratio >= 100 ? Math.round(ratio).toLocaleString() : ratio.toFixed(1)}× ${faster ? "faster" : "slower"}`,
      sub:
        typeof comparison.predictedSpeedup === "number"
          ? `model predicted ${speedWords(comparison.predictedSpeedup)} before running`
          : faster
            ? "estimate vs exact, timed back to back"
            : "sampling cost more than it saved here",
      good: faster && ratio >= 2,
    });
  }
  if (comparison && comparison.errorPercent !== null) {
    tiles.push({
      label: "Accuracy",
      value: `${comparison.errorPercent.toFixed(2)}% off`,
      sub:
        comparison.coverage !== null
          ? `on average · ${Math.round(comparison.coverage * 100)}% of true values inside the ± ranges`
          : "average error",
    });
  } else if (estimate?.maxRelativeCi) {
    tiles.push({
      label: "Precision",
      value: `±${formatPercent(estimate.maxRelativeCi)}`,
      sub: "widest 95% interval, relative to its estimate",
    });
  }
  if (!tiles.length) return null;
  return (
    <div className="tiles" style={{ "--tiles": tiles.length } as CSSProperties}>
      {tiles.map((t, i) => (
        <div className="tile" key={i}>
          <div className="tile-label">{t.label}</div>
          <div className={`tile-value${t.good ? " good" : ""}`}>{t.value}</div>
          <div className="tile-sub">{t.sub}</div>
        </div>
      ))}
    </div>
  );
}

const PROFILE_NOTES: Record<string, string> = {
  fast: "fast: 0.75 s time budget",
  balanced: "balanced: 1.5 s time budget",
  accurate: "accurate: 3 s time budget",
};

/** Everything measured about the last run, and each sample the estimate took. */
function RunDetails({
  exact,
  estimate,
  comparison,
  stopNote,
}: {
  exact: QueryResult | null;
  estimate: QueryResult | null;
  comparison: Comparison | null;
  stopNote: string | null | undefined;
}) {
  if (!exact && !estimate) return <p className="table-note">Run a query to see how it ran.</p>;
  const margin = estimate?.accuracyTarget ? (100 - estimate.accuracyTarget) / 100 : null;
  const facts: Array<[string, ReactNode]> = [];
  if (exact) {
    facts.push(["Normal query", `${formatSeconds(exact.time)}, ${ranWhere(exact.sql)}${exact.cached ? " (from cache)" : ""}`]);
    facts.push(["Rows returned", exact.rows.length.toLocaleString()]);
  }
  if (estimate) {
    facts.push(["Estimate", `${formatSeconds(estimate.time)}${estimate.cached ? " (from cache)" : ""}`]);
    if (margin !== null) facts.push(["Target margin", `±${formatPercent(margin)} at 95% confidence`]);
    if (estimate.profile) facts.push(["Sampler profile", PROFILE_NOTES[estimate.profile] ?? estimate.profile]);
    if (typeof estimate.sampleRate === "number") facts.push(["Final sample", `${formatPercent(estimate.sampleRate)} of the rows`]);
    if (estimate.maxRelativeCi !== null && estimate.maxRelativeCi !== undefined)
      facts.push(["Widest interval", `±${formatPercent(estimate.maxRelativeCi)} of its value`]);
    if (estimate.storedSample) facts.push(["Read from", `stored sample ${estimate.sampledTable ? `of ${estimate.sampledTable}` : ""}`]);
    else if (estimate.sampledTable) facts.push(["Sampled table", estimate.sampledTable]);
    if (estimate.stopReason) facts.push(["Stopped because", stopNote ?? estimate.stopReason]);
  }
  if (comparison?.speedup) facts.push(["Estimate vs normal", speedWords(comparison.speedup)]);
  if (typeof comparison?.predictedSpeedup === "number") facts.push(["Model predicted", speedWords(comparison.predictedSpeedup)]);
  if (comparison?.errorPercent !== null && comparison?.errorPercent !== undefined) facts.push(["Mean error", `${comparison.errorPercent.toFixed(3)}%`]);
  if (comparison?.coverage !== null && comparison?.coverage !== undefined)
    facts.push(["Interval coverage", `${Math.round(comparison.coverage * 100)}% of true values inside the ± ranges`]);

  const steps = estimate?.iterations ?? [];
  const anyRows = steps.some((s) => typeof s.rows_sampled === "number");
  const widest = Math.max(...steps.map((s) => s.relative_ci ?? 0), margin ?? 0, 1e-9);
  return (
    <div className="details">
      <dl className="facts">
        {facts.map(([k, v]) => (
          <div key={k}>
            <dt>{k}</dt>
            <dd>{v}</dd>
          </div>
        ))}
      </dl>
      {steps.length > 0 && (
        <>
          <h3 className="details-h">Samples the estimate took</h3>
          <p className="table-note">
            Each step reads a larger sample until every 95% interval is inside the margin
            {margin !== null && <> (dashed line, ±{formatPercent(margin)})</>}. The first step is a pilot: its spread decides how
            large the next sample must be.
          </p>
          <div className="table-wrap">
            <table className="data-table ladder">
              <thead>
                <tr>
                  <th>Step</th>
                  <th className="num">Sample</th>
                  {anyRows && <th className="num">Rows read</th>}
                  <th className="num">Step time</th>
                  <th className="num">Elapsed</th>
                  <th>Widest 95% interval</th>
                </tr>
              </thead>
              <tbody>
                {steps.map((step, i) => (
                  <tr key={i}>
                    <td>{i === 0 && steps.length > 1 ? "1 · pilot" : i + 1}</td>
                    <td className="num">{formatPercent(step.sample_fraction)}</td>
                    {anyRows && (
                      <td className="num">{typeof step.rows_sampled === "number" ? step.rows_sampled.toLocaleString() : "–"}</td>
                    )}
                    {/* Wall time between steps: includes building and reading the sample. */}
                    <td className="num">{formatSeconds(step.elapsed_time - (i ? steps[i - 1].elapsed_time : 0))}</td>
                    <td className="num">{formatSeconds(step.elapsed_time)}</td>
                    <td>
                      {typeof step.relative_ci === "number" ? (
                        <div className="ladder-ci">
                          <span className="ladder-track">
                            <span className="ladder-bar" style={{ width: `${(step.relative_ci / widest) * 100}%` }} />
                            {margin !== null && <span className="ladder-target" style={{ left: `${(margin / widest) * 100}%` }} />}
                          </span>
                          <span className="num">±{formatPercent(step.relative_ci)}</span>
                        </div>
                      ) : (
                        "–"
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}

const formatSpeed = (x: number) => (x >= 10 ? `${Math.round(x).toLocaleString()}×` : `${x.toFixed(1)}×`);
const speedWords = (x: number) => (x >= 1 ? `${formatSpeed(x)} faster` : `${formatSpeed(1 / x)} slower`);

function PlanTab({ backend, query, engine }: { backend: string; query: string; engine: Engine }) {
  const [plan, setPlan] = useState<PlanTree | null>(null);
  const [text, setText] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [for_, setFor] = useState<string | null>(null);

  const load = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`${backend}/api/sql/parse-plan`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query, source: engine }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data?.detail ?? "Could not build the plan.");
      setPlan(data.plan_tree ?? null);
      setText(
        data.plan_tree
          ? null
          : Array.isArray(data.raw_plan)
            ? data.raw_plan.map((r: unknown) => (Array.isArray(r) ? r.at(-1) : r)).join("\n")
            : String(data.raw_plan ?? ""),
      );
      setFor(query);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (query.trim() && for_ === null) void load();
    // Load once when the tab opens; later loads are explicit.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="results">
      <div className="field-row" style={{ alignItems: "center", justifyContent: "space-between" }}>
        <p className="table-note">How the engine will run this query, read bottom-up: scans first, then filters, joins, grouping.</p>
        <button className="btn btn-small" onClick={() => void load()} disabled={loading || !query.trim()}>
          {loading && <Spinner />} {for_ !== null && for_ !== query ? "Plan the edited query" : "Refresh"}
        </button>
      </div>
      {error && <div className="message error">{error}</div>}
      {plan && (
        <div className="plan-box">
          <PlanGraph plan={plan} />
        </div>
      )}
      {text && <pre className="code-block plan-text">{text}</pre>}
    </div>
  );
}

interface HistoryItem {
  query: string;
  mode: string;
  source: string;
  time: number;
  cached?: boolean;
}

function HistoryTab({ backend, onLoadQuery }: { backend: string; onLoadQuery: (sql: string) => void }) {
  const [items, setItems] = useState<HistoryItem[] | null>(null);
  useEffect(() => {
    fetch(`${backend}/api/history`)
      .then((r) => (r.ok ? r.json() : []))
      .then(setItems)
      .catch(() => setItems([]));
  }, [backend]);
  if (items === null) return <div className="running"><Spinner /> Loading</div>;
  if (!items.length) return <p className="table-note">Queries you run appear here.</p>;
  const modeLabel = (mode: string) => (mode === "benchmark" ? "both" : mode === "exact" ? "exact" : "estimate");
  return (
    <ul className="history">
      {items.slice(0, 40).map((item, i) => (
        <li key={i}>
          <button onClick={() => onLoadQuery(item.query)} title="Load into the editor">
            <code>{item.query.replace(/\s+/g, " ")}</code>
            <span className="meta">{formatSeconds(item.time)}</span>
            <span className="meta">
              {modeLabel(item.mode)}
              {item.cached ? " · from cache" : ""}
            </span>
          </button>
        </li>
      ))}
    </ul>
  );
}

export default function ResultsView({
  backend,
  query,
  engine,
  exact,
  estimate,
  exactError,
  estimateError,
  comparison,
  runningMessage,
  normalRunning = false,
  advice = null,
  auto = false,
  onLoadQuery,
}: {
  backend: string;
  query: string;
  engine: Engine;
  exact: QueryResult | null;
  estimate: QueryResult | null;
  exactError: string | null;
  estimateError: string | null;
  comparison: Comparison | null;
  runningMessage: string | null;
  normalRunning?: boolean;
  advice?: Advice | null;
  auto?: boolean;
  onLoadQuery: (sql: string) => void;
}) {
  const [tab, setTab] = useState<Tab>("results");
  const stale = (exact ?? estimate) && (exact ?? estimate)!.query.trim() !== query.trim();
  const stopNote =
    estimate?.stopReason === "needs_exact" && typeof estimate.neededFraction === "number"
      ? `Reaching the chosen margin would need about ${formatPercent(estimate.neededFraction)} of the rows, close to the cost of the exact query, so the estimate stopped at half.`
      : estimate?.stopReason
        ? STOP_NOTES[estimate.stopReason]
        : null;

  const tabs: Array<[Tab, string]> = [
    ["results", "Results"],
    ["details", "Run details"],
    ["ran", "What ran"],
    ["plan", "Plan"],
    ["history", "History"],
  ];

  return (
    <section className="results" aria-label="Results">
      {runningMessage && (
        <div>
          <div className="running" style={{ marginBottom: 8 }}>
            <Spinner /> {runningMessage}
          </div>
          <div className="progress-line" />
        </div>
      )}
      <Tiles
        exact={exact}
        estimate={estimate}
        comparison={comparison}
        normalRunning={normalRunning}
        predictedNormal={advice?.predicted_exact_seconds ?? null}
      />
      <div className="tabs" role="tablist">
        {tabs.map(([key, label]) => (
          <button key={key} role="tab" className="tab" aria-selected={tab === key} onClick={() => setTab(key)}>
            {label}
          </button>
        ))}
      </div>

      {tab === "results" && (
        <>
          {stale && <div className="message info">These results are for an earlier version of the query. Run it again to update them.</div>}
          {exactError && (
            <div className="message error">
              <strong>Exact query failed.</strong> {exactError}
            </div>
          )}
          {estimateError && (
            <div className="message error">
              <strong>No estimate.</strong> {estimateError}
            </div>
          )}
          {estimate?.warning && (
            <div className="message error">
              <strong>Rough estimate.</strong> {estimate.warning}
            </div>
          )}
          <ModelHelped advice={advice} exact={exact} estimate={estimate} normalRunning={normalRunning} auto={auto} />
          <RunStats exact={exact} estimate={estimate} />
          {estimate ? <CompareTable exact={exact} estimate={estimate} /> : exact && <PlainTable result={exact} />}
          {stopNote && <p className="table-note">{stopNote}</p>}
        </>
      )}

      {tab === "details" && <RunDetails exact={exact} estimate={estimate} comparison={comparison} stopNote={stopNote} />}

      {tab === "ran" && (
        <div className="ran">
          {!exact && !estimate && <p className="table-note">Run a query to see the SQL each side actually executed.</p>}
          {exact && (
            <div>
              <h3>
                <i className="swatch exact" /> Normal query (exact), {ranWhere(exact.sql)}
              </h3>
              <pre className="code-block">{exact.sql ?? exact.query}</pre>
            </div>
          )}
          {estimate && (
            <div>
              <h3>
                <i className="swatch estimate" /> Estimate
              </h3>
              <pre className="code-block">{estimate.sql ?? estimate.query}</pre>
            </div>
          )}
        </div>
      )}

      {tab === "plan" && <PlanTab backend={backend} query={query} engine={engine} />}
      {tab === "history" && <HistoryTab backend={backend} onLoadQuery={onLoadQuery} />}
    </section>
  );
}
