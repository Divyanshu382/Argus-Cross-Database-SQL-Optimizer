import { useEffect, useState } from "react";
import type { Suggestion } from "../utils/assist";
import { formatSeconds } from "../utils/format";
import Spinner from "./Spinner";

interface Win extends Suggestion {
  table: string;
}

interface BestResponse {
  queries: Win[];
  reconnect: Array<{ name: string; public: boolean }>;
}

const SHOWN = 8;

const speed = (x: number) => (x >= 10 ? Math.round(x).toLocaleString() : x.toFixed(1));

/** Queries across every source where the estimate is predicted to beat the
 * normal query, ranked by predicted speedup. */
export default function BestQueries({
  backend,
  refreshKey,
  onUseQuery,
  onSourcesChanged,
}: {
  backend: string;
  refreshKey: number;
  onUseQuery: (sql: string) => void;
  onSourcesChanged: () => void;
}) {
  const [data, setData] = useState<BestResponse | null>(null);
  const [failed, setFailed] = useState(false);
  const [connecting, setConnecting] = useState<string | null>(null);
  const [showAll, setShowAll] = useState(false);

  useEffect(() => {
    // First time per source this profiles tables and asks the cost model,
    // which can take a few seconds; later calls are cached on the server.
    fetch(`${backend}/api/suggest/best`)
      .then((r) => (r.ok ? r.json() : Promise.reject()))
      .then(setData)
      .catch(() => setFailed(true));
  }, [backend, refreshKey]);

  const reconnect = async (name: string) => {
    setConnecting(name);
    try {
      const res = await fetch(`${backend}/api/connections/public/${name}`, { method: "POST" });
      if (res.ok) onSourcesChanged();
    } finally {
      setConnecting(null);
    }
  };

  const ranked = [...(data?.queries ?? [])].sort((a, b) => (b.prediction?.speedup ?? 0) - (a.prediction?.speedup ?? 0));
  const shown = showAll ? ranked : ranked.slice(0, SHOWN);
  // Bars on a log scale: speedups run from ~2× to thousands.
  const top = Math.log10(Math.max(2, ranked[0]?.prediction?.speedup ?? 2));

  return (
    <section className="best" aria-label="Recommended queries">
      <div className="best-head">
        <h2>Recommended queries</h2>
        <span className="best-note">ranked by predicted speedup · click one to load it</span>
      </div>
      {data === null && !failed && (
        <div className="running">
          <Spinner /> Looking across your sources…
        </div>
      )}
      {failed && <p className="table-note">Couldn't build the list. Is the backend running?</p>}
      {data && data.queries.length === 0 && (
        <p className="table-note">
          No predicted wins yet. Connect a database from <strong>Add source</strong> (Rfam is one click), then build a
          stored sample of its largest table.
        </p>
      )}
      {data?.reconnect.map((r) => (
        <div className="message info" key={r.name}>
          <code>{r.name}</code> isn't connected since the last restart.{" "}
          {r.public ? (
            <button className="btn btn-small" onClick={() => void reconnect(r.name)} disabled={connecting !== null}>
              {connecting === r.name && <Spinner />} Reconnect {r.name}
            </button>
          ) : (
            "Reconnect it from Add source."
          )}
        </div>
      ))}
      {ranked.length > 0 && (
        <div className="table-wrap">
          <table className="data-table best-table">
            <thead>
              <tr>
                <th className="num">#</th>
                <th>Query</th>
                <th>Table</th>
                <th className="num">Normal</th>
                <th className="num">Estimate</th>
                <th className="num">Saves</th>
                <th>Speedup</th>
              </tr>
            </thead>
            <tbody>
              {shown.map((q, i) => {
                const p = q.prediction;
                const x = p?.speedup ?? 0;
                return (
                  <tr key={q.sql} tabIndex={0} onClick={() => onUseQuery(q.sql)} onKeyDown={(e) => e.key === "Enter" && onUseQuery(q.sql)}>
                    <td className="num rank">{i + 1}</td>
                    <td className="best-sql">
                      <code>{q.sql.replace(/\s+/g, " ")}</code>
                    </td>
                    <td className="best-table-name">{q.table}</td>
                    <td className="num">{p?.exact_seconds ? formatSeconds(p.exact_seconds) : "–"}</td>
                    <td className="num">{p?.estimate_seconds ? formatSeconds(p.estimate_seconds) : "–"}</td>
                    <td className="num">
                      {p?.exact_seconds && p?.estimate_seconds ? formatSeconds(p.exact_seconds - p.estimate_seconds) : "–"}
                    </td>
                    <td>
                      <span className="best-x">
                        <span className="best-bar" style={{ width: `${(Math.log10(Math.max(x, 1)) / top) * 100}%` }} />
                        <span className="num">{speed(x)}×</span>
                      </span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {ranked.length > SHOWN && (
        <button className="btn-link best-more" onClick={() => setShowAll(!showAll)}>
          {showAll ? `Show top ${SHOWN}` : `Show all ${ranked.length}`}
        </button>
      )}
    </section>
  );
}
