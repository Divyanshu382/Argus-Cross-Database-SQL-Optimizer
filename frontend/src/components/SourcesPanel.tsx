import { useEffect, useState } from "react";
import type { SelectedTable, SourceKind } from "../types";
import { ENGINE_BADGE, ENGINE_LABEL } from "../utils/engines";
import { formatNumber } from "../utils/format";

interface LocalTable {
  name: string;
  kind: "table" | "view";
  rows: number | null;
}

interface Connection {
  name: string;
  kind: Exclude<SourceKind, "local">;
  target: string;
  mode: "attached" | "passthrough" | "live";
  tables: string[];
  note: string | null;
}

const MODE_NOTE: Record<Connection["mode"], string> = {
  attached: "Queried in place. Queries that only touch this database run there.",
  passthrough: "Queried in place, but this server blocks catalog access, so whole tables are read. Prefer small tables.",
  live: "Queried live. Single-collection SQL runs inside MongoDB as an aggregation pipeline.",
};

const SHOWN = 12;

export default function SourcesPanel({
  backend,
  refreshKey,
  selected,
  onSelect,
  onChanged,
  disabled,
}: {
  backend: string;
  refreshKey: number;
  selected: SelectedTable | null;
  onSelect: (table: SelectedTable) => void;
  onChanged: () => void;
  disabled: boolean;
}) {
  const [local, setLocal] = useState<LocalTable[]>([]);
  const [connections, setConnections] = useState<Connection[]>([]);
  const [filters, setFilters] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const load = async () => {
      try {
        const [tables, conns] = await Promise.all([
          fetch(`${backend}/api/tables`).then((r) => (r.ok ? r.json() : [])),
          fetch(`${backend}/api/connections`).then((r) => (r.ok ? r.json() : [])),
        ]);
        setLocal(tables);
        setConnections(conns);
        setError(null);
      } catch {
        setError(`Can't reach the backend at ${backend}. Start it, then reload this page.`);
      }
    };
    void load();
  }, [backend, refreshKey]);

  const disconnect = async (name: string) => {
    const res = await fetch(`${backend}/api/connections/${name}`, { method: "DELETE" });
    if (res.ok) onChanged();
  };

  const item = (relation: string, label: string, kind: SourceKind, meta?: string) => (
    <li key={relation}>
      <button
        className={`src-item${selected?.relation === relation ? " active" : ""}`}
        onClick={() => onSelect({ relation, label, kind })}
        disabled={disabled}
        title={relation}
      >
        <span className="name">{label}</span>
        {meta && <span className="meta">{meta}</span>}
      </button>
    </li>
  );

  return (
    <>
      {error && <div className="src-empty">{error}</div>}

      <section className="src-group">
        <div className="src-group-head">
          <span className="badge">{ENGINE_BADGE.local}</span>
          <span className="grow">This computer</span>
        </div>
        {local.length === 0 ? (
          <div className="src-note">No local tables yet. Add a sample dataset or upload a CSV.</div>
        ) : (
          <ul className="src-list">
            {local.map((t) =>
              item(t.name, t.name, "local", t.kind === "view" ? "view" : t.rows !== null ? `${formatNumber(t.rows)} rows` : undefined),
            )}
          </ul>
        )}
      </section>

      {connections.map((c) => {
        const needle = (filters[c.name] ?? "").trim().toLowerCase();
        const matches = c.tables.filter((t) => t.toLowerCase().includes(needle));
        return (
          <section className="src-group" key={c.name}>
            <div className="src-group-head">
              <span className="badge">{ENGINE_BADGE[c.kind]}</span>
              <span className="grow" title={`${ENGINE_LABEL[c.kind]} · ${c.target}`}>
                {c.name}
              </span>
              <button className="btn-link danger" onClick={() => void disconnect(c.name)} disabled={disabled}>
                disconnect
              </button>
            </div>
            <div className="src-note">
              {ENGINE_LABEL[c.kind]} at {c.target}. {MODE_NOTE[c.mode]}
            </div>
            {c.tables.length > SHOWN && (
              <input
                className="input src-filter"
                placeholder={`Filter ${c.tables.length} tables`}
                value={filters[c.name] ?? ""}
                onChange={(e) => setFilters({ ...filters, [c.name]: e.target.value })}
              />
            )}
            <ul className="src-list">
              {matches.slice(0, SHOWN).map((t) => item(t, t.slice(c.name.length + 1), c.kind))}
            </ul>
            {matches.length > SHOWN && <div className="src-more">{matches.length - SHOWN} more, filter to find them</div>}
            {matches.length === 0 && <div className="src-more">No tables match.</div>}
          </section>
        );
      })}
    </>
  );
}
