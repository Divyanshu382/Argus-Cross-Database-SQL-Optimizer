import Spinner from "./Spinner";

export interface SpeedComparison {
  exactTime: number;
  approxTime: number;
  errorPercent: number | null;
  speedup: number | null;
  sampleRate?: number | null;
  rowsSampled?: number | null;
  stopReason?: string | null;
  // Share of exact values that fell inside their estimate's 95% interval.
  coverage?: number | null;
}

const formatTime = (seconds: number) => {
  if (!Number.isFinite(seconds)) return "n/a";
  if (seconds < 0.001) return `${(seconds * 1_000_000).toFixed(0)} µs`;
  if (seconds < 1) return `${(seconds * 1000).toFixed(1)} ms`;
  return `${seconds.toFixed(2)} s`;
};

const formatCount = (value: number) => new Intl.NumberFormat().format(value);

export default function SpeedPanel({
  time,
  approx,
  cached,
  sampleRate,
  rowsSampled,
  comparison,
  comparing,
  onCompare,
  comparisonError,
}: {
  time: number | null | undefined;
  approx?: boolean;
  cached?: boolean;
  sampleRate?: number | null;
  rowsSampled?: number | null;
  comparison?: SpeedComparison | null;
  comparing?: boolean;
  onCompare?: () => void;
  comparisonError?: string | null;
}) {
  if (typeof time !== "number") return null;

  const accent = cached ? "#5aaaf5" : approx ? "#f5b84a" : "#7fd1a8";

  const speedup =
    comparison && comparison.approxTime > 0
      ? comparison.exactTime / comparison.approxTime
      : null;
  const faster = speedup !== null && speedup > 1;
  const saved = comparison ? comparison.exactTime - comparison.approxTime : null;

  const maxTime = comparison
    ? Math.max(comparison.exactTime, comparison.approxTime, 1e-9)
    : 1;
  const exactWidth = comparison
    ? `${Math.max((comparison.exactTime / maxTime) * 100, 2)}%`
    : "0%";
  const approxWidth = comparison
    ? `${Math.max((comparison.approxTime / maxTime) * 100, 2)}%`
    : "0%";

  const verdictColor = faster ? "#7fd1a8" : "#f5b84a";

  return (
    <div
      style={{
        marginBottom: "10px",
        padding: "14px",
        borderRadius: "12px",
        background:
          "linear-gradient(135deg, rgba(255,255,255,0.045), rgba(255,255,255,0.015))",
        border: "0.5px solid rgba(255,255,255,0.10)",
      }}
    >
      <div
        style={{
          fontSize: "10px",
          letterSpacing: "0.8px",
          textTransform: "uppercase",
          color: "#7c8794",
          fontWeight: 700,
          marginBottom: "10px",
        }}
      >
        Execution speed
      </div>

      {/* headline */}
      <div
        style={{
          display: "flex",
          alignItems: "baseline",
          gap: "10px",
          flexWrap: "wrap",
        }}
      >
        <span
          style={{
            fontSize: "30px",
            fontWeight: 700,
            color: accent,
            lineHeight: 1,
            letterSpacing: "-0.5px",
          }}
        >
          {formatTime(time)}
        </span>
        <span style={{ fontSize: "12px", color: "#8b949e" }}>
          {cached
            ? "served from cache"
            : approx
              ? "approximate"
              : "exact — full scan"}
          {typeof sampleRate === "number" &&
            ` · sampled ${(sampleRate * 100).toFixed(0)}%`}
          {typeof rowsSampled === "number" &&
            ` · ${formatCount(rowsSampled)} rows read`}
        </span>
      </div>

      {/* comparison bars */}
      {comparison && (
        <div style={{ marginTop: "14px", display: "grid", gap: "9px" }}>
          {[
            {
              label: "Exact query",
              value: comparison.exactTime,
              width: exactWidth,
              color: "#5aaaf5",
            },
            {
              label: "Approximate (sampled)",
              value: comparison.approxTime,
              width: approxWidth,
              color: "#f5b84a",
            },
          ].map((bar) => (
            <div key={bar.label}>
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  fontSize: "10px",
                  color: "#8b949e",
                  marginBottom: "3px",
                }}
              >
                <span>{bar.label}</span>
                <span style={{ color: bar.color, fontWeight: 600 }}>
                  {formatTime(bar.value)}
                </span>
              </div>
              <div
                style={{
                  height: "9px",
                  borderRadius: "999px",
                  background: "rgba(255,255,255,0.07)",
                  overflow: "hidden",
                }}
              >
                <div
                  style={{
                    height: "100%",
                    width: bar.width,
                    background: bar.color,
                    borderRadius: "999px",
                    transition: "width 0.4s ease",
                  }}
                />
              </div>
            </div>
          ))}
        </div>
      )}

      {/* verdict */}
      {comparison && speedup !== null && (
        <div
          style={{
            marginTop: "12px",
            padding: "9px 11px",
            borderRadius: "8px",
            background: faster
              ? "rgba(127,209,168,0.10)"
              : "rgba(245,184,74,0.10)",
            border: `0.5px solid ${faster ? "rgba(127,209,168,0.28)" : "rgba(245,184,74,0.28)"}`,
            fontSize: "12px",
            color: verdictColor,
            display: "flex",
            gap: "14px",
            flexWrap: "wrap",
            alignItems: "center",
          }}
        >
          <strong style={{ fontSize: "13px" }}>
            {faster
              ? `${speedup.toFixed(1)}× faster`
              : `${(1 / speedup).toFixed(1)}× slower`}
          </strong>
          {saved !== null && (
            <span>
              {saved >= 0
                ? `saved ${formatTime(saved)}`
                : `cost ${formatTime(-saved)} extra`}
            </span>
          )}
          {typeof comparison.errorPercent === "number" && (
            <span>average error {comparison.errorPercent.toFixed(2)}%</span>
          )}
          {typeof comparison.coverage === "number" && (
            <span title="How many exact values fell inside their estimate's 95% confidence interval">
              {Math.round(comparison.coverage * 100)}% of true values inside the ± intervals
            </span>
          )}
          {comparison.stopReason && (
            <span style={{ color: "#7c8794" }}>{comparison.stopReason}</span>
          )}
          <span style={{ color: "#7c8794", fontSize: "10px" }}>
            both timings measured just now
          </span>
        </div>
      )}

      {/* honesty note when sampling did not pay off */}
      {comparison && speedup !== null && !faster && (
        <div
          style={{
            marginTop: "8px",
            fontSize: "11px",
            color: "#7c8794",
            lineHeight: 1.5,
          }}
        >
          Sampling overhead exceeded the scan it avoided — this dataset is small
          enough that a full scan is already cheaper. Approximation pays off as
          the table grows.
        </div>
      )}

      {/* call to action */}
      {!comparison && onCompare && (
        <button
          type="button"
          onClick={onCompare}
          disabled={comparing}
          style={{
            marginTop: "12px",
            padding: "8px 14px",
            borderRadius: "8px",
            border: "0.5px solid rgba(90,170,245,0.35)",
            background: comparing
              ? "rgba(90,170,245,0.08)"
              : "rgba(90,170,245,0.16)",
            color: "#9dccfb",
            fontSize: "12px",
            fontWeight: 600,
            cursor: comparing ? "default" : "pointer",
            display: "flex",
            alignItems: "center",
            gap: "8px",
          }}
        >
          {comparing ? (
            <>
              <Spinner size={12} />
              Running full scan…
            </>
          ) : (
            "Compare with normal processing"
          )}
        </button>
      )}

      {!comparison && onCompare && !comparisonError && (
        <div
          style={{
            marginTop: "6px",
            fontSize: "10px",
            color: "#6b7480",
            lineHeight: 1.5,
          }}
        >
          Runs the query exactly as well, so the saving shown is measured — not
          estimated.
        </div>
      )}

      {!comparison && comparisonError && (
        <div
          style={{
            marginTop: "10px",
            padding: "9px 11px",
            borderRadius: "8px",
            background: "rgba(245,184,74,0.08)",
            border: "0.5px solid rgba(245,184,74,0.22)",
            fontSize: "11px",
            color: "#c9a971",
            lineHeight: 1.6,
          }}
        >
          <strong style={{ display: "block", marginBottom: "3px" }}>
            Comparison not available for this query
          </strong>
          {comparisonError}
          <div style={{ marginTop: "6px", color: "#7c8794" }}>
            The query above still ran correctly — only the speed comparison
            needs a query the sampler can rewrite.
          </div>
        </div>
      )}
    </div>
  );
}
