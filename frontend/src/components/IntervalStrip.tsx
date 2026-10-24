import { formatCell } from "../utils/format";

const WIDTH = 132;
const HEIGHT = 14;
const PAD = 5;

/**
 * One estimate against its 95% interval, and the exact value when known.
 * Bar: the interval. Dot: the estimate. Tick: the exact value. Each cell
 * has its own scale; the strip shows where the truth fell relative to the
 * interval, and the numbers beside it carry the values.
 */
export default function IntervalStrip({
  estimate,
  width,
  exact,
}: {
  estimate: number;
  width: number;
  exact: number | null;
}) {
  const low = estimate - width;
  const high = estimate + width;
  const values = exact === null ? [low, high] : [low, high, exact];
  let min = Math.min(...values);
  let max = Math.max(...values);
  const span = max - min || Math.abs(estimate) || 1;
  min -= span * 0.08;
  max += span * 0.08;
  const x = (v: number) => PAD + ((v - min) / (max - min)) * (WIDTH - 2 * PAD);
  const mid = HEIGHT / 2;
  const inside = exact !== null && exact >= low && exact <= high;
  const label =
    `Estimate ${formatCell(estimate)}, 95% interval ${formatCell(low)} to ${formatCell(high)}` +
    (exact === null ? "" : `. Exact ${formatCell(exact)}: ${inside ? "inside" : "outside"} the interval.`);

  return (
    <svg className="strip" width={WIDTH} height={HEIGHT} role="img" aria-label={label}>
      <title>{label}</title>
      <line x1={PAD} x2={WIDTH - PAD} y1={mid} y2={mid} stroke="var(--line)" strokeWidth={1} />
      <line
        x1={x(low)}
        x2={x(high)}
        y1={mid}
        y2={mid}
        stroke="var(--estimate)"
        strokeOpacity={0.35}
        strokeWidth={4}
        strokeLinecap="round"
      />
      {exact !== null && (
        <line x1={x(exact)} x2={x(exact)} y1={1} y2={HEIGHT - 1} stroke="var(--exact)" strokeWidth={2} />
      )}
      <circle cx={x(estimate)} cy={mid} r={4} fill="var(--estimate)" stroke="var(--raised)" strokeWidth={2} />
    </svg>
  );
}
