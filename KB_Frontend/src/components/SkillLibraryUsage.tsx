import type { ReactNode } from "react";

import { ActivityIcon, NoteIcon } from "./icons";
import { SkillSearchChart } from "./SkillSearchChart";
import { DaysSelect } from "./SkillUsage";
import type { SkillLibraryUsageData, UsageDays } from "../types";

/** Rows in the "Most loaded" card. */
const MOST_LOADED = 8;

function Stat({ label, value, sub, tone }: {
  label: string;
  value: number;
  sub?: string;
  tone?: "warn";
}) {
  return (
    <div className={`skills-kpi${tone ? ` skills-kpi-${tone}` : ""}`}>
      <span className="skills-kpi-value">{value.toLocaleString()}</span>
      <span className="skills-kpi-label">{label}</span>
      {sub && <span className="skills-kpi-sub">{sub}</span>}
    </div>
  );
}

function Card({ title, note, className, children }: {
  title: string;
  note?: string;
  className?: string;
  children: ReactNode;
}) {
  return (
    <div className={`skills-overview-card${className ? ` ${className}` : ""}`}>
      <div className="skills-overview-card-head">
        <h4>{title}</h4>
        {note && <span className="skill-meta">{note}</span>}
      </div>
      {children}
    </div>
  );
}

/** A ranked list whose rows carry a bar on one shared scale (the top count). With
 * `onSelect`, each label opens that skill. */
function CountList({ title, note, empty, items, area, onSelect, foot, mono = false }: {
  area: string;
  /** Labels are skill names (set in mono like every other skill name). */
  mono?: boolean;
  /** A last line pinned to the card's foot. */
  foot?: ReactNode;
  title: string;
  note?: string;
  empty: string;
  items: { key: string; label: string; count: number }[];
  onSelect?: (key: string) => void;
}) {
  const max = Math.max(1, ...items.map((i) => i.count));
  return (
    <Card title={title} note={note} className={`skills-usage-list ${area}`}>
      {items.length === 0 ? (
        <p className="skills-empty">{empty}</p>
      ) : (
        <ul>
          {items.map((i) => (
            <li key={i.key} className="skills-count-row">
              {onSelect ? (
                <button className="link-btn skills-count-label" onClick={() => onSelect(i.key)}>{i.label}</button>
              ) : (
                <span className={`skills-count-label${mono ? " mono" : ""}`}>{i.label}</span>
              )}
              {/* A floor keeps a count of 1 visible beside a count of 60. */}
              <span className="skills-count-bar" aria-hidden="true">
                <span style={{ width: `max(4px, ${Math.round((i.count / max) * 100)}%)` }} />
              </span>
              <span className="skills-count-n">{i.count}</span>
            </li>
          ))}
        </ul>
      )}
      {foot && <div className="skills-card-foot">{foot}</div>}
    </Card>
  );
}

/** The library overview, shown in the main pane while nothing is selected.
 * Presentational: SkillsPage owns the fetch so the list's per-skill load counts come
 * from the same window. */
export function SkillLibraryUsage({ usage, error, days, onDaysChange, onSelectSkill, hint, libraryEmpty = false }: {
  usage: SkillLibraryUsageData | null;
  error: string | null;
  days: UsageDays;
  onDaysChange: (d: UsageDays) => void;
  onSelectSkill: (name: string) => void;
  /** A line under the title, e.g. the prompt to pick a skill. */
  hint?: string;
  /** The library has no active skills (archived ones may exist): an idle overview
   * explains how skills arrive instead. */
  libraryEmpty?: boolean;
}) {
  const head = (
    <header className="skills-usage-head">
      <div>
        <h3>Library overview</h3>
        {hint && <p className="skills-usage-hint">{hint}</p>}
      </div>
      <label className="skills-window" title="Sets this overview and the load counts in the list">
        <span>Last</span>
        <DaysSelect value={days} onChange={onDaysChange} label="Library usage window" />
      </label>
    </header>
  );
  if (error) {
    return (
      <section className="skills-overview" aria-label="Library usage">
        {head}
        <div className="skills-empty" role="alert">{error}</div>
      </section>
    );
  }
  if (!usage) {
    return (
      <section className="skills-overview" aria-label="Library usage">
        {head}
        <div className="skills-empty">Loading usage…</div>
      </section>
    );
  }
  const searches = usage.searches_per_day.reduce((n, d) => n + d.searches, 0);
  const missed = usage.searches_per_day.reduce((n, d) => n + d.missed, 0);
  const loaded = Object.entries(usage.per_skill)
    .filter(([, c]) => c.loads > 0)
    .sort((a, b) => b[1].loads - a[1].loads || a[0].localeCompare(b[0]));
  const totalLoads = loaded.reduce((n, [, c]) => n + c.loads, 0);
  // Every skill the window knows of: loaded ones, and active ones never loaded.
  const known = new Set([...loaded.map(([name]) => name), ...usage.never_loaded]).size;
  const idle = searches === 0 && loaded.length === 0 && usage.missing_requests.length === 0;

  if (idle) {
    return (
      <section className="skills-overview" aria-label="Library usage">
        {head}
        {libraryEmpty ? (
          <div className="skills-idle">
            <span className="skills-placeholder-icon"><NoteIcon size={22} /></span>
            <p className="skills-placeholder-title">No active skills in the library</p>
            <p className="skills-placeholder-sub">
              Agents save skills with upsert_skill. Each new revision waits here for your
              review; once one is approved, agents find it with search_skills and this
              overview shows how they use it. Archived skills are listed with “show
              archived”.
            </p>
          </div>
        ) : (
          <div className="skills-idle">
            <span className="skills-placeholder-icon"><ActivityIcon size={22} /></span>
            <p className="skills-placeholder-title">No skill activity in the last {usage.days} days</p>
            <p className="skills-placeholder-sub">
              When agents search the library and load skills, this overview shows searches per
              day, what they looked for and did not find, and which skills they use.
            </p>
          </div>
        )}
        {usage.never_loaded.length > 0 && (
          <NeverLoaded names={usage.never_loaded} onSelectSkill={onSelectSkill} />
        )}
      </section>
    );
  }

  const first = usage.searches_per_day[0]?.day;
  const last = usage.searches_per_day[usage.searches_per_day.length - 1]?.day;
  return (
    <section className="skills-overview" aria-label="Library usage">
      {head}
      <div className="skills-kpis">
        <Stat label="searches" value={searches} sub={`skill searches in the last ${usage.days} days`} />
        <Stat label="missed searches" value={missed} tone={missed > 0 ? "warn" : undefined}
              sub={searches > 0 ? `${Math.round((missed / searches) * 100)}% found nothing` : undefined} />
        <Stat label="loads" value={totalLoads}
              sub={`${loaded.length} ${loaded.length === 1 ? "skill" : "skills"} loaded`} />
        <Stat label="never loaded" value={usage.never_loaded.length}
              sub={`active, no loads in ${usage.days}d`} />
      </div>
      <div className="skills-overview-grid">
        <Card title="Searches per day" className="skills-chart-card"
              note={first && last ? `${first} → ${last}` : undefined}>
          <div className="skills-legend" aria-hidden="true">
            <span><i className="skills-key found" />found a skill</span>
            <span><i className="skills-key missed" />missed</span>
          </div>
          {searches > 0 ? (
            <SkillSearchChart days={usage.searches_per_day} />
          ) : (
            <p className="skills-empty">No searches in the last {usage.days} days.</p>
          )}
        </Card>
        <CountList area="skills-area-most" title="Most loaded" note={`loads · ${usage.days}d`}
                   empty="No loads yet." onSelect={onSelectSkill}
                   foot={loaded.length > 0 && (
                     <>
                       <span>{loaded.length} of {known} skills loaded</span>
                       {loaded.length > MOST_LOADED && <span>top {MOST_LOADED} shown</span>}
                     </>
                   )}
                   items={loaded.slice(0, MOST_LOADED).map(([name, c]) => (
                     { key: name, label: name, count: c.loads }))} />
        <CountList area="skills-area-missed" title="Missed searches" note="queries that found nothing" empty="No missed searches."
                   items={usage.missed_searches.map((m) => (
                     { key: m.query, label: `“${m.query}”`, count: m.count }))} />
        <CountList area="skills-area-missing" title="Missing skills requested" note="candidates to write" empty="None." mono
                   items={usage.missing_requests.map((m) => (
                     { key: m.name, label: m.name, count: m.count }))} />
        <NeverLoaded names={usage.never_loaded} onSelectSkill={onSelectSkill} />
      </div>
    </section>
  );
}

function NeverLoaded({ names, onSelectSkill }: { names: string[]; onSelectSkill: (n: string) => void }) {
  return (
    <Card title="Never loaded" note="open one to review it" className="skills-usage-list skills-area-never">
      {names.length === 0 ? (
        <p className="skills-empty">None.</p>
      ) : (
        <ul className="skills-chip-list">
          {names.map((n) => (
            <li key={n}>
              <button className="skills-chip" onClick={() => onSelectSkill(n)}>{n}</button>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}
