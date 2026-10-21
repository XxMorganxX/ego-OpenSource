export interface TrendPoint {
  x: number;
  y: number;
  label: string;
}

export interface TrendSeries {
  label: string;
  color: string;
  points: TrendPoint[];
}

const W = 600;
const PAD = { top: 10, right: 12, bottom: 20, left: 40 };

/**
 * Dependency-free SVG line chart for small run-over-run series. x is an ordinal
 * position (run index), y a metric value; `label` names the run (a date). The svg
 * scrolls inside its container rather than ever overflowing the page.
 */
export function TrendChart({
  series,
  yMax = 1,
  height = 180,
}: {
  series: TrendSeries[];
  yMax?: number;
  height?: number;
}) {
  const points = series.flatMap((s) => s.points);
  if (points.length === 0) {
    return <div className="linked-empty">No runs yet.</div>;
  }
  const xMax = Math.max(1, ...points.map((p) => p.x));
  const yCap = yMax > 0 ? yMax : 1;
  const plotW = W - PAD.left - PAD.right;
  const plotH = height - PAD.top - PAD.bottom;
  const sx = (x: number) => PAD.left + (x / xMax) * plotW;
  const sy = (y: number) => PAD.top + (1 - Math.min(y, yCap) / yCap) * plotH;

  return (
    <div className="trend-chart">
      <svg
        viewBox={`0 0 ${W} ${height}`}
        role="img"
        aria-label="Metric trend across eval runs"
      >
        {[0, 0.5, 1].map((f) => (
          <g key={f}>
            <line
              x1={PAD.left}
              y1={sy(f * yCap)}
              x2={W - PAD.right}
              y2={sy(f * yCap)}
              className="trend-grid"
            />
            <text x={PAD.left - 6} y={sy(f * yCap) + 3} className="trend-tick">
              {(f * yCap).toFixed(1)}
            </text>
          </g>
        ))}
        {series.map((s) => (
          <g key={s.label}>
            <polyline
              fill="none"
              stroke={s.color}
              strokeWidth={2}
              points={s.points.map((p) => `${sx(p.x)},${sy(p.y)}`).join(" ")}
            />
            {s.points.map((p) => (
              <circle key={p.x} cx={sx(p.x)} cy={sy(p.y)} r={3} fill={s.color}>
                <title>{`${p.label} · ${s.label} ${p.y}`}</title>
              </circle>
            ))}
          </g>
        ))}
      </svg>
      <div className="trend-legend">
        {series.map((s) => (
          <span key={s.label} className="trend-legend-item">
            <span className="trend-swatch" style={{ background: s.color }} />
            {s.label}
          </span>
        ))}
      </div>
    </div>
  );
}
