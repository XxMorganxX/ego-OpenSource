import { useEffect, useRef, useState } from "react";

import { fetchSkillUsage } from "../api/client";
import { browserTimeZone } from "../lib/heat";
import { fmtAgo, fmtWhen, openTurn, surfacedLabel, USAGE_DAYS } from "../lib/skills";
import type { SkillUsageData, UsageDays } from "../types";

export function DaysSelect({ value, onChange, label }: {
  value: UsageDays;
  onChange: (d: UsageDays) => void;
  label: string;
}) {
  return (
    <select aria-label={label} value={value}
            onChange={(e) => onChange(Number(e.target.value) as UsageDays)}>
      {USAGE_DAYS.map((d) => <option key={d} value={d}>{d}d</option>)}
    </select>
  );
}

/** Recent loads shown before "Show all", so the history card below stays in reach. */
const LOADS_PREVIEW = 4;

/** One skill's usage from the MCP trace log: loads, file reads, search surfacing,
 * and the newest loads, each linked to its request on the Retrieval page. */
export function SkillUsage({ name, refreshTick }: { name: string; refreshTick: number }) {
  const [days, setDays] = useState<UsageDays>(30);
  const [allLoads, setAllLoads] = useState(false);
  const [usage, setUsage] = useState<SkillUsageData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const seq = useRef(0);

  useEffect(() => setAllLoads(false), [name]);

  useEffect(() => {
    const mine = ++seq.current;
    setError(null);
    fetchSkillUsage(name, days, browserTimeZone())
      .then((u) => {
        if (mine === seq.current) setUsage(u);
      })
      .catch(() => {
        if (mine === seq.current) setError("Failed to load usage.");
      });
  }, [name, days, refreshTick]);

  const shown = usage && usage.name === name ? usage : null;
  const loads = shown?.recent_loads ?? [];
  const visible = allLoads ? loads : loads.slice(0, LOADS_PREVIEW);

  return (
    <section className="skill-card skill-usage" aria-label="Usage">
      <div className="skill-usage-head">
        <h3 className="skill-card-title">Usage</h3>
        <label className="skill-window">
          <span>Last</span>
          <DaysSelect value={days} onChange={setDays} label="Usage window" />
        </label>
      </div>
      {error ? (
        <div className="skills-empty" role="alert">{error}</div>
      ) : !shown ? (
        <div className="skills-empty">Loading…</div>
      ) : (
        <>
          <div className="skill-usage-stats">
            <div className="skill-stat">
              <strong>{shown.loads}</strong>
              <span>loads</span>
            </div>
            <div className="skill-stat">
              <strong>{shown.file_reads}</strong>
              <span>file reads</span>
            </div>
          </div>
          <p className="skill-usage-counts">
            {/* Each fact is unbreakable; the line may only wrap at the separator. */}
            <span>{surfacedLabel(shown.surfaced, shown.surfaced_since, shown.since)}</span>
            {shown.last_loaded_at && (
              <>
                {" · "}
                <span title={fmtWhen(shown.last_loaded_at)}>last loaded {fmtAgo(shown.last_loaded_at)}</span>
              </>
            )}
          </p>
          {loads.length === 0 ? (
            <p className="skills-empty">No loads in the last {shown.days} days.</p>
          ) : (
            <>
              <h4 className="skill-subhead">Recent loads</h4>
              <ol className="skill-loads">
                {visible.map((l, i) => (
                  <li key={`${l.at}-${i}`}>
                    <span className="skill-load-when" title={fmtWhen(l.at)}>{fmtAgo(l.at)}</span>
                    <span className="skill-load-rev">
                      {l.revision === null ? "current (not recorded)" : `rev ${l.revision}`}
                    </span>
                    {l.turn_id !== null ? (
                      <a className="skill-load-turn" href={`?view=retrieval&turn=${l.turn_id}`}
                         onClick={(e) => {
                           e.preventDefault();
                           openTurn(l.turn_id as number);
                         }}>
                        request #{l.turn_id}
                      </a>
                    ) : <span className="skill-load-turn none">no request</span>}
                  </li>
                ))}
              </ol>
              {loads.length > LOADS_PREVIEW && (
                <button className="link-btn skill-loads-more" aria-expanded={allLoads}
                        onClick={() => setAllLoads((v) => !v)}>
                  {allLoads ? "Show fewer" : `Show all ${loads.length} recent loads`}
                </button>
              )}
            </>
          )}
        </>
      )}
    </section>
  );
}
