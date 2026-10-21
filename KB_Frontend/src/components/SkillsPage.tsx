import { type ReactNode, useEffect, useRef, useState } from "react";

import { fetchSkillLibraryUsage } from "../api/client";
import { useChangeFeed } from "../hooks/useChangeFeed";
import { useUrlParam } from "../hooks/useUrlParam";
import { browserTimeZone } from "../lib/heat";
import { parseRev } from "../lib/skills";
import { setParams } from "../lib/url";
import type { LibraryKind, SkillLibraryUsageData, UsageDays } from "../types";
import { ActivityIcon, NoteIcon } from "./icons";
import { SkillDetail } from "./SkillDetail";
import { SkillLibraryUsage } from "./SkillLibraryUsage";
import { SkillList } from "./SkillList";
import { SkillUsage } from "./SkillUsage";

/** The sidebar's way back to the library's own page (the usage overview for skills),
 * with a one-line summary. Both libraries have one, so the search box below it does not
 * move when the library switches. */
function OverviewRow({ title, sub, icon, active, onOpen }: {
  title: string;
  sub: string;
  icon: ReactNode;
  active: boolean;
  onOpen: () => void;
}) {
  return (
    <button className={`skills-overview-row${active ? " active" : ""}`}
            aria-current={active ? "true" : undefined} onClick={onOpen}>
      <span className="skills-overview-row-icon">{icon}</span>
      <span className="skills-overview-row-text">
        <span className="skills-overview-row-title">{title}</span>
        <span className="skills-overview-row-sub">{sub}</span>
      </span>
    </button>
  );
}

function usageSummary(usage: SkillLibraryUsageData | null, days: UsageDays): string {
  if (!usage) return "usage, misses, gaps";
  const searches = usage.searches_per_day.reduce((n, d) => n + d.searches, 0);
  const missed = usage.searches_per_day.reduce((n, d) => n + d.missed, 0);
  return `${searches.toLocaleString()} searches · ${missed.toLocaleString()} missed · ${days}d`;
}

interface Browse { kind: LibraryKind; count: number; needsReview: string[] }

function agentSummary(browse: Browse | null): string {
  if (!browse || browse.kind !== "agent") return "subagent definitions";
  const n = `${browse.count} ${browse.count === 1 ? "agent" : "agents"}`;
  const k = browse.needsReview.length;
  if (k === 0) return n;
  return `${n} · ${k} ${k === 1 ? "needs" : "need"} review`;
}

/**
 * Read-only window into the skill registry, laid out as a workspace: the library
 * sidebar on the left, and on the right either the library overview (nothing
 * selected) or the selected skill's reader and metadata rail. The selected skill
 * and a pinned revision live in the URL (?skill=, ?rev=) so a view is linkable.
 * Live: a `skills` change (an agent's upsert/archive) reloads the list and the open
 * skill; a `mcp_trace` change reloads usage. Skills never change from this page.
 */
export function SkillsPage() {
  const [skill] = useUrlParam<string>("skill", "");
  const [rawKind] = useUrlParam<string>("kind", "skill");
  const kind: LibraryKind = rawKind === "agent" ? "agent" : "skill";
  const [rawRev] = useUrlParam<string>("rev", "");
  const revision = parseRev(rawRev);
  const [days, setDays] = useState<UsageDays>(30);
  const [library, setLibrary] = useState<SkillLibraryUsageData | null>(null);
  const [libraryError, setLibraryError] = useState<string | null>(null);
  const [skillsTick, setSkillsTick] = useState(0);
  const [usageTick, setUsageTick] = useState(0);
  // The latest browse (no search) of the listed library: its size and what needs review.
  const [browse, setBrowse] = useState<Browse | null>(null);
  const skillCount = browse?.kind === "skill" ? browse.count : null;
  const seq = useRef(0);

  useChangeFeed((tables) => {
    if (tables.has("skills")) setSkillsTick((n) => n + 1);
    // never_loaded depends on the skill set too, so a skills change refreshes usage.
    if (tables.has("mcp_trace") || tables.has("skills")) setUsageTick((n) => n + 1);
  });

  useEffect(() => {
    const mine = ++seq.current;
    setLibraryError(null);
    fetchSkillLibraryUsage(days, browserTimeZone())
      .then((u) => {
        if (mine === seq.current) setLibrary(u);
      })
      .catch(() => {
        if (mine === seq.current) setLibraryError("Failed to load usage.");
      });
  }, [days, usageTick]);

  const select = (name: string) => setParams({ skill: name, rev: null });
  const selectRevision = (rev: number | null) =>
    setParams({ rev: rev === null ? null : String(rev) });
  const prompt = `Select ${kind === "agent" ? "an agent" : "a skill"} to read it.`;

  return (
    <div className={`skills-page${skill ? " has-selection" : ""}`}>
      <aside className="skills-sidebar">
        <div className="skills-kind" role="tablist" aria-label="Library">
          {(["skill", "agent"] as const).map((k) => (
            <button key={k} role="tab" aria-selected={kind === k}
                    className={`skills-kind-tab${kind === k ? " active" : ""}`}
                    onClick={() => setParams({ kind: k === "skill" ? null : k, skill: null, rev: null })}>
              {k === "skill" ? "Skills" : "Agents"}
            </button>
          ))}
        </div>
        <OverviewRow active={!skill} onOpen={() => setParams({ skill: null, rev: null })}
                     {...(kind === "skill"
                       ? { title: "Library overview", sub: usageSummary(library, days),
                           icon: <ActivityIcon size={15} /> }
                       : { title: "Agent library", sub: agentSummary(browse),
                           icon: <NoteIcon size={15} /> })} />
        <SkillList selected={skill || null} onSelect={select}
                   perSkill={library?.per_skill ?? {}} days={days} refreshTick={skillsTick}
                   kind={kind}
                   onBrowsed={(count, needsReview) => setBrowse({ kind, count, needsReview })} />
      </aside>
      <section className="skills-main">
        {skill ? (
          <SkillDetail name={skill} revision={revision} refreshTick={skillsTick}
                       onSelectRevision={selectRevision} kind={kind}
                       aside={kind === "skill"
                         ? <SkillUsage name={skill} refreshTick={usageTick} />
                         : null} />
        ) : kind === "skill" ? (
          <SkillLibraryUsage usage={library} error={libraryError} days={days}
                             onDaysChange={setDays} onSelectSkill={select}
                             hint={skillCount === 0 ? undefined : prompt}
                             libraryEmpty={skillCount === 0} />
        ) : (
          <div className="skills-placeholder">
            <span className="skills-placeholder-icon"><NoteIcon size={22} /></span>
            <p className="skills-placeholder-title">{prompt}</p>
            <p className="skills-placeholder-sub">
              Subagent definitions from the library. Devices get each agent's approved revision.
            </p>
            {browse?.kind === "agent" && browse.needsReview.length > 0 && (
              <div className="skills-placeholder-review">
                <h4>Needs review</h4>
                <ul>
                  {browse.needsReview.map((n) => (
                    <li key={n}><button className="skills-chip" onClick={() => select(n)}>{n}</button></li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        )}
      </section>
    </div>
  );
}
