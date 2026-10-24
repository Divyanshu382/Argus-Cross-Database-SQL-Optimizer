import { useEffect, useState } from "react";
import type { Column, SelectedTable } from "../types";
import { ENGINE_LABEL } from "../utils/engines";
import { type SuggestResponse, predictionTag } from "../utils/assist";
import { quoteIdent } from "../utils/examples";
import SamplePanel from "./SamplePanel";

export default function TableDetail({
  backend,
  table,
  disabled,
  onInsert,
  onUseQuery,
  onLoadingChange,
}: {
  backend: string;
  table: SelectedTable;
  disabled: boolean;
  onInsert: (text: string) => void;
  onUseQuery: (sql: string) => void;
  onLoadingChange: (loading: boolean) => void;
}) {
  const [columns, setColumns] = useState<Column[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [suggested, setSuggested] = useState<SuggestResponse | null>(null);
  const [suggestError, setSuggestError] = useState(false);

  // Profiling takes a sample of the table, so it can take a moment on a
  // remote source. Only queries that can be run both ways come back.
  useEffect(() => {
    fetch(`${backend}/api/suggest?table=${encodeURIComponent(table.relation)}`)
      .then((res) => (res.ok ? res.json() : Promise.reject()))
      .then(setSuggested)
      .catch(() => setSuggestError(true));
  }, [backend, table.relation]);

  // The parent keys this component by table, so state starts fresh per table.
  useEffect(() => {
    fetch(`${backend}/api/columns?table=${encodeURIComponent(table.relation)}`)
      .then(async (res) => {
        const data = await res.json();
        if (!res.ok) throw new Error(data?.detail ?? "Could not read the columns.");
        setColumns(data);
      })
      .catch((e: Error) => setError(e.message));
  }, [backend, table.relation]);


  return (
    <div className="detail">
      <div>
        <div className="detail-title">{table.relation}</div>
        <div className="detail-sub">
          {ENGINE_LABEL[table.kind]}
          {columns ? " · click a column to insert it" : ""}
        </div>
      </div>
      {error && <div className="message error">{error}</div>}
      {columns && (
        // Wide tables would push everything else off screen, so they start closed.
        <details className="col-details" open={columns.length <= 16}>
          <summary>{columns.length} columns</summary>
          <div className="col-list">
            {columns.map((c) => (
              <button key={c.name} className="col-chip" onClick={() => onInsert(quoteIdent(c.name))} title={c.type}>
                {c.name}
                <i>{c.type.toLowerCase().replace(/\(.*\)/, "")}</i>
              </button>
            ))}
          </div>
        </details>
      )}
      <div className="examples">
        <span className="eyebrow">Queries you can compare</span>
        {suggested === null && !suggestError && <span className="detail-sub">Looking at the data…</span>}
        {suggestError && <span className="detail-sub">Couldn't read this table to suggest queries.</span>}
        {suggested?.note && <span className="detail-sub">{suggested.note}</span>}
        {suggested?.suggestions.map((item) => (
          <button key={item.sql} className="suggestion" onClick={() => onUseQuery(item.sql)} title={item.why}>
            <span className="suggestion-head">
              <span className="suggestion-title">{item.title}</span>
              {item.prediction && <span className="suggestion-tag">{predictionTag(item.prediction)}</span>}
            </span>
            <code>{item.sql.replace(/\s+/g, " ")}</code>
          </button>
        ))}
      </div>
      {table.kind !== "local" && (
        <SamplePanel
          key={table.relation}
          backend={backend}
          table={table.relation}
          optional={table.kind === "mongodb"}
          disabled={disabled}
          onLoadingChange={onLoadingChange}
        />
      )}
    </div>
  );
}
