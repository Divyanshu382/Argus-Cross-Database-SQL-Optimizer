import { useCallback, useEffect, useRef, useState } from "react";
import "./App.css";
import AddSourceDialog from "./components/AddSourceDialog";
import BestQueries from "./components/BestQueries";
import QueryEditor, { type QueryEditorHandle } from "./components/QueryEditor";
import ResultsView from "./components/ResultsView";
import SourcesPanel from "./components/SourcesPanel";
import TableDetail from "./components/TableDetail";
import type { Advice, Comparison, Engine, ExecutePayload, ExecutionProgress, QueryResult, SelectedTable } from "./types";
import { accuracy } from "./utils/compare";

const BACKEND = "http://127.0.0.1:8093";

// The sampler stops once every 95% interval is within ±(100 - target)%.
const MARGINS = [
  { label: "±20%", target: 80 },
  { label: "±8%", target: 92 },
  { label: "±2%", target: 98 },
];

const PROFILES = [
  { key: "fast", label: "Fast", note: "stops within 0.75 s" },
  { key: "balanced", label: "Balanced", note: "stops within 1.5 s" },
  { key: "accurate", label: "Accurate", note: "stops within 3 s" },
] as const;

type Running = "both" | "exact" | "estimate" | "auto" | null;
type Theme = "system" | "light" | "dark";
type Profile = (typeof PROFILES)[number]["key"];

const makeRequestId = () =>
  typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`;

const toRows = (payload: ExecutePayload): unknown[][] => {
  const rows = payload.rows ?? payload.result;
  if (Array.isArray(rows)) return rows as unknown[][];
  return rows === undefined ? [] : [[rows]];
};

const toResult = (query: string, data: ExecutePayload, cached = Boolean(data.cached)): QueryResult => ({
  query,
  rows: toRows(data),
  columns: data.columns ?? [],
  time: data.time ?? 0,
  cached,
  sampleRate: data.sample_rate ?? null,
  stopReason: data.stop_reason ?? null,
  neededFraction: data.needed_fraction ?? null,
  sql: data.rewritten_query ?? data.iterations?.at(-1)?.sample_query ?? null,
  ci: data.ci_rows ?? null,
  maxRelativeCi: data.max_relative_ci ?? null,
  storedSample: Boolean(data.stored_sample),
  sampledTable: data.sampled_table ?? null,
  warning: data.warning ?? null,
  iterations: data.iterations ?? [],
  profile: data.mode_profile ?? data.approx_mode ?? null,
  accuracyTarget: data.accuracy_target ?? null,
});

const readTheme = (): Theme => {
  try {
    const saved = localStorage.getItem("argus-theme");
    return saved === "light" || saved === "dark" ? saved : "system";
  } catch {
    return "system";
  }
};

export default function App() {
  const [selected, setSelected] = useState<SelectedTable | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const [adding, setAdding] = useState(false);
  const [showHowTo, setShowHowTo] = useState(false);
  const [showBest, setShowBest] = useState(false);
  const [theme, setTheme] = useState<Theme>(readTheme);

  const [query, setQuery] = useState("");
  const [engine, setEngine] = useState<Engine>("duckdb");
  const [target, setTarget] = useState(92);
  const [profile, setProfile] = useState<Profile>("balanced");
  // Off forces fresh runs, so the times shown are real rather than cache lookups.
  const [useCache, setUseCache] = useState(true);
  const [cacheNote, setCacheNote] = useState<string | null>(null);

  const [running, setRunning] = useState<Running>(null);
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState<ExecutionProgress | null>(null);
  const [exact, setExact] = useState<QueryResult | null>(null);
  const [estimate, setEstimate] = useState<QueryResult | null>(null);
  const [exactError, setExactError] = useState<string | null>(null);
  const [estimateError, setEstimateError] = useState<string | null>(null);
  const [comparison, setComparison] = useState<Comparison | null>(null);

  // What the cost model predicted for the run on screen, asked just before it ran.
  const [runAdvice, setRunAdvice] = useState<Advice | null>(null);
  // Set when the cost model itself chose how the query ran (Auto).
  const [autoChosen, setAutoChosen] = useState<"exact" | "approx" | null>(null);
  // Time the normal (exact) query in the background after an estimate.
  const [alsoNormal, setAlsoNormal] = useState(true);
  const [normalRunning, setNormalRunning] = useState(false);
  // Bumped by every new run, so a late background result from an older run is ignored.
  const runToken = useRef(0);
  const editor = useRef<QueryEditorHandle>(null);
  const disabled = running !== null || busy;

  useEffect(() => {
    const root = document.documentElement;
    if (theme === "system") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", theme);
    try {
      localStorage.setItem("argus-theme", theme);
    } catch {
      // Private windows may refuse storage; the theme still applies.
    }
  }, [theme]);

  const pollProgress = (requestId: string) => {
    let active = true;
    const poll = async () => {
      if (!active) return;
      try {
        const res = await fetch(`${BACKEND}/api/sql/execute/progress/${requestId}`);
        if (res.ok) {
          const data = (await res.json()) as ExecutionProgress;
          setProgress(data);
          if (data.status === "completed" || data.status === "error") return;
        }
      } catch {
        return;
      }
      if (active) setTimeout(poll, 350);
    };
    void poll();
    return () => {
      active = false;
    };
  };

  const execute = async (sql: string, mode: string, requestId?: string) => {
    const res = await fetch(`${BACKEND}/api/sql/execute`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        query: sql,
        mode,
        source: engine,
        request_id: requestId,
        accuracy_target: mode === "exact" ? undefined : target,
        profile: mode === "exact" ? undefined : profile,
        use_cache: useCache,
      }),
    });
    return { ok: res.ok, data: (await res.json()) as ExecutePayload };
  };

  const unreachable = `Can't reach the backend at ${BACKEND}. Is it running?`;

  // The cost model's prediction, made before the query runs, so it can be
  // set against the measured times afterwards.
  const adviseFor = async (sql: string): Promise<Advice | null> => {
    if (engine !== "duckdb") return null;
    try {
      const res = await fetch(`${BACKEND}/api/advise`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query: sql, source: engine, accuracy_target: target }),
      });
      return res.ok ? ((await res.json()) as Advice) : null;
    } catch {
      return null;
    }
  };

  const begin = (kind: Running) => {
    runToken.current += 1;
    setRunAdvice(null);
    setAutoChosen(null);
    setNormalRunning(false);
    setRunning(kind);
    setProgress(null);
    setComparison(null);
    setExactError(null);
    setEstimateError(null);
  };

  // Both ways in one benchmark request, so the timings are measured back to
  // back on the same data.
  const runBoth = async (sql: string = query) => {
    if (!sql.trim() || disabled) return;
    begin("both");
    const advice = await adviseFor(sql);
    setRunAdvice(advice);
    const predicted = advice?.predicted_speedup ?? null;
    const requestId = makeRequestId();
    const stop = pollProgress(requestId);
    try {
      const { ok, data } = await execute(sql, "benchmark", requestId);
      if (!ok) {
        // Usually a query the sampler can't estimate; the exact answer still stands.
        setEstimate(null);
        setEstimateError(data?.detail ?? "This query can't be estimated.");
        const exactRun = await execute(sql, "exact");
        if (exactRun.ok) setExact(toResult(sql, exactRun.data));
        else {
          setExact(null);
          setExactError(exactRun.data?.detail ?? "Query failed.");
        }
        return;
      }
      setExact(toResult(sql, { ...(data.exact ?? {}), rows: undefined }, Boolean(data.cached)));
      setEstimate(toResult(sql, data.approx ?? {}, Boolean(data.cached)));
      setComparison({
        speedup: data.speedup ?? null,
        predictedSpeedup: predicted,
        errorPercent: data.error_percent ?? null,
        coverage: data.ci_coverage ?? null,
      });
    } catch {
      setExactError(unreachable);
    } finally {
      stop();
      setRunning(null);
    }
  };

  const runOne = async (mode: "exact" | "approx") => {
    if (!query.trim() || disabled) return;
    const sql = query;
    begin(mode === "exact" ? "exact" : "estimate");
    const advice = await adviseFor(sql);
    setRunAdvice(advice);
    const requestId = makeRequestId();
    const stop = pollProgress(requestId);
    const setResult = mode === "exact" ? setExact : setEstimate;
    const setError = mode === "exact" ? setExactError : setEstimateError;
    try {
      const { ok, data } = await execute(sql, mode, requestId);
      if (!ok) {
        setResult(null);
        setError(data?.detail ?? "Query failed.");
        return;
      }
      const result = toResult(sql, data);
      setResult(result);
      if (mode === "approx" && alsoNormal) void timeNormal(sql, result, advice?.predicted_speedup ?? null);
    } catch {
      setError(unreachable);
    } finally {
      stop();
      setRunning(null);
    }
  };

  // Auto: the cost model predicts both times and the backend runs only the
  // faster way, so an estimate is never used where it would be slower.
  const runAuto = async () => {
    if (!query.trim() || disabled) return;
    const sql = query;
    begin("auto");
    const requestId = makeRequestId();
    const stop = pollProgress(requestId);
    try {
      const { ok, data } = await execute(sql, "auto", requestId);
      if (!ok) {
        setExact(null);
        setEstimate(null);
        setExactError(data?.detail ?? "Query failed.");
        return;
      }
      const choice = data.auto_choice;
      if (choice) {
        setRunAdvice({
          recommendation: choice.recommendation === "estimate" ? "estimate" : "exact",
          method: choice.method === "model" ? "model" : "rule",
          reason: choice.reason ?? "",
          approximable: true,
          predicted_exact_seconds: choice.predicted_exact_seconds ?? undefined,
          predicted_estimate_seconds: choice.predicted_estimate_seconds ?? undefined,
          predicted_speedup: choice.predicted_speedup ?? undefined,
          threshold: choice.threshold ?? undefined,
        });
        setAutoChosen(choice.chosen);
      }
      const result = toResult(sql, data);
      if (choice?.chosen === "approx") {
        setExact(null);
        setEstimate(result);
        if (alsoNormal) void timeNormal(sql, result, choice.predicted_speedup ?? null);
      } else {
        setEstimate(null);
        setExact(result);
      }
    } catch {
      setExactError(unreachable);
    } finally {
      stop();
      setRunning(null);
    }
  };

  // The estimate is on screen already; run the normal query behind it and
  // fill in its time, the speedup and the accuracy when it finishes.
  const timeNormal = async (sql: string, estimated: QueryResult, predicted: number | null) => {
    const token = runToken.current;
    setExact(null);
    setNormalRunning(true);
    try {
      const { ok, data } = await execute(sql, "exact");
      if (token !== runToken.current) return;
      if (!ok) {
        setExactError(data?.detail ?? "The normal query failed.");
        return;
      }
      const normal = toResult(sql, data);
      setExact(normal);
      setComparison({
        speedup: normal.time / Math.max(estimated.time, 1e-9),
        predictedSpeedup: predicted,
        ...accuracy(normal, estimated),
      });
    } catch {
      if (token === runToken.current) setExactError(unreachable);
    } finally {
      if (token === runToken.current) setNormalRunning(false);
    }
  };

  // Picking a query loads it without running it, so results from the
  // previous query are cleared rather than shown against the new one.
  const applyQuery = (sql: string) => {
    setQuery(sql);
    setEngine("duckdb");
    if (running !== null) return;
    runToken.current += 1;
    setNormalRunning(false);
    setExact(null);
    setEstimate(null);
    setExactError(null);
    setEstimateError(null);
    setComparison(null);
    setRunAdvice(null);
    setAutoChosen(null);
  };

  const clearCache = async () => {
    try {
      const res = await fetch(`${BACKEND}/api/cache/clear?clear_history=false`, { method: "POST" });
      setCacheNote(res.ok ? "Cache cleared" : "Could not clear the cache");
    } catch {
      setCacheNote(unreachable);
    }
    setTimeout(() => setCacheNote(null), 2500);
  };

  const sourcesChanged = useCallback((select?: SelectedTable) => {
    setRefreshKey((k) => k + 1);
    setAdding(false);
    if (select) setSelected(select);
  }, []);

  const closeDialog = useCallback(() => setAdding(false), []);

  const runningMessage =
    running === null
      ? null
      : progress?.message ??
        (running === "both"
          ? "Running the exact query, then the estimate"
          : running === "exact"
            ? "Running the exact query"
            : running === "auto"
              ? "The cost model is choosing the faster way, then running it"
              : "Sampling");

  const nothingYet = !exact && !estimate && !exactError && !estimateError && running === null;

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="brand-name">
            Argus<span>.</span>
          </span>
          <span className="brand-sub">Exact and estimated SQL across your databases</span>
        </div>
        <div className="topbar-actions">
          <button
            className="btn-quiet"
            aria-pressed={showBest}
            onClick={() => setShowBest(!showBest)}
          >
            Recommended queries
          </button>
          <button className="btn-quiet" aria-expanded={showHowTo} onClick={() => setShowHowTo(!showHowTo)}>
            How it works
          </button>
          <button
            className="btn-quiet"
            onClick={() => setTheme(theme === "system" ? "dark" : theme === "dark" ? "light" : "system")}
            title="Switch between system, dark and light themes"
          >
            Theme: {theme}
          </button>
        </div>
        {showHowTo && (
          <div className="howto" role="dialog" aria-label="How Argus works">
            <h2>How Argus works</h2>
            <ol>
              <li>
                Add sources on the left: files, PostgreSQL, MySQL or MongoDB. One query can join tables from all of
                them.
              </li>
              <li>
                The <b className="exact">exact</b> answer reads every row. When all tables live in one database, the
                whole query runs there, translated to its dialect (a MongoDB aggregation pipeline for MongoDB).
              </li>
              <li>
                The <b className="estimate">estimate</b> reads a random sample and scales it up. Each value comes with
                a 95% interval: the true value lies inside it 19 times out of 20. The margin you pick (±20%, ±8%,
                ±2%) decides how large a sample to take.
              </li>
              <li>
                For tables in other databases, estimates use a sample stored on this computer, built once from the
                table's panel, so they never wait on the network.
              </li>
            </ol>
          </div>
        )}
      </header>

      <div className="workbench">
        <aside className="sidebar" aria-label="Data sources">
          <div className="sidebar-head">
            <span className="eyebrow">Sources</span>
            <button className="btn btn-small" onClick={() => setAdding(true)} disabled={disabled}>
              Add source
            </button>
          </div>
          <div className="sidebar-scroll">
            <SourcesPanel
              backend={BACKEND}
              refreshKey={refreshKey}
              selected={selected}
              onSelect={setSelected}
              onChanged={() => sourcesChanged()}
              disabled={disabled}
            />
          </div>
          {selected && (
            <TableDetail
              key={selected.relation}
              backend={BACKEND}
              table={selected}
              disabled={disabled}
              onInsert={(text) => editor.current?.insert(text)}
              onUseQuery={applyQuery}
              onLoadingChange={setBusy}
            />
          )}
        </aside>

        <main className="main">
          <div className="editor-card">
            <div className="editor-head">
              <span className="context">
                {engine === "duckdb" ? (
                  <>Any source on the left, by name, e.g. <code>{selected?.relation ?? "rfam.family"}</code></>
                ) : (
                  <>Runs directly on the {engine === "postgres" ? "PostgreSQL" : "MySQL"} server configured for the backend</>
                )}
              </span>
              <select className="select" value={engine} onChange={(e) => setEngine(e.target.value as Engine)} aria-label="Engine">
                <option value="duckdb">Argus engine</option>
                <option value="postgres">Server PostgreSQL</option>
                <option value="mysql">Server MySQL</option>
              </select>
            </div>
            <QueryEditor
              ref={editor}
              value={query}
              onChange={setQuery}
              onRun={() => void runBoth()}
              placeholder={"Start typing: suggestions for the table you name (or pick on the left)\nappear below. Tab accepts one."}
              backend={BACKEND}
              defaultTable={selected?.relation ?? null}
              refreshKey={refreshKey}
            />
          </div>

          <div className="controls" aria-label="Run settings">
            <div className="control">
              <span className="control-label">Estimate margin</span>
              <div className="control-row">
                <div className="segmented" role="group" aria-label="Estimate margin presets">
                  {MARGINS.map((m) => (
                    <button key={m.target} aria-pressed={target === m.target} onClick={() => setTarget(m.target)}>
                      {m.label}
                    </button>
                  ))}
                </div>
                <input
                  type="range"
                  className="range"
                  min={80}
                  max={99.5}
                  step={0.5}
                  value={target}
                  onChange={(e) => setTarget(Number(e.target.value))}
                  aria-label="Estimate margin"
                />
                <output className="control-value">±{(100 - target).toFixed(1).replace(/\.0$/, "")}%</output>
              </div>
              <span className="control-note">95% intervals must fit inside this; tighter takes a bigger sample</span>
            </div>
            <div className="control">
              <span className="control-label">Sampler</span>
              <div className="segmented" role="group" aria-label="Sampler profile">
                {PROFILES.map((p) => (
                  <button key={p.key} aria-pressed={profile === p.key} onClick={() => setProfile(p.key)} title={p.note}>
                    {p.label}
                  </button>
                ))}
              </div>
              <span className="control-note">{PROFILES.find((p) => p.key === profile)?.note}, even if wider</span>
            </div>
            <div className="control">
              <span className="control-label">Timing</span>
              <label className="check">
                <input type="checkbox" checked={useCache} onChange={(e) => setUseCache(e.target.checked)} />
                reuse cached results
              </label>
              <label className="check" title="After an estimate, run the normal query in the background and compare">
                <input type="checkbox" checked={alsoNormal} onChange={(e) => setAlsoNormal(e.target.checked)} />
                time the normal query after an estimate
              </label>
              <span className="control-note">
                <button className="btn-link" onClick={() => void clearCache()}>
                  Clear cache
                </button>
                {cacheNote && <> · {cacheNote}</>}
              </span>
            </div>
          </div>

          <div className="toolbar">
            <span className="hint">
              <kbd>Ctrl</kbd> <kbd>Enter</kbd> runs both
            </span>
            <div className="actions">
              <button
                className="btn btn-auto"
                onClick={() => void runAuto()}
                disabled={disabled || !query.trim()}
                title="The ML cost model predicts both times and runs only the faster way"
              >
                Auto (ML decides)
              </button>
              <button className="btn" onClick={() => void runOne("exact")} disabled={disabled || !query.trim()}>
                <i className="swatch exact" /> Normal (exact)
              </button>
              <button className="btn" onClick={() => void runOne("approx")} disabled={disabled || !query.trim()}>
                <i className="swatch estimate" /> Estimate
              </button>
              <button className="btn btn-primary" onClick={() => void runBoth()} disabled={disabled || !query.trim()}>
                Run both and compare
              </button>
            </div>
          </div>

          {(showBest || (nothingYet && !query.trim())) && (
            <BestQueries
              backend={BACKEND}
              refreshKey={refreshKey}
              onUseQuery={(sql) => {
                applyQuery(sql);
                setShowBest(false);
              }}
              onSourcesChanged={() => sourcesChanged()}
            />
          )}

          {nothingYet && query.trim() ? (
            <p className="not-run">
              Not run yet. <strong>Run both and compare</strong> shows the exact answer, the estimate, the error of each
              value and how the cost model's prediction held up.
            </p>
          ) : nothingYet ? (
            <section className="empty">
              <h2>Ask one question of every database you have.</h2>
              <p>
                Argus runs your SQL two ways: exactly, and as a fast estimate from a sample, and shows how much time
                the estimate saved and how close it came.
              </p>
              <ol className="steps">
                <li>
                  <strong>Add a source</strong>
                  Sample data is one click; your own PostgreSQL, MySQL or MongoDB takes a connection string.
                </li>
                <li>
                  <strong>Pick a table</strong>
                  Its columns and a few starting queries appear under the list.
                </li>
                <li>
                  <strong>Run both and compare</strong>
                  Exact answer, estimate with its error range, and the speedup, side by side.
                </li>
              </ol>
            </section>
          ) : (
            <ResultsView
              backend={BACKEND}
              query={query}
              engine={engine}
              exact={exact}
              estimate={estimate}
              exactError={exactError}
              estimateError={estimateError}
              comparison={comparison}
              runningMessage={runningMessage}
              normalRunning={normalRunning}
              advice={runAdvice}
              auto={autoChosen !== null}
              onLoadQuery={(sql) => setQuery(sql)}
            />
          )}
        </main>
      </div>

      {adding && <AddSourceDialog backend={BACKEND} onClose={closeDialog} onAdded={sourcesChanged} />}
    </div>
  );
}
