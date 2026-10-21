import { useEffect, useRef, useState } from "react";

export interface SearchDay {
  day: string;        // YYYY-MM-DD in the viewer's zone
  searches: number;
  missed: number;
}

const HEIGHT = 200;
const PAD = { top: 12, right: 8, bottom: 24, left: 34 };
const GAP = 2;        // surface gap between the stacked segments

/** Clean y ticks (0 and multiples of 1/2/5 x 10^k) covering `max` in about four steps.
 * Searches are counts, so the step is never below 1: a quiet window gets 0, 1, 2. */
export function niceTicks(max: number): number[] {
  if (max <= 0) return [0, 1];
  const raw = max / 4;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = Math.max(1, [1, 2, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? 10 * mag);
  const ticks: number[] = [];
  for (let v = 0; v < max + step; v += step) ticks.push(Math.round(v * 1e6) / 1e6);
  return ticks;
}

const dayLabel = (day: string) =>
  new Date(`${day}T00:00:00Z`).toLocaleDateString(undefined,
    { month: "short", day: "numeric", timeZone: "UTC" });

/** A top segment with rounded data-end corners, square at its base. */
function topBar(x: number, y: number, w: number, h: number): string {
  const r = Math.min(3, w / 2, h);
  return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`;
}

/**
 * Searches per day as stacked columns: found (a skill matched) below, missed on top.
 * Sized to its container in pixels, so it fills any width without scaling its text.
 * Hovering a day shows its counts.
 */
export function SkillSearchChart({ days }: { days: SearchDay[] }) {
  const box = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(640);
  const [hover, setHover] = useState<number | null>(null);

  useEffect(() => {
    const el = box.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(([e]) => setWidth(Math.max(240, Math.floor(e.contentRect.width))));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const ticks = niceTicks(Math.max(0, ...days.map((d) => d.searches)));
  const yMax = ticks[ticks.length - 1];
  const plotW = width - PAD.left - PAD.right;
  const plotH = HEIGHT - PAD.top - PAD.bottom;
  const band = plotW / Math.max(1, days.length);
  const barW = Math.max(2, Math.min(24, band * 0.62));
  const sy = (v: number) => PAD.top + plotH - (v / yMax) * plotH;
  const every = Math.max(1, Math.ceil(days.length / Math.max(2, Math.floor(plotW / 72))));
  const hovered = hover === null ? null : days[hover];

  return (
    <div className="skills-chart" ref={box} onMouseLeave={() => setHover(null)}>
      <svg width={width} height={HEIGHT} role="img"
           aria-label={`Searches per day over ${days.length} days`}>
        {ticks.map((t) => (
          <g key={t}>
            <line className="skills-chart-grid" x1={PAD.left} x2={width - PAD.right} y1={sy(t)} y2={sy(t)} />
            <text className="skills-chart-tick" x={PAD.left - 8} y={sy(t) + 3.5}>{t}</text>
          </g>
        ))}
        {days.map((d, i) => {
          const cx = PAD.left + band * i + band / 2;
          const x = cx - barW / 2;
          const found = Math.max(0, d.searches - d.missed);
          const foundH = (found / yMax) * plotH;
          const missedH = (d.missed / yMax) * plotH;
          const base = PAD.top + plotH;
          const missedGap = found > 0 && d.missed > 0 ? GAP : 0;
          return (
            <g key={d.day} className={hover === i ? "skills-chart-day hover" : "skills-chart-day"}>
              <rect className="skills-chart-band" x={PAD.left + band * i} y={PAD.top}
                    width={band} height={plotH} />
              {found > 0 && (d.missed > 0
                ? <rect className="skills-chart-found" x={x} y={base - foundH} width={barW} height={foundH} />
                : <path className="skills-chart-found" d={topBar(x, base - foundH, barW, foundH)} />)}
              {d.missed > 0 && (
                <path className="skills-chart-missed"
                      d={topBar(x, base - foundH - missedH, barW, Math.max(1, missedH - missedGap))} />
              )}
              {i % every === 0 && (
                <text className="skills-chart-xlabel" x={cx} y={HEIGHT - 6}>{dayLabel(d.day)}</text>
              )}
              <rect className="skills-chart-hit" x={PAD.left + band * i} y={PAD.top}
                    width={band} height={plotH + PAD.bottom}
                    onMouseEnter={() => setHover(i)} />
            </g>
          );
        })}
      </svg>
      {hovered && hover !== null && (
        <div className="skills-chart-tip" role="status"
             style={{ left: Math.min(width - 150, Math.max(0, PAD.left + band * hover + band / 2 - 70)) }}>
          <strong>{dayLabel(hovered.day)}</strong>
          <span><i className="skills-key found" />{hovered.searches - hovered.missed} found</span>
          <span><i className="skills-key missed" />{hovered.missed} missed</span>
        </div>
      )}
    </div>
  );
}
