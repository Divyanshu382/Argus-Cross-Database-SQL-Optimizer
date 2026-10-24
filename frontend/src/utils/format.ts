/** 0.01 -> "1%", 0.001 -> "0.1%". */
export const formatPercent = (fraction: number) => {
  const percent = fraction * 100;
  return `${percent < 1 ? Number(percent.toFixed(2)) : Math.round(percent)}%`;
};

export const formatSeconds = (seconds: number) => {
  if (!Number.isFinite(seconds)) return "–";
  if (seconds < 0.001) return `${Math.max(1, Math.round(seconds * 1_000_000))} µs`;
  if (seconds < 1) return `${(seconds * 1000).toFixed(seconds < 0.01 ? 1 : 0)} ms`;
  if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 2 : 1)} s`;
  return `${Math.floor(seconds / 60)} min ${Math.round(seconds % 60)} s`;
};

const numberFormat = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 });
const compactFormat = new Intl.NumberFormat(undefined, { maximumSignificantDigits: 3 });

export const formatNumber = (value: number) => numberFormat.format(value);

/** For ± widths: three significant digits is all the precision they carry. */
export const formatWidth = (value: number) => compactFormat.format(value);

export const isNumeric = (value: unknown) =>
  typeof value === "number" || (typeof value === "string" && /^-?\d+(\.\d+)?$/.test(value));

export const formatCell = (value: unknown) => {
  if (value === null || value === undefined) return "NULL";
  if (isNumeric(value)) return numberFormat.format(Number(value));
  return String(value);
};
