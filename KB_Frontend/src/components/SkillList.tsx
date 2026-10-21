import { useEffect, useRef, useState } from "react";

import { fetchAgents, fetchSkills } from "../api/client";
import { firstLine, fmtAgo, fmtWhen } from "../lib/skills";
import type { LibraryKind, SkillLoadCount, SkillSummary, UsageDays } from "../types";

const DEBOUNCE_MS = 250;
/** Browsing reads every page up to this many, so "Needs attention" covers the whole
 * library rather than the first page; a search still pages on demand. */
const BROWSE_PAGE_CAP = 20;

/**
 * The library: lexical search (the same search agents use), an archived toggle, and
 * one row per skill with its load count from the library usage window. Browsing reads
 * the whole library and groups it (needs attention, all, archived); a search shows the
 * server's ranking, a page at a time. Each fetch is numbered so a slow earlier search can
 * never replace a newer one's rows, and the rows keep the fetch, query and kind that
 * produced them: they are never grouped as if they answered a newer one, and a "Load
 * more" page joins only the rows it continues.
 */
export function SkillList({ selected, onSelect, perSkill, days, refreshTick, kind = "skill", onBrowsed }: {
  kind?: LibraryKind;
  /** Called with the row count of each browse (no search) result and how many of those
   * rows need review, so the page can tell an empty library from an idle one. */
  /** After each browse (no search): the active items' count and those needing review. */
  onBrowsed?: (count: number, needsReview: string[]) => void;
  selected: string | null;
  onSelect: (name: string) => void;
  perSkill: Record<string, SkillLoadCount>;
  days: UsageDays;
  refreshTick: number;
}) {
  const [query, setQuery] = useState("");
  const [debounced, setDebounced] = useState("");
  const [archived, setArchived] = useState(false);
  const [rows, setRows] = useState<SkillSummary[] | null>(null);
  const [rowsQuery, setRowsQuery] = useState("");
  const [rowsKind, setRowsKind] = useState<LibraryKind>(kind);
  const [next, setNext] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const seq = useRef(0);
  /** The fetch whose rows (and cursor) are on screen. */
  const rowsSeq = useRef(0);
  const list = kind === "agent" ? fetchAgents : fetchSkills;
  const noun = kind === "agent" ? "agents" : "skills";

  useEffect(() => {
    const t = window.setTimeout(() => setDebounced(query.trim()), DEBOUNCE_MS);
    return () => window.clearTimeout(t);
  }, [query]);

  useEffect(() => {
    const mine = ++seq.current;
    const q = debounced;
    setError(null);
    (async () => {
      let p = await list({ q: q || undefined, archived });
      let all = p.skills;
      for (let n = 1; !q && p.next_cursor && n < BROWSE_PAGE_CAP; n++) {
        if (mine !== seq.current) return;
        p = await list({ q: undefined, archived, cursor: p.next_cursor });
        all = [...all, ...p.skills];
      }
      if (mine !== seq.current) return;
      rowsSeq.current = mine;
      setRows(all);
      setRowsQuery(q);
      setRowsKind(kind);
      setNext(p.next_cursor);
      if (!q) {
        // The library's own size: the "show archived" filter must not change it.
        const active = all.filter((s) => !s.archived);
        onBrowsed?.(active.length, active.filter((s) => rowState(s) !== "ok").map((s) => s.name));
      }
    })().catch(() => {
      if (mine === seq.current) setError(`Failed to load ${noun}.`);
    });
  }, [debounced, archived, refreshTick, kind]);

  const loadMore = () => {
    if (!next) return;
    // The page continues the rows on screen: it is dropped once a newer fetch has
    // started (its rows would replace these) or landed (these rows are gone).
    const gen = rowsSeq.current;
    const current = () => gen === rowsSeq.current && gen === seq.current;
    list({ q: rowsQuery || undefined, archived, cursor: next })
      .then((p) => {
        if (!current()) return;
        setRows((r) => [...(r ?? []), ...p.skills]);
        setNext(p.next_cursor);
      })
      .catch(() => {
        if (current()) setError(`Failed to load ${noun}.`);
      });
  };

  // Rows from the other library are not shown under this one while its own load.
  const shown = rowsKind === kind ? rows : null;
  const groups = shown === null ? [] : groupRows(shown, Boolean(rowsQuery), kind);
  const showLoads = kind === "skill";

  return (
    <div className="skill-list">
      <div className="skill-list-controls">
        <input type="search" aria-label={`Search ${noun}`} placeholder={`Search ${noun}`}
               value={query} onChange={(e) => setQuery(e.target.value)} />
        <div className="skill-list-bar">
          <span className="skill-list-count">
            {shown && !error ? `${shown.length}${next ? "+" : ""} ${noun}` : ""}
          </span>
          <label className="skill-archived-toggle">
            <input type="checkbox" checked={archived}
                   onChange={(e) => setArchived(e.target.checked)} />{" "}
            show archived
          </label>
        </div>
      </div>
      <div className="skill-list-scroll">
        {error ? (
          <div className="skills-empty" role="alert">{error}</div>
        ) : shown === null ? (
          <div className="skills-empty">Loading…</div>
        ) : shown.length === 0 ? (
          <div className="skills-empty">
            {rowsQuery ? `No ${noun} match “${rowsQuery}”.`
              : kind === "agent" ? "No agents yet. Agents are saved with upsert_agent."
              /* The overview beside the list explains how skills arrive. */
              : archived ? "No skills yet." : "No active skills."}
          </div>
        ) : (
          groups.map((g, gi) => (
            <section key={g.key} className={`skill-group skill-group-${g.key}`} aria-label={g.title}>
              <h3 className="skill-group-head" title={g.hint}>
                <span>{g.title}</span>
                <span className="skill-group-count">{g.rows.length}</span>
                {showLoads && gi === 0 && (
                  <span className="skill-group-loads" title={`Loads in the last ${days} days`}>
                    loads · {days}d
                  </span>
                )}
              </h3>
              <ul className="skill-rows">
                {g.rows.map((s) => {
                  const loads = perSkill[s.name]?.loads ?? 0;
                  const state = rowState(s);
                  const waiting = state !== "ok";
                  return (
                    <li key={s.name}>
                      <button className={`skill-row skill-row-${state}${s.archived ? " skill-row-archived" : ""}${s.name === selected ? " active" : ""}`}
                              aria-current={s.name === selected ? "true" : undefined}
                              onClick={() => onSelect(s.name)}>
                        <span className="skill-row-name">{s.name}</span>
                        <span className="skill-row-desc">{firstLine(s.description)}</span>
                        <span className="skill-row-foot">
                          {s.archived && <span className="skill-badge">archived</span>}
                          {state === "pending" && (
                            <span className="skill-badge skill-badge-pending">pending review</span>
                          )}
                          {state === "rejected" && (
                            <span className="skill-badge skill-badge-rejected">rejected</span>
                          )}
                          {/* updated_at is the item's last activity (a new revision, a review,
                              an archive), not a revision's date, so it says "changed", apart
                              from the header's "rev N written". Rows that need review show
                              their revision numbers instead, so every such row reads alike. */}
                          <span className="skill-row-meta" title={`last activity ${fmtWhen(s.updated_at)}`}>
                            <span className="skill-row-rev">{revisionLabel(s, state)}</span>
                            {!waiting && <span className="skill-row-when">changed {fmtAgo(s.updated_at)}</span>}
                          </span>
                        </span>
                        {showLoads && (
                          <span className={`skill-row-loads${loads === 0 ? " none" : ""}`}
                                title={`${loads} ${loads === 1 ? "load" : "loads"} in the last ${days} days`}>
                            {loads}
                          </span>
                        )}
                      </button>
                    </li>
                  );
                })}
              </ul>
            </section>
          ))
        )}
        {next && shown && !error && (
          <button className="link-btn skill-more" onClick={loadMore}>Load more</button>
        )}
      </div>
    </div>
  );
}

type RowState = "ok" | "pending" | "rejected";

/** What a row needs from the owner, whether or not it is archived (approving a revision
 * of an archived item reactivates it). The NEWEST revision decides, as in the detail
 * header: turned down beats an older revision still waiting (KB_Core's pending_revision
 * is the newest PENDING one, and rejecting a newer revision leaves it pending). */
function rowState(s: SkillSummary): RowState {
  if (s.review_status === "rejected" && s.latest_revision !== s.approved_revision) return "rejected";
  if (s.pending_revision !== null || (s.review_status === "pending" && s.latest_revision !== s.approved_revision)) {
    return "pending";
  }
  return "ok";
}

/** The row's revision numbers, worded so they agree with the detail header (which opens
 * on the newest revision): the newest first, then what agents are served when that
 * differs. The pill beside it carries the newest revision's review state. */
function revisionLabel(s: SkillSummary, state: RowState): string {
  if (state === "ok") return `rev ${s.revision}`;
  const served = s.approved_revision === null ? "none served" : `serving rev ${s.approved_revision}`;
  return `rev ${s.latest_revision} · ${served}`;
}

interface RowGroup {
  key: string;
  title: string;
  hint?: string;
  rows: SkillSummary[];
}

/** Browsing: what needs the owner first (archived items included: approving one brings
 * it back), then the library, then archived. A search keeps the server's ranking in one
 * group. */
function groupRows(rows: SkillSummary[], searching: boolean, kind: LibraryKind): RowGroup[] {
  if (searching) return [{ key: "results", title: "Results", hint: "Best match first", rows }];
  const attention = (s: SkillSummary) => rowState(s) !== "ok";
  const groups: RowGroup[] = [
    { key: "attention", title: "Needs attention",
      hint: "The newest revision is waiting for review, or was rejected",
      rows: rows.filter(attention) },
    { key: "library", title: kind === "agent" ? "All agents" : "All skills",
      rows: rows.filter((s) => !s.archived && !attention(s)) },
    { key: "archived", title: "Archived", rows: rows.filter((s) => s.archived && !attention(s)) },
  ];
  return groups.filter((g) => g.rows.length > 0);
}
