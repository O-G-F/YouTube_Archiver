interface Series {
  label: string;
  color: string;
  values: number[];
}

/** Tiny dependency-free SVG line chart for progress history. */
export function Sparkline({
  series,
  width = 520,
  height = 120,
  max,
}: {
  series: Series[];
  width?: number;
  height?: number;
  max?: number;
}) {
  const n = Math.max(...series.map((s) => s.values.length), 0);
  if (n < 2) {
    return <p className="muted small">Not enough data points yet (need ≥ 2 runs).</p>;
  }
  const pad = 6;
  const top = Math.max(max ?? 0, ...series.flatMap((s) => s.values), 1);
  const x = (i: number) => pad + (i * (width - 2 * pad)) / (n - 1);
  const y = (v: number) => height - pad - (v / top) * (height - 2 * pad);

  return (
    <div>
      <svg width={width} height={height} role="img" aria-label="progress history chart" className="sparkline">
        <rect x={0} y={0} width={width} height={height} fill="transparent" />
        {series.map((s) => {
          const pts = s.values.map((v, i) => `${x(i)},${y(v)}`).join(" ");
          return <polyline key={s.label} points={pts} fill="none" stroke={s.color} strokeWidth={2} />;
        })}
      </svg>
      <div className="row" style={{ flexWrap: "wrap", gap: 12, marginTop: 4 }}>
        {series.map((s) => (
          <span key={s.label} className="small" style={{ color: s.color }}>
            ■ {s.label}
          </span>
        ))}
        <span className="muted small">max {top}</span>
      </div>
    </div>
  );
}
