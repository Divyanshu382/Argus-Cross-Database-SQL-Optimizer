import { useEffect, useRef, useState } from "react";
import type { SelectedTable, SourceKind } from "../types";
import { ENGINE_BADGE, ENGINE_LABEL } from "../utils/engines";
import Spinner from "./Spinner";

type Tab = "samples" | "database" | "file";
type DbKind = Exclude<SourceKind, "local">;

interface PublicFile {
  name: string;
  label: string;
  url: string;
}

interface PublicDatabase {
  name: string;
  kind: DbKind;
  label: string;
}

const URI_PLACEHOLDER: Record<DbKind, string> = {
  postgres: "postgresql://user:password@host:5432/database",
  mysql: "mysql://user:password@host:3306/database",
  mongodb: "mongodb+srv://user:password@cluster0.example.mongodb.net/",
};

export default function AddSourceDialog({
  backend,
  onClose,
  onAdded,
}: {
  backend: string;
  onClose: () => void;
  onAdded: (select?: SelectedTable) => void;
}) {
  const [tab, setTab] = useState<Tab>("samples");
  const [files, setFiles] = useState<PublicFile[]>([]);
  const [databases, setDatabases] = useState<PublicDatabase[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [kind, setKind] = useState<DbKind>("postgres");
  const [name, setName] = useState("");
  const [uri, setUri] = useState("");
  const [showFields, setShowFields] = useState(false);
  const [host, setHost] = useState("");
  const [port, setPort] = useState("");
  const [database, setDatabase] = useState("");
  const [user, setUser] = useState("");
  const [password, setPassword] = useState("");
  const [schemaName, setSchemaName] = useState("");

  const [url, setUrl] = useState("");
  const [rowLimit, setRowLimit] = useState("1000000");
  const [keepCopy, setKeepCopy] = useState(true);

  const dialogRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    fetch(`${backend}/api/datasets/public`).then((r) => (r.ok ? r.json() : [])).then(setFiles).catch(() => setFiles([]));
    fetch(`${backend}/api/connections/public`).then((r) => (r.ok ? r.json() : [])).then(setDatabases).catch(() => setDatabases([]));
    dialogRef.current?.focus();
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [backend, onClose]);

  const request = async (label: string, path: string, init: RequestInit) => {
    setBusy(label);
    setError(null);
    try {
      const res = await fetch(`${backend}${path}`, init);
      const data = await res.json();
      if (!res.ok) {
        setError(data?.detail || "That didn't work.");
        return null;
      }
      return data;
    } catch {
      setError(`Can't reach the backend at ${backend}.`);
      return null;
    } finally {
      setBusy(null);
    }
  };

  const json = (body: unknown): RequestInit => ({
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

  const loadFile = async (fileUrl: string, tableName?: string) => {
    const limit = parseInt(rowLimit, 10);
    const data = await request(fileUrl, "/api/load-url", json({
      url: fileUrl,
      mode: keepCopy ? "table" : "view",
      table_name: tableName,
      row_limit: limit > 0 ? limit : null,
    }));
    if (data) onAdded({ relation: data.table_name, label: data.table_name, kind: "local" });
  };

  const connectPublic = async (db: PublicDatabase) => {
    const data = await request(db.name, `/api/connections/public/${db.name}`, { method: "POST" });
    if (data) onAdded();
  };

  const connect = async () => {
    const data = await request("connect", "/api/connections", json({
      name: name.trim(),
      kind,
      uri: uri.trim() || null,
      host: host.trim() || null,
      port: port ? Number(port) : null,
      database: database.trim() || null,
      user: user.trim() || null,
      password: password || null,
      schema_name: schemaName.trim() || null,
    }));
    if (data) {
      setPassword("");
      onAdded();
    }
  };

  const upload = async (file: File | undefined) => {
    if (!file) return;
    const form = new FormData();
    form.append("file", file);
    const data = await request(file.name, "/api/upload", { method: "POST", body: form });
    if (data) onAdded({ relation: data.table_name, label: data.table_name, kind: "local" });
  };

  const tabs: Array<[Tab, string]> = [
    ["samples", "Sample data"],
    ["database", "Database"],
    ["file", "File"],
  ];

  return (
    <div className="backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="dialog" role="dialog" aria-modal="true" aria-labelledby="add-source-title" tabIndex={-1} ref={dialogRef}>
        <div className="dialog-head">
          <h2 id="add-source-title">Add a data source</h2>
          <button className="btn-quiet" onClick={onClose} aria-label="Close">
            Close
          </button>
        </div>
        <div className="tabs" role="tablist">
          {tabs.map(([key, label]) => (
            <button key={key} role="tab" className="tab" aria-selected={tab === key} onClick={() => setTab(key)}>
              {label}
            </button>
          ))}
        </div>

        <div className="dialog-body">
          {tab === "samples" && (
            <>
              <p>Real, public data to try Argus with. Files are copied to this computer (up to the row limit below); the databases are queried where they are.</p>
              <div className="cards">
                {files.map((f) => (
                  <div className="card" key={f.url}>
                    <span className="badge">file</span>
                    <span className="card-title">{f.label.split("(")[0].trim()}</span>
                    <button className="btn btn-small" disabled={busy !== null} onClick={() => void loadFile(f.url, f.name)}>
                      {busy === f.url && <Spinner />} Add
                    </button>
                    <span className="card-sub">{f.label.match(/\((.*)\)/)?.[1] ?? "Parquet file"}</span>
                  </div>
                ))}
                {databases.map((d) => (
                  <div className="card" key={d.name}>
                    <span className="badge">{ENGINE_BADGE[d.kind]}</span>
                    <span className="card-title">{d.label.split(":")[0].replace(/\s*\(.*\)/, "")}</span>
                    <button className="btn btn-small" disabled={busy !== null} onClick={() => void connectPublic(d)}>
                      {busy === d.name && <Spinner />} Connect
                    </button>
                    <span className="card-sub">
                      {ENGINE_LABEL[d.kind]} · {d.label.split(":").slice(1).join(":").trim()}
                    </span>
                  </div>
                ))}
              </div>
              <label className="field" style={{ maxWidth: 220 }}>
                Row limit for files
                <input className="input" type="number" min={1} value={rowLimit} onChange={(e) => setRowLimit(e.target.value)} />
              </label>
            </>
          )}

          {tab === "database" && (
            <>
              <div className="segmented" role="radiogroup" aria-label="Database engine" style={{ justifySelf: "start" }}>
                {(["postgres", "mysql", "mongodb"] as DbKind[]).map((k) => (
                  <button key={k} role="radio" aria-checked={kind === k} aria-pressed={kind === k} onClick={() => setKind(k)}>
                    {ENGINE_LABEL[k]}
                  </button>
                ))}
              </div>
              <div className="field-row">
                <label className="field" style={{ flex: "0 0 160px" }}>
                  Name in queries
                  <input className="input" placeholder="sales" value={name} onChange={(e) => setName(e.target.value.toLowerCase())} />
                </label>
                <label className="field">
                  Connection string
                  <input className="input" placeholder={URI_PLACEHOLDER[kind]} value={uri} onChange={(e) => setUri(e.target.value)} autoComplete="off" spellCheck={false} />
                </label>
              </div>
              {(kind === "mongodb" || showFields) && (
                <div className="field-row">
                  {showFields && (
                    <>
                      <label className="field">
                        Host
                        <input className="input" value={host} onChange={(e) => setHost(e.target.value)} />
                      </label>
                      <label className="field" style={{ flex: "0 0 90px", minWidth: 90 }}>
                        Port
                        <input className="input" type="number" value={port} onChange={(e) => setPort(e.target.value)} />
                      </label>
                    </>
                  )}
                  <label className="field">
                    Database{kind === "mongodb" ? " (required)" : ""}
                    <input className="input" placeholder={kind === "mongodb" ? "sample_mflix" : ""} value={database} onChange={(e) => setDatabase(e.target.value)} />
                  </label>
                  {showFields && (
                    <>
                      <label className="field">
                        User
                        <input className="input" value={user} onChange={(e) => setUser(e.target.value)} />
                      </label>
                      <label className="field">
                        Password
                        <input className="input" type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="new-password" />
                      </label>
                    </>
                  )}
                </div>
              )}
              {kind === "postgres" && showFields && (
                <label className="field" style={{ maxWidth: 220 }}>
                  Schema (optional)
                  <input className="input" placeholder="public" value={schemaName} onChange={(e) => setSchemaName(e.target.value)} />
                </label>
              )}
              <div className="field-row" style={{ justifyContent: "space-between", alignItems: "center" }}>
                <button className="btn-link" onClick={() => setShowFields(!showFields)}>
                  {showFields ? "Use a connection string" : "Enter host, user and password instead"}
                </button>
                <button
                  className="btn btn-primary"
                  disabled={busy !== null || !name.trim() || !(uri.trim() || host.trim())}
                  onClick={() => void connect()}
                >
                  {busy === "connect" && <Spinner />} Connect
                </button>
              </div>
              <p className="table-note">
                Read-only. {kind === "mongodb"
                  ? "Collections become tables; nested fields are flattened (customer.city becomes customer_city)."
                  : "Nothing is copied; queries that only touch this database run on it."}{" "}
                Credentials stay in the backend's memory and are forgotten when it restarts.
              </p>
            </>
          )}

          {tab === "file" && (
            <>
              <div className="field-row">
                <label className="field">
                  Parquet, CSV or JSON URL
                  <input className="input" placeholder="https://example.com/data.parquet" value={url} onChange={(e) => setUrl(e.target.value)} spellCheck={false} />
                </label>
                <label className="field" style={{ flex: "0 0 140px", minWidth: 120 }}>
                  Row limit
                  <input className="input" type="number" min={1} value={rowLimit} onChange={(e) => setRowLimit(e.target.value)} />
                </label>
              </div>
              <label className="field-row" style={{ alignItems: "center", gap: 8, fontSize: 13 }}>
                <input type="checkbox" checked={keepCopy} onChange={(e) => setKeepCopy(e.target.checked)} />
                Keep a copy on this computer (recommended: without it every query downloads the file again)
              </label>
              <div>
                <button className="btn btn-primary" disabled={busy !== null || !url.trim()} onClick={() => void loadFile(url.trim())}>
                  {busy === url.trim() && <Spinner />} Load
                </button>
              </div>
              <hr className="divider" />
              <label className="field">
                Or upload a CSV from this computer
                <input type="file" accept=".csv" disabled={busy !== null} onChange={(e) => void upload(e.target.files?.[0])} />
              </label>
            </>
          )}

          {busy && (
            <div className="running">
              <Spinner /> Working on it. Connecting to a remote database can take 10–20 seconds.
            </div>
          )}
          {error && <div className="message error">{error}</div>}
        </div>
      </div>
    </div>
  );
}
