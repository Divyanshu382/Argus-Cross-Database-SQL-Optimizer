import { forwardRef, useEffect, useImperativeHandle, useMemo, useReducer, useRef, useState } from "react";
import {
  type Suggestion,
  type SuggestResponse,
  guessTable,
  predictionTag,
  rankQueries,
  tablesIn,
} from "../utils/assist";

export interface QueryEditorHandle {
  /** Insert text at the cursor (or replace the selection). */
  insert: (text: string) => void;
}

interface Props {
  value: string;
  onChange: (value: string) => void;
  onRun: () => void;
  placeholder: string;
  backend: string;
  /** The table picked in the sources panel, used until the query names one. */
  defaultTable: string | null;
  /** Bumped when sources change, so the table list is reloaded. */
  refreshKey: number;
}

const QueryEditor = forwardRef<QueryEditorHandle, Props>(function QueryEditor(
  { value, onChange, onRun, placeholder, backend, defaultTable, refreshKey },
  ref,
) {
  const textarea = useRef<HTMLTextAreaElement>(null);
  const [focused, setFocused] = useState(false);
  const [dismissedAt, setDismissedAt] = useState<string | null>(null);
  const [highlight, setHighlight] = useState(0);
  const [localTables, setLocalTables] = useState<string[]>([]);
  // Per-table caches; the reducer re-renders when a fetch lands.
  const columns = useRef(new Map<string, string[]>());
  const suggestions = useRef(new Map<string, SuggestResponse | "loading">());
  const [version, refresh] = useReducer((n: number) => n + 1, 0);

  useImperativeHandle(ref, () => ({
    insert(text: string) {
      const el = textarea.current;
      if (!el) return onChange(value + text);
      const start = el.selectionStart ?? value.length;
      const end = el.selectionEnd ?? value.length;
      onChange(value.slice(0, start) + text + value.slice(end));
      requestAnimationFrame(() => {
        el.focus();
        el.setSelectionRange(start + text.length, start + text.length);
      });
    },
  }));

  useEffect(() => {
    fetch(`${backend}/api/tables`)
      .then((r) => (r.ok ? r.json() : []))
      .then((tables: Array<{ name: string }>) => setLocalTables(tables.map((t) => t.name)))
      .catch(() => setLocalTables([]));
  }, [backend, refreshKey]);

  const named = tablesIn(value);
  const target = named.at(-1) ?? defaultTable ?? guessTable(value, columns.current);

  // No FROM yet and nothing picked: learn the local tables' columns (cheap,
  // once) so the table can be guessed from the column names being typed.
  useEffect(() => {
    if (named.length || defaultTable || !value.trim()) return;
    for (const table of localTables.slice(0, 20)) {
      if (columns.current.has(table)) continue;
      columns.current.set(table, []);
      fetch(`${backend}/api/columns?table=${encodeURIComponent(table)}`)
        .then((r) => (r.ok ? r.json() : []))
        .then((cols: Array<{ name: string }>) => {
          columns.current.set(table, cols.map((c) => c.name));
          refresh();
        })
        .catch(() => undefined);
    }
    // `named` is derived from value, which is listed.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value, defaultTable, localTables, backend]);

  // Comparable queries for the table, fetched once (shortly after typing pauses).
  useEffect(() => {
    if (!target || suggestions.current.has(target)) return;
    const timer = setTimeout(() => {
      suggestions.current.set(target, "loading");
      refresh();
      fetch(`${backend}/api/suggest?table=${encodeURIComponent(target)}`)
        .then((r) => (r.ok ? r.json() : Promise.reject()))
        .then((found: SuggestResponse) => suggestions.current.set(target, found))
        .catch(() => suggestions.current.delete(target))
        .finally(refresh);
    }, 300);
    return () => clearTimeout(timer);
  }, [target, backend]);

  const known = target ? suggestions.current.get(target) : undefined;
  const items: Suggestion[] = useMemo(() => {
    if (!focused || !value.trim() || dismissedAt === value || !known || known === "loading") return [];
    return rankQueries(known.suggestions, value);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focused, value, dismissedAt, known, version]);

  const typing = focused && !!value.trim() && dismissedAt !== value;
  const note = known && known !== "loading" && !known.suggestions.length ? known.note : null;
  const open = typing && (items.length > 0 || known === "loading" || !!note);
  const active = Math.min(highlight, Math.max(items.length - 1, 0));

  const accept = (item: Suggestion) => {
    onChange(item.sql);
    // The query is chosen: close the panel (it covers the run buttons) until
    // the user types again.
    setDismissedAt(item.sql);
    setHighlight(0);
    requestAnimationFrame(() => {
      const el = textarea.current;
      el?.focus();
      el?.setSelectionRange(item.sql.length, item.sql.length);
    });
  };

  const lines = Math.max(value.split("\n").length, 7);

  return (
    <div className="editor-wrap">
      <div className="editor">
        <div className="gutter" aria-hidden="true">
          {Array.from({ length: lines }, (_, i) => i + 1).join("\n")}
        </div>
        <textarea
          ref={textarea}
          className="code"
          value={value}
          onChange={(e) => {
            onChange(e.target.value);
            setHighlight(0);
          }}
          onFocus={() => setFocused(true)}
          onBlur={() => setFocused(false)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
              e.preventDefault();
              setDismissedAt(value);
              onRun();
              return;
            }
            if (!items.length) return;
            if (e.key === "ArrowDown") {
              e.preventDefault();
              setHighlight((active + 1) % items.length);
            } else if (e.key === "ArrowUp") {
              e.preventDefault();
              setHighlight((active - 1 + items.length) % items.length);
            } else if (e.key === "Tab") {
              e.preventDefault();
              accept(items[active]);
            } else if (e.key === "Escape") {
              setDismissedAt(value);
            }
          }}
          rows={lines}
          placeholder={placeholder}
          spellCheck={false}
          aria-label="SQL query"
          aria-autocomplete="list"
          aria-expanded={open}
          aria-controls="assist-list"
        />
      </div>
      {open && (
        <div className="assist" role="listbox" id="assist-list" aria-label="Queries you can compare">
          <div className="assist-head">
            <span>
              Queries you can compare on <code>{target}</code>
            </span>
            {items.length > 0 && (
              <span className="hint">
                <kbd>↑</kbd> <kbd>↓</kbd> choose · <kbd>Tab</kbd> use · <kbd>Esc</kbd> close
              </span>
            )}
          </div>
          {items.map((item, i) => (
            <button
              key={item.sql}
              role="option"
              aria-selected={i === active}
              className={`assist-item${i === active ? " active" : ""}`}
              onMouseDown={(e) => e.preventDefault()}
              onMouseEnter={() => setHighlight(i)}
              onClick={() => accept(item)}
              title={item.why}
            >
              <span className="assist-label">
                {item.title}
                <code>{item.sql.replace(/\s+/g, " ")}</code>
              </span>
              {item.prediction && <span className="assist-detail">{predictionTag(item.prediction)}</span>}
            </button>
          ))}
          {known === "loading" && <div className="assist-loading">Looking at {target} for queries to compare…</div>}
          {note && <div className="assist-loading">{note}</div>}
        </div>
      )}
    </div>
  );
});

export default QueryEditor;
