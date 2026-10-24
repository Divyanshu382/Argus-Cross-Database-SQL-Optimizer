import { useEffect, useState } from "react";
import { formatPercent } from "../utils/format";
import Spinner from "./Spinner";

interface StoredSample {
  fraction: number;
  sample_rows: number;
  built_at: string;
  build_seconds: number;
}

const FRACTIONS = [
  { value: 0.001, label: "0.1% of rows" },
  { value: 0.01, label: "1% of rows" },
  { value: 0.05, label: "5% of rows" },
  { value: 0.2, label: "20% of rows" },
  // A full copy: for small tables that big ones are joined with.
  { value: 1, label: "Full copy" },
];

const formatAge = (iso: string) => {
  const minutes = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  return hours < 48 ? `${hours} h ago` : `${Math.round(hours / 24)} days ago`;
};

// Stored samples let estimates on a connected database's table run locally
// instead of over the network (see backend/db/samples.py).
export default function SamplePanel({
  backend,
  table,
  optional = false,
  disabled,
  onLoadingChange,
}: {
  backend: string;
  table: string;
  // MongoDB can sample live, so a stored sample only speeds things up.
  optional?: boolean;
  disabled: boolean;
  onLoadingChange: (loading: boolean) => void;
}) {
  const [sample, setSample] = useState<StoredSample | null>(null);
  const [fraction, setFraction] = useState(0.01);
  const [building, setBuilding] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Keyed by table in the parent, so state starts fresh per table.
  useEffect(() => {
    fetch(`${backend}/api/samples/lookup?table=${encodeURIComponent(table)}`)
      .then((res) => (res.ok ? res.json() : null))
      .then(setSample)
      .catch(() => setSample(null));
  }, [backend, table]);

  const build = async () => {
    setError(null);
    setBuilding(true);
    onLoadingChange(true);
    try {
      const res = await fetch(`${backend}/api/samples`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ table, fraction }),
      });
      const data = await res.json();
      if (!res.ok) {
        setError(data?.detail || "Building the sample failed.");
        return;
      }
      setSample(data);
    } catch {
      setError(`Can't reach the backend at ${backend}.`);
    } finally {
      setBuilding(false);
      onLoadingChange(false);
    }
  };

  const remove = async () => {
    const res = await fetch(`${backend}/api/samples?table=${encodeURIComponent(table)}`, { method: "DELETE" });
    if (res.ok) setSample(null);
  };

  return (
    <div className="sample">
      {sample ? (
        <div>
          <strong>{sample.fraction >= 1 ? "Local copy" : `Stored ${formatPercent(sample.fraction)} sample`}</strong>
          {" · "}
          {sample.sample_rows.toLocaleString()} rows, built {formatAge(sample.built_at)}. Estimates read it instead of
          the network; exact queries still go to the source.
        </div>
      ) : optional ? (
        <div>
          <strong>Estimates sample MongoDB live</strong> ($sample, without scanning the collection). A stored sample
          makes them instant and is needed to estimate joins.
        </div>
      ) : (
        <div>
          <strong>Estimates need a stored sample.</strong> Sampling over the network saves nothing, so Argus pulls a
          random sample once and estimates from it. Small tables used in joins: take a full copy.
        </div>
      )}
      <div className="field-row">
        <select
          className="select"
          value={fraction}
          onChange={(e) => setFraction(Number(e.target.value))}
          disabled={disabled}
          aria-label="Sample size"
        >
          {FRACTIONS.map((f) => (
            <option key={f.value} value={f.value}>
              {f.label}
            </option>
          ))}
        </select>
        <button className="btn btn-small" onClick={() => void build()} disabled={disabled}>
          {building && <Spinner />}
          {building ? "Building" : sample ? "Rebuild" : "Build"}
        </button>
        {sample && (
          <button className="btn-link danger" onClick={() => void remove()} disabled={disabled}>
            delete
          </button>
        )}
      </div>
      {building && <div>Reading the table on the source. Large tables take a minute or more.</div>}
      {error && <div className="message error">{error}</div>}
    </div>
  );
}
